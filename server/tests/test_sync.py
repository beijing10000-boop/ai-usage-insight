"""同步任务：按天整批替换、单个数据集失败不拖垮任务、断点续跑、巡检、停止。"""

from __future__ import annotations

import time
from datetime import timedelta

import pytest

from aius_server.analytics import Filter
from aius_server.feishu import LEGACY_APP_ENTITY_TYPES, FeishuError
from aius_server.labels import APP_ENTITY_TYPES
from aius_server.sync import SyncError, today
from conftest import D0, D1, D2, MEMBER_D1, stamped


def count(env, table: str, where: str = "1=1") -> int:
    return env.db.val(f"SELECT COUNT(*) FROM {table} WHERE {where}")


def test_sync_writes_logs_and_details(env):
    run = env.ctx.engine.runs()[0][0]
    assert run["status"] == "success"
    assert run["stats"]["log_member"] == 6 and run["stats"]["log_app"] == 2
    assert run["stats"]["detail_member"] == 2 and run["stats"]["detail_app"] == 1
    assert (run["stats"]["members"], run["stats"]["depts"], run["stats"]["apps"]) == (4, 3, 2)
    assert env.db.val("SELECT SUM(used_quota) FROM usage_logs WHERE usage_date=?", (D1,)) == 206
    row = env.db.one("SELECT * FROM usage_logs WHERE entity_id='ou_carol'")
    assert row["scenario"] == "豆包工作伙伴>专属伙伴>对话" and row["feature_key"] == 3 and "标准模型" in row["notes"]
    assert row["ts"] < 10**11  # 接口给的是毫秒，存的是秒
    detail = env.db.one("SELECT * FROM usage_details WHERE entity_id='ou_bob'")
    assert (detail["q1"], detail["q4"]) == (10, 20)


def test_resync_replaces_the_day_instead_of_duplicating(env):
    before = count(env, "usage_logs")
    assert env.sync(D2, D1)["status"] == "success"
    assert count(env, "usage_logs") == before
    # 飞书那边这一天的数据变了：重新同步后以新的为准
    env.feishu.usage[("log", "member", D1)] = stamped(MEMBER_D1[:2], D1)
    env.sync(D1, D1)
    assert count(env, "usage_logs", f"usage_date='{D1}' AND subject='member'") == 2
    assert count(env, "usage_logs", f"usage_date='{D2}'") == 1  # 没同步的那天不动


def test_one_dataset_failing_gives_partial_and_keeps_old_data(env):
    before = count(env, "usage_logs", "subject='app'")
    env.feishu.fail[("log", "app")] = FeishuError("查询 AI 用量日志失败：code=99991672 Access denied")
    run = env.sync(D1, D1)
    assert run["status"] == "partial" and run["stats"]["failed"] == 1
    assert "Access denied" in run["error"] and "应用用量日志" in run["error"]
    assert count(env, "usage_logs", "subject='app'") == before  # 失败的数据集不清空已有数据
    failed = [s for s in run["steps"] if s["status"] == "failed"]
    assert [s["dataset"] for s in failed] == ["log_app"]


def test_everything_failing_is_failed(empty):
    for key in (("log", "member"), ("log", "app"), ("detail", "member"), ("detail", "app")):
        empty.feishu.fail[key] = FeishuError("没有权限")
    run = empty.sync(D1, D1)
    assert run["status"] == "failed" and run["stats"]["failed"] == 4
    assert count(empty, "usage_logs") == 0


def test_details_are_skipped_for_today(empty):
    run = empty.sync(D0, D0)
    assert run["status"] == "success"
    kinds = [call[1] for call in empty.feishu.calls if call[0] == "usage_page"]
    assert kinds == ["log", "log"]  # 统计详情是 T+1，当天不去拉


def test_steps_are_listed_newest_first(empty):
    run = empty.sync(D2, D1)
    ids = [s["id"] for s in run["steps"]]
    assert ids == sorted(ids, reverse=True) and run["steps"][-1]["usage_date"] == D2  # 最早的在最后
    assert run["steps"][0]["dataset"] == "names"  # 最后做的名称解析在最前面


def test_pagination_collects_every_page(empty):
    empty.feishu.page_size = 2
    run = empty.sync(D1, D1)
    assert run["stats"]["log_member"] == 5
    step = next(s for s in run["steps"] if s["dataset"] == "log_member")
    assert step["pages"] == 3


def test_progress_is_visible_while_a_dataset_is_being_fetched(empty):
    """一个数据集要拉完才写库：拉的过程中要能看到拉到第几页、取到多少条、拉到当天几点。"""
    from aius_server.manage import progress_text

    engine = empty.ctx.engine
    empty.feishu.page_size = 2
    seen = []
    original = empty.feishu.usage_page

    def page(kind, subject, day, token="", app_types=APP_ENTITY_TYPES):
        if (kind, subject, token) == ("log", "member", "4"):  # 第三页开始前看一眼进度
            seen.append(engine.run(engine.active()["id"]))
        return original(kind, subject, day, token, app_types)

    empty.feishu.usage_page = page
    run = empty.sync(D1, D1)
    p = seen[0]["progress"]
    assert p["day"] == D1 and p["dataset"] == "log_member" and p["pages"] == 2 and p["rows"] == 4 and p["reached"] == "10:00"
    text = progress_text(seen[0])
    assert "正在拉" in text and "人员用量日志" in text and "第 2 页" in text and "已取到 4 条" in text and "当天 10:00" in text
    assert run["progress"] is None and count(empty, "sync_runs", "progress<>''") == 0  # 结束后清掉


def agg_matches_logs(env) -> bool:
    """汇总表和原始日志按「日期 × 主体 × 对象 × 场景」对得上（点数和条数都一样）。"""
    cols = "usage_date, subject, entity_id, scenario"
    logs = {tuple(r)[:4]: (round(r[4], 6), r[5]) for r in env.db.q(f"SELECT {cols}, SUM(used_quota), COUNT(*) FROM usage_logs GROUP BY {cols}")}
    agg = {tuple(r)[:4]: (round(r[4], 6), r[5]) for r in env.db.q(f"SELECT {cols}, SUM(amount), SUM(n) FROM usage_agg GROUP BY {cols}")}
    return logs == agg


def test_summary_table_always_matches_the_raw_logs(env):
    assert agg_matches_logs(env) and env.db.val("SELECT COUNT(*) FROM usage_agg") < env.db.val("SELECT COUNT(*) FROM usage_logs") + 1
    hours = {r[0] for r in env.db.q("SELECT hour FROM usage_agg WHERE subject='member'")}
    assert hours == {10, 15}  # 北京时间的小时
    env.feishu.usage[("log", "member", D1)] = stamped(MEMBER_D1[:2], D1)
    env.sync(D1, D1)  # 重新同步：汇总表跟着整批替换
    assert agg_matches_logs(env) and env.db.val("SELECT SUM(n) FROM usage_agg WHERE usage_date=? AND subject='member'", (D1,)) == 2


def test_old_databases_get_the_summary_table_filled(tmp_path):
    from aius_server.db import Database

    db = Database(tmp_path / "a.db")
    db.x("INSERT INTO usage_logs(usage_date, subject, ts, entity_type, entity_id, department_id, feature_key, usage_type, used_quota, scenario, run_id) "
         "VALUES('2026-11-09','member',1794200000,2,'ou_x','d1',1,1,2.5,'知识问答',1),('2026-11-09','member',1794200100,2,'ou_x','d1',1,1,1.5,'知识问答',1)")  # fmt: skip
    db.x("DELETE FROM usage_agg")  # 模拟升级前的库：只有原始日志，没有汇总
    db2 = Database(tmp_path / "a.db")
    # 1794200000 是北京时间 2026-11-09 12:53
    assert [tuple(r) for r in db2.q("SELECT entity_id, scenario, hour, amount, n FROM usage_agg")] == [("ou_x", "知识问答", 12, 4.0, 2)]


def test_old_databases_get_the_progress_column(tmp_path):
    import sqlite3

    from aius_server.db import Database

    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE sync_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, trigger TEXT NOT NULL, actor TEXT NOT NULL, start_date TEXT NOT NULL, "
              "end_date TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', created_at INTEGER NOT NULL, started_at INTEGER, finished_at INTEGER, "
              "heartbeat_at INTEGER, done_units TEXT NOT NULL DEFAULT '[]', stats TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '', "
              "resume_count INTEGER NOT NULL DEFAULT 0, stop_requested INTEGER NOT NULL DEFAULT 0)")  # fmt: skip
    c.execute("INSERT INTO sync_runs(trigger, actor, start_date, end_date, status, created_at) VALUES('manual','x','2026-11-09','2026-11-09','running',1)")
    c.commit()
    c.close()
    db = Database(path)
    assert db.val("SELECT progress FROM sync_runs WHERE id=1") == "" and db.val("PRAGMA user_version") == 3


def test_start_validates_dates_and_refuses_a_second_run(empty):
    engine = empty.ctx.engine
    tomorrow = (today() + timedelta(days=1)).isoformat()
    for start, end, word in ((D1, D2, "不能晚于"), (D1, tomorrow, "还没到"), ("2026/01/01", D1, "格式"), ((today() - timedelta(days=400)).isoformat(), D1, "最多")):
        with pytest.raises(SyncError, match=word):
            engine.start(start, end, "manual", "ou_admin")
    first = engine.start(D1, D1, "manual", "ou_admin")
    with pytest.raises(SyncError, match=f"#{first}"):
        engine.start(D2, D2, "manual", "ou_admin")


def test_stop_a_queued_run(empty):
    engine = empty.ctx.engine
    run_id = engine.start(D1, D1, "manual", "ou_admin")
    assert engine.stop(run_id) is True
    assert engine.run(run_id)["status"] == "stopped"
    assert engine.run_pending() == 0 and count(empty, "usage_logs") == 0
    assert engine.stop(run_id) is False  # 已经结束的停不了


def test_stop_a_running_run_keeps_what_was_written(empty):
    engine = empty.ctx.engine
    original = empty.feishu.usage_page

    def page(kind, subject, day, token="", app_types=APP_ENTITY_TYPES):
        if (kind, subject) == ("log", "app"):  # 拉到第二个数据集时有人点了停止
            engine.stop(engine.active()["id"])
        return original(kind, subject, day, token, app_types)

    empty.feishu.usage_page = page
    run = empty.sync(D1, D1)
    assert run["status"] == "stopped" and "手动停止" in run["error"]
    # 停止的时候正在拉的这一页拉完、写完；之后的数据集（统计详情）不再拉
    assert count(empty, "usage_logs", "subject='member'") == 5 and count(empty, "usage_logs", "subject='app'") == 2
    assert count(empty, "usage_details") == 0
    assert not [c for c in empty.feishu.calls if c[0] == "usage_page" and c[1] == "detail"]


def test_interrupted_run_resumes_from_the_checkpoint(empty):
    engine = empty.ctx.engine
    run_id = engine.start(D2, D1, "manual", "ou_admin")
    # 模拟：跑完 D2 的人员日志后进程被杀
    empty.db.x("UPDATE sync_runs SET status='running', heartbeat_at=?, done_units=? WHERE id=?", (int(time.time()), f'["{D2}|log_member"]', run_id))
    engine.recover()
    row = empty.db.one("SELECT status, resume_count FROM sync_runs WHERE id=?", (run_id,))
    assert (row["status"], row["resume_count"]) == ("queued", 1)
    engine.run_pending()
    run = engine.run(run_id)
    assert run["status"] == "success"
    fetched = [(c[1], c[2], c[3]) for c in empty.feishu.calls if c[0] == "usage_page"]
    assert ("log", "member", D2) not in fetched and ("log", "member", D1) in fetched


def test_watchdog_requeues_stale_runs_then_gives_up(empty):
    engine = empty.ctx.engine
    run_id = engine.start(D1, D1, "manual", "ou_admin")
    stale = int(time.time()) - engine.heartbeat_timeout - 5
    empty.db.x("UPDATE sync_runs SET status='running', heartbeat_at=? WHERE id=?", (int(time.time()), run_id))
    engine.watchdog()
    assert empty.db.val("SELECT status FROM sync_runs WHERE id=?", (run_id,)) == "running"  # 心跳还新鲜，不动
    for attempt in range(1, engine.max_resume + 1):
        empty.db.x("UPDATE sync_runs SET status='running', heartbeat_at=? WHERE id=?", (stale, run_id))
        engine.watchdog()
        row = empty.db.one("SELECT status, resume_count FROM sync_runs WHERE id=?", (run_id,))
        assert (row["status"], row["resume_count"]) == ("queued", attempt)
    empty.db.x("UPDATE sync_runs SET status='running', heartbeat_at=? WHERE id=?", (stale, run_id))
    engine.watchdog()
    row = empty.db.one("SELECT status, error FROM sync_runs WHERE id=?", (run_id,))
    assert row["status"] == "failed" and "续跑" in row["error"]


def test_app_dimension_falls_back_to_the_early_entity_types(empty):
    original = empty.feishu.usage_page

    def page(kind, subject, day, token="", app_types=APP_ENTITY_TYPES):
        if subject == "app" and 13 in app_types:
            raise FeishuError("查询 AI 用量日志失败：code=1000001 invalid entity_type")
        return original(kind, subject, day, token, app_types)

    empty.feishu.usage_page = page
    run = empty.sync(D1, D1)
    assert run["status"] == "success" and run["stats"]["log_app"] == 2
    used = {c[5] for c in empty.feishu.calls if c[0] == "usage_page" and c[2] == "app"}
    assert used == {LEGACY_APP_ENTITY_TYPES}


def test_fallback_does_not_happen_for_unrelated_errors_and_never_drops_other_app_types(empty):
    from conftest import app_log

    empty.feishu.usage[("log", "app", D1)] = stamped([app_log("cli_a", 30), app_log("", 40, etype=10), app_log("-1", 5, etype=13)], D1)
    assert empty.sync(D1, D1)["status"] == "success"
    apps = {r["entity_id"]: (r["entity_type"], r["entity_name"], r["used_quota"]) for r in empty.db.q("SELECT * FROM usage_logs WHERE subject='app'")}
    # 飞书项目、CodeM 沙箱没有自己的应用 ID：各给一个固定 ID，不丢、不混在一起
    assert apps == {"cli_a": (5, "", 30), "builtin_10": (10, "飞书项目", 40), "builtin_13": (13, "CodeM 沙箱应用", 5)}
    an = empty.ctx.analytics
    names = {a["id"]: a["name"] for a in an.rank_apps(an.slice(Filter(D1, D1)))}
    assert names["builtin_10"] == "飞书项目"

    original = empty.feishu.usage_page

    def denied(kind, subject, day, token="", app_types=APP_ENTITY_TYPES):
        if (kind, subject) == ("log", "app"):
            raise FeishuError("查询 AI 用量日志失败：code=99991663 token expired")
        return original(kind, subject, day, token, app_types)

    empty.feishu.usage_page = denied
    run = empty.sync(D1, D1)  # 不是参数问题：不回退，这个数据集算失败，原有数据不动
    assert run["status"] == "partial" and count(empty, "usage_logs", "subject='app'") == 3
    assert not [c for c in empty.feishu.calls if c[0] == "usage_page" and c[5] == LEGACY_APP_ENTITY_TYPES]

    def rejects_new_types(kind, subject, day, token="", app_types=APP_ENTITY_TYPES):
        if subject == "app" and 13 in app_types:
            raise FeishuError("查询 AI 用量日志失败：code=1000001 invalid param: entity_type")
        return [i for i in original(kind, subject, day, token, app_types)[0] if i.get("entity_type") in app_types], ""

    empty.feishu.usage_page = rejects_new_types
    run = empty.sync(D1, D1)  # 接口不认新类型：退回早期四类，但以前同步到的飞书项目 / CodeM 数据要留着
    assert run["status"] == "success" and count(empty, "usage_logs", "subject='app'") == 3
    assert agg_matches_logs(empty)  # 汇总表只替换了那四类，没有多也没有少
    step = next(s for s in run["steps"] if s["dataset"] == "log_app")
    assert "只同步了其余四类应用" in step["message"]


def test_a_run_stopped_just_before_the_worker_picks_it_up_does_not_run(empty):
    engine = empty.ctx.engine
    run_id = engine.start(D1, D1, "manual", "ou_admin")
    row = empty.db.one("SELECT * FROM sync_runs WHERE id=?", (run_id,))  # 工作线程刚取到这一行……
    engine.stop(run_id)  # ……这时有人点了停止
    engine._run(row)
    assert engine.run(run_id)["status"] == "stopped" and count(empty, "usage_logs") == 0


def test_missing_department_falls_back_to_the_directory(empty):
    items = stamped(MEMBER_D1, D1)
    for it in items:
        it.pop("department_id")  # 应用没有部门字段权限时，接口不返回 department_id
    empty.feishu.usage[("log", "member", D1)] = items
    empty.sync(D1, D1)
    assert empty.db.val("SELECT department_id FROM usage_logs WHERE entity_id='ou_bob' LIMIT 1") == "d-rd"


def test_names_of_people_and_apps_outside_the_directory_are_resolved(empty):
    empty.feishu.usage[("log", "member", D1)] = stamped(
        [{"entity_type": 2, "entity_id": "ou_gone", "department_id": "d-old", "scenario_translate": "知识问答", "used_quota": 3, "_hour": 9}], D1
    )
    empty.feishu.user_names = lambda ids: {"ou_gone": "已离职的人"} if "ou_gone" in ids else {}
    empty.sync(D1, D1)
    assert empty.ctx.directory.user_name("ou_gone") == "已离职的人"
    assert empty.ctx.directory.dept_name("d-old") == "d-old"  # 查不到名称的部门先显示 ID
    assert empty.ctx.analytics.app_names() == {"cli_a": "客服机器人"}


def test_manual_app_names_are_not_overwritten_by_sync(env):
    env.ctx.engine.save_app_names({"cli_a": "我起的名字"}, "manual", "ou_admin")
    env.sync(D1, D1)
    env.ctx.engine.save_app_names({"cli_a": "自动解析的名字"}, "api")
    assert env.ctx.analytics.app_names()["cli_a"] == "我起的名字"

"""同步任务：按天整批替换、单个数据集失败不拖垮任务、断点续跑、巡检、停止。"""

from __future__ import annotations

import time
from datetime import timedelta

import pytest

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


def test_pagination_collects_every_page(empty):
    empty.feishu.page_size = 2
    run = empty.sync(D1, D1)
    assert run["stats"]["log_member"] == 5
    step = next(s for s in run["steps"] if s["dataset"] == "log_member")
    assert step["pages"] == 3


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

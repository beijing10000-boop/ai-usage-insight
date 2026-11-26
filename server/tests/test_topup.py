"""追加额度自动审批：只认申请 AI 额度的单子；审批节点核对后评论或同意，办理节点加用户组并办理；只预览不操作；月初清空用户组。"""

from __future__ import annotations

import json
import time
from datetime import datetime

import pytest

from aius_server.feishu import CST, FeishuError
from aius_server.topup import TAG, is_quota_request

ME = "ou_admin"  # 以他的身份处理
NOW = datetime(2026, 11, 15, 10, 0, tzinfo=CST)
CATEGORY = "协作/AI（AI额度，邮箱相关，配置相关）"
GROUP = "AI 4000点额度"


def instance(code: str, user: str = "ou_bob", name: str = "豆包 AI 额度", category: str = CATEGORY, node: str = "协作审批人", who: str = ME, users: list[str] | None = None) -> dict:
    form = [
        {"id": "w1", "name": "资源名称", "type": "input", "value": name},
        {"id": "w2", "name": "资源环境/类别", "type": "checkboxV2", "value": [category]},
        {"id": "w3", "name": "资源负责人/使用人", "type": "contact", "value": ["u1"], "open_ids": [user] if users is None else users},
        {"id": "w4", "name": "申请原因及用途", "type": "textarea", "value": "整理调研数据"},
    ]
    return {
        "status": "PENDING", "serial_number": f"S-{code}", "open_id": user, "start_time": str(int(time.time() * 1000)),
        "form": json.dumps(form, ensure_ascii=False),
        "task_list": [{"id": f"t-{code}", "node_name": node, "open_id": who, "status": "PENDING"}],
        "_flow": [("审批", "ou_alice"), ("协作办理人", ME)],
    }  # fmt: skip


@pytest.fixture
def tp(env):
    """基础额度 2000，用到 1950 算用完；每次追加 2000、费用 100；鲍勃用完了，戴维没用完。"""
    t = env.ctx.topups
    env.ctx.config.save("allowance", {"per_member": "2000"}, "ou_admin")
    env.feishu.groups[GROUP] = set()
    env.feishu.approval_form = [{"id": "f-amount", "name": "预估费用", "type": "amount"}, {"id": "f-note", "name": "费用说明", "type": "textarea"}]
    t.usage = {"ou_bob": 2000.0, "ou_dave": 500.0, "ou_carol": 1990.0, "ou_erin": 2100.0}
    t.month_used = lambda open_id, month: t.usage.get(open_id, 0.0)
    t.save({"mode": "on", "approval_code": "CODE-1", "approver": ME, "approve_node": "协作审批人", "handle_node": "协作办理人",
            "category": "协作/AI", "group": GROUP}, "ou_admin", now=NOW)  # fmt: skip
    return t


def row(env, code: str) -> dict:
    return dict(env.db.one("SELECT * FROM topup_requests WHERE instance_code=?", (code,)))


def actions(env, kind: str) -> list[tuple]:
    return [a for a in env.feishu.approval_actions if a[0] == kind]


def test_which_requests_count_as_ai_quota():
    def form(name, category=CATEGORY):
        return [{"name": "资源名称", "value": name}, {"name": "资源环境/类别", "value": [category]}]

    for name in ("飞书豆包 AI 额度", "豆包额度申请", "AI额度申请", "豆包工作版订阅额度 增额", "飞书豆包ai额度"):
        assert is_quota_request(form(name), "协作/AI"), name
    assert not is_quota_request(form("公共邮箱申请"), "协作/AI")  # 同一个类别下别的东西
    assert not is_quota_request(form("云服务器额度"), "协作/AI")  # 有「额度」但不是 AI 的
    assert not is_quota_request(form("豆包 AI 额度", "固定资产"), "协作/AI")  # 类别不对


def test_normal_request_is_approved_then_granted(env, tp):
    env.feishu.instances["i1"] = instance("i1")
    env.feishu.instances["mail"] = instance("mail", name="公共邮箱申请")
    tp.run(NOW)
    # 审批节点：填预估费用 100、费用说明，并以审批人的身份同意；意见以【自动处理】开头
    (_, code, node, opinion, form), = actions(env, "approve")
    assert (code, node) == ("i1", "协作审批人") and opinion == f"{TAG}已核对：本月基础额度已用完，本月未追加过，同意追加 2000 点。"
    assert form == [{"id": "f-amount", "type": "amount", "value": 100}, {"id": "f-note", "type": "textarea", "value": "本月基础额度已经用完，额外追加2000点，预计费用100元，次月失效。"}]
    assert row(env, "i1")["status"] == "approved" and row(env, "i1")["used"] == 2000 and row(env, "mail")["kind"] == "other"
    # 到了业务方审批（别人名下）：不动；不是 AI 额度的单子以后也不再去读
    env.feishu.calls.clear()
    tp.run(NOW)
    assert len(actions(env, "approve")) == 1 and ("approval_instance", "mail") not in env.feishu.calls
    # 业务方同意后到办理节点：加进用户组、记追加、以审批人的身份办理
    env.feishu.approval_approve("CODE-1", "i1", "t-i1+", "ou_alice", "同意")
    tp.run(NOW)
    assert env.feishu.groups[GROUP] == {"ou_bob"} and env.ctx.config.topup_points("ou_bob", "2026-11") == 2000
    assert actions(env, "approve")[-1][1:4] == ("i1", "协作办理人", f"{TAG}已经追加。")
    assert row(env, "i1")["status"] == "granted" and env.feishu.instances["i1"]["status"] == "APPROVED"
    assert "自动追加 2000 点" in env.db.val("SELECT detail FROM audit_log WHERE action='topup' ORDER BY id DESC LIMIT 1")
    records = tp.records()
    assert [(r["serial"], r["member_name"], r["status_name"]) for r in records] == [("S-i1", "鲍勃", "已追加")] and tp.grants("2026-11")[0]["name"] == "鲍勃"


def test_base_allowance_not_used_up_waits_then_continues(env, tp):
    env.feishu.instances["i2"] = instance("i2", user="ou_dave")
    tp.run(NOW)
    assert actions(env, "comment") == [("comment", "i2", ME, f"{TAG}本月基础额度未用完（已用 500 / 2,000 点），此审批单等用完后处理。")]
    assert row(env, "i2")["status"] == "waiting" and not actions(env, "approve")
    tp.run(NOW)  # 还没用完：不再评论第二次
    assert len(actions(env, "comment")) == 1
    tp.usage["ou_dave"] = 1960.0  # 用到 1950 以上就算用完（剩几十点跑不了任务）
    tp.run(NOW)
    assert row(env, "i2")["status"] == "approved" and actions(env, "approve")[0][1] == "i2"


def test_already_topped_up_this_month_gets_one_comment(env, tp):
    env.feishu.groups[GROUP].add("ou_bob")  # 已经在用户组里（之前手工加的）
    env.db.x("INSERT INTO topups(month, open_id, points, source, instance_code, created_at) VALUES('2026-11','ou_erin',2000,'approval','old',0)")  # 平台有追加记录
    env.feishu.instances["i3"] = instance("i3")
    env.feishu.instances["i4"] = instance("i4", user="ou_erin")
    tp.run(NOW)
    tp.run(NOW)
    text = f"{TAG}本月已经追加额外 2000 点，不再自动处理，转为人工处理。"
    assert sorted(actions(env, "comment")) == [("comment", "i3", ME, text), ("comment", "i4", ME, text)] and not actions(env, "approve")
    assert row(env, "i3")["status"] == row(env, "i4")["status"] == "duplicate"
    env.feishu.instances["i3"]["status"] = "CANCELED"  # 申请人撤回了
    tp.run(NOW)
    assert (row(env, "i3")["status"], row(env, "i3")["note"]) == ("closed", "已撤回")


def refill(env, code: str, **values) -> None:
    """申请人在被退回的单子上改了表单、重新提交（还是同一张单）。"""
    form = json.loads(env.feishu.instances[code]["form"])
    for f in form:
        if f["name"] in values:
            f["open_ids" if f["type"] == "contact" else "value"] = values[f["name"]]
    env.feishu.instances[code]["form"] = json.dumps(form, ensure_ascii=False)


def test_request_sent_back_and_fixed_is_picked_up(env, tp):
    """退回让申请人改：资源名称写错被当成别的申请的，隔几分钟重新读表单，改对了就接着处理；使用人填了两个人的，改成一个人后下一轮就处理。"""
    env.feishu.instances["fix"] = instance("fix", name="AI资源申请")  # 没写「额度」：认不出来
    env.feishu.instances["two"] = instance("two", user="ou_carol", users=["ou_carol", "ou_dave"])
    tp.run(NOW)
    assert (row(env, "fix")["kind"], row(env, "two")["status"]) == ("other", "manual") and not env.feishu.approval_actions
    refill(env, "fix", 资源名称="飞书豆包 AI 额度")
    refill(env, "two", **{"资源负责人/使用人": ["ou_carol"]})
    env.feishu.calls.clear()
    tp.run(NOW)
    assert (row(env, "two")["status"], row(env, "two")["member"]) == ("approved", "ou_carol")
    assert ("approval_instance", "fix") not in env.feishu.calls and row(env, "fix")["kind"] == "other"  # 5 分钟内不重读
    env.db.x("UPDATE topup_requests SET updated_at=updated_at-301 WHERE instance_code='fix'")
    tp.run(NOW)
    assert (row(env, "fix")["kind"], row(env, "fix")["status"], row(env, "fix")["resource"]) == ("quota", "approved", "飞书豆包 AI 额度")
    # 重读了还不是额度申请的：照旧不碰，再等 5 分钟
    env.feishu.instances["mail"] = instance("mail", name="公共邮箱申请")
    tp.run(NOW)
    env.db.x("UPDATE topup_requests SET updated_at=updated_at-301 WHERE instance_code='mail'")
    env.feishu.calls.clear()
    tp.run(NOW)
    tp.run(NOW)
    assert env.feishu.calls.count(("approval_instance", "mail")) == 1 and row(env, "mail")["kind"] == "other"


def test_cases_left_to_a_human(env, tp):
    env.feishu.instances["two"] = instance("two", users=["ou_bob", "ou_dave"])  # 使用人填了两个人
    env.ctx.config.set_extra("ou_erin", "10000", "", "ou_admin")  # 单独设置了追加额度
    env.feishu.instances["vip"] = instance("vip", user="ou_erin")
    env.feishu.instances["other"] = instance("other", who="ou_alice")  # 审批人不是指定的那位
    tp.run(NOW)
    assert not env.feishu.approval_actions
    assert (row(env, "two")["status"], row(env, "vip")["status"], row(env, "other")["status"]) == ("manual", "manual", "pending")
    assert "不止一个人" in row(env, "two")["note"] and "单独设置" in row(env, "vip")["note"]
    # 同一个人还有一张已经同意、在等办理的：第二张留给人工
    env.feishu.instances["a"] = instance("a")
    tp.run(NOW)
    env.feishu.instances["b"] = instance("b")
    tp.run(NOW)
    assert row(env, "a")["status"] == "approved" and row(env, "b")["status"] == "manual" and "S-a" in row(env, "b")["note"]


def test_preview_mode_plans_but_does_nothing(env, tp):
    tp.save({"mode": "preview"}, "ou_admin", now=NOW)
    env.feishu.instances["i1"] = instance("i1")
    env.feishu.instances["i2"] = instance("i2", user="ou_dave")
    env.feishu.instances["h"] = instance("h", user="ou_carol", node="协作办理人")
    tp.run(NOW)
    assert not env.feishu.approval_actions and env.feishu.groups[GROUP] == set() and not tp.grants("2026-11")
    assert row(env, "i1")["plan"] == "会填预估费用 100、费用说明，并同意" and row(env, "i1")["status"] == "pending"
    assert row(env, "i2")["plan"].startswith(f"会评论：{TAG}本月基础额度未用完（已用 500") and row(env, "h")["plan"] == f"会把他加进用户组「{GROUP}」并办理"
    # 切到自动处理：预览的结果作废，下一轮真的做
    tp.save({"mode": "on"}, "ou_admin", now=NOW)
    assert row(env, "i1")["plan"] == ""
    tp.run(NOW)
    assert row(env, "i1")["status"] == "approved" and row(env, "i2")["status"] == "waiting" and row(env, "h")["status"] == "granted"


def test_group_failure_is_reported_and_retried(env, tp):
    env.feishu.instances["h"] = instance("h", node="协作办理人")
    env.feishu.group_error = FeishuError("把人加进用户组失败：code=99991672 Access denied")
    tp.run(NOW)
    tp.run(NOW)
    assert row(env, "h")["status"] == "error" and "Access denied" in row(env, "h")["note"] and not actions(env, "approve")  # 没加成就不办理
    assert [oid for oid, _ in env.feishu.sent] == [ME] and "没有处理成功" in json.dumps(env.feishu.sent[0][1], ensure_ascii=False)  # 只通知一次
    env.feishu.group_error = None
    tp.run(NOW)
    assert row(env, "h")["status"] == "granted" and env.feishu.groups[GROUP] == {"ou_bob"}


def test_group_is_emptied_when_the_month_rolls_over(env, tp):
    env.feishu.groups[GROUP] = {"ou_bob", "ou_dave"}
    assert env.ctx.config.get("topup")["reset_month"] == "2026-11"  # 切到自动处理时只记下本月，不清
    tp.run(NOW)
    assert env.feishu.groups[GROUP] == {"ou_bob", "ou_dave"}
    tp.run(datetime(2026, 12, 1, 0, 5, tzinfo=CST))
    assert env.feishu.groups[GROUP] == set() and env.ctx.config.get("topup")["reset_month"] == "2026-12"
    assert "移出 2 人" in env.db.val("SELECT detail FROM audit_log WHERE action='topup' ORDER BY id DESC LIMIT 1")
    # 只预览、关闭时不清
    env.feishu.groups[GROUP] = {"ou_bob"}
    tp.save({"mode": "preview"}, "ou_admin", now=NOW)
    tp.run(datetime(2027, 1, 1, 0, 5, tzinfo=CST))
    assert env.feishu.groups[GROUP] == {"ou_bob"}


def test_granted_points_show_up_in_personal_allowance(env, tp):
    env.db.x("INSERT INTO topups(month, open_id, points, source, instance_code, created_at) VALUES('2026-11','ou_bob',2000,'approval','i1',0)")
    config = env.ctx.config
    assert config.quota_extra("ou_bob", "2026-11") == (2000, True) and config.quota_extra("ou_bob", "2026-12") == (2000, False)
    # 额度提醒：有追加记录的人按「基础 + 追加」再提醒一轮，卡片上直接按合计列；没有记录的人、别的月份只按基础额度
    alerts = env.ctx.alerts
    assert [lv["level"] for lv in alerts.planner("2026-11")("ou_bob")] == ["base:80", "base:100", "total:80", "total:100"]
    assert [lv["level"] for lv in alerts.planner("2026-11")("ou_dave")] == ["base:80", "base:100"]
    assert [lv["level"] for lv in alerts.planner("2026-12")("ou_bob")] == ["base:80", "base:100"]
    card = alerts.card("ou_bob", 2100.0, alerts.planner("2026-11")("ou_bob")[1], NOW)
    assert "追加额度" in json.dumps(card["elements"][1], ensure_ascii=False) and "**52%**" in json.dumps(card["elements"][1], ensure_ascii=False)  # 2100 / 4000


def test_topups_page_and_settings(env, tp):
    env.feishu.instances["i2"] = instance("i2", user="ou_dave")
    tp.run(NOW)
    c = env.browser("admin@example.com")
    page = c.get("/topups").text
    assert "追加审批" in page and "自动处理中" in page and "S-i2" in page and "等基础额度用完" in page and "戴维" in page
    assert f"{TAG}本月已经追加额外 2000 点，不再自动处理，转为人工处理。" in page and f"<p>{TAG}已经追加。</p>" in page
    data = {"action": "save", "mode": "preview", "approval_code": "CODE-1", "approver": ME, "approve_node": "协作审批人", "handle_node": "协作办理人",
            "category": "协作/AI", "group": GROUP, "points": "2000", "fee": "100", "used_up": "1950"}  # fmt: skip
    assert c.post("/topups", data=data).headers["location"] == "/topups?done=topup_saved"
    assert env.ctx.config.get("topup")["mode"] == "preview" and "只预览" in c.get("/topups").text
    assert c.post("/topups", data={**data, "mode": "auto"}).status_code == 400 and c.post("/topups", data={**data, "points": "很多"}).status_code == 400
    assert c.post("/topups", data={"action": "run"}).headers["location"] == "/topups?done=topup_run#records"
    assert "追加审批：只预览" in env.db.val("SELECT detail FROM audit_log WHERE action='topup' AND actor='ou_admin' ORDER BY id DESC LIMIT 1")
    assert env.browser("bob@example.com").get("/topups").status_code == 403
    assert env.browser("bob@example.com").post("/topups", data=data).status_code == 403


def test_nothing_runs_until_it_is_set_up(env):
    t = env.ctx.topups
    assert not t.tick(NOW) and t.ready() != ""  # 默认关着，设置也没填
    env.ctx.config.save("topup", {"mode": "on"}, "ou_admin")
    assert not t.tick(NOW) and "审批编码" in t.ready() and t.run(NOW)["pending"] == 0

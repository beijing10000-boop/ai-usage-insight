"""额度告警：基础额度和「基础 + 追加」各提醒一轮、每个档位只提醒一次、一下跨过几个只发最高的、开启前已超过的不补发、
个别人的追加额度、发送时段、卡片内容、页面。"""

from __future__ import annotations

import json
import time
from datetime import datetime

import pytest

from aius_server.alerts import level_name, plan_levels
from aius_server.feishu import CST, FeishuError
from aius_server.prefs import ConfigError
from conftest import D1

NOW = datetime(2026, 11, 15, 10, 0, tzinfo=CST)


def text_of(card: dict) -> str:
    return json.dumps(card, ensure_ascii=False).replace("\\n", "\n")


def labels_of(row: dict) -> list[str]:
    return [col["elements"][0]["content"].split("\n")[1].removeprefix("<font color='grey'>").removesuffix("</font>") for col in row["columns"]]


@pytest.fixture
def alerts(env):
    """基础 100 点，一般追加 100 点：档位是 80、100（基础）；追加过的人（top_up）再加 160、200（基础 + 追加）。"""
    a = env.ctx.alerts
    a.sleep = lambda _s: None
    env.ctx.config.save("allowance", {"per_member": "100", "extra": "100"}, "ou_admin")
    env.ctx.config.save("quota_alert", {"enabled": True}, "ou_admin")  # 直接打开（不经过页面），不记「开启前已超过」
    a.now_usage = {"ou_bob": 85.0, "ou_dave": 50.0}
    a.usage = lambda month: dict(a.now_usage)
    return a


def top_up(env, open_id: str, points: float = 100, month: str = "2026-11") -> None:
    """记一条追加记录：这个人这个月追加过额度。"""
    env.db.x("INSERT OR REPLACE INTO topups(month, open_id, points, source, instance_code, created_at) VALUES(?,?,?,'approval','i1',0)", (month, open_id, points))


def statuses(env) -> dict:
    return {(r["open_id"], r["level"]): r["status"] for r in env.db.q("SELECT * FROM quota_alerts")}


def test_levels_are_base_then_base_plus_top_up():
    # 没追加过的人只有基础额度的档位：用得再多也不按合计提醒
    assert [lv["level"] for lv in plan_levels(2000, 2000, [80, 100])] == ["base:80", "base:100"]
    assert [(lv["level"], lv["line"]) for lv in plan_levels(2000, 2000, [80, 100], topped=True)] == [
        ("base:80", 1600), ("base:100", 2000), ("total:80", 3200), ("total:100", 4000),
    ]  # fmt: skip
    # 追加得少：合计的档位只留比基础额度多的（2400 的 80% 是 1920，没超过基础额度）
    assert [lv["level"] for lv in plan_levels(2000, 400, [80, 100], topped=True)] == ["base:80", "base:100", "total:100"]
    assert [lv["line"] for lv in plan_levels(2000, 5000, [80, 100], topped=True)][-2:] == [5600, 7000]  # 个别人追加 5000
    assert plan_levels(0, 2000, [80, 100], topped=True) == []
    assert (level_name("base:80"), level_name("total:100")) == ("基础 80%", "基础 + 追加 100%")


def test_each_level_is_alerted_once_and_only_the_highest_one_is_sent(env, alerts):
    sent = alerts.check(NOW)
    assert [(s["open_id"], s["level"], s["status"]) for s in sent] == [("ou_bob", "base:80", "sent")]
    assert alerts.check(NOW) == []  # 同一个档位不再提醒
    top_up(env, "ou_bob")  # 鲍勃追加了 100 点
    alerts.now_usage["ou_bob"] = 210.0  # 两次检查之间一下跨过 100、160、200
    assert [(s["open_id"], s["level"]) for s in alerts.check(NOW)] == [("ou_bob", "total:100")]
    assert statuses(env) == {("ou_bob", "base:80"): "sent", ("ou_bob", "base:100"): "covered", ("ou_bob", "total:80"): "covered", ("ou_bob", "total:100"): "sent"}
    cards = [card for oid, card in env.feishu.sent]
    assert cards[0]["header"]["title"]["content"] == "飞书/豆包 AI 额度提醒 ｜ 本月已达 80%" and cards[0]["header"]["template"] == "orange"
    assert cards[1]["header"]["title"]["content"] == "飞书/豆包 AI 额度提醒 ｜ 本月已达 100%（含追加额度）" and cards[1]["header"]["template"] == "carmine"
    # 下个月从头算；追加记录只管当月，下个月没追加就只按基础额度提醒
    assert [(s["open_id"], s["level"]) for s in alerts.check(NOW.replace(month=12, day=2))] == [("ou_bob", "base:100")]


def test_heavy_use_without_a_top_up_record_is_only_alerted_on_the_base_allowance(env, alerts):
    alerts.now_usage = {"ou_bob": 500.0}  # 用了基础额度的 5 倍，但没有追加记录：不去猜他追加过
    assert [(s["open_id"], s["level"]) for s in alerts.check(NOW)] == [("ou_bob", "base:100")]
    assert set(statuses(env)) == {("ou_bob", "base:80"), ("ou_bob", "base:100")}
    assert "追加额度" not in text_of(env.feishu.sent[-1][1])
    assert [(lv["level"], lv["people"]) for lv in alerts.levels(NOW)] == [("base:80", 1), ("base:100", 1), ("total:80", 0), ("total:100", 0)]
    top_up(env, "ou_bob")  # 之后追加了：按合计再提醒一轮
    assert [(s["open_id"], s["level"]) for s in alerts.check(NOW)] == [("ou_bob", "total:100")]
    assert [lv["people"] for lv in alerts.levels(NOW)] == [1, 1, 1, 1]


def test_special_top_up_for_one_person(env, alerts):
    env.ctx.config.set_extra("ou_bob", "300", "项目需要", "ou_admin")  # 鲍勃单独设置了追加 300：一直按合计 400 算，是 320、400
    top_up(env, "ou_dave")  # 戴维这个月追加了一般的 100
    alerts.now_usage = {"ou_bob": 210.0, "ou_dave": 210.0, "ou_erin": 210.0}
    got = {s["open_id"]: s["level"] for s in alerts.check(NOW)}
    assert got == {"ou_bob": "base:100", "ou_dave": "total:100", "ou_erin": "base:100"}
    quota_extra = env.ctx.config.quota_extra
    assert (quota_extra("ou_bob", "2026-11"), quota_extra("ou_dave", "2026-11"), quota_extra("ou_erin", "2026-11")) == ((300, True), (100, True), (100, False))
    alerts.now_usage["ou_bob"] = 330.0
    assert [(s["open_id"], s["level"]) for s in alerts.check(NOW)] == [("ou_bob", "total:80")]
    env.ctx.config.delete_extra("ou_bob")  # 改回一般的：没有追加记录，就是没追加过
    assert quota_extra("ou_bob", "2026-11") == (100, False)


def test_turning_it_on_does_not_alert_people_already_over_until_they_cross_the_next_level(env, alerts):
    env.ctx.config.save("quota_alert", {"enabled": False}, "ou_admin")
    alerts.now_usage = {"ou_bob": 120.0, "ou_dave": 85.0, "ou_erin": 10.0}
    saved = alerts.save({"enabled": True, "thresholds": "80，100"}, "ou_admin", now=NOW)
    assert saved["baseline"] == 2 and saved["thresholds"] == [80, 100]
    assert alerts.check(NOW) == [] and env.feishu.sent == []
    assert alerts.waiting(NOW) == {"ou_bob": "base:100", "ou_dave": "base:80"}
    top_up(env, "ou_bob")  # 鲍勃之后追加了额度，又用到合计的 80%
    alerts.now_usage["ou_bob"] = 165.0
    assert [(s["open_id"], s["level"]) for s in alerts.check(NOW)] == [("ou_bob", "total:80")]
    # 补发：只给还没收到过提醒的人发他当时最高的档位（鲍勃已经收到合计 80% 的了）
    assert [(s["open_id"], s["level"]) for s in alerts.catch_up(NOW)] == [("ou_dave", "base:80")]
    assert alerts.waiting(NOW) == {} and alerts.catch_up(NOW) == []
    assert alerts.save({"enabled": True}, "ou_admin", now=NOW)["baseline"] == 0  # 已经开着、档位没变：不重新记


def test_send_hours_interval_and_failures(env, alerts):
    assert not alerts.tick(NOW.replace(hour=8)) and not alerts.tick(NOW.replace(hour=21))  # 发送时段外不看
    assert alerts.tick(NOW)
    for _ in range(50):
        if env.feishu.sent:
            break
        time.sleep(0.05)
    assert [oid for oid, _ in env.feishu.sent] == ["ou_bob"]
    assert not alerts.tick(NOW.replace(minute=5))  # 不到 10 分钟不再看
    while alerts._lock.locked():
        time.sleep(0.01)
    assert alerts.tick(NOW.replace(minute=10))
    while alerts._lock.locked():
        time.sleep(0.01)
    env.feishu.send_error = FeishuError("发送飞书消息失败：code=230013 user has left")
    alerts.now_usage["ou_dave"] = 100.0
    assert alerts.check(NOW)[0]["status"] == "failed"
    assert statuses(env)[("ou_dave", "base:100")] == "failed" and statuses(env)[("ou_dave", "base:80")] == "covered"
    env.db.x("UPDATE quota_alerts SET status='sending' WHERE open_id='ou_dave' AND level='base:100'")  # 正在发的时候服务重启了
    assert alerts.recover() == 1 and statuses(env)[("ou_dave", "base:100")] == "failed"


def test_no_alerts_without_a_monthly_allowance(env, alerts):
    env.ctx.config.save("allowance", {"per_member": ""}, "ou_admin")
    assert alerts.check(NOW) == [] and not alerts.tick(NOW)


def test_alert_card(env, alerts):
    alerts.usage = env.ctx.alerts.__class__.usage.__get__(alerts)  # 用真实的用量
    env.ctx.config.save("allowance", {"per_member": "20", "extra": "2000"}, "ou_admin")
    now = datetime.fromisoformat(f"{D1}T15:30:00+08:00")
    used = alerts.usage(now.strftime("%Y-%m"))["ou_bob"]  # 鲍勃这个月至少用了 30 点
    base100 = next(lv for lv in alerts.planner(now.strftime("%Y-%m"))("ou_bob") if lv["level"] == "base:100")
    card = alerts.card("ou_bob", used, base100, now)
    text = text_of(card)
    assert card["header"]["title"]["content"] == "飞书/豆包 AI 额度提醒 ｜ 本月已达 100%"
    assert card["elements"][0]["content"].startswith("**鲍勃**，您 **") and "已经达到基础额度的 **100%**" in text and "每月" not in text
    # 没追加过：额度那一排只和基础额度比；没有提示句、申请链接和「每个档位每月只提醒一次」
    assert labels_of(card["elements"][1]) == ["本月已用", "基础额度", "已用比例", "超出"]
    assert "请合理安排" not in text and "额度申请说明" not in text and "已经用完" not in text and "只提醒一次" not in text
    assert "**常用场景**\n① " in text and card["elements"][-2]["actions"][0]["url"] == "http://testserver/self-usage"
    assert card["elements"][-1]["elements"][0]["content"] == f"统计时间：{now.month} 月 1 日 - {now.month} 月 {now.day} 日 15:30（当天的用量每 10 分钟更新一次）"
    # 按合计提醒：写明合计多少点，额度那一排列出追加额度，已用比例按基础 + 追加合计算（37 / 40 = 92%）
    top_up(env, "ou_bob", 20, now.strftime("%Y-%m"))
    total80 = next(lv for lv in alerts.planner(now.strftime("%Y-%m"))("ou_bob") if lv["level"] == "total:80")
    card = alerts.card("ou_bob", 37.0, total80, now)
    assert card["header"]["title"]["content"] == "飞书/豆包 AI 额度提醒 ｜ 本月已达 80%（含追加额度）"
    assert "已经达到基础额度和追加额度合计（40 点）的 **80%**" in text_of(card)
    assert labels_of(card["elements"][1]) == ["本月已用", "基础额度", "追加额度", "已用比例", "剩余"]
    assert "**92%**\n<font color='grey'>已用比例</font>" in text_of(card["elements"][1])


def test_settings_are_checked(env):
    config = env.ctx.config
    for key, patch in (("quota_alert", {"thresholds": "八十,100"}), ("quota_alert", {"thresholds": ""}), ("quota_alert", {"thresholds": "80,180"}),
                       ("allowance", {"extra": "-1"}), ("allowance", {"extra": "两千"})):  # fmt: skip
        with pytest.raises(ConfigError):
            config.save(key, patch, "ou_admin")
    assert config.save("quota_alert", {"thresholds": "100, 80%,80"}, "ou_admin")["thresholds"] == [80, 100]
    config.save("allowance", {"per_member": "2000"}, "ou_admin")
    config.save("allowance", {"extra": ""}, "ou_admin")  # 留空 = 默认；也不会把基础额度清掉
    assert config.get("allowance") == {"per_member": 2000, "extra": 2000}
    # 老版本存过的「用量超过多少点算已追加」：现在不认了，读出来没有它
    env.db.x("UPDATE settings SET value=? WHERE key='allowance'", (json.dumps({"per_member": 2000, "extra": 2500, "topped_at": 3000}),))
    assert config.get("allowance") == {"per_member": 2000, "extra": 2500}
    with pytest.raises(ConfigError):
        config.set_extra("", "100", "", "ou_admin")
    with pytest.raises(ConfigError):
        config.set_extra("ou_bob", "很多", "", "ou_admin")


def test_alert_and_top_up_pages(env):
    env.ctx.alerts.sleep = lambda _s: None
    env.ctx.config.save("allowance", {"per_member": "2000"}, "ou_admin")  # 一般追加 2000：档位 1600、2000、3200、4000
    c = env.browser("admin@example.com")
    page = c.get("/reports").text
    assert 'id="alerts"' in page and "额度告警" in page and 'value="80,100"' in page and "已关闭" in page and "基础 + 追加 80%" in page
    r = c.post("/reports", data={"action": "alert_save", "enabled": "1", "thresholds": "80,100"})
    assert r.headers["location"].startswith("/reports?done=alert_") and env.ctx.config.get("quota_alert")["enabled"]
    assert "额度告警：开启" in env.db.val("SELECT detail FROM audit_log WHERE action='report' ORDER BY id DESC LIMIT 1")
    assert c.post("/reports", data={"action": "alert_save", "enabled": "1", "thresholds": "abc"}).status_code == 400
    assert c.post("/reports", data={"action": "alert_test"}).headers["location"] == "/reports?done=alert_test#alerts"
    assert [oid for oid, _ in env.feishu.sent][-1] == "ou_admin"
    # 额度分配页：一般追加多少、个别人的追加额度（谁追加过看追加记录，页面上不再有「用量超过多少点算已追加」）。选人用和成员用量一样的找人框：框里显示姓名，open_id 在隐藏字段里
    quota = c.get("/quota").text
    assert "追加额度" in quota and 'data-member-pick="extra-who-value"' in quota and '<input type="hidden" id="extra-who-value" name="target" value="" />' in quota
    assert 'list="extra-who-list"' not in quota
    assert "算已追加" not in quota and 'name="topped_at"' not in quota and '<a href="/topups">「追加审批」</a>' in quota and "这个月现在有 <b>0</b> 人" in quota
    assert c.post("/quota", data={"action": "topup", "extra": "2500"}).status_code == 303
    assert env.ctx.config.get("allowance") == {"per_member": 2000, "extra": 2500}
    assert c.post("/quota", data={"action": "extra_set", "target": "ou_bob", "amount": "5000", "note": "项目需要"}).status_code == 303
    page = c.get("/quota").text
    assert "鲍勃" in page and "项目需要" in page and env.ctx.config.quota_extra("ou_bob", "2026-11") == (5000, True)
    assert c.post("/quota", data={"action": "extra_set", "target": "", "amount": "5000"}).status_code == 400
    c.post("/quota", data={"action": "extra_delete", "target": "ou_bob"})
    assert env.ctx.config.quota_extra("ou_bob", "2026-11") == (2500, False)
    assert env.browser("bob@example.com").post("/reports", data={"action": "alert_test"}).status_code == 403
    assert env.browser("bob@example.com").post("/quota", data={"action": "extra_set", "target": "ou_bob", "amount": "1"}).status_code == 403

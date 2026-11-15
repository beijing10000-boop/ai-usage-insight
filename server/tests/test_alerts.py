"""额度告警：跨过档位只提醒一次、一下跨过几个只发最高的、开启前已超过的不补发、发送时段、卡片内容、页面。"""

from __future__ import annotations

import time
from datetime import datetime

import pytest

from aius_server.feishu import CST, FeishuError
from aius_server.prefs import ConfigError
from conftest import D1

NOW = datetime(2026, 11, 15, 10, 0, tzinfo=CST)


def text_of(card: dict) -> str:
    import json

    return json.dumps(card, ensure_ascii=False).replace("\\n", "\n")


@pytest.fixture
def alerts(env):
    a = env.ctx.alerts
    a.sleep = lambda _s: None
    env.ctx.config.save("allowance", {"per_member": "100"}, "ou_admin")
    env.ctx.config.save("quota_alert", {"enabled": True}, "ou_admin")  # 直接打开（不经过页面），不记「开启前已超过」
    a.now_usage = {"ou_bob": 85.0, "ou_dave": 50.0}
    a.usage = lambda month: dict(a.now_usage)
    return a


def statuses(env) -> dict:
    return {(r["open_id"], r["threshold"]): r["status"] for r in env.db.q("SELECT * FROM quota_alerts")}


def test_each_threshold_is_alerted_once_and_only_the_highest_one_is_sent(env, alerts):
    sent = alerts.check(NOW)
    assert [(s["open_id"], s["threshold"], s["status"]) for s in sent] == [("ou_bob", 80, "sent")]
    assert alerts.check(NOW) == []  # 同一个档位不再提醒
    alerts.now_usage["ou_bob"] = 210.0  # 两次检查之间一下跨过 100、180、200
    assert [(s["open_id"], s["threshold"]) for s in alerts.check(NOW)] == [("ou_bob", 200)]
    assert statuses(env) == {("ou_bob", 80): "sent", ("ou_bob", 100): "covered", ("ou_bob", 180): "covered", ("ou_bob", 200): "sent"}
    cards = [card for oid, card in env.feishu.sent]
    assert cards[0]["header"]["title"]["content"] == "飞书/豆包 AI 额度提醒 ｜ 本月已用 80%" and cards[0]["header"]["template"] == "orange"
    assert cards[1]["header"]["title"]["content"] == "飞书/豆包 AI 额度提醒 ｜ 本月已用 200%" and cards[1]["header"]["template"] == "carmine"
    # 下个月从头算
    assert [(s["open_id"], s["threshold"]) for s in alerts.check(NOW.replace(month=12, day=2))] == [("ou_bob", 200)]


def test_turning_it_on_does_not_alert_people_already_over_until_they_cross_the_next_level(env, alerts):
    env.ctx.config.save("quota_alert", {"enabled": False}, "ou_admin")
    alerts.now_usage = {"ou_bob": 120.0, "ou_dave": 85.0, "ou_erin": 10.0}
    saved = alerts.save({"enabled": True, "thresholds": "80，100,180,200"}, "ou_admin", now=NOW)
    assert saved["baseline"] == 2 and saved["thresholds"] == [80, 100, 180, 200]
    assert alerts.check(NOW) == [] and env.feishu.sent == []
    assert alerts.waiting(NOW) == {"ou_bob": 100, "ou_dave": 80}
    alerts.now_usage["ou_bob"] = 185.0
    assert [(s["open_id"], s["threshold"]) for s in alerts.check(NOW)] == [("ou_bob", 180)]
    # 补发：只给还没收到过提醒的人发他当时最高的档位（鲍勃已经收到 180% 的了）
    assert [(s["open_id"], s["threshold"]) for s in alerts.catch_up(NOW)] == [("ou_dave", 80)]
    assert alerts.waiting(NOW) == {} and alerts.catch_up(NOW) == []
    # 已经开着、只改了别的设置：不重新记
    assert alerts.save({"enabled": True, "extra": "2500"}, "ou_admin", now=NOW)["baseline"] == 0


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
    assert statuses(env)[("ou_dave", 100)] == "failed" and statuses(env)[("ou_dave", 80)] == "covered"
    env.db.x("UPDATE quota_alerts SET status='sending' WHERE open_id='ou_dave' AND threshold=100")  # 正在发的时候服务重启了
    assert alerts.recover() == 1 and statuses(env)[("ou_dave", 100)] == "failed"


def test_no_alerts_without_a_monthly_allowance(env, alerts):
    env.ctx.config.save("allowance", {"per_member": ""}, "ou_admin")
    assert alerts.check(NOW) == [] and not alerts.tick(NOW)


def labels_of(row: dict) -> list[str]:
    return [col["elements"][0]["content"].split("\n")[1].removeprefix("<font color='grey'>").removesuffix("</font>") for col in row["columns"]]


def test_alert_card(env, alerts):
    alerts.usage = env.ctx.alerts.__class__.usage.__get__(alerts)  # 用真实的用量
    env.ctx.config.save("allowance", {"per_member": "20"}, "ou_admin")
    now = datetime.fromisoformat(f"{D1}T15:30:00+08:00")
    used = alerts.usage(now.strftime("%Y-%m"))["ou_bob"]  # 鲍勃这个月至少用了 30 点
    card = alerts.card("ou_bob", used, 100, now)
    text = text_of(card)
    assert card["elements"][0]["content"].startswith("**鲍勃**，您 **") and "已经达到基础额度的 **100%**" in text and "每月" not in text
    # 没超过 3000 点：额度那一排只和基础额度比；不再有提示句、申请链接和「每个档位每月只提醒一次」
    assert labels_of(card["elements"][1]) == ["本月已用", "基础额度", "已用比例", "超出"]
    assert "请合理安排" not in text and "额度申请说明" not in text and "已经用完" not in text and "只提醒一次" not in text
    assert "**常用场景**\n① " in text and card["elements"][-2]["actions"][0]["url"] == "http://testserver/self-usage"
    assert card["elements"][-1]["elements"][0]["content"] == f"统计时间：{now.month} 月 1 日 - {now.month} 月 {now.day} 日 15:30（当天的用量每 10 分钟更新一次）"
    # 超过「算作已追加的用量」：列出追加额度，已用比例按基础 + 追加合计算（37 / 40 = 92%）
    env.ctx.config.save("quota_alert", {"topped_at": "30", "extra": "20"}, "ou_admin")
    row = alerts.card("ou_bob", 37.0, 180, now)["elements"][1]
    assert labels_of(row) == ["本月已用", "基础额度", "追加额度", "已用比例", "剩余"]
    assert "**92%**\n<font color='grey'>已用比例</font>" in text_of(row)
    env.ctx.config.save("quota_alert", {"extra": "", "topped_at": ""}, "ou_admin")
    q = env.ctx.config.get("quota_alert")
    assert (q["extra"], q["topped_at"]) == (2000, 3000)  # 留空 = 默认


def test_thresholds_and_settings_are_checked(env):
    config = env.ctx.config
    with pytest.raises(ConfigError):
        config.save("quota_alert", {"thresholds": "八十,100"}, "ou_admin")
    with pytest.raises(ConfigError):
        config.save("quota_alert", {"thresholds": ""}, "ou_admin")
    with pytest.raises(ConfigError):
        config.save("quota_alert", {"topped_at": "三千"}, "ou_admin")
    with pytest.raises(ConfigError):
        config.save("quota_alert", {"extra": "-1"}, "ou_admin")
    assert config.save("quota_alert", {"thresholds": "200, 80%,100,180,100"}, "ou_admin")["thresholds"] == [80, 100, 180, 200]


def test_reports_page_alert_section(env):
    env.ctx.alerts.sleep = lambda _s: None
    env.ctx.config.save("allowance", {"per_member": "20"}, "ou_admin")
    c = env.browser("admin@example.com")
    page = c.get("/reports").text
    assert 'id="alerts"' in page and "额度告警" in page and 'value="80,100,180,200"' in page and "已关闭" in page
    r = c.post("/reports", data={"action": "alert_save", "enabled": "1", "thresholds": "80,100,180,200", "extra": "2000", "topped_at": "3000"})
    assert r.headers["location"].startswith("/reports?done=alert_") and env.ctx.config.get("quota_alert")["enabled"]
    assert "额度告警：开启" in env.db.val("SELECT detail FROM audit_log WHERE action='report' ORDER BY id DESC LIMIT 1")
    assert c.post("/reports", data={"action": "alert_save", "enabled": "1", "thresholds": "abc"}).status_code == 400
    assert c.post("/reports", data={"action": "alert_test"}).headers["location"] == "/reports?done=alert_test#alerts"
    assert [oid for oid, _ in env.feishu.sent][-1] == "ou_admin"
    assert env.browser("bob@example.com").post("/reports", data={"action": "alert_test"}).status_code == 403

"""AI 用量周报：周期、卡片内容、推送范围、到点判断。"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from aius_server.feishu import CST, FeishuError
from conftest import log, stamped


def week(env, monday: date):
    """给上一周（monday 之前的那一周）造两天的用量，返回 (周一, 周日)。"""
    start, end = monday - timedelta(days=7), monday - timedelta(days=1)
    for day, items in (
        (start.isoformat(), [log("ou_bob", "d-rd", "知识问答>主对话>网页版问答", 40), log("ou_bob", "d-rd", "智能纪要>会议纪要>纪要生成", 2, feature=2)]),
        (end.isoformat(), [log("ou_bob", "d-rd", "豆包工作>主对话>深度思考", 10, feature=4), log("ou_carol", "d-sales", "知识问答>主对话>网页版问答", 5)]),
        ((start - timedelta(days=3)).isoformat(), [log("ou_bob", "d-rd", "知识问答>主对话>网页版问答", 100)]),  # 上上周：只算进累计
    ):
        env.feishu.usage[("log", "member", day)] = stamped(items, day)
    env.sync((start - timedelta(days=3)).isoformat(), end.isoformat())
    return start, end


def monday_of_this_week() -> date:
    now = datetime.now(CST).date()
    return now - timedelta(days=now.weekday())


def test_period_is_last_monday_to_sunday(empty):
    report = empty.ctx.report
    p = report.period(date(2026, 11, 11))  # 周三
    assert p == {"start": "2026-11-02", "end": "2026-11-08", "since": "2026-01-01"}
    empty.ctx.config.save("report", {"since": "2026-10-01"}, "ou_admin")
    assert report.period(date(2026, 11, 9))["since"] == "2026-10-01"  # 周一看的也是上一周


def test_card_content(empty):
    monday = monday_of_this_week()
    start, end = week(empty, monday)
    report = empty.ctx.report
    period = report.period(monday)
    period["since"] = (start - timedelta(days=5)).isoformat()
    rep = report.reports(period)["ou_bob"]
    assert rep["week"] == 52 and rep["total"] == 152 and rep["scenarios"][0] == ("知识问答>主对话>网页版问答", 40)
    card = report.card("鲍勃", rep, period)
    text = "\n".join(e.get("content", "") for e in card["elements"])
    assert card["header"]["title"]["content"] == f"AI 用量周报 ｜ {start:%m/%d} - {end:%m/%d}"
    assert "上周消耗 **52.00 点**，折算 **¥2.60**" in text
    assert "通用额度：40.00 点 ｜ ¥2.00" in text and "智能纪要：2.00 点" in text and "豆包团队订阅：10.00 点" in text and "飞书 Aily" not in text
    assert "🥇 知识问答>主对话>网页版问答 · 40.00 点" in text and "**152.00 点** ｜ ¥7.60" in text
    assert card["elements"][-1]["actions"][0]["url"] == "http://testserver/self-usage"
    idle = report.card("弗兰克", {"week": 0.0, "total": 0.0, "features": {}, "scenarios": []}, period)
    assert "上周没有 AI 用量记录" in idle["elements"][0]["content"]


def test_push_scopes_and_test_send(empty):
    monday = monday_of_this_week()
    week(empty, monday)
    report = empty.ctx.report
    report.sleep = lambda _s: None
    run = report.push("manual", "ou_admin", now=monday)
    assert run["status"] == "success" and (run["total"], run["sent"]) == (2, 2)
    assert {oid for oid, _ in empty.feishu.sent} == {"ou_bob", "ou_carol"}  # 默认只发给上周有用量的人
    empty.feishu.sent.clear()
    empty.ctx.config.save("report", {"scope": "all"}, "ou_admin")
    assert report.push("manual", "ou_admin", now=monday)["total"] == 7  # 通讯录里所有人
    empty.feishu.sent.clear()
    run = report.push("test", "ou_dave", only=["ou_dave"], now=monday)
    assert run["trigger"] == "test" and [oid for oid, _ in empty.feishu.sent] == ["ou_dave"]


def test_push_records_failures(empty):
    monday = monday_of_this_week()
    week(empty, monday)
    empty.ctx.report.sleep = lambda _s: None
    empty.feishu.send_error = FeishuError("发送飞书消息失败：code=230006 bot ability is not activated")
    run = empty.ctx.report.push("manual", "ou_admin", now=monday)
    assert run["status"] == "failed" and run["failed"] == 2 and "bot ability" in run["error"]
    assert empty.ctx.report.runs()[0]["actor_name"] == "管理员"


def test_due_only_once_on_the_configured_day_after_the_configured_hour(empty):
    report, config = empty.ctx.report, empty.ctx.config
    wednesday_10 = datetime(2026, 11, 11, 10, 5, tzinfo=CST)
    assert report.due(wednesday_10) is False  # 没开启
    config.save("report", {"enabled": True, "weekday": 3, "hour": 10}, "ou_admin")
    assert report.due(wednesday_10) is True
    assert report.due(wednesday_10.replace(hour=9)) is False  # 还没到点
    assert report.due(wednesday_10.replace(hour=15)) is True  # 那个整点没赶上（服务重启）：当天晚些时候补发
    assert report.due(wednesday_10 + timedelta(days=1)) is False  # 不是推送日
    config.mark("report", "last_push_date", "2026-11-11")
    assert report.due(wednesday_10) is False  # 今天已经推送过

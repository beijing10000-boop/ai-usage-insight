"""报表发送：日报 / 周报 / 月报的周期、卡片内容（个人 + 部门负责人的部门）、发送范围、到点判断。"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta

from aius_server.feishu import CST, FeishuError
from aius_server.report import delta, points
from aius_server.sync import today
from conftest import D1


def text_of(card: dict) -> str:
    """卡片里所有文字（markdown、指标列、按钮、脚注）拼在一起，方便断言。"""
    return json.dumps(card, ensure_ascii=False).replace("\\n", "\n")


def test_periods():
    from aius_server.report import UsageReports

    r = UsageReports.__new__(UsageReports)  # 周期只和日期有关
    d = r.period("daily", date(2026, 11, 11))  # 周三发周二的
    assert (d["start"], d["end"], d["prev_start"], d["prev_end"]) == ("2026-11-10", "2026-11-10", "2026-11-03", "2026-11-03")
    assert d["label"] == "11 月 10 日（周二）" and d["title"] == "11/10 周二" and d["prev_label"] == "上周同日" and d["query"] == {"start": "2026-11-10", "end": "2026-11-10"}
    w = r.period("weekly", date(2026, 11, 11))
    assert (w["start"], w["end"], w["prev_start"], w["prev_end"]) == ("2026-11-02", "2026-11-08", "2026-10-26", "2026-11-01")
    assert w["title"] == "11/02 - 11/08" and w["prev_label"] == "上一周"
    assert r.period("weekly", date(2026, 11, 9))["start"] == "2026-11-02"  # 周一发的也是上一周
    m = r.period("monthly", date(2026, 11, 1))
    assert (m["start"], m["end"], m["prev_start"], m["prev_end"]) == ("2026-10-01", "2026-10-31", "2026-09-01", "2026-09-30")
    assert m["label"] == "2026 年 10 月" and m["query"] == {"month": "2026-10"}
    assert r.period("monthly", date(2027, 1, 5))["start"] == "2026-12-01"  # 跨年


def test_number_formats():
    assert (points(1234.5), points(30), points(0.126)) == ("1,234.5", "30", "0.13")
    assert (delta(120, 100), delta(80, 100), delta(100, 100), delta(5, 0)) == ("▲ 20%", "▼ 20%", "持平", "—")
    assert delta(112.3, 100) == "▲ 12.3%"


def test_personal_and_department_cards(env):
    report = env.ctx.report
    period = report.period("daily")  # 发昨天（D1）的
    assert period["start"] == D1
    data = report.build(period)
    # 鲍勃：自己用了 30 点（2 次）；他负责研发部：鲍勃 30 + 戴维 1
    me = report.personal(data, "ou_bob")
    assert (me["amount"], me["records"]) == (30, 2)
    rd = report.department(data, "d-rd")
    assert (rd["amount"], rd["active"], rd["headcount"], rd["top"][0]) == (31, 2, 2, ("ou_bob", 30))
    card = report.card(period, "ou_bob", me, [rd])
    text = text_of(card)
    assert card["header"]["title"]["content"].startswith("AI 用量日报 ｜ ") and card["header"]["template"] == "blue"
    assert "Hi，**鲍勃**" in text and "你和你负责的部门" in text
    assert "**30**" in text and "¥1.50" in text and "比上周同日" in text  # 上周同日没有数据：—
    assert "研发部 · 部门用量" in text and "**31**" in text and "活跃 **2** / 2 人" in text and "🥇 鲍勃 · 30 点" in text
    self_url = f"http://testserver/self-usage?start={D1}&end={D1}"
    dept_url = f"http://testserver/dept-ops?dept=d-rd&start={D1}&end={D1}"
    assert f"**详情请查看：**[{self_url}]({self_url})" in text and f"部门详情请查看：[{dept_url}]({dept_url})" in text
    assert [b["url"] for b in card["elements"][-2]["actions"]] == [self_url, dept_url]
    assert card["elements"][-1]["tag"] == "note"
    # 技术中心的负责人爱丽丝自己没用过，部门（含研发部、运维部）有：鲍勃 30 + 戴维 1 + 艾琳 5
    tech = report.department(data, "d-tech")
    assert tech["amount"] == 36 and tech["active"] == 3
    alice = text_of(report.card(period, "ou_alice", report.personal(data, "ou_alice"), [tech]))
    assert "没有 AI 用量记录" in alice and "技术中心 · 部门用量" in alice and "**36**" in alice
    # 普通人：没有部门部分，只有一个按钮
    dave = report.card(period, "ou_dave", report.personal(data, "ou_dave"), [])
    assert "部门用量" not in text_of(dave) and "Hi，**戴维**\n这是你在" in text_of(dave) and len(dave["elements"][-2]["actions"]) == 1


def test_monthly_allowance_line(env):
    env.ctx.config.save("allowance", {"per_member": "20"}, "ou_admin")
    report = env.ctx.report
    period = report.period("daily")
    data = report.build(period)
    text = text_of(report.card(period, "ou_bob", report.personal(data, "ou_bob"), []))
    used = data["month_used"]["ou_bob"]  # 这个月 1 日到昨天
    assert "基础额度：已用" in text and "/ 20 点" in text and ("已用完" in text if used >= 20 else "剩余" in text)


def test_push_scope_and_test_send(env):
    report = env.ctx.report
    report.sleep = lambda _s: None
    run = report.push("daily", "manual", "ou_admin")
    # 昨天有用量的四个人，加上自己没用、但部门里有人用了的负责人爱丽丝
    assert run["status"] == "success" and run["kind"] == "daily" and run["kind_name"] == "日报" and run["total"] == 5
    assert {oid for oid, _ in env.feishu.sent} == {"ou_alice", "ou_bob", "ou_dave", "ou_erin", "ou_carol"}
    cards = dict(env.feishu.sent)
    assert "技术中心 · 部门用量" in text_of(cards["ou_alice"]) and "销售部 · 部门用量" in text_of(cards["ou_carol"])
    env.feishu.sent.clear()
    env.ctx.config.save("report", {"scope": "all"}, "ou_admin")
    assert report.push("daily", "manual", "ou_admin")["total"] == 7  # 通讯录里所有人
    env.feishu.sent.clear()
    run = report.push("weekly", "test", "ou_dave", only=["ou_dave"])
    assert run["trigger"] == "test" and [oid for oid, _ in env.feishu.sent] == ["ou_dave"]
    assert [r["kind_name"] for r in report.runs()] == ["周报", "日报", "日报"]


def test_push_records_failures(env):
    env.ctx.report.sleep = lambda _s: None
    env.feishu.send_error = FeishuError("发送飞书消息失败：code=230006 bot ability is not activated")
    run = env.ctx.report.push("daily", "manual", "ou_admin")
    assert run["status"] == "failed" and run["failed"] == 5 and "bot ability" in run["error"] and "连续失败" in run["error"]
    assert env.ctx.report.runs()[0]["actor_name"] == "管理员"


def test_due_rules(env):
    report, config = env.ctx.report, env.ctx.config
    wed = datetime(2026, 11, 11, 9, 5, tzinfo=CST)  # 周三
    assert not any(report.due(k, wed) for k in ("daily", "weekly", "monthly"))  # 默认都关着
    config.save("report_daily", {"enabled": True, "hour": 9}, "ou_admin")
    assert report.due("daily", wed) and not report.due("daily", wed.replace(hour=8))
    assert report.due("daily", wed.replace(hour=15))  # 那个整点没赶上（服务重启）：当天晚些时候补发
    config.mark("report_daily", "last_push_date", "2026-11-11")
    assert not report.due("daily", wed) and report.due("daily", wed + timedelta(days=1))  # 一天一次
    config.save("report_weekly", {"enabled": True, "weekday": 3, "hour": 9}, "ou_admin")
    assert report.due("weekly", wed) and not report.due("weekly", wed + timedelta(days=1))  # 只在周三
    config.save("report_monthly", {"enabled": True, "day": 5, "hour": 9}, "ou_admin")
    assert not report.due("monthly", wed.replace(day=4)) and report.due("monthly", wed)  # 5 号之后，错过了当月补发
    config.mark("report_monthly", "last_push_date", "2026-11-05")
    assert not report.due("monthly", wed) and report.due("monthly", datetime(2026, 12, 5, 9, tzinfo=CST))  # 一个月一次
    env.ctx.engine.start(D1, D1, "manual", "ou_admin")  # 有同步任务在跑：先不发
    assert not report.due("weekly", wed)


def test_tick_marks_and_sends_in_the_background(env):
    report, config = env.ctx.report, env.ctx.config
    report.sleep = lambda _s: None
    config.save("report_daily", {"enabled": True, "hour": 0}, "ou_admin")
    now = datetime.now(CST)
    assert report.tick(now) == ["daily"] and config.get("report_daily")["last_push_date"] == today().isoformat()
    assert report.tick(now) == []  # 记下来了：不会再发一轮
    for _ in range(50):  # 后台线程发完
        if report.runs() and report.runs()[0]["status"] != "running":
            break
        time.sleep(0.05)
    assert report.runs()[0]["trigger"] == "schedule" and report.runs()[0]["sent"] == 5

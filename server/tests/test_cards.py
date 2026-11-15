"""发出去的卡片：每发一张记一张，发送记录 / 提醒记录里能看到发给每个人的卡片（按飞书卡片的样子画出来）。"""

from __future__ import annotations

import time
from datetime import datetime

from aius_server import cards
from aius_server.feishu import CST, FeishuError


def test_every_report_card_is_kept_with_its_run(env):
    report = env.ctx.report
    report.sleep = lambda _s: None
    run = report.push("daily", "manual", "ou_admin")
    rows = env.db.q("SELECT open_id, status, title, card FROM sent_cards WHERE run_id=? ORDER BY id", (run["id"],))
    assert len(rows) == 5 and {r["status"] for r in rows} == {"sent"}
    sent = dict(env.feishu.sent)
    stored = {r["open_id"]: cards.get(env.db, env.db.val("SELECT id FROM sent_cards WHERE run_id=? AND open_id=?", (run["id"], r["open_id"])))["card"] for r in rows}
    assert stored == sent  # 和发出去的一模一样
    assert rows[0]["title"].startswith("飞书/豆包 AI 用量日报")
    assert report.runs()[0]["cards"] == 5 and [p["name"] for p in report.recipients(run["id"])][:1]
    # 发送失败的也记，带失败原因
    env.feishu.send_error = FeishuError("发送飞书消息失败：code=230013 user has left")
    failed = report.push("daily", "test", "ou_admin", only=["ou_dave"])
    assert env.db.one("SELECT status, error FROM sent_cards WHERE run_id=?", (failed["id"],))[:] == ("failed", "发送飞书消息失败：code=230013 user has left")


def test_old_card_contents_are_cleaned_up(env):
    old = int(time.time()) - (cards.CARD_KEEP_DAYS + 1) * 86400
    env.db.x("INSERT INTO sent_cards(run_id, open_id, card, status, sent_at) VALUES(1, 'ou_bob', '{}', 'sent', ?)", (old,))
    env.ctx.report.sleep = lambda _s: None
    env.ctx.report.push("daily", "manual", "ou_admin")
    assert env.db.val("SELECT COUNT(*) FROM sent_cards WHERE sent_at=?", (old,)) == 0


def test_render_draws_the_card_and_escapes_everything():
    card = {
        "header": {"template": "red", "title": {"tag": "plain_text", "content": "标题 <b>x</b>"}},
        "elements": [
            {"tag": "markdown", "content": "**鲍勃**，<script>alert(1)</script>\n[查看](https://example.com/a?x=1&y=2) <font color='grey'>小字</font>"},
            {"tag": "column_set", "background_style": "grey", "columns": [{"tag": "column", "elements": [{"tag": "markdown", "content": "**30**\n<font color='grey'>消耗点数</font>", "text_align": "center"}]}]},
            {"tag": "hr"},
            {"tag": "action", "actions": [{"tag": "button", "type": "primary", "text": {"tag": "plain_text", "content": "点击查看"}, "url": "javascript:alert(1)"}]},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": "统计时间 <i>"}]},
        ],
    }
    out = str(cards.render(card))
    assert '<div class="fcard-head fcard-red">标题 &lt;b&gt;x&lt;/b&gt;</div>' in out
    assert "<strong>鲍勃</strong>，&lt;script&gt;alert(1)&lt;/script&gt;<br>" in out and "<script>" not in out
    assert '<a href="https://example.com/a?x=1&amp;y=2" target="_blank" rel="noopener noreferrer">查看</a>' in out
    assert '<span class="fcard-font-grey">小字</span>' in out and '<div class="fcard-cols fcard-grey">' in out and "fcard-center" in out
    assert "javascript:" not in out and '<span class="fcard-btn fcard-btn-primary">点击查看</span>' in out  # 不是 http(s) 的链接不做成链接
    assert '<div class="fcard-note">统计时间 &lt;i&gt;</div>' in out


def test_send_records_pages_show_each_persons_card(env):
    env.ctx.report.sleep = lambda _s: None
    run = env.ctx.report.push("daily", "manual", "ou_admin")
    c = env.browser("admin@example.com")
    assert f'<a href="/reports/runs/{run["id"]}">查看 5 张</a>' in c.get("/reports").text
    page = c.get(f"/reports/runs/{run['id']}").text
    assert "发给了谁" in page and "鲍勃" in page and "卡萝" in page and 'class="fcard"' in page and "飞书/豆包 AI 用量日报" in page
    carol = env.db.val("SELECT id FROM sent_cards WHERE run_id=? AND open_id='ou_carol'", (run["id"],))
    page = c.get(f"/reports/runs/{run['id']}", params={"card": carol}).text
    assert "卡萝 收到的卡片" in page and "销售部 · 部门用量" in page
    assert c.get("/reports/runs/9999").status_code == 404
    # 报表的卡片单独打开：跳到那一轮的页面
    r = c.get(f"/reports/cards/{carol}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/reports/runs/{run['id']}?card={carol}"
    assert c.get("/reports/cards/9999").status_code == 404
    assert env.browser("bob@example.com").get(f"/reports/runs/{run['id']}").status_code == 403


def test_alert_records_link_to_the_card(env):
    alerts = env.ctx.alerts
    alerts.sleep = lambda _s: None
    env.ctx.config.save("allowance", {"per_member": "100", "extra": "100", "topped_at": "150"}, "ou_admin")
    env.ctx.config.save("quota_alert", {"enabled": True}, "ou_admin")
    alerts.usage = lambda month: {"ou_bob": 85.0}
    alerts.check(datetime(2026, 11, 15, 10, 0, tzinfo=CST))
    record = alerts.records()[0]
    assert record["card_id"] and record["level"] == "base:80"
    c = env.browser("admin@example.com")
    assert f'<a href="/reports/cards/{record["card_id"]}">查看</a>' in c.get("/reports").text
    page = c.get(f"/reports/cards/{record['card_id']}").text
    assert "鲍勃 收到的卡片" in page and "本月已达 80%" in page and 'class="fcard"' in page

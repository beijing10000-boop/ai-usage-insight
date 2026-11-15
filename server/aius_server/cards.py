"""发出去的飞书卡片：每发一张记一张（报表发送和额度提醒都记），页面上按飞书卡片的样子画出来给管理员看。

- 记的是和发出去一模一样的卡片 JSON，发送失败的也记（带失败原因）。
- 卡片内容只留 CARD_KEEP_DAYS 天，每次发送前把更早的删掉（一周几百张、一张几 KB，留太久没意义）。
- render() 只认这个平台自己用到的几种元素：markdown（**加粗**、<font color='grey'>、[文字](链接)、换行）、
  column_set（一排指标）、hr、action（按钮）、note（脚注）。文字一律先转义再处理这几种写法，卡片里的内容不能往页面里塞 HTML。
"""

from __future__ import annotations

import html
import re
import time

from markupsafe import Markup

from .db import Database, dumps, loads

CARD_KEEP_DAYS = 180
COLORS = {"blue", "violet", "indigo", "orange", "red", "carmine", "green", "turquoise", "yellow", "grey", "purple", "wathet"}


def store(db: Database, open_id: str, card: dict, status: str, error: str = "", run_id: int | None = None, alert: str = "") -> int:
    title = ((card.get("header") or {}).get("title") or {}).get("content", "")
    return db.x(
        "INSERT INTO sent_cards(run_id, alert, open_id, title, card, status, error, sent_at) VALUES(?,?,?,?,?,?,?,?)",
        (run_id, alert, open_id, title[:200], dumps(card), status, error[:300], int(time.time())),
    ).lastrowid


def purge(db: Database, keep_days: int = CARD_KEEP_DAYS) -> int:
    return db.x("DELETE FROM sent_cards WHERE sent_at < ?", (int(time.time()) - keep_days * 86400,)).rowcount


def get(db: Database, card_id: int) -> dict | None:
    row = db.one("SELECT * FROM sent_cards WHERE id=?", (card_id,))
    return {**dict(row), "card": loads(row["card"], {})} if row else None


# ---------- 画成网页 ----------
_FONT = re.compile(r"&lt;font color=(?:&#x27;|&quot;)([a-z-]+)(?:&#x27;|&quot;)&gt;(.*?)&lt;/font&gt;", re.S)
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")


def markdown(text: str) -> str:
    out = html.escape(text or "", quote=True)
    out = _FONT.sub(lambda m: f'<span class="fcard-font-{m[1]}">{m[2]}</span>', out)
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    out = _LINK.sub(r'<a href="\2" target="_blank" rel="noopener noreferrer">\1</a>', out)
    return out.replace("\n", "<br>")


def _element(el: dict) -> str:
    tag = el.get("tag")
    if tag == "markdown":
        align = " fcard-center" if el.get("text_align") == "center" else ""
        return f'<div class="fcard-md{align}">{markdown(el.get("content", ""))}</div>'
    if tag == "column_set":
        grey = " fcard-grey" if el.get("background_style") == "grey" else ""
        cols = "".join(f'<div class="fcard-col">{"".join(_element(e) for e in col.get("elements") or [])}</div>' for col in el.get("columns") or [])
        return f'<div class="fcard-cols{grey}">{cols}</div>'
    if tag == "hr":
        return '<hr class="fcard-hr" />'
    if tag == "action":
        buttons = []
        for b in el.get("actions") or []:
            text = html.escape(((b.get("text") or {}).get("content")) or "")
            url = str(b.get("url") or "")
            kind = " fcard-btn-primary" if b.get("type") == "primary" else ""
            if url.startswith(("http://", "https://")):
                buttons.append(f'<a class="fcard-btn{kind}" href="{html.escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{text}</a>')
            else:
                buttons.append(f'<span class="fcard-btn{kind}">{text}</span>')
        return f'<div class="fcard-actions">{"".join(buttons)}</div>'
    if tag == "note":
        return f'<div class="fcard-note">{"".join(html.escape(e.get("content", "")) for e in el.get("elements") or [])}</div>'
    return f'<pre class="fcard-unknown">{html.escape(dumps(el))}</pre>'


def render(card: dict) -> Markup:
    header = card.get("header") or {}
    color = header.get("template") if header.get("template") in COLORS else "blue"
    title = html.escape(((header.get("title") or {}).get("content")) or "")
    body = "".join(_element(el) for el in card.get("elements") or [])
    return Markup(f'<div class="fcard"><div class="fcard-head fcard-{color}">{title}</div><div class="fcard-body">{body}</div></div>')

"""报表发送：日报、周报、月报，各自一个开关，用应用机器人给每个人发一张飞书卡片。

- 每个人：这一期自己用了多少点、折算多少钱、和上一期比、用了几次、常用场景，以及这个月的基础额度用到了哪儿。
- 部门负责人：再加上他负责的部门（含下级）这一期的用量、活跃人数、用量最多的人和主要场景（最多 MAX_DEPTS 个部门）。
- 卡片最下面写「更多信息请查看」和平台的网址、一个「更多信息查看」按钮，最后一行是数据截止时间（按整天统计，到哪天 24:00）。
- 用平台自己的飞书应用（机器人）发送。

日报发昨天的，和上周同一天比（和前一天比会被周末带偏）；周报发上一个自然周；月报发上一个自然月。
发送用应用机器人（应用要开机器人能力和 im:message:send_as_bot 权限）。每种报表一期只自动发一次；
「测试发送给我」只发给点按钮的人自己。有同步任务在跑时（数据正在整批替换）先不发，等它跑完再发。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from .analytics import Analytics, Filter, month_name
from .db import Database
from .directory import Directory
from .feishu import CST, FeishuError
from .labels import UNKNOWN_SCENARIO
from .prefs import Config

log = logging.getLogger("aius")

KINDS = {"daily": "日报", "weekly": "周报", "monthly": "月报"}
COLORS = {"daily": "blue", "weekly": "violet", "monthly": "indigo"}  # 卡片标题栏的颜色
WEEKDAYS = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}
RANKS = ["①", "②", "③", "④", "⑤"]
TITLE = "飞书/豆包 AI 用量"  # 卡片标题和开头的叫法
SEND_GAP = 0.2  # 发消息接口限频，留出余量
MAX_DEPTS = 3  # 一张卡片里最多放几个部门；负责更多部门的，其余的在「部门运营」里看
TOP_PEOPLE = 5
TOP_SCENES = 3


def points(value: float) -> str:
    """1234.5 → 1,234.5；整数不带小数点。"""
    value = round(value or 0.0, 2)
    return f"{int(value):,}" if value == int(value) else f"{value:,.2f}".rstrip("0").rstrip(".")


def money(value: float) -> str:
    return f"¥{(value or 0.0):,.2f}"


def delta(cur: float, prev: float) -> str:
    """和上一期比：▲ 12.3% / ▼ 5% / 持平 / —（上一期没有用量，没法比）。"""
    if prev <= 0:
        return "—"
    rate = (cur - prev) / prev * 100
    if abs(rate) < 0.05:
        return "持平"
    return f"{'▲' if rate > 0 else '▼'} {abs(rate):.1f}%".replace(".0%", "%")


def short(day: date) -> str:
    return f"{day.month:02d}/{day.day:02d}"


def scene(raw: str | None) -> str:
    """场景只留前两级：「豆包工作>任务模式>xxx」→「豆包工作>任务模式」。"""
    return ">".join((raw or UNKNOWN_SCENARIO).split(">")[:2])


def _md(content: str, **extra) -> dict:
    return {"tag": "markdown", "content": content, **extra}


def _stats(items: list[tuple[str, str]]) -> dict:
    """一排指标：灰底、等宽、居中，上面是数值，下面是灰色的说明。"""
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "background_style": "grey",
        "horizontal_spacing": "default",
        "columns": [
            {"tag": "column", "width": "weighted", "weight": 1, "vertical_align": "top",
             "elements": [_md(f"**{value}**\n<font color='grey'>{label}</font>", text_align="center")]}
            for label, value in items
        ],
    }  # fmt: skip


class UsageReports:
    def __init__(self, db: Database, feishu, directory: Directory, analytics: Analytics, config: Config, public_url: str, engine=None, sleep=time.sleep):
        self.db = db
        self.feishu = feishu
        self.directory = directory
        self.analytics = analytics
        self.config = config
        self.public_url = public_url.rstrip("/")
        self.engine = engine
        self.sleep = sleep
        self._lock = threading.Lock()

    # ---------- 周期 ----------
    def period(self, kind: str, now: date | None = None) -> dict:
        """这一期是哪几天、和哪几天比，以及卡片上怎么写。now 是发送的那一天。"""
        now = now or datetime.now(CST).date()
        if kind == "daily":
            start = end = now - timedelta(days=1)
            prev_start = prev_end = start - timedelta(days=7)
            label = f"{start.month} 月 {start.day} 日（{WEEKDAYS[start.isoweekday()]}）"
            title, prev_label = f"{short(start)} {WEEKDAYS[start.isoweekday()]}", "上周同日"
        elif kind == "weekly":
            monday = now - timedelta(days=now.weekday())
            start, end = monday - timedelta(days=7), monday - timedelta(days=1)
            prev_start, prev_end = start - timedelta(days=7), start - timedelta(days=1)
            label, title, prev_label = f"{start.month} 月 {start.day} 日 - {end.month} 月 {end.day} 日", f"{short(start)} - {short(end)}", "上一周"
        elif kind == "monthly":
            end = now.replace(day=1) - timedelta(days=1)
            start = end.replace(day=1)
            prev_end = start - timedelta(days=1)
            prev_start = prev_end.replace(day=1)
            label = title = month_name(start.isoformat()[:7])
            prev_label = "上个月"
        else:
            raise ValueError(f"没有这种报表：{kind}")
        span = {"start": start.isoformat(), "end": end.isoformat()}
        return {
            "kind": kind, "name": KINDS[kind], **span, "prev_start": prev_start.isoformat(), "prev_end": prev_end.isoformat(),
            "label": label, "title": title, "prev_label": prev_label,
            # 点开网址时停在这一期：月报按自然月（还能和上个月比），其余按起止日期
            "query": {"month": span["start"][:7]} if kind == "monthly" else span,
        }  # fmt: skip

    # ---------- 数据 ----------
    def build(self, period: dict) -> dict:
        """这一期所有人、所有部门要用的数据，一次算好（每个人都单独查一遍太慢）。"""
        an = self.analytics
        cur = an.slice(Filter(period["start"], period["end"]))
        prev = an.slice(Filter(period["prev_start"], period["prev_end"]))
        span = (period["start"], period["end"])
        person_scenes: dict[str, dict[str, float]] = {}
        dept_scenes: dict[str, dict[str, float]] = {}
        for r in self.db.q(
            "SELECT entity_id, department_id, scenario, SUM(amount) AS v FROM usage_agg WHERE subject='member' AND usage_date BETWEEN ? AND ? "
            "GROUP BY entity_id, department_id, scenario",
            span,
        ):
            key = scene(r["scenario"])
            mine = person_scenes.setdefault(r["entity_id"], {})
            mine[key] = mine.get(key, 0.0) + r["v"]
            ours = dept_scenes.setdefault(r["department_id"], {})
            ours[key] = ours.get(key, 0.0) + r["v"]
        # 这个月的基础额度用到了哪儿：从 1 日算到这一期的最后一天（月报就是那个月整月）
        month_used = {}
        if self.config.monthly_allowance:
            month_start = period["end"][:8] + "01"
            month_used = {
                r[0]: r[1]
                for r in self.db.q(
                    "SELECT entity_id, SUM(amount) FROM usage_agg WHERE subject='member' AND usage_date BETWEEN ? AND ? GROUP BY entity_id",
                    (month_start, period["end"]),
                )
            }
        def by_dept(sl) -> dict[str, dict[str, float]]:
            """人员日志上记的部门 → {人 → 点数}：部门的数据从这里按部门树加起来，不用每个部门都查一遍。"""
            out: dict[str, dict[str, float]] = {}
            for r in sl.rows:
                if r["subject"] == "member":
                    people = out.setdefault(r["department_id"], {})
                    people[r["entity_id"]] = people.get(r["entity_id"], 0.0) + r["amount"]
            return out

        return {"cur": cur, "prev": prev, "person_scenes": person_scenes, "dept_scenes": dept_scenes, "month_used": month_used,
                "cur_depts": by_dept(cur), "prev_depts": by_dept(prev), "depts": {}}  # fmt: skip

    def personal(self, data: dict, open_id: str) -> dict:
        me = data["cur"].by_member.get(open_id, {"amount": 0.0, "records": 0, "days": set()})
        scenes = sorted(data["person_scenes"].get(open_id, {}).items(), key=lambda kv: -kv[1])[:TOP_SCENES]
        return {
            "amount": me["amount"], "records": me["records"], "days": len(me["days"]),
            "prev": data["prev"].by_member.get(open_id, {}).get("amount", 0.0), "scenes": scenes,
            "month_used": data["month_used"].get(open_id, 0.0),
        }  # fmt: skip

    def department(self, data: dict, dept_id: str) -> dict:
        """一个部门（含下级）这一期的用量。和「部门运营」一样，按人员日志上记的部门算。同一轮里算过的直接用。"""
        if dept_id in data["depts"]:
            return data["depts"][dept_id]
        tree = self.directory.subtree(dept_id)
        people: dict[str, float] = {}
        scenes: dict[str, float] = {}
        prev = 0.0
        for d in tree:
            for oid, v in data["cur_depts"].get(d, {}).items():
                people[oid] = people.get(oid, 0.0) + v
            prev += sum(data["prev_depts"].get(d, {}).values())
            for key, v in data["dept_scenes"].get(d, {}).items():
                scenes[key] = scenes.get(key, 0.0) + v
        out = data["depts"][dept_id] = {
            "id": dept_id, "name": self.directory.dept_name(dept_id), "amount": sum(people.values()), "prev": prev,
            "active": len(people), "headcount": self.directory.headcount(dept_id),
            "top": sorted(people.items(), key=lambda kv: -kv[1])[:TOP_PEOPLE],
            "scenes": sorted(scenes.items(), key=lambda kv: -kv[1])[:TOP_SCENES],
        }  # fmt: skip
        return out

    # ---------- 卡片 ----------
    def link(self, path: str, **params) -> str:
        return f"{self.public_url}{path}?{urlencode(params)}" if params else f"{self.public_url}{path}"

    def card(self, period: dict, open_id: str, me: dict, depts: list[dict], more_depts: int = 0) -> dict:
        price = self.config.point_price
        per = self.config.monthly_allowance
        name = self.directory.user_name(open_id) if self.directory.user(open_id) else ""
        kind, prev_label = period["kind"], period["prev_label"]
        whose = "你和你负责的部门" if depts else "你"
        end = date.fromisoformat(period["end"])
        elements: list[dict] = [_md(f"Hi，**{name or '你好'}**\n这是{whose}在 **{period['label']}** 的{TITLE}{period['name']}。")]

        # 我的用量
        elements.append(_md("**👤 我的用量**"))
        if me["amount"] > 0:
            elements.append(_stats([("消耗点数", points(me["amount"])), ("消耗金额", money(me["amount"] * price)), (f"比{prev_label}", delta(me["amount"], me["prev"]))]))
            facts = [f"使用 {me['records']:,} 次"] + ([f"有用量的天数 {me['days']} 天"] if kind != "daily" else [])
            lines = ["　·　".join(facts)]
            if me["scenes"]:
                lines.append("**常用场景**\n" + "\n".join(f"{RANKS[i]} {s} · {points(v)} 点（{v / me['amount'] * 100:.0f}%）" for i, (s, v) in enumerate(me["scenes"])))
            elements.append(_md("\n\n".join(lines)))
        else:
            elements.append(_md(f"🌙 {period['label']}没有 AI 用量记录"))
        if per:
            used = me["month_used"]
            month = month_name(period["end"][:7]) + ("" if kind == "monthly" else f"（截至 {short(date.fromisoformat(period['end']))}）")
            state = f"剩余 {points(per - used)} 点" if used < per else f"已用完，超出 {points(used - per)} 点（来自追加的额度）"
            elements.append(_md(f"📊 {month}基础额度：已用 **{points(used)} / {points(per)} 点**（{used / per * 100:.0f}%），{state}"))

        # 负责的部门
        for d in depts:
            elements.append({"tag": "hr"})
            elements.append(_md(f"**🏢 {d['name']} · 部门用量**　<font color='grey'>含下级部门</font>"))
            if d["amount"] > 0:
                elements.append(_stats([("部门点数", points(d["amount"])), ("部门金额", money(d["amount"] * price)), (f"比{prev_label}", delta(d["amount"], d["prev"]))]))
                per_capita = d["amount"] / d["active"] if d["active"] else 0.0
                lines = [f"活跃 **{d['active']}** / {d['headcount'] or '—'} 人　·　人均 {points(per_capita)} 点"]
                lines.append("**用量最多的人**\n" + "\n".join(f"{RANKS[i]} {self.directory.user_name(oid)} · {points(v)} 点" for i, (oid, v) in enumerate(d["top"])))
                if d["scenes"]:
                    lines.append("**主要场景**\n" + "\n".join(f"· {s} · {points(v)} 点" for s, v in d["scenes"]))
                elements.append(_md("\n\n".join(lines)))
            else:
                elements.append(_md(f"🌙 {period['label']}部门里没有 AI 用量记录"))
            url = self.link("/dept-ops", dept=d["id"], **period["query"])
            elements.append(_md(f"部门详情请查看：[部门运营 · {d['name']}]({url})"))
        if more_depts:
            elements.append(_md(f"你还负责 {more_depts} 个部门，在「部门运营」里切换部门查看。"))

        # 最下面：平台网址 + 一个「更多信息查看」按钮 + 数据截止时间。报表按整天统计（日报是昨天全天），不是发卡片那一刻的实时数据
        elements.append({"tag": "hr"})
        elements.append(_md(f"**更多信息请查看：**[{self.public_url}]({self.public_url})"))
        button = {"tag": "button", "type": "primary", "text": {"tag": "plain_text", "content": "更多信息查看"}, "url": self.public_url}
        elements.append({"tag": "action", "actions": [button]})
        note = f"数据截止时间：{end.year} 年 {end.month} 月 {end.day} 日 24:00（按整天统计）· 按 {price:g} 元/点折算 · 在公司网络里用飞书登录后查看"
        elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": note}]})
        return {
            "config": {"wide_screen_mode": True},
            "header": {"template": COLORS[kind], "title": {"tag": "plain_text", "content": f"{TITLE}{period['name']} ｜ {period['title']}"}},
            "elements": elements,
        }

    # ---------- 发送 ----------
    def targets(self, data: dict, leaders: dict[str, list[str]]) -> list[str]:
        """按发送范围定收件人。只发给通讯录里的人（离职的人收不到）。"""
        if self.config.get("report")["scope"] == "all":
            people = {u.open_id for u in self.directory.users()}
        else:
            people = set(data["cur"].by_member)
            for open_id, depts in leaders.items():  # 负责人：自己没用、但部门里有人用了，也要发
                if open_id not in people and any(self.department(data, d)["amount"] > 0 for d in depts):
                    people.add(open_id)
        return sorted(oid for oid in people if self.directory.user(oid))

    def push(self, kind: str, trigger: str, actor: str = "system", only: list[str] | None = None, now: date | None = None) -> dict:
        """发一期报表。only 给了就只发给这些人（测试发送）；否则按发送范围发。返回这一轮的记录。"""
        with self._lock:
            period = self.period(kind, now)
            data = self.build(period)
            leaders = self.directory.leaders()
            targets = list(only) if only is not None else self.targets(data, leaders)
            run_id = self.db.x(
                "INSERT INTO report_runs(kind, week_start, week_end, trigger, actor, status, total, started_at) VALUES(?,?,?,?,?,'running',?,?)",
                (kind, period["start"], period["end"], trigger, actor, len(targets), int(time.time())),
            ).lastrowid
            sent, errors = 0, []
            for i, open_id in enumerate(targets):
                led = leaders.get(open_id, [])
                depts = [self.department(data, d) for d in led[:MAX_DEPTS]]
                try:
                    self.feishu.send_card(open_id, self.card(period, open_id, self.personal(data, open_id), depts, max(0, len(led) - MAX_DEPTS)))
                    sent += 1
                except FeishuError as e:
                    errors.append(f"{self.directory.user_name(open_id)}：{e}")
                    if len(errors) >= 5 and sent == 0:  # 一开始就连着失败：多半是没开机器人或没权限，别再一个个试了
                        errors.append("连续失败，已停止这一轮发送")
                        break
                if i + 1 < len(targets):
                    self.sleep(SEND_GAP)
            failed = len(targets) - sent
            status = "success" if not failed else ("partial" if sent else "failed")
            self.db.x(
                "UPDATE report_runs SET status=?, sent=?, failed=?, error=?, finished_at=? WHERE id=?",
                (status, sent, failed, "\n".join(errors[:6]), int(time.time()), run_id),
            )
            log.info("%s发送 #%s（%s）：%s，共 %d 人，成功 %d，失败 %d", KINDS[kind], run_id, trigger, status, len(targets), sent, failed)
            return self._view(self.db.one("SELECT * FROM report_runs WHERE id=?", (run_id,)))

    # ---------- 定时 ----------
    def due(self, kind: str, now: datetime) -> bool:
        """到点了没有：开着、过了设定的整点、这一期还没发过；周报还要是设定的周几，月报是设定的几号之后（错过了当月补发）。
        有同步任务在跑时先不发：数据正在整批替换，等它跑完。"""
        cfg = self.config.get(f"report_{kind}")
        if not cfg["enabled"] or now.hour < cfg["hour"]:
            return False
        today, last = now.date().isoformat(), cfg["last_push_date"]
        if kind == "daily":
            fresh = last != today
        elif kind == "weekly":
            fresh = now.isoweekday() == cfg["weekday"] and last != today
        else:
            fresh = now.day >= cfg["day"] and last[:7] != today[:7]
        return fresh and not (self.engine and self.engine.active())

    def tick(self, now: datetime) -> list[str]:
        """调度每分钟调一次：到点的报表在后台线程里依次发（同一时刻到点的几种报表不会同时发）。"""
        kinds = [k for k in KINDS if self.due(k, now)]
        for k in kinds:  # 先记下来：发送很慢，中途重启也不要再发一轮
            self.config.mark(f"report_{k}", "last_push_date", now.date().isoformat())
        if kinds:
            threading.Thread(target=self._push_all, args=(kinds, now.date()), name="usage-reports", daemon=True).start()
        return kinds

    def _push_all(self, kinds: list[str], day: date) -> None:
        for k in kinds:
            try:
                self.push(k, "schedule", now=day)
            except Exception:
                log.exception("%s定时发送出错", KINDS[k])

    # ---------- 记录 ----------
    def _view(self, row) -> dict:
        out = dict(row)
        out["kind_name"] = KINDS.get(row["kind"], row["kind"])
        out["actor_name"] = "定时任务" if row["actor"] in ("", "system") else self.directory.user_name(row["actor"])
        return out

    def runs(self, limit: int = 30) -> list[dict]:
        return [self._view(r) for r in self.db.q("SELECT * FROM report_runs ORDER BY id DESC LIMIT ?", (limit,))]

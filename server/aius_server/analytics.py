"""用量分析：驾驶舱、部门运营、个人用量、豆包运营页、OpenAPI、MCP、洞察助手、周报都从这里取数。

口径：
- 总用量 = 人员用量日志 + 应用用量日志的消耗点数之和。
- 限定了部门、某个人或豆包工作时，只算人员用量日志（应用不属于部门）。
- 部门用量按部门树向上汇总：一个部门的用量 = 它自己和所有下级部门成员的用量。
- 环比 = 和紧挨着的上一个同样长度的时间段相比；按自然月看的时候（从某月 1 日开始、不跨月），和上个月的同一段相比：
  本月 1–10 日比上月 1–10 日，整月比整月。飞书的 AI 额度按自然月发放、每月 1 日重置，月和月比才有意义。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from functools import cached_property

from . import labels
from .db import Database, loads, marks
from .directory import Directory
from .feishu import CST, ROOT
from .prefs import Config
from .sync import LIVE_CURSOR, today

DIRECT = "__direct__"  # 部门下钻时「直属成员」这一行
MIN_GROUP = 3  # 普通用户看「所在部门」的汇总：除了自己，至少要有这么多同事用过 AI 才显示点数


def pct(part: float, whole: float) -> float:
    return round(part / whole * 100, 1) if whole else 0.0


def change(cur: float, prev: float) -> float | None:
    """环比百分比；上一个周期没有用量时没法算，返回 None。"""
    return round((cur - prev) / prev * 100, 1) if prev else None


def r2(value: float) -> float:
    return round(value or 0, 2)


def month_end(d: date) -> date:
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def month_aligned(start: str, end: str) -> bool:
    """从某月 1 日开始、不跨月的范围（整月，或者月初到某一天）。"""
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        return False
    return first.day == 1 and first <= last and (first.year, first.month) == (last.year, last.month)


def month_name(month: str) -> str:
    """2026-11 -> 2026 年 11 月"""
    return f"{month[:4]} 年 {int(month[5:7])} 月"


@dataclass(frozen=True)
class Filter:
    start: str
    end: str
    depts: frozenset[str] | None = None  # 只看这些部门（人员日志上的部门）
    member: str | None = None  # 只看这个人
    doubao: bool = False  # 只看豆包工作
    monthly: bool = False  # 按自然月看：环比和上个月的同一段比（只在范围从 1 日开始、不跨月时生效）

    @property
    def days(self) -> int:
        return (date.fromisoformat(self.end) - date.fromisoformat(self.start)).days + 1

    @property
    def members_only(self) -> bool:
        return self.depts is not None or self.member is not None or self.doubao

    @property
    def by_month(self) -> bool:
        return self.monthly and month_aligned(self.start, self.end)

    @property
    def full_month(self) -> bool:
        return self.by_month and date.fromisoformat(self.end) == month_end(date.fromisoformat(self.end))

    @property
    def prev_label(self) -> str:
        """环比和谁比，页面上写成「比上个月」「比上月同期」「比上一个周期」。"""
        if not self.by_month:
            return "上一个周期"
        return "上个月" if self.full_month else "上月同期"

    def previous(self) -> "Filter":
        first, last = date.fromisoformat(self.start), date.fromisoformat(self.end)
        if self.by_month:
            # 上个月的同一段：1 日到同一天（上个月没有这一天就到月底）；整月就是上个月整月
            prev_last = first - timedelta(days=1)
            prev_first = prev_last.replace(day=1)
            end = prev_last if self.full_month else min(prev_last, prev_first + timedelta(days=last.day - 1))
            return replace(self, start=prev_first.isoformat(), end=end.isoformat())
        return replace(self, start=(first - timedelta(days=self.days)).isoformat(), end=(first - timedelta(days=1)).isoformat())

    def dates(self) -> list[str]:
        first = date.fromisoformat(self.start)
        return [(first + timedelta(days=i)).isoformat() for i in range(self.days)]

    def where(self) -> tuple[str, list]:
        sql, params = ["usage_date BETWEEN ? AND ?"], [self.start, self.end]
        if self.members_only:
            sql.append("subject='member'")
        if self.member:
            sql.append("entity_id=?")
            params.append(self.member)
        if self.depts is not None:
            if self.depts:
                sql.append(f"department_id IN ({marks(len(self.depts))})")
                params.extend(sorted(self.depts))
            else:
                sql.append("0")
        if self.doubao:
            sql.append(labels.DOUBAO_SQL)
        return " AND ".join(sql), params


class Slice:
    """一个筛选范围内的用量，先按「日期 × 主体 × 对象 × 部门」汇总一次，各种指标都从这份汇总里算。"""

    def __init__(self, db: Database, f: Filter):
        self.f = f
        where, params = f.where()
        self.rows = db.q(
            f"SELECT usage_date, subject, entity_id, department_id, SUM(amount) AS amount, SUM(n) AS n "
            f"FROM usage_agg WHERE {where} GROUP BY usage_date, subject, entity_id, department_id",
            params,
        )
        self.member_total = sum(r["amount"] for r in self.rows if r["subject"] == "member")
        self.app_total = sum(r["amount"] for r in self.rows if r["subject"] == "app")
        self.total = self.member_total + self.app_total
        self.records = sum(r["n"] for r in self.rows)

    @cached_property
    def by_day(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for r in self.rows:
            day = out.setdefault(r["usage_date"], {"member": 0.0, "app": 0.0, "users": set()})
            day[r["subject"]] += r["amount"]
            if r["subject"] == "member":
                day["users"].add(r["entity_id"])
        return out

    @cached_property
    def by_member(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for r in self.rows:
            if r["subject"] != "member":
                continue
            m = out.setdefault(r["entity_id"], {"amount": 0.0, "records": 0, "days": set(), "dept": "", "last": ""})
            m["amount"] += r["amount"]
            m["records"] += r["n"]
            m["days"].add(r["usage_date"])
            if r["usage_date"] >= m["last"]:  # 调过部门的人，按最近一次使用时所在的部门显示
                m["last"], m["dept"] = r["usage_date"], r["department_id"]
        return out

    @cached_property
    def by_leaf(self) -> dict[str, dict]:
        """按人员日志上的部门（不向上汇总）。"""
        out: dict[str, dict] = {}
        for r in self.rows:
            if r["subject"] != "member":
                continue
            d = out.setdefault(r["department_id"], {"amount": 0.0, "users": set()})
            d["amount"] += r["amount"]
            d["users"].add(r["entity_id"])
        return out

    @cached_property
    def by_app(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for r in self.rows:
            if r["subject"] != "app":
                continue
            a = out.setdefault(r["entity_id"], {"amount": 0.0, "records": 0})
            a["amount"] += r["amount"]
            a["records"] += r["n"]
        return out

    @property
    def active_members(self) -> int:
        return len(self.by_member)

    @property
    def active_depts(self) -> int:
        return sum(1 for d in self.by_leaf if d)


class Analytics:
    def __init__(self, db: Database, directory: Directory, config: Config):
        self.db = db
        self.directory = directory
        self.config = config

    # ---------- 日期范围 ----------
    def latest_date(self) -> str | None:
        return self.db.val("SELECT MAX(usage_date) FROM usage_agg")

    def earliest_date(self) -> str | None:
        return self.db.val("SELECT MIN(usage_date) FROM usage_agg")

    def live_clock(self, day: str | None) -> str:
        """当天增量同步拉到了 day 这一天的几点（"15:20"）；没拉到这一天就是空串。"""
        until = int(loads(self.db.meta(LIVE_CURSOR), {}).get("until", 0))
        if not day or not until:
            return ""
        at = datetime.fromtimestamp(until, CST)
        return at.strftime("%H:%M") if at.date().isoformat() == day else ""

    def latest_label(self) -> str | None:
        """侧栏的「数据截至」：最近一个有数据的日子；当天增量同步拉到了这一天，再带上几点。"""
        latest = self.latest_date()
        clock = self.live_clock(latest)
        return f"{latest} {clock}" if clock else latest

    def default_range(self, days: int = 30) -> tuple[str, str]:
        """默认看到最近一个有数据的日子为止的 days 天；还没同步过就到昨天。"""
        last = date.fromisoformat(self.latest_date() or (today() - timedelta(days=1)).isoformat())
        return (last - timedelta(days=days - 1)).isoformat(), last.isoformat()

    def slice(self, f: Filter) -> Slice:
        return Slice(self.db, f)

    def previous(self, f: Filter) -> Slice | None:
        """上一个周期（同样长度的上一段；按自然月看时是上个月的同一段），用来算环比。上一个周期里只要有一天没同步成功过（还没同步到那么早、中间服务停过、
        那天同步失败了），就返回 None：拿残缺的数据去比，会算出吓人的「增长」。
        「同步成功过」看执行记录里那天的人员用量日志——不能看那天有没有数据，周末本来就可能没人用。"""
        before = f.previous()
        synced = self.db.val(
            "SELECT COUNT(DISTINCT usage_date) FROM sync_steps WHERE dataset='log_member' AND status IN ('ok','kept') AND usage_date BETWEEN ? AND ?",
            (before.start, before.end),
            0,
        )
        return self.slice(before) if synced >= before.days else None

    def _headcount(self, f: Filter) -> int | None:
        """筛选范围里的在职人数：全企业、某些部门（含下级，已经展开在 f.depts 里），个人范围没有这个概念。"""
        if f.member:
            return None
        if f.depts is None:
            return self.directory.total_headcount()
        return sum(1 for u in self.directory.users() if f.depts.intersection(u.dept_ids))

    # ---------- 名称 ----------
    def app_names(self) -> dict[str, str]:
        return {r["app_id"]: r["name"] for r in self.db.q("SELECT app_id, name FROM app_mapping")}

    # ---------- 核心指标 ----------
    def kpis(self, cur: Slice, prev: Slice | None = None) -> dict:
        f = cur.f
        price = self.config.point_price
        last_day = cur.by_day.get(f.end, {"member": 0.0, "app": 0.0, "users": set()})
        top10 = sum(sorted((m["amount"] for m in cur.by_member.values()), reverse=True)[:10])
        out = {
            "start": f.start,
            "end": f.end,
            "days": f.days,
            "total": r2(cur.total),
            "member_total": r2(cur.member_total),
            "app_total": r2(cur.app_total),
            "records": cur.records,
            "cost": r2(cur.total * price),
            "point_price": price,
            "last_day": r2(last_day["member"] + last_day["app"]),
            "last_day_users": len(last_day["users"]),
            "active_members": cur.active_members,
            "active_depts": cur.active_depts,
            "active_apps": len(cur.by_app),
            "daily_avg": r2(cur.total / f.days),
            "per_capita": r2(cur.member_total / cur.active_members) if cur.active_members else 0.0,
            "avg_dau": round(sum(len(d["users"]) for d in cur.by_day.values()) / f.days, 1),
            "top10_share": pct(top10, cur.member_total),
            "headcount": self._headcount(f),
        }
        out["coverage"] = min(100.0, pct(out["active_members"], out["headcount"])) if out["headcount"] else None
        out.update(self.compared(f, prev, cur.total, prev.total if prev else 0.0))
        return out

    @staticmethod
    def compared(f: Filter, prev: Slice | None, now: float, before: float) -> dict:
        """环比那几项：和谁比（上一个周期 / 上个月 / 上月同期）、比的是哪几天、能不能比、变化多少。"""
        p = f.previous()
        return {
            "prev_label": f.prev_label,
            "prev_start": p.start,
            "prev_end": p.end,
            "comparable": prev is not None,
            "prev_total": r2(before) if prev else None,
            "change": change(now, before) if prev else None,
        }

    def daily(self, cur: Slice) -> list[dict]:
        out = []
        for day in cur.f.dates():
            d = cur.by_day.get(day, {"member": 0.0, "app": 0.0, "users": set()})
            users = len(d["users"])
            out.append(
                {
                    "date": day,
                    "label": f"{int(day[5:7])}/{int(day[8:10])}",
                    "total": r2(d["member"] + d["app"]),
                    "member": r2(d["member"]),
                    "app": r2(d["app"]),
                    "users": users,
                    "per_capita": r2(d["member"] / users) if users else 0.0,
                }
            )
        return out

    def periods(self, cur: Slice, granularity: str) -> list[dict]:
        """按日 / 周（周一到周日）/ 自然月汇总的趋势。活跃人数是这一周 / 这个月里去重后的人数，
        所以按月看时的人均用量就是「每个用过 AI 的人这个月用了多少」，可以直接和每人每月的额度对照。
        范围两头不完整的周 / 月照样画，提示里写明只算了哪几天；没有用量的周 / 月是 0。"""
        if granularity not in ("week", "month"):
            return self.daily(cur)
        week = granularity == "week"
        first, last = date.fromisoformat(cur.f.start), date.fromisoformat(cur.f.end)

        def head(d: date) -> date:
            return d - timedelta(days=d.weekday()) if week else d.replace(day=1)

        def tail(d: date) -> date:
            return d + timedelta(days=6) if week else month_end(d)

        buckets: dict[date, dict] = {}
        b = head(first)
        while b <= last:
            buckets[b] = {"member": 0.0, "app": 0.0, "users": set()}
            b = tail(b) + timedelta(days=1)
        for r in cur.rows:
            bucket = buckets[head(date.fromisoformat(r["usage_date"]))]
            bucket[r["subject"]] += r["amount"]
            if r["subject"] == "member":
                bucket["users"].add(r["entity_id"])
        across_years = first.year != last.year
        out = []
        for b, v in buckets.items():
            lo, hi = max(b, first), min(tail(b), last)
            partial = (lo, hi) != (b, tail(b))
            if week:
                label, title = f"{b.month}/{b.day} 周", f"{lo.isoformat()} ~ {hi.isoformat()}"
            else:
                label = f"{b.year}-{b.month:02d}" if across_years else f"{b.month} 月"
                title = month_name(b.isoformat()) + (f"（只算了 {lo.month}/{lo.day} ~ {hi.month}/{hi.day}）" if partial else "")
            users = len(v["users"])
            out.append({"date": b.isoformat() if week else b.isoformat()[:7], "label": label, "title": title, "start": lo.isoformat(), "end": hi.isoformat(),
                        "days": (hi - lo).days + 1, "partial": partial, "total": r2(v["member"] + v["app"]), "member": r2(v["member"]), "app": r2(v["app"]),
                        "users": users, "per_capita": r2(v["member"] / users) if users else 0.0})  # fmt: skip
        return out

    # ---------- 排行 ----------
    def rank_members(self, cur: Slice, prev: Slice | None = None, limit: int | None = 20) -> list[dict]:
        ranked = sorted(cur.by_member.items(), key=lambda kv: -kv[1]["amount"])
        out = []
        for open_id, m in ranked[:limit] if limit else ranked:
            before = prev.by_member.get(open_id, {}).get("amount", 0.0) if prev else 0.0
            out.append(
                {
                    "id": open_id,
                    "name": self.directory.user_name(open_id),
                    "dept_id": m["dept"],
                    "dept": self.directory.dept_name(m["dept"]),
                    "dept_path": self.directory.dept_path(m["dept"]),
                    "amount": r2(m["amount"]),
                    "share": pct(m["amount"], cur.member_total),
                    "records": m["records"],
                    "active_days": len(m["days"]),
                    "last_used": m["last"],
                    "per_record": r2(m["amount"] / m["records"]) if m["records"] else 0.0,
                    "change": change(m["amount"], before) if prev else None,
                }
            )
        return out

    def rank_apps(self, cur: Slice, prev: Slice | None = None, limit: int | None = 20) -> list[dict]:
        if not cur.by_app:
            return []
        # 名称来自映射表：手工 / Aily / 开放平台，以及同步时接口自己带的名称（_resolve_names 存进映射表）。
        # 不去翻原始日志——那张表一天几万行，按应用分组要扫整段日期
        names = self.app_names()
        ranked = sorted(cur.by_app.items(), key=lambda kv: -kv[1]["amount"])
        picked = ranked[:limit] if limit else ranked
        ids = [app_id for app_id, _a in picked]
        types = {
            r["entity_id"]: r["t"]
            for r in self.db.q(
                f"SELECT entity_id, MAX(entity_type) AS t FROM usage_agg WHERE subject='app' AND entity_id IN ({marks(len(ids))}) "
                "AND usage_date BETWEEN ? AND ? GROUP BY entity_id",
                (*ids, cur.f.start, cur.f.end),
            )
        }
        out = []
        for app_id, a in picked:
            before = prev.by_app.get(app_id, {}).get("amount", 0.0) if prev else 0.0
            name = names.get(app_id, "")
            out.append(
                {
                    "id": app_id,
                    "name": name or app_id,
                    "named": bool(name),
                    "type": labels.entity_type(types.get(app_id)),
                    "amount": r2(a["amount"]),
                    "share": pct(a["amount"], cur.app_total),
                    "records": a["records"],
                    "change": change(a["amount"], before) if prev else None,
                }
            )
        return out

    def _group_of(self, leaf: str, parent: str) -> str | None:
        """人员日志上的部门 leaf 归到 parent 下面的哪一行：某个子部门、直属成员，或者不在 parent 之下（None）。"""
        if parent == ROOT:
            return self.directory.top_level(leaf) if leaf else ""
        if leaf == parent:
            return DIRECT
        chain = self.directory.chain(leaf)
        return chain[chain.index(parent) - 1] if parent in chain else None

    def rank_depts(self, cur: Slice, prev: Slice | None = None, parent: str = ROOT) -> list[dict]:
        """parent 的直接下级部门各自的用量（含各自的下级）。parent 是根时就是一级部门汇总。"""

        def grouped(s: Slice) -> dict[str, dict]:
            out: dict[str, dict] = {}
            for leaf, d in s.by_leaf.items():
                key = self._group_of(leaf, parent)
                if key is None:
                    continue
                g = out.setdefault(key, {"amount": 0.0, "users": set()})
                g["amount"] += d["amount"]
                g["users"] |= d["users"]
            return out

        now, before = grouped(cur), grouped(prev) if prev else {}
        total = sum(g["amount"] for g in now.values())
        out = []
        for key, g in sorted(now.items(), key=lambda kv: -kv[1]["amount"]):
            if key == DIRECT:
                name, path, headcount, drill = "直属成员", self.directory.dept_path(parent), 0, False
            else:
                name, path = self.directory.dept_name(key), self.directory.dept_path(key)
                headcount, drill = self.directory.headcount(key), bool(key and self.directory.dept(key))
            users = len(g["users"])
            out.append(
                {
                    "id": key,
                    "name": name,
                    "path": path,
                    "amount": r2(g["amount"]),
                    "share": pct(g["amount"], total),
                    "users": users,
                    "headcount": headcount,
                    "coverage": pct(users, headcount) if headcount else None,
                    "per_capita": r2(g["amount"] / users) if users else 0.0,
                    "change": change(g["amount"], before.get(key, {}).get("amount", 0.0)) if prev else None,
                    "drill": drill,
                }
            )
        return out

    def rollup(self, cur: Slice, dept_ids: list[str]) -> dict:
        """部门圈选：把选中的几个部门（各自含下级）合在一起看。
        每一行是那个部门自己的完整用量（含下级），和加入的先后无关；合计按选中部门的并集算——
        同时选了上级和它的下级时，下级不重复计入合计，同一个人也只算一个人。"""
        rows, union = [], set()
        for dept_id in dept_ids:
            tree = self.directory.subtree(dept_id)
            union |= tree
            amount = sum(cur.by_leaf[d]["amount"] for d in tree if d in cur.by_leaf)
            members = set().union(*(cur.by_leaf[d]["users"] for d in tree if d in cur.by_leaf))
            rows.append({"id": dept_id, "name": self.directory.dept_name(dept_id), "path": self.directory.dept_path(dept_id),
                         "amount": r2(amount), "users": len(members), "per_capita": r2(amount / len(members)) if members else 0.0})  # fmt: skip
        total = sum(cur.by_leaf[d]["amount"] for d in union if d in cur.by_leaf)
        users = set().union(*(cur.by_leaf[d]["users"] for d in union if d in cur.by_leaf))
        for row in rows:
            row["share"] = pct(row["amount"], total)
        return {"rows": rows, "total": r2(total), "users": len(users), "per_capita": r2(total / len(users)) if users else 0.0,
                "share_of_all": pct(total, cur.member_total), "cost": r2(total * self.config.point_price)}  # fmt: skip

    # ---------- 场景 / 额度类型 / 时段 ----------
    def scenarios(self, f: Filter, level: int = 1, limit: int | None = 12) -> list[dict]:
        """level=1 按一级场景（产品）汇总；level=0 按完整的场景路径。"""
        where, params = f.where()
        groups: dict[str, dict] = {}
        for r in self.db.q(
            f"SELECT scenario, subject, entity_id, SUM(amount) AS amount, SUM(n) AS n FROM usage_agg WHERE {where} GROUP BY scenario, subject, entity_id",
            params,
        ):
            name = labels.top_scenario(r["scenario"]) if level == 1 else (r["scenario"] or labels.UNKNOWN_SCENARIO)
            g = groups.setdefault(name, {"amount": 0.0, "records": 0, "users": set()})
            g["amount"] += r["amount"]
            g["records"] += r["n"]
            if r["subject"] == "member":  # 「使用人数」只数人，应用不算
                g["users"].add(r["entity_id"])
        total = sum(g["amount"] for g in groups.values())
        ranked = sorted(groups.items(), key=lambda kv: -kv[1]["amount"])
        return [
            {"name": name, "amount": r2(g["amount"]), "share": pct(g["amount"], total), "records": g["records"], "users": len(g["users"])}
            for name, g in (ranked[:limit] if limit else ranked)
        ]

    def split(self, f: Filter, column: str) -> list[dict]:
        """按额度类型（feature_key）或付费 / 免费（usage_type）拆分。"""
        assert column in ("feature_key", "usage_type")
        where, params = f.where()
        rows = self.db.q(f"SELECT {column} AS k, SUM(amount) AS amount, SUM(n) AS n FROM usage_agg WHERE {where} GROUP BY {column}", params)
        total = sum(r["amount"] for r in rows)
        name = labels.feature_key if column == "feature_key" else labels.usage_type
        return sorted(
            ({"key": r["k"], "name": name(r["k"]), "amount": r2(r["amount"]), "share": pct(r["amount"], total), "records": r["n"]} for r in rows),
            key=lambda x: -x["amount"],
        )

    def hourly(self, f: Filter) -> list[dict]:
        """一天 24 小时里各个小时的用量（北京时间）。"""
        where, params = f.where()
        got = {
            r["h"]: r
            for r in self.db.q(
                f"SELECT hour AS h, SUM(amount) AS amount, SUM(n) AS n FROM usage_agg WHERE {where} AND hour IS NOT NULL GROUP BY h",
                params,
            )
        }
        return [{"hour": h, "label": f"{h} 时", "total": r2(got[h]["amount"]) if h in got else 0.0, "records": got[h]["n"] if h in got else 0} for h in range(24)]

    # ---------- 异常提醒 ----------
    def anomalies(self, cur: Slice, prev: Slice | None, parent: str = ROOT, by_dept: bool = True) -> list[dict]:
        """异常提醒。parent：在看哪个部门（根 = 全企业），「部门快速增长」比的是它的直接下级部门；
        by_dept=False 时不看部门（数据范围是好几个互不相干的部门时，没有一个合适的上级可以拆）。"""
        out: list[dict] = []
        days = [(d["date"], d["total"]) for d in self.daily(cur)]
        avg = cur.total / len(days) if days else 0
        peak = max(days, key=lambda x: x[1], default=None)
        if peak and len(days) >= 3 and avg > 0 and peak[1] > avg * 2:
            out.append({"type": "spike", "level": "high", "title": "单日使用突增", "dimension": "趋势",
                        "text": f"{peak[0]} 用了 {peak[1]:,.0f} 点，超过这段时间日均（{avg:,.0f} 点）的 2 倍。",
                        "link": f"/data?start={peak[0]}&end={peak[0]}"})  # fmt: skip
        for row in self.rank_depts(cur, prev, parent) if by_dept else []:
            if row["id"] != DIRECT and row["change"] is not None and row["change"] > 50 and row["amount"] > 0:
                out.append({"type": "growth", "level": "medium", "title": "部门使用快速增长", "dimension": "部门",
                            "text": f"{row['name']} 环比增长 {row['change']}%（{row['amount']:,.0f} 点），建议看看是哪些人、哪些场景带来的。",
                            "link": f"/dept-ops?dept={row['id']}" if row["drill"] else "/dashboard#depts"})  # fmt: skip
                break
        for row in self.rank_apps(cur, prev):
            if row["change"] is not None and row["change"] > 50 and row["amount"] > 0:
                out.append({"type": "growth", "level": "medium", "title": "应用使用快速增长", "dimension": "应用",
                            "text": f"{row['name']} 环比增长 {row['change']}%（{row['amount']:,.0f} 点），可能正在形成新的用量来源。",
                            "link": "/dashboard#apps"})  # fmt: skip
                break
        members = self.rank_members(cur, None, 50)
        if members and members[0]["share"] >= 25 and cur.active_members >= 4:
            top = members[0]
            out.append({"type": "concentration", "level": "high", "title": "人员使用集中", "dimension": "人员",
                        "text": f"{top['name']} 一个人占了人员用量的 {top['share']}%（{top['amount']:,.0f} 点），建议核对他的高用量场景。",
                        "link": f"/data?member={top['id']}"})  # fmt: skip
        member_records = sum(m["records"] for m in cur.by_member.values())
        overall = cur.member_total / member_records if member_records else 0
        heavy = next((m for m in members if m["records"] >= 5 and overall > 0 and m["per_record"] >= overall * 3), None)
        if heavy:
            out.append({"type": "heavy", "level": "low", "title": "单次用量偏高", "dimension": "人员",
                        "text": f"{heavy['name']} 平均每次使用 {heavy['per_record']:,.1f} 点，是整体平均（{overall:,.1f} 点）的 {heavy['per_record'] / overall:.1f} 倍。",
                        "link": f"/data?member={heavy['id']}"})  # fmt: skip
        return out

    def narrative(self, k: dict, depts: list[dict], apps: list[dict], scenes: list[dict], anomalies: list[dict]) -> str:
        """「本期解读」：一句话说清用了多少、涨了还是跌了、主要来自哪里、有没有异常。"""
        if not k["total"]:
            return "这段时间还没有用量数据。完成一次同步之后，这里会给出用量分布、增长来源和异常提醒。"
        prev = k.get("prev_label", "上一个周期")
        trend = f"{prev}没有完整的数据可比" if k["change"] is None else f"比{prev}{'增长' if k['change'] >= 0 else '下降'} {abs(k['change'])}%"
        depts = [d for d in depts if d["id"] != DIRECT]  # 「直属成员」不是一个部门，不能说「用量最高的部门是直属成员」
        sources = [f"{kind}是「{rows[0]['name']}」" for kind, rows in (("部门", depts), ("应用", apps), ("场景", scenes)) if rows]
        where = f"；用量最高的{'，'.join(sources)}" if sources else ""
        alert = f"发现 {len(anomalies)} 条异常，建议先看「{anomalies[0]['title']}」。" if anomalies else "没有发现明显的异常。"
        return f"这段时间一共用了 {k['total']:,.0f} 点（折算 ¥{k['cost']:,.2f}），{trend}{where}。{alert}"

    # ---------- 用量明细 ----------
    def logs(
        self, f: Filter, subject: str = "", query: str = "", feature_key: int | None = None, usage_type: int | None = None,
        limit: int = 50, offset: int = 0,
    ) -> tuple[list[dict], int, float]:  # fmt: skip
        """用量日志明细，返回 (这一页, 总条数, 总点数)。query 匹配场景、人名或应用名。"""
        where, params = f.where()
        if subject in ("member", "app") and not f.members_only:
            where += " AND subject=?"
            params.append(subject)
        if feature_key:
            where += " AND feature_key=?"
            params.append(feature_key)
        if usage_type:
            where += " AND usage_type=?"
            params.append(usage_type)
        names = self.app_names()
        if query.strip():
            q = query.strip()
            ids = [u.open_id for u in self.directory.search_users(q, 50)] + [k for k, v in names.items() if q.casefold() in v.casefold()]
            ids = ids[:200]
            among = f" OR entity_id IN ({marks(len(ids))})" if ids else ""
            where += " AND (scenario LIKE ? ESCAPE '\\' OR entity_id=?" + among + ")"
            params += ["%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%", q, *ids]
        # 条数和合计从汇总表算（筛选条件用到的列汇总表里都有），只有这一页的记录去翻原始日志
        total, amount = self.db.one(f"SELECT COALESCE(SUM(n), 0), COALESCE(SUM(amount), 0) FROM usage_agg WHERE {where}", params)
        rows = self.db.q(
            f"SELECT * FROM usage_logs WHERE {where} ORDER BY usage_date DESC, ts DESC, id DESC LIMIT ? OFFSET ?", [*params, limit, offset]
        )
        price = self.config.point_price
        out = []
        for r in rows:
            member = r["subject"] == "member"
            out.append(
                {
                    "date": r["usage_date"],
                    "ts": r["ts"],
                    "subject": r["subject"],
                    "entity_id": r["entity_id"],
                    "name": self.directory.user_name(r["entity_id"]) if member else (names.get(r["entity_id"]) or r["entity_name"] or r["entity_id"]),
                    "entity_type": labels.entity_type(r["entity_type"]),
                    "dept_id": r["department_id"],
                    "dept": self.directory.dept_path(r["department_id"]) if member else "",
                    "scenario": r["scenario"] or labels.UNKNOWN_SCENARIO,
                    "feature": labels.feature_key(r["feature_key"]),
                    "usage_type": labels.usage_type(r["usage_type"]),
                    "amount": r2(r["used_quota"]),
                    "cost": r2(r["used_quota"] * price),
                    "notes": [n for n in loads(r["notes"], []) if isinstance(n, dict)],
                }
            )
        return out, total, r2(amount)

    def details(
        self, start: str, end: str, subject: str = "", member: str | None = None, limit: int = 50, offset: int = 0
    ) -> tuple[list[dict], int, dict]:  # fmt: skip
        """统计详情（飞书按天给出的各额度类型用量汇总，T+1）。返回 (这一页, 总条数, 合计)：
        合计是整个筛选范围（不只是这一页）各额度类型的点数 quotas 和总点数 total。"""
        where, params = "usage_date BETWEEN ? AND ?", [start, end]
        if member:
            where += " AND subject='member' AND entity_id=?"
            params.append(member)
        elif subject in ("member", "app"):
            where += " AND subject=?"
            params.append(subject)
        got = self.db.one(f"SELECT COUNT(*), {', '.join(f'COALESCE(SUM(q{i}), 0)' for i in range(1, 7))} FROM usage_details WHERE {where}", params)
        total, sums = got[0], [r2(v) for v in tuple(got)[1:]]
        names = self.app_names()
        out = []
        for r in self.db.q(
            f"SELECT * FROM usage_details WHERE {where} ORDER BY usage_date DESC, (q1+q2+q3+q4+q5+q6) DESC, id LIMIT ? OFFSET ?", [*params, limit, offset]
        ):
            quotas = [r2(r[f"q{i}"]) for i in range(1, 7)]
            out.append(
                {
                    "date": r["usage_date"],
                    "subject": r["subject"],
                    "entity_id": r["entity_id"],
                    "name": self.directory.user_name(r["entity_id"]) if r["subject"] == "member" else (names.get(r["entity_id"]) or r["entity_id"]),
                    "entity_type": labels.entity_type(r["entity_type"]),
                    "quotas": quotas,
                    "total": r2(sum(quotas)),
                }
            )
        return out, total, {"quotas": sums, "total": r2(sum(sums))}

    # ---------- 个人用量 ----------
    def member_profile(self, open_id: str, start: str, end: str, dept_visible: bool = False, monthly: bool = False) -> dict:
        """一个人的用量：指标、趋势、场景分布、额度类型，以及他所在部门的整体情况（不含其他人的个人数据）。

        dept_visible：看的人本来就有权看这个部门的成员用量（运营管理员、部门负责人）。没有这个权限的人，
        部门里除了他自己之外用 AI 的人少于 MIN_GROUP 个时，不给部门的点数和场景——两三个人的「部门合计」
        减掉自己的，就是同事的个人用量。"""
        f = Filter(start, end, member=open_id, monthly=monthly)
        cur, prev = self.slice(f), self.previous(f)
        me = cur.by_member.get(open_id, {"amount": 0.0, "records": 0, "days": set(), "dept": "", "last": ""})
        dept_id = me["dept"] or self.directory.user_dept(open_id)
        price = self.config.point_price
        out = {
            "open_id": open_id,
            "name": self.directory.user_name(open_id),
            "dept_id": dept_id,
            "dept_path": self.directory.dept_path(dept_id),
            "start": start,
            "end": end,
            "total": r2(me["amount"]),
            "cost": r2(me["amount"] * price),
            "records": me["records"],
            "active_days": len(me["days"]),
            "days": f.days,
            **self.compared(f, prev, me["amount"], prev.member_total if prev else 0.0),
            "daily": self.daily(cur),
            "scenarios": self.scenarios(f, level=0, limit=10),
            "products": self.scenarios(f, level=1, limit=8),
            "features": self.split(f, "feature_key"),
            "dept": None,
        }
        if dept_id:
            team = self.slice(Filter(start, end, depts=frozenset([dept_id])))
            mine = team.by_member.get(open_id, {}).get("amount", 0.0)  # 我在这个部门名下的用量（中途调过部门的人，别的部门那部分不算）
            others = team.active_members - (1 if open_id in team.by_member else 0)
            out["dept"] = {"id": dept_id, "name": self.directory.dept_name(dept_id), "headcount": self.directory.headcount(dept_id), "hidden": False}
            if not dept_visible and others < MIN_GROUP:
                out["dept"]["hidden"] = True
            else:
                amounts = sorted((m["amount"] for m in team.by_member.values()), reverse=True)
                out["dept"].update(
                    total=r2(team.member_total),
                    active_members=team.active_members,
                    per_capita=r2(team.member_total / team.active_members) if team.active_members else 0.0,
                    my_share=pct(mine, team.member_total),
                    my_rank=amounts.index(mine) + 1 if mine and mine in amounts else None,
                    scenarios=self.scenarios(Filter(start, end, depts=frozenset([dept_id])), level=1, limit=6),
                )
        return out

    # ---------- 每人每月额度（自然月） ----------
    def month_span(self, month: str) -> tuple[str, str, bool]:
        """一个自然月里可以统计的那一段：(1 日, 月底或最近一个有数据的日子, 是不是整月都有了)。"""
        first = date.fromisoformat(month + "-01")
        last = month_end(first)
        latest = date.fromisoformat(self.latest_date() or (today() - timedelta(days=1)).isoformat())
        end = max(first, min(last, latest))
        return first.isoformat(), end.isoformat(), end == last

    def allowance(self, month: str, depts: frozenset[str] | None = None, member: str | None = None, limit: int = 10) -> dict | None:
        """每人每月基础额度的使用情况（month = "2026-11"）。飞书的个人 AI 额度按自然月发放、每月 1 日重置，
        几个 AI 应用共用一份；这里拿这个月每个人的人员用量合计去和它比。没有设置基础额度时返回 None。

        用完基础额度之后还能用，是因为追加过额度：超出基础额度的部分（over_points）就是这个月追加额度里用掉的点数。
        平台拿不到每个人追加了多少，所以「剩余」只按基础额度算。"""
        per = self.config.monthly_allowance
        if not per:
            return None
        start, end, complete = self.month_span(month)
        cur = self.slice(Filter(start, end, depts=depts, member=member))
        used = sorted(((oid, m["amount"]) for oid, m in cur.by_member.items()), key=lambda x: -x[1])
        if member and not used:
            used = [(member, 0.0)]
        rows = []
        for open_id, amount in used[:limit] if limit else used:
            rate = pct(amount, per)
            dept = cur.by_member.get(open_id, {}).get("dept") or self.directory.user_dept(open_id)
            rows.append({"id": open_id, "name": self.directory.user_name(open_id), "dept_path": self.directory.dept_path(dept),
                         "amount": r2(amount), "rate": rate, "remaining": r2(max(0.0, per - amount)), "over": r2(max(0.0, amount - per)),
                         "state": "failed" if rate >= 100 else ("warn" if rate >= 90 else ("pending" if rate >= 75 else "ok")),
                         "state_label": "已用完" if rate >= 100 else ("即将用完" if rate >= 90 else ("偏高" if rate >= 75 else "正常"))})  # fmt: skip
        return {
            "month": month,
            "label": month_name(month) + ("" if complete else f"（截至 {int(end[5:7])}/{int(end[8:10])}{(' ' + self.live_clock(end)).rstrip()}）"),
            "start": start,
            "end": end,
            "complete": complete,
            "per_member": per,
            "active": cur.active_members,
            "used_up": sum(1 for _o, a in used if a >= per),
            "near": sum(1 for _o, a in used if per * 0.9 <= a < per),
            "over_points": r2(sum(a - per for _o, a in used if a > per)),
            "members": rows,
        }

    def top_scenario_by_member(self, f: Filter) -> dict[str, str]:
        """每个人用得最多的场景（成员表里的「主要场景」一列）。"""
        where, params = f.where()
        best: dict[str, tuple[float, str]] = {}
        for r in self.db.q(
            f"SELECT entity_id, scenario, SUM(amount) AS amount FROM usage_agg WHERE {where} AND subject='member' GROUP BY entity_id, scenario", params
        ):
            if r["entity_id"] not in best or r["amount"] > best[r["entity_id"]][0]:
                best[r["entity_id"]] = (r["amount"], r["scenario"] or labels.UNKNOWN_SCENARIO)
        return {k: v[1] for k, v in best.items()}

    # ---------- 页面级汇总 ----------
    def overview(self, f: Filter, parent: str = ROOT, granularity: str = "day") -> dict:
        """驾驶舱 / 部门运营 / 豆包运营页要的一整套数据。trend 是按 granularity（日 / 周 / 月）汇总的趋势。"""
        cur, prev = self.slice(f), self.previous(f)
        k = self.kpis(cur, prev)
        depts = self.rank_depts(cur, prev, parent)
        apps = [] if f.members_only else self.rank_apps(cur, prev)
        scenes = self.scenarios(f, level=1)
        anomalies = [] if f.member else self.anomalies(cur, prev, parent)
        daily = self.daily(cur)
        return {
            "kpis": k,
            "daily": daily,
            "trend": daily if granularity not in ("week", "month") else self.periods(cur, granularity),
            "depts": depts,
            "members": self.rank_members(cur, prev),
            "apps": apps,
            "scenarios": scenes,
            "scenario_paths": self.scenarios(f, level=0),
            "features": self.split(f, "feature_key"),
            "usage_types": self.split(f, "usage_type"),
            "hourly": self.hourly(f),
            "anomalies": anomalies,
            "narrative": self.narrative(k, depts, apps, scenes, anomalies),
        }

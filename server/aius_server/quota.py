"""额度分配：给部门、人员或应用在一个周期里分配点数，对照实际用量算出已用、剩余和使用率。

已用量的口径：
- 部门：这个部门（含下级部门）成员在周期内的人员用量日志之和。
- 人员：这个人在周期内的人员用量日志之和。
- 应用：这个应用在周期内的应用用量日志之和。
"""

from __future__ import annotations

import time
from datetime import date

from .db import Database, marks
from .directory import Directory
from .sync import today

DIMENSIONS = {"department": "部门", "member": "人员", "app": "应用"}


class QuotaError(ValueError):
    pass


def state_of(rate: float | None, enabled: bool) -> tuple[str, str]:
    """(样式, 文字)：≥100% 超额，≥90% 即将用完，≥75% 偏高。"""
    if not enabled:
        return "", "已停用"
    if rate is None:
        return "", "—"
    if rate >= 100:
        return "failed", "已超额"
    if rate >= 90:
        return "warn", "即将用完"
    if rate >= 75:
        return "pending", "偏高"
    return "ok", "正常"


class Quotas:
    def __init__(self, db: Database, directory: Directory):
        self.db = db
        self.directory = directory

    def _used(self, dimension: str, target_id: str, start: str, end: str) -> float:
        span = (start, end)
        if dimension == "department":
            tree = sorted(self.directory.subtree(target_id))
            return self.db.val(
                f"SELECT SUM(used_quota) FROM usage_logs WHERE subject='member' AND usage_date BETWEEN ? AND ? AND department_id IN ({marks(len(tree))})",
                (*span, *tree),
                0.0,
            )
        subject = "member" if dimension == "member" else "app"
        return self.db.val(
            "SELECT SUM(used_quota) FROM usage_logs WHERE subject=? AND entity_id=? AND usage_date BETWEEN ? AND ?", (subject, target_id, *span), 0.0
        )

    def target_name(self, dimension: str, target_id: str) -> str:
        if dimension == "department":
            return self.directory.dept_path(target_id)
        if dimension == "member":
            return self.directory.user_name(target_id)
        return self.db.val("SELECT name FROM app_mapping WHERE app_id=?", (target_id,), target_id)

    def list(self) -> list[dict]:
        now = today().isoformat()
        out = []
        for r in self.db.q("SELECT * FROM quota_allocations ORDER BY enabled DESC, period_end DESC, id DESC"):
            used = round(self._used(r["dimension"], r["target_id"], r["period_start"], r["period_end"]), 2)
            rate = round(used / r["amount"] * 100, 1) if r["amount"] else None
            style, text = state_of(rate, bool(r["enabled"]))
            period = "当前周期" if r["period_start"] <= now <= r["period_end"] else ("已结束" if r["period_end"] < now else "未开始")
            out.append(
                {
                    **dict(r),
                    "dimension_label": DIMENSIONS.get(r["dimension"], r["dimension"]),
                    "name": self.target_name(r["dimension"], r["target_id"]) if r["target_name"] in ("", r["target_id"]) else r["target_name"],
                    "used": used,
                    "remaining": round(r["amount"] - used, 2),
                    "rate": rate,
                    "state": style,
                    "state_label": text,
                    "period": period,
                    "current": period == "当前周期" and bool(r["enabled"]),
                }
            )
        return out

    def summary(self, rows: list[dict]) -> dict:
        live = [r for r in rows if r["current"]]
        # 合计只算「最外层」的额度：给上级部门分了额度、又给它的下级部门或成员单独分了的，下级那部分已经含在上级里，
        # 再加一遍会把已分配和已使用都算多
        depts = {r["target_id"] for r in live if r["dimension"] == "department"}

        def covered(r: dict) -> bool:
            if r["dimension"] == "department":
                return any(d in depts for d in self.directory.chain(r["target_id"])[1:])
            if r["dimension"] == "member":
                member = self.directory.user(r["target_id"])
                return bool(member) and any(d in depts for dept in member.dept_ids for d in self.directory.chain(dept))
            return False

        outer = [r for r in live if not covered(r)]
        allocated, used = sum(r["amount"] for r in outer), sum(r["used"] for r in outer)
        return {
            "count": len(live),
            "allocated": round(allocated, 2),
            "used": round(used, 2),
            "rate": round(used / allocated * 100, 1) if allocated else None,
            "over": sum(1 for r in live if r["rate"] is not None and r["rate"] >= 100),
            "near": sum(1 for r in live if r["rate"] is not None and 90 <= r["rate"] < 100),
        }

    def _clean(self, dimension: str, target_id: str, amount, period_start: str, period_end: str) -> tuple[float, str, str, str]:
        if dimension not in DIMENSIONS:
            raise QuotaError("对象类型只能是部门、人员或应用")
        target_id = (target_id or "").strip()
        if not target_id:
            raise QuotaError("请选择分配对象")
        if dimension == "department" and not self.directory.dept(target_id):
            raise QuotaError("通讯录里没有这个部门，请从联想列表里选")
        if dimension == "member" and not self.directory.user(target_id):
            raise QuotaError("通讯录里没有这个人，请从联想列表里选")
        try:
            value = float(amount)
        except (TypeError, ValueError):
            raise QuotaError("分配额度要填数字") from None
        if not 0 < value <= 1e12:
            raise QuotaError("分配额度要大于 0")
        try:
            first, last = date.fromisoformat(period_start), date.fromisoformat(period_end)
        except (TypeError, ValueError):
            raise QuotaError("周期的开始和结束日期都要填") from None
        if first > last:
            raise QuotaError("周期的开始日期不能晚于结束日期")
        return value, first.isoformat(), last.isoformat(), target_id

    def save(self, actor: str, dimension: str, target_id: str, amount, period_start: str, period_end: str, note: str = "", quota_id: int | None = None) -> int:
        value, first, last, target_id = self._clean(dimension, target_id, amount, period_start, period_end)
        name = self.target_name(dimension, target_id)
        now = int(time.time())
        if quota_id:
            cur = self.db.x(
                "UPDATE quota_allocations SET dimension=?, target_id=?, target_name=?, amount=?, period_start=?, period_end=?, note=?, updated_by=?, updated_at=? WHERE id=?",
                (dimension, target_id, name, value, first, last, note.strip()[:200], actor, now, quota_id),
            )
            if not cur.rowcount:
                raise QuotaError("这条额度记录已经不存在了")
            return quota_id
        return self.db.x(
            "INSERT INTO quota_allocations(dimension, target_id, target_name, amount, period_start, period_end, note, updated_by, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (dimension, target_id, name, value, first, last, note.strip()[:200], actor, now),
        ).lastrowid

    def set_enabled(self, quota_id: int, enabled: bool, actor: str) -> bool:
        return self.db.x("UPDATE quota_allocations SET enabled=?, updated_by=?, updated_at=? WHERE id=?", (int(enabled), actor, int(time.time()), quota_id)).rowcount > 0

    def delete(self, quota_id: int) -> bool:
        return self.db.x("DELETE FROM quota_allocations WHERE id=?", (quota_id,)).rowcount > 0

    def get(self, quota_id: int) -> dict | None:
        row = self.db.one("SELECT * FROM quota_allocations WHERE id=?", (quota_id,))
        return dict(row) if row else None

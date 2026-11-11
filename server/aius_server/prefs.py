"""页面上能改的运营配置：定时同步、点数单价、每人每月额度、周报推送。存在数据库 settings 表里，缺的项用默认值。"""

from __future__ import annotations

import time
from datetime import date
from typing import Any

from .db import Database, dumps, loads

DEFAULTS: dict[str, dict[str, Any]] = {
    # 每日定时同步。飞书建议避开凌晨 2 点左右的请求高峰，所以默认 5 点；默认同步前 2 天：
    # 统计详情是 T+1 的，头一天同步时可能还没出齐，第二天顺带补上。
    # live_minutes：开着定时同步时，当天的用量日志每隔几分钟增量更新一次；0 = 当天不自动更新
    "sync": {"enabled": False, "hour": 5, "range_days": 2, "live_minutes": 10, "last_auto_date": ""},
    # 点数折算费用：1 点 = point_price 元
    "pricing": {"point_price": 0.05},
    # 每人每月的基础额度（点）：飞书的个人 AI 额度按自然月发放、每月 1 日重置。0 = 没有设置，页面上不显示月度额度
    "allowance": {"per_member": 0},
    # AI 用量周报：weekday 1..7 = 周一..周日；scope: active（上周有用量的人）| all（通讯录里所有人）；
    # since: 累计用量从哪天算起，空 = 当年 1 月 1 日
    "report": {"enabled": False, "weekday": 1, "hour": 9, "scope": "active", "since": "", "last_push_date": ""},
}


class ConfigError(ValueError):
    pass


def _int(value: Any, low: int, high: int, what: str) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{what}要填整数") from None
    if not low <= n <= high:
        raise ConfigError(f"{what}要在 {low} 到 {high} 之间")
    return n


def _date(value: Any, what: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise ConfigError(f"{what}要写成 2026-01-31 这样的日期") from None


def clean(key: str, patch: dict[str, Any]) -> dict[str, Any]:
    """校验并规整一组要保存的配置项；只收认识的字段。"""
    out: dict[str, Any] = {}
    if key == "sync":
        if "enabled" in patch:
            out["enabled"] = bool(patch["enabled"])
        if "hour" in patch:
            out["hour"] = _int(patch["hour"], 0, 23, "执行时间")
        if "range_days" in patch:
            out["range_days"] = _int(patch["range_days"], 1, 31, "同步范围")
        if "live_minutes" in patch:
            minutes = _int(patch["live_minutes"], 0, 60, "当天数据更新间隔")
            if 0 < minutes < 5:
                raise ConfigError("当天数据最快 5 分钟更新一次（飞书的用量接口每秒只能查 1 次）")
            out["live_minutes"] = minutes
    elif key == "pricing":
        try:
            price = float(patch.get("point_price"))
        except (TypeError, ValueError):
            raise ConfigError("单价要填数字") from None
        if not 0 < price <= 1000:
            raise ConfigError("单价要大于 0")
        out["point_price"] = round(price, 6)
    elif key == "allowance":
        raw = str(patch.get("per_member", "")).strip()
        try:
            value = float(raw) if raw else 0.0
        except ValueError:
            raise ConfigError("每人每月额度要填数字，不设置就留空") from None
        if not 0 <= value <= 1e9:
            raise ConfigError("每人每月额度不能是负数")
        out["per_member"] = round(value, 2)
    elif key == "report":
        if "enabled" in patch:
            out["enabled"] = bool(patch["enabled"])
        if "weekday" in patch:
            out["weekday"] = _int(patch["weekday"], 1, 7, "推送日")
        if "hour" in patch:
            out["hour"] = _int(patch["hour"], 0, 23, "推送时间")
        if "scope" in patch:
            if patch["scope"] not in ("active", "all"):
                raise ConfigError("推送范围只能是「上周有用量的人」或「所有人」")
            out["scope"] = patch["scope"]
        if "since" in patch:
            out["since"] = _date(patch["since"], "累计起始日")
    else:
        raise ConfigError("没有这个配置项")
    return out


class Config:
    def __init__(self, db: Database):
        self.db = db

    def get(self, key: str) -> dict[str, Any]:
        stored = loads(self.db.val("SELECT value FROM settings WHERE key=?", (key,)), {})
        return {**DEFAULTS[key], **{k: v for k, v in stored.items() if k in DEFAULTS[key]}}

    def save(self, key: str, patch: dict[str, Any], actor: str) -> dict[str, Any]:
        """页面提交的修改：先校验，再和已有的合并保存。"""
        return self._write(key, clean(key, patch), actor)

    def mark(self, key: str, field: str, value: Any) -> None:
        """系统自己记的状态（上次自动同步 / 推送是哪天），不经过校验。"""
        self._write(key, {field: value}, "system")

    def _write(self, key: str, patch: dict[str, Any], actor: str) -> dict[str, Any]:
        # 读出来、合并、写回去要在一个事务里：调度线程记「今天推送过了」和管理员保存配置撞在一起时，谁的修改都不能丢
        with self.db.tx() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            stored = loads(row["value"] if row else None, {})
            merged = {**DEFAULTS[key], **{k: v for k, v in stored.items() if k in DEFAULTS[key]}, **patch}
            c.execute(
                "INSERT INTO settings(key, value, updated_by, updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                (key, dumps(merged), actor, int(time.time())),
            )
        return merged

    @property
    def point_price(self) -> float:
        return float(self.get("pricing")["point_price"])

    @property
    def monthly_allowance(self) -> float:
        return float(self.get("allowance")["per_member"] or 0)

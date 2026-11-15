"""页面上能改的运营配置：定时同步、点数单价、每人每月额度、报表发送。存在数据库 settings 表里，缺的项用默认值。"""

from __future__ import annotations

import time
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
    # 报表发送（日报 / 周报 / 月报，各自一个开关）。scope 三种报表共用：active（这一期有用量的人，部门负责人只要部门有用量也发）
    # | all（通讯录里所有人）。日报发昨天的；周报 weekday 1..7 = 周一..周日，发上一周；月报 day 1..28 号，发上个月
    # last_push_date：定时任务上次发送是哪天；last_period：最近一次全量发送（定时或手动）发的是哪一期（这一期的第一天），
    # 同一期只自动发一次——手动全量发过的，定时就不再发
    "report": {"scope": "active"},
    "report_daily": {"enabled": False, "hour": 9, "last_push_date": "", "last_period": ""},
    "report_weekly": {"enabled": False, "weekday": 1, "hour": 9, "last_push_date": "", "last_period": ""},
    "report_monthly": {"enabled": False, "day": 1, "hour": 9, "last_push_date": "", "last_period": ""},
    # 额度告警：这个月的个人用量到了每人每月基础额度的这些百分比时提醒本人；guide_url = 卡片上「额度申请说明」的链接（可不填）；
    # extra = 每次追加的额度（点），超过 100% 的档位卡片上和基础额度一起列出来
    "quota_alert": {"enabled": False, "thresholds": [80, 100, 180, 200], "guide_url": "", "extra": 2000},
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
        if "scope" in patch:
            if patch["scope"] not in ("active", "all"):
                raise ConfigError("发送范围只能是「这一期有用量的人」或「通讯录里所有人」")
            out["scope"] = patch["scope"]
    elif key in ("report_daily", "report_weekly", "report_monthly"):
        if "enabled" in patch:
            out["enabled"] = bool(patch["enabled"])
        if "hour" in patch:
            out["hour"] = _int(patch["hour"], 0, 23, "发送时间")
        if key == "report_weekly" and "weekday" in patch:
            out["weekday"] = _int(patch["weekday"], 1, 7, "发送日")
        if key == "report_monthly" and "day" in patch:
            out["day"] = _int(patch["day"], 1, 28, "发送日（每月几号）")
    elif key == "quota_alert":
        if "enabled" in patch:
            out["enabled"] = bool(patch["enabled"])
        if "thresholds" in patch:
            raw = patch["thresholds"]
            items = raw if isinstance(raw, list) else str(raw).replace("，", ",").replace("%", "").split(",")
            try:
                values = sorted({int(float(str(x).strip())) for x in items if str(x).strip()})
            except ValueError:
                raise ConfigError("告警档位要填数字，用逗号隔开，例如 80,100,180,200") from None
            if not values or len(values) > 8 or values[0] < 1 or values[-1] > 1000:
                raise ConfigError("告警档位要在 1 到 1000（%）之间，至少 1 个、最多 8 个")
            out["thresholds"] = values
        if "guide_url" in patch:
            url = str(patch["guide_url"] or "").strip()
            if url and not url.startswith(("http://", "https://")):
                raise ConfigError("额度申请说明的链接要以 http:// 或 https:// 开头，不需要就留空")
            out["guide_url"] = url[:500]
        if "extra" in patch:
            raw = str(patch["extra"] or "").strip()
            try:
                extra = float(raw) if raw else float(DEFAULTS["quota_alert"]["extra"])  # 留空 = 默认的 2000 点
            except ValueError:
                raise ConfigError("追加额度要填数字（点），留空就是默认的 2000 点") from None
            if not 0 <= extra <= 1e9:
                raise ConfigError("追加额度不能是负数")
            out["extra"] = round(extra, 2)
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

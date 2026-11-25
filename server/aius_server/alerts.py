"""额度告警：每个人这个月的用量到了基础额度的 80%、100%（档位可以改）时，用应用机器人给他本人发一张飞书卡片提醒；
追加过额度的人（这个月用量超过 3000 点，见「额度分配」），再按「基础 + 追加」合计的 80%、100% 提醒一轮
（一般每人追加 2000 点，合计 4000 点，就是 3200 点和 4000 点；个别人追加得不一样的，在「额度分配」里单独设置）。

- 看的是这个月 1 日到现在的个人用量（当天的靠当天增量同步，每 10 分钟更新）。
- 卡片上的额度那一排和周报、月报一样（report.allowance_stats）：追加过的把追加额度列出来，已用比例按「基础 + 追加」合计算。
- 每个人每个月每个档位只提醒一次；两次检查之间一下跨过好几个档位，只发最高的那个，低的记成「已覆盖」。每月 1 日从头算。
- 打开开关、改档位时，已经超过的档位不补发，只提醒之后新跨过的（记成「开启前已超过」）；需要的话可以在页面上给这些人补发一次。
- 只在 SEND_HOURS 之间发；夜里跨过的，第二天早上发那时最高的档位。每 CHECK_MINUTES 分钟看一次；有同步任务在整批替换数据时先不看。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from . import cards
from .analytics import month_name
from .db import Database
from .directory import Directory
from .feishu import CST, FeishuError
from .prefs import Config
from .report import SEND_GAP, TOP_SCENES, _md, allowance_stats, points, scene, scene_lines

log = logging.getLogger("aius")

CHECK_MINUTES = 10
SEND_HOURS = (9, 21)  # 9:00 到 20:59 之间发
STATUS = {"sending": "发送中", "sent": "已提醒", "failed": "发送失败", "covered": "已覆盖（同时跨过了更高的档位）", "baseline": "开启前已超过"}


def level_name(level: str) -> str:
    """base:80 → 基础 80%；total:100 → 基础 + 追加 100%。"""
    scope, _, pct = level.partition(":")
    return f"{'基础' if scope == 'base' else '基础 + 追加'} {pct}%"


def plan_levels(per: float, extra: float, topped_at: float, thresholds: list[int], topped: bool = False) -> list[dict]:
    """一个人这个月的提醒档位，按点数从低到高：先是基础额度的各个百分比；再是「基础 + 追加」合计的各个百分比，
    只算比基础额度多、也超过「算作已追加的用量」的那些（没到那么多，就不当作追加过）。
    topped：这个月有追加记录、确实追加过，合计的档位只要比基础额度多就算。"""
    if not per:
        return []
    total = per + extra
    floor = per if topped else max(per, topped_at)
    out = [{"level": f"base:{t}", "scope": "base", "pct": t, "line": per * t / 100, "total": per} for t in thresholds]
    out += [{"level": f"total:{t}", "scope": "total", "pct": t, "line": total * t / 100, "total": total} for t in thresholds if total * t / 100 > floor]
    return sorted(out, key=lambda lv: lv["line"])


class QuotaAlerts:
    def __init__(self, db: Database, feishu, directory: Directory, config: Config, public_url: str, engine=None, sleep=time.sleep):
        self.db = db
        self.feishu = feishu
        self.directory = directory
        self.config = config
        self.public_url = public_url.rstrip("/")
        self.engine = engine
        self.sleep = sleep
        self._lock = threading.Lock()
        self._last = 0.0

    # ---------- 数据 ----------
    @staticmethod
    def month_of(now: datetime) -> str:
        return now.strftime("%Y-%m")

    def usage(self, month: str) -> dict[str, float]:
        """这个月每个人用了多少点（1 日到现在）。"""
        return {
            r[0]: r[1]
            for r in self.db.q(
                "SELECT entity_id, SUM(amount) FROM usage_agg WHERE subject='member' AND usage_date BETWEEN ? AND ? GROUP BY entity_id",
                (f"{month}-01", f"{month}-31"),
            )
        }

    def thresholds(self) -> list[int]:
        return list(self.config.get("quota_alert")["thresholds"])

    def planner(self, month: str):
        """open_id → 他这个月的提醒档位。设置、个别人的追加额度、这个月的追加记录一次读好（每次检查要算几百个人）。"""
        allowance = self.config.get("allowance")
        per, extra, topped_at = float(allowance["per_member"] or 0), float(allowance["extra"]), float(allowance["topped_at"])
        special = {r["open_id"]: float(r["extra"]) for r in self.config.extras()}
        granted = {r[0]: float(r[1]) for r in self.db.q("SELECT open_id, points FROM topups WHERE month=?", (month,))}  # 自动审批加的
        thresholds = self.thresholds()
        return lambda open_id: plan_levels(per, granted.get(open_id) or special.get(open_id, extra), topped_at, thresholds, topped=open_id in granted)

    def levels(self, now: datetime) -> list[dict]:
        """页面上看：这个月到了每个档位的各有多少人（档位的点数按一般的追加额度写）。"""
        month = self.month_of(now)
        plan = self.planner(month)
        counts: dict[str, int] = {}
        for oid, used in self.usage(month).items():
            for lv in plan(oid):
                if used >= lv["line"]:
                    counts[lv["level"]] = counts.get(lv["level"], 0) + 1
        return [{"level": lv["level"], "name": level_name(lv["level"]), "line": lv["line"], "people": counts.get(lv["level"], 0)} for lv in plan("")]

    def pending(self, now: datetime) -> list[tuple[str, float, dict, list[dict]]]:
        """现在要提醒的人：(open_id, 这个月已用, 要提醒的档位 = 跨过的最高档位, 这次新跨过的档位)。"""
        month = self.month_of(now)
        done: dict[str, set[str]] = {}
        for r in self.db.q("SELECT open_id, level FROM quota_alerts WHERE month=?", (month,)):
            done.setdefault(r[0], set()).add(r[1])
        plan = self.planner(month)
        out = []
        for oid, used in sorted(self.usage(month).items(), key=lambda kv: -kv[1]):
            levels = plan(oid)
            hit = [lv for lv in levels if used >= lv["line"]]  # 从低到高，是 levels 的前几个
            mine = done.get(oid, set())
            reached = max((i for i, lv in enumerate(levels) if lv["level"] in mine), default=-1)
            if not hit or reached >= len(hit) - 1:
                continue
            out.append((oid, used, hit[-1], [lv for i, lv in enumerate(hit) if i > reached and lv["level"] not in mine]))
        return out

    # ---------- 检查和发送 ----------
    def tick(self, now: datetime) -> bool:
        """调度每分钟调一次：开着、在发送时段里、离上次检查满 CHECK_MINUTES 分钟、没有同步任务在整批替换数据，就在后台检查一次。"""
        if not self.config.get("quota_alert")["enabled"] or not self.config.monthly_allowance or not SEND_HOURS[0] <= now.hour < SEND_HOURS[1]:
            return False
        if now.timestamp() - self._last < CHECK_MINUTES * 60 - 30 or self._lock.locked() or (self.engine and self.engine.active()):
            return False
        self._last = now.timestamp()
        threading.Thread(target=self.check, args=(now,), name="quota-alerts", daemon=True).start()
        return True

    def check(self, now: datetime) -> list[dict]:
        """检查一次：新跨过档位的人先记下来（中途重启也不会再发一遍），再一个个发。返回这次发了谁。"""
        if not self.config.get("quota_alert")["enabled"]:
            return []
        with self._lock:
            month = self.month_of(now)
            todo = self.pending(now)
            if todo:
                cards.purge(self.db)  # 卡片内容只留 CARD_KEEP_DAYS 天
            stamp = int(time.time())
            with self.db.tx() as c:
                for oid, used, top, new in todo:
                    for lv in new:
                        c.execute(
                            "INSERT OR IGNORE INTO quota_alerts(month, open_id, level, line, used, status, created_at) VALUES(?,?,?,?,?,?,?)",
                            (month, oid, lv["level"], lv["line"], round(used, 2), "sending" if lv is top else "covered", stamp),
                        )
            return [self._send(month, oid, used, top, now) for oid, used, top, _new in todo]

    def _send(self, month: str, open_id: str, used: float, level: dict, now: datetime, record: bool = True) -> dict:
        status, error = "sent", ""
        card = self.card(open_id, used, level, now)
        try:
            self.feishu.send_card(open_id, card)
        except FeishuError as e:
            status, error = "failed", str(e)[:300]
            log.warning("额度提醒发送失败 %s %s：%s", open_id, level["level"], e)
        if record:
            self.db.x("UPDATE quota_alerts SET status=?, error=?, used=? WHERE month=? AND open_id=? AND level=?", (status, error, round(used, 2), month, open_id, level["level"]))
            cards.store(self.db, open_id, card, status, error, alert=f"{month}|{open_id}|{level['level']}")  # 提醒记录里能看到这张卡片
        self.sleep(SEND_GAP)
        return {"open_id": open_id, "level": level["level"], "used": used, "status": status, "error": error}

    # ---------- 开关 ----------
    def save(self, patch: dict, actor: str, now: datetime | None = None) -> dict:
        """保存设置。打开开关或改了档位时，已经超过的档位记成「开启前已超过」，不补发。返回设置，baseline = 这次记下的人数。"""
        now = now or datetime.now(CST)
        before = self.config.get("quota_alert")
        saved = self.config.save("quota_alert", patch, actor)
        people = 0
        if saved["enabled"] and (not before["enabled"] or saved["thresholds"] != before["thresholds"]):
            people = self.baseline(now)
        return {**saved, "baseline": people}

    def baseline(self, now: datetime) -> int:
        month = self.month_of(now)
        plan = self.planner(month)
        stamp = int(time.time())
        people = 0
        with self.db.tx() as c:
            for oid, used in self.usage(month).items():
                hit = [lv for lv in plan(oid) if used >= lv["line"]]
                people += bool(hit)
                for lv in hit:
                    c.execute(
                        "INSERT OR IGNORE INTO quota_alerts(month, open_id, level, line, used, status, created_at) VALUES(?,?,?,?,?,'baseline',?)",
                        (month, oid, lv["level"], lv["line"], round(used, 2), stamp),
                    )
        return people

    def waiting(self, now: datetime) -> dict[str, str]:
        """开启前就已经超过档位、这个月还没收到过提醒的人 → 他们最高的那个档位。"""
        month = self.month_of(now)
        out: dict[str, tuple[float, str]] = {}
        for r in self.db.q(
            "SELECT open_id, level, line FROM quota_alerts WHERE month=? AND status='baseline' "
            "AND open_id NOT IN (SELECT open_id FROM quota_alerts WHERE month=? AND status IN ('sent','sending','failed'))",
            (month, month),
        ):
            if r[0] not in out or r[2] > out[r[0]][0]:
                out[r[0]] = (r[2], r[1])
        return {oid: level for oid, (_line, level) in out.items()}

    def catch_up(self, now: datetime) -> list[dict]:
        """给开启前已经超过档位的人补发一次（每人只发他那时最高的档位）。"""
        with self._lock:
            month = self.month_of(now)
            usage = self.usage(month)
            plan = self.planner(month)
            out = []
            for oid, key in self.waiting(now).items():
                level = next((lv for lv in plan(oid) if lv["level"] == key), None)
                if level is None:  # 设置改过，这个档位已经没有了
                    continue
                self.db.x("UPDATE quota_alerts SET status='sending' WHERE month=? AND open_id=? AND level=?", (month, oid, key))
                out.append(self._send(month, oid, usage.get(oid, 0.0), level, now))
            return out

    def test_send(self, open_id: str, now: datetime | None = None) -> dict:
        """「测试发送给我」：按自己这个月的实际用量发一张（没跨过档位就按第一个档位发），不记录。"""
        now = now or datetime.now(CST)
        used = self.usage(self.month_of(now)).get(open_id, 0.0)
        levels = self.planner(self.month_of(now))(open_id)
        if not levels:
            return {"open_id": open_id, "level": "", "used": used, "status": "failed", "error": "还没有设置每人每月基础额度"}
        hit = [lv for lv in levels if used >= lv["line"]]
        return self._send(self.month_of(now), open_id, used, hit[-1] if hit else levels[0], now, record=False)

    def recover(self) -> int:
        """服务启动时调：上次正在发、被重启打断的提醒记成发送失败（不自动重发，免得重复）。"""
        return self.db.x("UPDATE quota_alerts SET status='failed', error='服务重启，没有发出去' WHERE status='sending'").rowcount

    # ---------- 页面 ----------
    def records(self, limit: int = 50) -> list[dict]:
        rows = self.db.q(
            "SELECT q.*, (SELECT MAX(id) FROM sent_cards s WHERE s.alert = q.month || '|' || q.open_id || '|' || q.level) AS card_id "
            "FROM quota_alerts q WHERE q.status IN ('sending','sent','failed') ORDER BY q.created_at DESC, q.line DESC LIMIT ?",
            (limit,),
        )
        return [
            {**dict(r), "name": self.directory.user_name(r["open_id"]), "level_name": level_name(r["level"]), "status_name": STATUS.get(r["status"], r["status"])}
            for r in rows
        ]  # fmt: skip

    # ---------- 卡片 ----------
    def card(self, open_id: str, used: float, level: dict, now: datetime) -> dict:
        per = self.config.monthly_allowance
        month = self.month_of(now)
        name = self.directory.user_name(open_id) if self.directory.user(open_id) else ""
        pct, total = level["pct"], level["total"]
        if level["scope"] == "base":
            reached = f"基础额度的 **{pct}%**"
        else:
            reached = f"基础额度和追加额度合计（{points(total)} 点）的 **{pct}%**"
        intro = f"您 **{month_name(month)}** 的飞书/豆包 AI 用量已经达到{reached}。"
        elements: list[dict] = [_md(f"**{name}**，{intro}" if name else intro)]
        # 额度那一排：追加过的（这个月有追加记录、按合计提醒的，或者用量超过「算作已追加的用量」）把追加额度列出来，已用比例按基础 + 追加合计算
        extra, topped = self.config.quota_extra(open_id, month)
        elements.append(allowance_stats(used, per, extra, self.config.topped_at, force_total=topped or level["scope"] == "total"))
        scenes: dict[str, float] = {}
        for r in self.db.q(
            "SELECT scenario, SUM(amount) FROM usage_agg WHERE subject='member' AND entity_id=? AND usage_date BETWEEN ? AND ? GROUP BY scenario",
            (open_id, f"{month}-01", f"{month}-31"),
        ):
            scenes[scene(r[0])] = scenes.get(scene(r[0]), 0.0) + r[1]
        top = sorted(scenes.items(), key=lambda kv: -kv[1])[:TOP_SCENES]
        if top and used > 0:
            elements.append(_md(scene_lines(top, used)))
        elements.append({"tag": "hr"})
        button = {"tag": "button", "type": "primary", "text": {"tag": "plain_text", "content": "查看我的用量"}, "url": f"{self.public_url}/self-usage"}
        elements.append({"tag": "action", "actions": [button]})
        note = f"统计时间：{now.month} 月 1 日 - {now.month} 月 {now.day} 日 {now:%H:%M}（当天的用量每 10 分钟更新一次）"
        elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": note}]})
        if level["scope"] == "base":
            color = "orange" if pct < 100 else "red"
            title = f"飞书/豆包 AI 额度提醒 ｜ 本月已达 {pct}%"
        else:
            color = "red" if pct < 100 else "carmine"
            title = f"飞书/豆包 AI 额度提醒 ｜ 本月已达 {pct}%（含追加额度）"
        return {"config": {"wide_screen_mode": True}, "header": {"template": color, "title": {"tag": "plain_text", "content": title}}, "elements": elements}

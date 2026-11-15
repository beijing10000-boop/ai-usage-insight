"""额度告警：每个人这个月的用量到了每人每月基础额度的 80%、100%、180%、200%（档位可以改）时，用应用机器人给他本人发一张飞书卡片提醒。

- 卡片上的额度那一排和周报、月报一样（report.allowance_stats）：用量超过 3000 点（可改）就当作已经追加过额度，
  列出追加额度，已用比例按「基础 + 追加」合计算。
- 看的是这个月 1 日到现在的个人用量（当天的靠当天增量同步，每 10 分钟更新），和「额度分配」里的每人每月基础额度比。
- 每个人每个月每个档位只提醒一次；两次检查之间一下跨过好几个档位（比如从 75% 涨到 105%），只发最高的那个，低的记成「已覆盖」。
- 打开开关、改档位时，已经超过的档位不补发，只提醒之后新跨过的（记成「开启前已超过」）；需要的话可以在页面上给这些人补发一次。
  每月 1 日从头算。
- 只在 SEND_HOURS 之间发；夜里跨过的，第二天早上发那时最高的档位。每 CHECK_MINUTES 分钟看一次；有同步任务在整批替换数据时先不看。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from .analytics import month_name
from .db import Database
from .directory import Directory
from .feishu import CST, FeishuError
from .prefs import Config
from .report import SEND_GAP, TOP_SCENES, _md, allowance_stats, scene, scene_lines

log = logging.getLogger("aius")

CHECK_MINUTES = 10
SEND_HOURS = (9, 21)  # 9:00 到 20:59 之间发
STATUS = {"sending": "发送中", "sent": "已提醒", "failed": "发送失败", "covered": "已覆盖（同时跨过了更高的档位）", "baseline": "开启前已超过"}


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

    def crossed(self, used: float) -> list[int]:
        """用了这么多点，跨过了哪些档位（从低到高）。没设每人每月基础额度时一个都不算。"""
        per = self.config.monthly_allowance
        return [t for t in self.thresholds() if per and used >= per * t / 100]

    def levels(self, now: datetime) -> list[dict]:
        """页面上看：这个月到了每个档位的各有多少人。"""
        usage = self.usage(self.month_of(now))
        per = self.config.monthly_allowance
        return [{"threshold": t, "people": sum(1 for v in usage.values() if per and v >= per * t / 100)} for t in self.thresholds()]

    def _done(self, month: str) -> tuple[set[tuple[str, int]], dict[str, int]]:
        rows = self.db.q("SELECT open_id, threshold FROM quota_alerts WHERE month=?", (month,))
        done = {(r[0], r[1]) for r in rows}
        top: dict[str, int] = {}
        for oid, t in done:
            top[oid] = max(top.get(oid, 0), t)
        return done, top

    def pending(self, now: datetime) -> list[tuple[str, float, int, list[int]]]:
        """现在要提醒的人：(open_id, 这个月已用, 要提醒的档位 = 跨过的最高档位, 这次新跨过的档位)。"""
        month = self.month_of(now)
        done, top_done = self._done(month)
        out = []
        for oid, used in sorted(self.usage(month).items(), key=lambda kv: -kv[1]):
            hit = self.crossed(used)
            if not hit or top_done.get(oid, 0) >= hit[-1]:
                continue
            new = [t for t in hit if t > top_done.get(oid, 0) and (oid, t) not in done]
            out.append((oid, used, hit[-1], new))
        return out

    # ---------- 检查和发送 ----------
    def tick(self, now: datetime) -> bool:
        """调度每分钟调一次：开着、在发送时段里、离上次检查满 CHECK_MINUTES 分钟、没有同步任务在整批替换数据，就在后台检查一次。"""
        cfg = self.config.get("quota_alert")
        if not cfg["enabled"] or not self.config.monthly_allowance or not SEND_HOURS[0] <= now.hour < SEND_HOURS[1]:
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
            stamp = int(time.time())
            with self.db.tx() as c:
                for oid, used, top, new in todo:
                    for t in new:
                        c.execute(
                            "INSERT OR IGNORE INTO quota_alerts(month, open_id, threshold, used, status, created_at) VALUES(?,?,?,?,?,?)",
                            (month, oid, t, round(used, 2), "sending" if t == top else "covered", stamp),
                        )
            return [self._send(month, oid, used, top, now) for oid, used, top, _new in todo]

    def _send(self, month: str, open_id: str, used: float, threshold: int, now: datetime, record: bool = True) -> dict:
        status, error = "sent", ""
        try:
            self.feishu.send_card(open_id, self.card(open_id, used, threshold, now))
        except FeishuError as e:
            status, error = "failed", str(e)[:300]
            log.warning("额度提醒发送失败 %s %s%%：%s", open_id, threshold, e)
        if record:
            self.db.x("UPDATE quota_alerts SET status=?, error=?, used=? WHERE month=? AND open_id=? AND threshold=?", (status, error, round(used, 2), month, open_id, threshold))
        self.sleep(SEND_GAP)
        return {"open_id": open_id, "threshold": threshold, "used": used, "status": status, "error": error}

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
        stamp = int(time.time())
        people = 0
        with self.db.tx() as c:
            for oid, used in self.usage(month).items():
                hit = self.crossed(used)
                people += bool(hit)
                for t in hit:
                    c.execute(
                        "INSERT OR IGNORE INTO quota_alerts(month, open_id, threshold, used, status, created_at) VALUES(?,?,?,?,'baseline',?)",
                        (month, oid, t, round(used, 2), stamp),
                    )
        return people

    def waiting(self, now: datetime) -> dict[str, int]:
        """开启前就已经超过档位、这个月还没收到过提醒的人 → 他们最高的那个档位。"""
        month = self.month_of(now)
        rows = self.db.q(
            "SELECT open_id, MAX(threshold) FROM quota_alerts WHERE month=? AND status='baseline' "
            "AND open_id NOT IN (SELECT open_id FROM quota_alerts WHERE month=? AND status IN ('sent','sending','failed')) GROUP BY open_id",
            (month, month),
        )
        return {r[0]: r[1] for r in rows}

    def catch_up(self, now: datetime) -> list[dict]:
        """给开启前已经超过档位的人补发一次（每人只发他那时最高的档位）。"""
        with self._lock:
            month = self.month_of(now)
            usage = self.usage(month)
            out = []
            for oid, t in self.waiting(now).items():
                self.db.x("UPDATE quota_alerts SET status='sending' WHERE month=? AND open_id=? AND threshold=?", (month, oid, t))
                out.append(self._send(month, oid, usage.get(oid, 0.0), t, now))
            return out

    def test_send(self, open_id: str, now: datetime | None = None) -> dict:
        """「测试发送给我」：按自己这个月的实际用量发一张（没跨过档位就按第一个档位发），不记录。"""
        now = now or datetime.now(CST)
        used = self.usage(self.month_of(now)).get(open_id, 0.0)
        hit = self.crossed(used)
        return self._send(self.month_of(now), open_id, used, hit[-1] if hit else self.thresholds()[0], now, record=False)

    def recover(self) -> int:
        """服务启动时调：上次正在发、被重启打断的提醒记成发送失败（不自动重发，免得重复）。"""
        return self.db.x("UPDATE quota_alerts SET status='failed', error='服务重启，没有发出去' WHERE status='sending'").rowcount

    # ---------- 页面 ----------
    def records(self, limit: int = 50) -> list[dict]:
        rows = self.db.q("SELECT * FROM quota_alerts WHERE status IN ('sending','sent','failed') ORDER BY created_at DESC, threshold DESC LIMIT ?", (limit,))
        return [{**dict(r), "name": self.directory.user_name(r["open_id"]), "status_name": STATUS.get(r["status"], r["status"])} for r in rows]

    # ---------- 卡片 ----------
    def card(self, open_id: str, used: float, threshold: int, now: datetime) -> dict:
        per = self.config.monthly_allowance
        quota = self.config.get("quota_alert")
        month = self.month_of(now)
        name = self.directory.user_name(open_id) if self.directory.user(open_id) else ""
        intro = f"您 **{month_name(month)}** 的飞书/豆包 AI 用量已经达到基础额度的 **{threshold}%**。"
        elements: list[dict] = [_md(f"**{name}**，{intro}" if name else intro)]
        # 额度那一排：用量超过 topped_at（默认 3000 点）就当作已经追加过，列出追加额度，已用比例按基础 + 追加合计算
        elements.append(allowance_stats(used, per, float(quota["extra"]), float(quota["topped_at"])))
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
        color = "orange" if threshold < 100 else "red" if threshold < 200 else "carmine"
        return {
            "config": {"wide_screen_mode": True},
            "header": {"template": color, "title": {"tag": "plain_text", "content": f"飞书/豆包 AI 额度提醒 ｜ 本月已用 {threshold}%"}},
            "elements": elements,
        }

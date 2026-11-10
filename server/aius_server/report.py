"""AI 用量周报：每周给每个人发一张飞书卡片——上周用了多少点、折算多少钱、用在哪几类额度、最常用的 5 个场景、今年累计。

发送用应用机器人（应用要开机器人能力和 im:message:send_as_bot 权限）。一周只自动推送一次；
「测试发送」只发给点按钮的人自己。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta

from .db import Database
from .directory import Directory
from .feishu import CST, FeishuError
from .labels import UNKNOWN_SCENARIO
from .prefs import Config

log = logging.getLogger("aius")

RANKS = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
WEEKDAYS = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}
# 卡片上「用在哪里」的分组：额度类型 → 显示名
FEATURE_GROUPS = [("🤖", "通用额度", (1,)), ("📝", "智能纪要", (2,)), ("✨", "飞书 Aily", (3,)), ("🫘", "豆包团队订阅", (4, 5, 6))]
SEND_GAP = 0.2  # 发消息接口限频，留出余量


def amount(value: float) -> str:
    return f"{value:,.2f}"


def short(day: str) -> str:
    return f"{day[5:7]}/{day[8:10]}"


class WeeklyReport:
    def __init__(self, db: Database, feishu, directory: Directory, config: Config, public_url: str, sleep=time.sleep):
        self.db = db
        self.feishu = feishu
        self.directory = directory
        self.config = config
        self.public_url = public_url
        self.sleep = sleep
        self._lock = threading.Lock()

    def period(self, now: date | None = None) -> dict:
        """上一个完整的自然周（周一到周日），以及累计用量从哪天算起。"""
        now = now or datetime.now(CST).date()
        monday = now - timedelta(days=now.weekday())
        since = self.config.get("report")["since"] or f"{now.year}-01-01"
        return {"start": (monday - timedelta(days=7)).isoformat(), "end": (monday - timedelta(days=1)).isoformat(), "since": since}

    def reports(self, period: dict, only: list[str] | None = None) -> dict[str, dict]:
        """每个人的周报数据：open_id → {week, total, features, scenarios}。只含这段时间里有过用量的人。"""
        where, params = "subject='member'", []
        if only is not None:
            if not only:
                return {}
            where += f" AND entity_id IN ({','.join('?' * len(only))})"
            params = list(only)
        span = (period["start"], period["end"])
        out: dict[str, dict] = {}

        def of(open_id: str) -> dict:
            return out.setdefault(open_id, {"week": 0.0, "total": 0.0, "features": {}, "scenarios": []})

        # 累计：从累计起始日到上周日
        for r in self.db.q(
            f"SELECT entity_id, SUM(used_quota) AS v FROM usage_logs WHERE {where} AND usage_date BETWEEN ? AND ? GROUP BY entity_id",
            [*params, period["since"], period["end"]],
        ):
            of(r["entity_id"])["total"] = r["v"]
        for r in self.db.q(f"SELECT entity_id, feature_key, SUM(used_quota) AS v FROM usage_logs WHERE {where} AND usage_date BETWEEN ? AND ? GROUP BY entity_id, feature_key", [*params, *span]):
            rep = of(r["entity_id"])
            rep["week"] += r["v"]
            rep["features"][r["feature_key"]] = rep["features"].get(r["feature_key"], 0.0) + r["v"]
        for r in self.db.q(
            f"SELECT entity_id, scenario, SUM(used_quota) AS v FROM usage_logs WHERE {where} AND usage_date BETWEEN ? AND ? GROUP BY entity_id, scenario ORDER BY v DESC",
            [*params, *span],
        ):
            scenes = of(r["entity_id"])["scenarios"]
            if len(scenes) < 5:
                scenes.append((r["scenario"] or UNKNOWN_SCENARIO, r["v"]))
        return out

    def card(self, name: str, rep: dict, period: dict) -> dict:
        price = self.config.point_price

        def cost(points: float) -> str:
            return f"¥{amount(points * price)}"

        head = f"Hi，**{name or '你'}**\n这是 {period['start']} ~ {period['end']} 的 AI 用量周报"
        year = f"📈 累计（自 {period['since']}）：**{amount(rep['total'])} 点** ｜ {cost(rep['total'])}"
        if rep["week"] <= 0:
            elements = [{"tag": "markdown", "content": f"{head}\n\n🌙 上周没有 AI 用量记录"}, {"tag": "hr"}, {"tag": "markdown", "content": year}]
        else:
            used = [(icon, label, sum(rep["features"].get(k, 0.0) for k in keys)) for icon, label, keys in FEATURE_GROUPS]
            where = "\n".join(f"{icon} {label}：{amount(v)} 点 ｜ {cost(v)}" for icon, label, v in used if v > 0 or label == "通用额度")
            scenes = "\n".join(f"{RANKS[i]} {s} · {amount(v)} 点" for i, (s, v) in enumerate(rep["scenarios"][:5])) or "暂无场景记录"
            elements = [
                {"tag": "markdown", "content": f"{head}\n\n⚡ 上周消耗 **{amount(rep['week'])} 点**，折算 **{cost(rep['week'])}**"},
                {"tag": "markdown", "content": f"**用在哪里**\n{where}"},
                {"tag": "markdown", "content": f"**热门场景**\n{scenes}"},
                {"tag": "hr"},
                {"tag": "markdown", "content": year},
            ]
        elements.append({"tag": "action", "actions": [{"tag": "button", "type": "primary", "text": {"tag": "plain_text", "content": "查看我的用量明细"}, "url": f"{self.public_url}/self-usage"}]})
        return {
            "config": {"wide_screen_mode": True},
            "header": {"template": "violet", "title": {"tag": "plain_text", "content": f"AI 用量周报 ｜ {short(period['start'])} - {short(period['end'])}"}},
            "elements": elements,
        }

    def push(self, trigger: str, actor: str = "system", only: list[str] | None = None, now: date | None = None) -> dict:
        """发一轮周报。only 给了就只发给这些人（测试发送）；否则按推送范围发。返回这一轮的记录。"""
        with self._lock:
            period = self.period(now)
            reports = self.reports(period, only)
            if only is not None:
                targets = list(only)
            elif self.config.get("report")["scope"] == "all":
                targets = [u.open_id for u in self.directory.users()]
            else:
                targets = [oid for oid, rep in reports.items() if rep["week"] > 0]
            run_id = self.db.x(
                "INSERT INTO report_runs(week_start, week_end, trigger, actor, status, total, started_at) VALUES(?,?,?,?,'running',?,?)",
                (period["start"], period["end"], trigger, actor, len(targets), int(time.time())),
            ).lastrowid
            sent, errors = 0, []
            for i, open_id in enumerate(targets):
                rep = reports.get(open_id) or {"week": 0.0, "total": 0.0, "features": {}, "scenarios": []}
                try:
                    self.feishu.send_card(open_id, self.card(self.directory.user_name(open_id) if self.directory.user(open_id) else "", rep, period))
                    sent += 1
                except FeishuError as e:
                    errors.append(f"{self.directory.user_name(open_id)}：{e}")
                    if len(errors) >= 5 and sent == 0:  # 一开始就连着失败：多半是没开机器人或没权限，别再一个个试了
                        errors.append("连续失败，已停止这一轮推送")
                        break
                if i + 1 < len(targets):
                    self.sleep(SEND_GAP)
            failed = len(targets) - sent
            status = "success" if not failed else ("partial" if sent else "failed")
            self.db.x(
                "UPDATE report_runs SET status=?, sent=?, failed=?, error=?, finished_at=? WHERE id=?",
                (status, sent, failed, "\n".join(errors[:6]), int(time.time()), run_id),
            )
            log.info("周报推送 #%s（%s）：%s，共 %d 人，成功 %d，失败 %d", run_id, trigger, status, len(targets), sent, failed)
            return dict(self.db.one("SELECT * FROM report_runs WHERE id=?", (run_id,)))

    def due(self, now: datetime) -> bool:
        """到点了没有：开启了、今天是设定的周几、已经过了设定的整点、今天还没推送过。"""
        cfg = self.config.get("report")
        return bool(cfg["enabled"]) and now.isoweekday() == cfg["weekday"] and now.hour >= cfg["hour"] and cfg["last_push_date"] != now.date().isoformat()

    def tick(self, now: datetime) -> bool:
        if not self.due(now):
            return False
        self.config.mark("report", "last_push_date", now.date().isoformat())  # 先记下来：推送很慢，中途重启也不要再发一轮
        threading.Thread(target=self.push, args=("schedule",), kwargs={"now": now.date()}, name="weekly-report", daemon=True).start()
        return True

    def runs(self, limit: int = 20) -> list[dict]:
        out = []
        for r in self.db.q("SELECT * FROM report_runs ORDER BY id DESC LIMIT ?", (limit,)):
            row = dict(r)
            row["actor_name"] = "定时任务" if r["actor"] in ("", "system") else self.directory.user_name(r["actor"])
            out.append(row)
        return out

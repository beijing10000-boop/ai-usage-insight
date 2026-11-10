"""后台调度：每分钟看一次——到点的每日定时同步、同步任务巡检、到点的周报推送。

时间都按北京时间算。定时同步「到点」的意思是：已经过了设定的整点，而且今天还没自动同步过；
所以服务在那个整点刚好重启也不会漏掉当天的同步。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from .db import Database
from .feishu import CST
from .prefs import Config
from .report import WeeklyReport
from .sync import SyncEngine, SyncError

log = logging.getLogger("aius")


class Scheduler:
    def __init__(self, db: Database, config: Config, engine: SyncEngine, report: WeeklyReport, interval: int = 60):
        self.db = db
        self.config = config
        self.engine = engine
        self.report = report
        self.interval = interval
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now(CST)
        self.engine.watchdog()
        self._daily_sync(now)
        self.report.tick(now)

    def _daily_sync(self, now: datetime) -> None:
        cfg = self.config.get("sync")
        day = now.date().isoformat()
        if not cfg["enabled"] or now.hour < cfg["hour"] or cfg["last_auto_date"] == day:
            return
        start = (now.date() - timedelta(days=cfg["range_days"])).isoformat()
        end = (now.date() - timedelta(days=1)).isoformat()
        try:
            run_id = self.engine.start(start, end, "schedule", "system")
        except SyncError as e:
            # 正好有手动任务在跑：这一分钟先不记，下一分钟再试，等它跑完再同步
            log.info("定时同步暂缓：%s", e)
            return
        self.config.mark("sync", "last_auto_date", day)
        self.db.audit("system", "sync_start", f"定时同步 #{run_id}：{start} ~ {end}", via="system")
        log.info("定时同步已发起 #%s：%s ~ %s", run_id, start, end)

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception:
                log.exception("调度出错")

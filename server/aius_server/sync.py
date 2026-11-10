"""同步任务：把飞书的 AI 用量日志和统计详情按天拉到本地。

一个任务 = 一段日期。每天有四个数据集：人员日志、应用日志、人员统计详情、应用统计详情。
- 每个「日期 × 数据集」拉完整之后在一个事务里整批替换，所以同一天重复同步不会产生重复数据，
  拉到一半失败也不会留下半截数据。
- 一个数据集失败不影响其它的（任务结束时是「部分成功」）。
- 每完成一个数据集就记一次断点；任务中断（服务重启、心跳超时）后从断点续跑，最多自动续跑 3 次。
- 同一时间只跑一个任务：两个用量接口限频 1 次/秒，并行没有意义。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta

from .db import Database, dumps, loads
from .directory import Directory
from .feishu import CST, LEGACY_APP_ENTITY_TYPES, FeishuError
from .labels import APP_ENTITY_TYPES, DETAIL_FIELDS, clean_scenario

log = logging.getLogger("aius")

DATASETS = ("log_member", "log_app", "detail_member", "detail_app")
DATASET_LABELS = {
    "log_member": "人员用量日志",
    "log_app": "应用用量日志",
    "detail_member": "人员统计详情",
    "detail_app": "应用统计详情",
    "names": "名称解析",
}
STATUS_LABELS = {"queued": "等待中", "running": "同步中", "success": "成功", "partial": "部分成功", "failed": "失败", "stopped": "已停止"}
MAX_DAYS = 366  # 接口一次最多查 365 天；这里按天拉，只是不让一个任务大得离谱
MAX_PAGES = 20000


class SyncError(Exception):
    """给用户看的错误：日期不对、已有任务在跑。"""


class _Stopped(Exception):
    pass


def today() -> date:
    return datetime.now(CST).date()


def date_list(start: str, end: str) -> list[str]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    return [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class SyncEngine:
    def __init__(self, db: Database, feishu, directory: Directory, heartbeat_timeout: int = 1800, max_resume: int = 3):
        self.db = db
        self.feishu = feishu
        self.directory = directory
        self.heartbeat_timeout = heartbeat_timeout
        self.max_resume = max_resume
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._current: int | None = None

    # ---------- 发起 / 停止 ----------
    def start(self, start_date: str, end_date: str, trigger: str, actor: str) -> int:
        try:
            first, last = date.fromisoformat(start_date), date.fromisoformat(end_date)
        except (TypeError, ValueError):
            raise SyncError("日期要写成 2026-01-31 这样的格式") from None
        if first > last:
            raise SyncError("开始日期不能晚于结束日期")
        if last > today():
            raise SyncError("不能同步还没到的日期")
        if (last - first).days + 1 > MAX_DAYS:
            raise SyncError(f"一次最多同步 {MAX_DAYS} 天，请分几次同步")
        with self.db.tx() as c:
            busy = c.execute("SELECT id FROM sync_runs WHERE status IN ('queued','running') LIMIT 1").fetchone()
            if busy:
                raise SyncError(f"已经有同步任务在进行（#{busy['id']}），等它结束或者先停止它")
            run_id = c.execute(
                "INSERT INTO sync_runs(trigger, actor, start_date, end_date, status, created_at) VALUES(?,?,?,?,'queued',?)",
                (trigger, actor, first.isoformat(), last.isoformat(), int(time.time())),
            ).lastrowid
        self._wake.set()
        return run_id

    def stop(self, run_id: int) -> bool:
        """停止一个任务：还没开始的直接结束；正在跑的打上标记，拉完当前这一页就停。已经写入的数据保留。"""
        now = int(time.time())
        with self.db.tx() as c:
            row = c.execute("SELECT status FROM sync_runs WHERE id=?", (run_id,)).fetchone()
            if not row or row["status"] not in ("queued", "running"):
                return False
            if row["status"] == "queued":
                c.execute("UPDATE sync_runs SET status='stopped', finished_at=?, error='任务在开始前被停止' WHERE id=?", (now, run_id))
            else:
                c.execute("UPDATE sync_runs SET stop_requested=1 WHERE id=?", (run_id,))
        return True

    def active(self) -> sqlite3.Row | None:
        return self.db.one("SELECT * FROM sync_runs WHERE status IN ('queued','running') ORDER BY id LIMIT 1")

    # ---------- 执行 ----------
    def run_pending(self) -> int:
        """把排队的任务一个个跑完，返回跑了几个。后台线程调用；测试里直接调用。"""
        count = 0
        while True:
            row = self.db.one("SELECT * FROM sync_runs WHERE status='queued' ORDER BY id LIMIT 1")
            if not row:
                return count
            self._run(row)
            count += 1

    def _run(self, row: sqlite3.Row) -> None:
        run_id = row["id"]
        self._current = run_id
        now = int(time.time())
        self.db.x("UPDATE sync_runs SET status='running', started_at=COALESCE(started_at, ?), heartbeat_at=? WHERE id=?", (now, now, run_id))
        done: set[str] = set(loads(row["done_units"], []))
        stats = {**{d: 0 for d in DATASETS}, "pages": 0, **loads(row["stats"], {}), "failed": 0}
        days = date_list(row["start_date"], row["end_date"])
        units = [(day, dataset) for day in days for dataset in DATASETS]
        stats["units"] = len(units)
        errors: list[str] = []
        app_types = APP_ENTITY_TYPES
        current_day = today().isoformat()
        try:
            for day, dataset in units:
                key = f"{day}|{dataset}"
                if key in done:
                    continue
                self._check_stop(run_id)
                kind, subject = dataset.split("_")
                if kind == "detail" and day >= current_day:
                    done.add(key)  # 统计详情是 T+1，当天还没有
                    continue
                try:
                    items, pages, app_types = self._fetch(run_id, kind, subject, day, app_types)
                    rows = self._write(run_id, kind, subject, day, items)
                except FeishuError as e:
                    stats["failed"] += 1
                    errors.append(f"{day} {DATASET_LABELS[dataset]}：{e}")
                    self._step(run_id, day, dataset, 0, 0, "failed", str(e))
                    self._save(run_id, done, stats)
                    continue
                stats[dataset] += rows
                stats["pages"] += pages
                done.add(key)
                self._step(run_id, day, dataset, pages, rows, "ok", "")
                self._save(run_id, done, stats)
            self._resolve_names(run_id, row["start_date"], row["end_date"])
            finished = len(done) - sum(1 for k in done if k.split("|")[1].startswith("detail") and k.split("|")[0] >= current_day)
            status = "success" if not stats["failed"] else ("partial" if finished else "failed")
        except _Stopped:
            status = "stopped"
            errors.append("任务被手动停止，已经写入的数据保留")
        except Exception as e:  # 没想到的错误也要把任务收尾，不能留在「同步中」
            log.exception("同步任务 #%s 异常中止", run_id)
            status = "failed"
            errors.append(f"内部错误：{type(e).__name__} {e}")
        finally:
            self._current = None
        stats.update(self._coverage(row["start_date"], row["end_date"]))
        self.db.x(
            "UPDATE sync_runs SET status=?, finished_at=?, done_units=?, stats=?, error=? WHERE id=?",
            (status, int(time.time()), dumps(sorted(done)), dumps(stats), "\n".join(errors[:8]), run_id),
        )
        log.info("同步任务 #%s 结束：%s %s~%s %s", run_id, status, row["start_date"], row["end_date"], dumps(stats))

    def _check_stop(self, run_id: int) -> None:
        if self.db.val("SELECT stop_requested FROM sync_runs WHERE id=?", (run_id,), 0):
            raise _Stopped()

    def _fetch(self, run_id: int, kind: str, subject: str, day: str, app_types: tuple[int, ...]) -> tuple[list[dict], int, tuple[int, ...]]:
        items: list[dict] = []
        pages, token, seen = 0, "", set()
        while True:
            self._check_stop(run_id)
            try:
                page, token = self.feishu.usage_page(kind, subject, day, token, app_types)
            except FeishuError as e:
                # 应用维度第一页就被拒：可能是这家企业的灰度版本还不认新的应用类型，用早期的四种再试一次
                if subject == "app" and pages == 0 and not e.retryable and app_types != LEGACY_APP_ENTITY_TYPES:
                    app_types = LEGACY_APP_ENTITY_TYPES
                    continue
                raise
            items.extend(page)
            pages += 1
            self.db.x("UPDATE sync_runs SET heartbeat_at=? WHERE id=?", (int(time.time()), run_id))
            if not token:
                return items, pages, app_types
            if token in seen or pages >= MAX_PAGES:
                raise FeishuError("分页异常：飞书接口返回了重复的分页标记")
            seen.add(token)

    def _write(self, run_id: int, kind: str, subject: str, day: str, items: list[dict]) -> int:
        if kind == "log":
            rows = []
            for it in items:
                entity_id = str(it.get("entity_id") or "")
                if not entity_id:
                    continue
                ts = _int(it.get("time"))
                if ts and ts > 10**11:  # 文档写的是毫秒；早期版本返回过秒
                    ts //= 1000
                dept = str(it.get("department_id") or "")
                if subject == "member" and dept in ("", "0"):
                    dept = self.directory.user_dept(entity_id)  # 接口没给部门（缺权限）：按通讯录里的部门算
                notes = it.get("descriptions") or it.get("notes") or []
                if isinstance(notes, dict):
                    notes = [notes]
                rows.append(
                    (
                        day, subject, ts, _int(it.get("entity_type")), entity_id,
                        str(it.get("entity_name") or it.get("app_name") or it.get("name") or ""),
                        dept, _int(it.get("usage_type")), _int(it.get("feature_key")), _num(it.get("used_quota")),
                        clean_scenario(it.get("scenario_translate")), dumps(notes) if notes else "", run_id,
                    )
                )  # fmt: skip
            with self.db.tx() as c:
                c.execute("DELETE FROM usage_logs WHERE usage_date=? AND subject=?", (day, subject))
                c.executemany(
                    "INSERT INTO usage_logs(usage_date, subject, ts, entity_type, entity_id, entity_name, department_id, "
                    "usage_type, feature_key, used_quota, scenario, notes, run_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    rows,
                )
            return len(rows)
        rows = []
        for it in items:
            entity_id = str(it.get("entity_id") or "")
            if not entity_id:
                continue
            values = [next((_num(it[f]) for f in fields if f in it), 0.0) for _key, fields in sorted(DETAIL_FIELDS.items())]
            rows.append((day, subject, _int(it.get("entity_type")), entity_id, *values, run_id))
        with self.db.tx() as c:
            c.execute("DELETE FROM usage_details WHERE usage_date=? AND subject=?", (day, subject))
            c.executemany(
                "INSERT INTO usage_details(usage_date, subject, entity_type, entity_id, q1, q2, q3, q4, q5, q6, run_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        return len(rows)

    def _step(self, run_id: int, day: str, dataset: str, pages: int, rows: int, status: str, message: str) -> None:
        self.db.x(
            "INSERT INTO sync_steps(run_id, at, usage_date, dataset, pages, rows, status, message) VALUES(?,?,?,?,?,?,?,?)",
            (run_id, int(time.time()), day, dataset, pages, rows, status, message[:1000]),
        )

    def _save(self, run_id: int, done: set[str], stats: dict) -> None:
        self.db.x(
            "UPDATE sync_runs SET done_units=?, stats=?, heartbeat_at=? WHERE id=?", (dumps(sorted(done)), dumps(stats), int(time.time()), run_id)
        )

    def _coverage(self, start: str, end: str) -> dict:
        """这段日期里现在有多少人、多少部门、多少应用的用量（任务结果卡片上显示）。"""
        span = (start, end)
        return {
            "members": self.db.val("SELECT COUNT(DISTINCT entity_id) FROM usage_logs WHERE subject='member' AND usage_date BETWEEN ? AND ?", span, 0),
            "depts": self.db.val(
                "SELECT COUNT(DISTINCT department_id) FROM usage_logs WHERE subject='member' AND department_id<>'' AND usage_date BETWEEN ? AND ?", span, 0
            ),
            "apps": self.db.val("SELECT COUNT(DISTINCT entity_id) FROM usage_logs WHERE subject='app' AND usage_date BETWEEN ? AND ?", span, 0),
        }

    # ---------- 名称解析 ----------
    def _resolve_names(self, run_id: int, start: str, end: str) -> None:
        """通讯录快照里没有的人和部门、还没有名称的应用，查一次记下来。查不到不影响用量数据，页面上先显示 ID。"""
        span = (start, end)
        notes: list[str] = []
        failed = False

        def attempt(what: str, fn) -> None:
            nonlocal failed
            try:
                count = fn()
                if count:
                    notes.append(f"{what} {count} 个")
            except FeishuError as e:
                failed = True
                notes.append(f"{what}解析失败：{e}")

        def users() -> int:
            ids = [r[0] for r in self.db.q("SELECT DISTINCT entity_id FROM usage_logs WHERE subject='member' AND usage_date BETWEEN ? AND ?", span)]
            missing = self.directory.missing("user", ids)
            found = self.feishu.user_names(missing) if missing else {}
            self.directory.remember("user", found)
            return len(found)

        def depts() -> int:
            ids = [
                r[0]
                for r in self.db.q(
                    "SELECT DISTINCT department_id FROM usage_logs WHERE subject='member' AND department_id<>'' AND usage_date BETWEEN ? AND ?", span
                )
            ]
            missing = self.directory.missing("dept", ids)
            found = {k: v.name for k, v in self.feishu.dept_infos(missing).items()} if missing else {}
            self.directory.remember("dept", found)
            return len(found)

        def apps() -> int:
            rows = self.db.q(
                "SELECT entity_id, MAX(entity_name) AS name FROM usage_logs WHERE subject='app' AND usage_date BETWEEN ? AND ? GROUP BY entity_id", span
            )
            known = {r[0] for r in self.db.q("SELECT app_id FROM app_mapping")}
            missing = [r["entity_id"] for r in rows if r["entity_id"] not in known]
            if not missing:
                return 0
            found = {r["entity_id"]: r["name"] for r in rows if r["name"] and r["entity_id"] in missing}  # 接口自己带了名称的
            found.update(self.feishu.app_names([i for i in missing if i not in found]))
            return self.save_app_names(found, "api")

        def aily() -> int:
            return self.save_app_names(self.feishu.aily_app_names(), "aily")

        attempt("人员名称", users)
        attempt("部门名称", depts)
        attempt("应用名称", apps)
        attempt("Aily 应用名称", aily)
        if notes:
            self._step(run_id, end, "names", 0, 0, "failed" if failed else "ok", "；".join(notes))

    def save_app_names(self, names: dict[str, str], source: str, actor: str = "system") -> int:
        """写入应用名称映射。手工维护的优先级最高，自动解析的不覆盖它。"""
        now = int(time.time())
        count = 0
        for app_id, name in names.items():
            if not app_id or not name:
                continue
            cur = self.db.x(
                "INSERT INTO app_mapping(app_id, name, source, updated_by, updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(app_id) DO UPDATE SET name=excluded.name, source=excluded.source, updated_by=excluded.updated_by, "
                "updated_at=excluded.updated_at WHERE app_mapping.source<>'manual' OR excluded.source='manual'",
                (app_id, name.strip()[:200], source, actor, now),
            )
            count += cur.rowcount
        return count

    # ---------- 巡检 / 续跑 ----------
    def watchdog(self) -> None:
        """每分钟巡检一次：心跳超时的「同步中」任务从断点续跑。"""
        now = int(time.time())
        for row in self.db.q("SELECT id, heartbeat_at, resume_count FROM sync_runs WHERE status='running'"):
            if row["id"] == self._current:
                continue  # 本进程正在跑的：每次接口调用都有超时，不会永远卡住
            if now - (row["heartbeat_at"] or 0) >= self.heartbeat_timeout:
                self._requeue(row, "心跳超时")

    def recover(self) -> None:
        """进程启动时调用：上一个进程留下的「同步中」任务一定是被中断了的。"""
        for row in self.db.q("SELECT id, heartbeat_at, resume_count FROM sync_runs WHERE status='running'"):
            self._requeue(row, "服务重启")

    def _requeue(self, row: sqlite3.Row, why: str) -> None:
        if row["resume_count"] < self.max_resume:
            self.db.x("UPDATE sync_runs SET status='queued', resume_count=resume_count+1 WHERE id=?", (row["id"],))
            log.warning("同步任务 #%s %s，从断点续跑（第 %d 次）", row["id"], why, row["resume_count"] + 1)
            self._wake.set()
        else:
            self.db.x(
                "UPDATE sync_runs SET status='failed', finished_at=?, error=? WHERE id=?",
                (int(time.time()), f"{why}，自动续跑已经 {self.max_resume} 次仍未完成。已写入的数据保留，请重新发起同步。", row["id"]),
            )
            log.error("同步任务 #%s %s，续跑次数用完，标记为失败", row["id"], why)

    def start_worker(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="sync-worker", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        self.recover()
        while True:
            try:
                self.run_pending()
            except Exception:
                log.exception("同步线程出错")
            self._wake.wait(30)
            self._wake.clear()

    # ---------- 执行日志 ----------
    def runs(self, status: str = "", start: str = "", end: str = "", limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
        where, params = ["1=1"], []
        if status in STATUS_LABELS:
            where.append("status=?")
            params.append(status)
        if start:
            where.append("end_date>=?")
            params.append(start)
        if end:
            where.append("start_date<=?")
            params.append(end)
        clause = " AND ".join(where)
        total = self.db.val(f"SELECT COUNT(*) FROM sync_runs WHERE {clause}", params, 0)
        rows = self.db.q(f"SELECT * FROM sync_runs WHERE {clause} ORDER BY id DESC LIMIT ? OFFSET ?", [*params, limit, offset])
        return [self._view(r) for r in rows], total

    def run(self, run_id: int) -> dict | None:
        row = self.db.one("SELECT * FROM sync_runs WHERE id=?", (run_id,))
        if not row:
            return None
        out = self._view(row)
        out["steps"] = [
            {**dict(s), "dataset_label": DATASET_LABELS.get(s["dataset"], s["dataset"])}
            for s in self.db.q("SELECT * FROM sync_steps WHERE run_id=? ORDER BY id", (run_id,))
        ]
        return out

    def _view(self, row: sqlite3.Row) -> dict:
        out = dict(row)
        stats = loads(row["stats"], {})
        out["stats"] = {**{d: 0 for d in DATASETS}, "pages": 0, "failed": 0, "units": 0, "members": 0, "depts": 0, "apps": 0, **stats}
        out["done"] = len(loads(row["done_units"], []))
        out["status_label"] = STATUS_LABELS.get(row["status"], row["status"])
        out["total_rows"] = sum(out["stats"][d] for d in DATASETS)
        end_ts = row["finished_at"] or int(time.time())
        out["seconds"] = max(0, end_ts - row["started_at"]) if row["started_at"] else None
        out["actor_name"] = "定时任务" if row["actor"] == "system" else self.directory.user_name(row["actor"])
        return out

    def synced_dates(self, start: str, end: str) -> set[str]:
        """这段日期里哪些天已经有人员用量日志（发起同步前提醒用）。"""
        return {r[0] for r in self.db.q("SELECT DISTINCT usage_date FROM usage_logs WHERE usage_date BETWEEN ? AND ?", (start, end))}

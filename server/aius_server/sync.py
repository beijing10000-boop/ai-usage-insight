"""同步任务：把飞书的 AI 用量日志和统计详情按天拉到本地。

一个任务 = 一段日期。每天有四个数据集：人员日志、应用日志、人员统计详情、应用统计详情。
- 每个「日期 × 数据集」拉完整之后在一个事务里整批替换，所以同一天重复同步不会产生重复数据，
  拉到一半失败也不会留下半截数据。
- 一个数据集失败不影响其它的（任务结束时是「部分成功」）。
- 每完成一个数据集就记一次断点；任务中断（服务重启、心跳超时）后从断点续跑，最多自动续跑 3 次。
- 同一时间只跑一个任务：两个用量接口限频 1 次/秒，并行没有意义。

另有「当天增量同步」（live）：每隔几分钟只拉当天最近一段的用量日志，替换库里这一段，不建任务、不进执行日志。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta

from . import labels
from .db import AGG_INSERT, Database, dumps, loads
from .directory import Directory
from .feishu import CST, LEGACY_APP_ENTITY_TYPES, FeishuError, day_range
from .labels import AILY_ENTITY_TYPES, APP_ENTITY_TYPES, DETAIL_FIELDS, clean_scenario

log = logging.getLogger("aius")

DATASETS = ("log_member", "log_app", "detail_member", "detail_app")
DATASET_LABELS = {
    "log_member": "人员用量日志",
    "log_app": "应用用量日志",
    "detail_member": "人员统计详情",
    "detail_app": "应用统计详情",
    "names": "名称解析",
    "check": "数据核对",
}
SUBJECT_LABELS = {"member": "人员", "app": "应用"}
RECONCILE_TOLERANCE = 0.011  # 点数是两位小数：一分以内的差算浮点误差
STATUS_LABELS = {"queued": "等待中", "running": "同步中", "success": "成功", "partial": "部分成功", "failed": "失败", "stopped": "已停止"}
MAX_DAYS = 366  # 接口一次最多查 365 天；这里按天拉，只是不让一个任务大得离谱
# 飞书的用量日志只保留最近一年（实测 2026-11-10 能查到的最早一天是 2025-11-11）。比这个天数更早的日子，
# 飞书给的比库里少就保留库里的；留几天余量，免得卡在保留期边上、只剩半天数据的那一天被覆盖
RETENTION_GUARD_DAYS = 355
MAX_PAGES = 20000
# 当天增量同步每次往回多拉一段：飞书的日志要晚一分多钟才查得到（实测 2026-11），留足余量
LIVE_OVERLAP = 600
LIVE_CURSOR = "live_cursor"  # meta：{"until": 拉到的时间戳, "gen": 整天替换过几次未过完的日子}
LIVE_LAST = "live_last"  # meta：最近一次增量同步的结果（同步管理页显示）
LOG_INSERT = (
    "INSERT INTO usage_logs(usage_date, subject, ts, entity_type, entity_id, entity_name, department_id, "
    "usage_type, feature_key, used_quota, scenario, notes, run_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


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


def _rejects_entity_type(error: FeishuError) -> bool:
    """这个错误是不是在说「参数 / 实体类型不对」（而不是没权限、限频、网络问题）。"""
    text = str(error).lower()
    return not error.retryable and any(word in text for word in ("entity_type", "entity type", "invalid param", "param", "参数"))


def segments(start: int, end: int) -> list[tuple[str, int, int]]:
    """把一段时间按北京时间的自然日切开：[(日期, 起, 止)]，起止是秒级时间戳、两头都含。"""
    out = []
    t = start
    while t <= end:
        day = datetime.fromtimestamp(t, CST).date().isoformat()
        last = day_range(day)[1]
        out.append((day, t, min(end, last)))
        t = last + 1
    return out


def check_text(rows: list[dict]) -> str:
    """数据核对的结论（执行步骤里的说明）。rows 是 SyncEngine.reconcile() 返回的各行。"""
    days = {r["usage_date"] for r in rows}
    bad = [r for r in rows if r["mismatched"]]
    if not bad:
        return f"{len(days)} 天的用量日志和飞书统计详情逐人核对一致（共 {sum(r['log_points'] for r in rows):,.2f} 点）"
    parts = [
        f"{r['usage_date']} {r['subject_label']}有 {r['mismatched']} {'人' if r['subject'] == 'member' else '个应用'}对不上"
        f"（日志 {r['log_points']:,.2f} 点，统计详情 {r['detail_points']:,.2f} 点）"
        for r in bad[:3]
    ]
    ok_days = len(days - {r["usage_date"] for r in bad})
    return "；".join(parts) + (f" 等 {len(bad)} 处" if len(bad) > 3 else "") + (f"；其余 {ok_days} 天一致" if ok_days else "")


def _meta(c: sqlite3.Connection, key: str) -> str | None:
    row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _set_meta(c: sqlite3.Connection, key: str, value: str) -> None:
    c.execute("INSERT INTO meta(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def _entity(item: dict, subject: str) -> tuple[str, str]:
    """(实体 ID, 内置名称)。飞书项目、CodeM 沙箱这类「整个产品算一个应用」的类型没有自己的应用 ID
    （接口返回空或 -1），给它们一个按类型定的固定 ID，免得被丢掉或者几类混成一个。"""
    entity_id = str(item.get("entity_id") or "")
    etype = _int(item.get("entity_type"))
    if subject == "app" and entity_id in ("", "-1") and etype is not None:
        return f"builtin_{etype}", labels.entity_type(etype)
    return entity_id, ""


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
        self._live_lock = threading.Lock()

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
        now = int(time.time())
        # 只接「等待中」的任务：刚好在这一刻被人点了停止的，就不跑了
        claimed = self.db.x(
            "UPDATE sync_runs SET status='running', started_at=COALESCE(started_at, ?), heartbeat_at=? WHERE id=? AND status='queued'", (now, now, run_id)
        )
        if not claimed.rowcount:
            return
        self._current = run_id
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
                    collected, pages, app_types, fetched_at = self._fetch(run_id, kind, subject, day, app_types)
                    kept = self._keep_existing(kind, subject, day, len(collected), app_types)
                    if kept:  # 飞书这次给的比库里已有的少（过了保留期、或者接口返回空）：不覆盖
                        stats["pages"] += pages
                        done.add(key)
                        self._step(run_id, day, dataset, pages, 0, "kept", kept)
                        self._save(run_id, done, stats)
                        continue
                    rows = self._write(kind, subject, day, collected, app_types, fetched_at)
                except FeishuError as e:
                    stats["failed"] += 1
                    errors.append(f"{day} {DATASET_LABELS[dataset]}：{e}")
                    self._step(run_id, day, dataset, 0, 0, "failed", str(e))
                    self._save(run_id, done, stats)
                    continue
                stats[dataset] += rows
                stats["pages"] += pages
                done.add(key)
                legacy = subject == "app" and app_types == LEGACY_APP_ENTITY_TYPES
                self._step(run_id, day, dataset, pages, rows, "ok", "飞书接口不接受「飞书项目」「CodeM 沙箱应用」这两类，只同步了其余四类应用" if legacy else "")
                self._save(run_id, done, stats)
            self._resolve_names(run_id, row["start_date"], row["end_date"])
            self._check(run_id, row["start_date"], row["end_date"])
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
            "UPDATE sync_runs SET status=?, finished_at=?, done_units=?, stats=?, error=?, progress='' WHERE id=?",
            (status, int(time.time()), dumps(sorted(done)), dumps(stats), "\n".join(errors[:8]), run_id),
        )
        log.info("同步任务 #%s 结束：%s %s~%s %s", run_id, status, row["start_date"], row["end_date"], dumps(stats))

    def _check_stop(self, run_id: int) -> None:
        if self.db.val("SELECT stop_requested FROM sync_runs WHERE id=?", (run_id,), 0):
            raise _Stopped()

    def _fetch(
        self, run_id: int, kind: str, subject: str, day: str, app_types: tuple[int, ...], window: tuple[int, int] | None = None
    ) -> tuple[list[tuple], int, tuple[int, ...], int]:
        """把一个「日期 × 数据集」（或这一天里的一段 window）的所有分页拉下来，每一页随到随转换成要写库的行
        （大企业一天几万条，只留转换后的行比留接口原始的 JSON 省得多）。每拉一页记一次进度，页面上能看到拉到哪儿了。
        run_id 为 0 是当天增量同步：不检查停止、不记进度。返回 (行, 页数, 应用类型, 开始拉的时间)。"""
        rows: list[tuple] = []
        pages, token, seen = 0, "", set()
        dataset = f"{kind}_{subject}"
        fetched_at = int(time.time())  # 第一页是最新的：这一刻之后产生的用量不在这次拉到的数据里
        while True:
            if run_id:
                self._check_stop(run_id)
            try:
                page, token = self.feishu.usage_page(kind, subject, day, token, app_types, window=window)
            except FeishuError as e:
                # 应用维度第一页因为参数被拒：可能是这家企业的灰度版本还不认新的应用类型，用早期的四种再试一次。
                # 只在错误说的是参数 / 实体类型时才这么做——别的错误（没权限、token 失效……）照常算失败，
                # 否则一次偶发错误就会让这一天少同步两类应用。
                if subject == "app" and pages == 0 and app_types != LEGACY_APP_ENTITY_TYPES and _rejects_entity_type(e):
                    app_types = LEGACY_APP_ENTITY_TYPES
                    continue
                raise
            converted = self._rows(kind, subject, day, page, run_id)
            rows.extend(converted)
            pages += 1
            if run_id:
                # 用量日志是从当天最晚往最早给的：这一页最早的那条记录的时间，就是「拉到当天几点了」
                stamps = [r[2] for r in converted if kind == "log" and r[2]]
                reached = datetime.fromtimestamp(min(stamps), CST).strftime("%H:%M") if stamps else ""
                progress = {"day": day, "dataset": dataset, "label": DATASET_LABELS[dataset], "pages": pages, "rows": len(rows), "reached": reached}
                self.db.x("UPDATE sync_runs SET heartbeat_at=?, progress=? WHERE id=?", (int(time.time()), dumps(progress), run_id))
            if not token:
                return rows, pages, app_types, fetched_at
            if token in seen or pages >= MAX_PAGES:
                raise FeishuError("分页异常：飞书接口返回了重复的分页标记")
            seen.add(token)

    def _rows(self, kind: str, subject: str, day: str, items: list[dict], run_id: int) -> list[tuple]:
        """接口返回的一页记录 → 要写进 usage_logs / usage_details 的行。"""
        rows: list[tuple] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            entity_id, builtin_name = _entity(it, subject)
            if not entity_id:
                continue
            if kind != "log":
                values = [next((_num(it[f]) for f in fields if f in it), 0.0) for _key, fields in sorted(DETAIL_FIELDS.items())]
                rows.append((day, subject, _int(it.get("entity_type")), entity_id, *values, run_id))
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
                    str(it.get("entity_name") or it.get("app_name") or it.get("name") or builtin_name),
                    dept, _int(it.get("usage_type")), _int(it.get("feature_key")), _num(it.get("used_quota")),
                    clean_scenario(it.get("scenario_translate")), dumps(notes) if notes else "", run_id,
                )
            )  # fmt: skip
        return rows

    @staticmethod
    def _scope(subject: str, day: str, app_types: tuple[int, ...]) -> tuple[str, list]:
        """整批替换的范围。应用维度退回早期四类时，只替换这四类（和没有类型的），别把以前同步到的另外两类删掉。"""
        scope, params = "usage_date=? AND subject=?", [day, subject]
        if subject == "app" and app_types != APP_ENTITY_TYPES:
            scope += f" AND (entity_type IS NULL OR entity_type IN ({','.join('?' * len(app_types))}))"
            params += list(app_types)
        return scope, params

    def _keep_existing(self, kind: str, subject: str, day: str, fetched: int, app_types: tuple[int, ...]) -> str:
        """这次拉到的数据要不要放弃、保留库里已有的。要保留时返回写进执行日志的原因，否则返回空串。

        - 飞书的用量日志只保留最近一年，一年前的日子再同步会返回空（或者只剩一部分）。库里早先同步下来的
          比飞书现在给的多，就保留库里的——那是已经查不到的数据，整批替换就等于把它删了。
        - 任何日子，库里有数据而飞书这次一条都没给，也不覆盖：用量记录不会凭空消失，更可能是接口那边的问题。
        库里本来没有、或者飞书给的不比库里少的，照常整批替换。"""
        scope, params = self._scope(subject, day, app_types)
        table = "usage_logs" if kind == "log" else "usage_details"
        existing = self.db.val(f"SELECT COUNT(*) FROM {table} WHERE {scope}", params, 0)
        if not existing or fetched >= existing:
            return ""
        label = DATASET_LABELS[f"{kind}_{subject}"]
        aged = date.fromisoformat(day) < today() - timedelta(days=RETENTION_GUARD_DAYS)
        if aged:
            note = "飞书只保留最近一年的用量日志" if kind == "log" else "这一天已经超出飞书的保留期限"
            return f"{note}，这次只返回了 {fetched:,} 条{label}，库里已有 {existing:,} 条：保留原数据，没有覆盖"
        if fetched == 0:
            return f"飞书这次没有返回这一天的{label}，库里已有 {existing:,} 条：保留原数据，没有覆盖。如果确认飞书那边的数据确实没有了，请联系管理员处理"
        return ""

    def _write(
        self, kind: str, subject: str, day: str, rows: list[tuple], app_types: tuple[int, ...] = APP_ENTITY_TYPES, fetched_at: int | None = None
    ) -> int:
        scope, scope_params = self._scope(subject, day, app_types)
        if kind == "log":
            with self.db.tx() as c:
                c.execute(f"DELETE FROM usage_logs WHERE {scope}", scope_params)
                c.executemany(LOG_INSERT, rows)
                # 汇总表跟着一起换（同一个事务），统计页面看到的总是和明细一致
                c.execute(f"DELETE FROM usage_agg WHERE {scope}", scope_params)
                c.execute(AGG_INSERT.format(where=scope), scope_params)
                if fetched_at is not None and fetched_at < day_range(day)[1]:
                    # 这一天还没过完就整天替换了：fetched_at 之后的用量（增量同步可能已经拉到过）刚被删掉，
                    # 把增量同步的游标退回到 fetched_at，下一次从那儿重新拉；gen 告诉正在跑的那次增量同步「别往前推游标」
                    cur = loads(_meta(c, LIVE_CURSOR), {})
                    _set_meta(c, LIVE_CURSOR, dumps({"until": min(cur.get("until", 0), fetched_at), "gen": cur.get("gen", 0) + 1}))
            return len(rows)
        with self.db.tx() as c:
            c.execute(f"DELETE FROM usage_details WHERE {scope}", scope_params)
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
            "members": self.db.val("SELECT COUNT(DISTINCT entity_id) FROM usage_agg WHERE subject='member' AND usage_date BETWEEN ? AND ?", span, 0),
            "depts": self.db.val(
                "SELECT COUNT(DISTINCT department_id) FROM usage_agg WHERE subject='member' AND department_id<>'' AND usage_date BETWEEN ? AND ?", span, 0
            ),
            "apps": self.db.val("SELECT COUNT(DISTINCT entity_id) FROM usage_agg WHERE subject='app' AND usage_date BETWEEN ? AND ?", span, 0),
        }

    # ---------- 当天增量同步 ----------
    def live_until(self) -> int:
        """当天增量同步拉到了什么时候（秒级时间戳）：这之前的用量日志都在库里了。0 = 还没拉过。"""
        return int(loads(self.db.meta(LIVE_CURSOR), {}).get("until", 0))

    def live_state(self) -> dict:
        """最近一次增量同步的结果：at 开始时间、rows 条数、pages 页数、seconds 用时、error 出错原因。"""
        return {"at": 0, "rows": 0, "pages": 0, "seconds": 0, "error": "", **loads(self.db.meta(LIVE_LAST), {}), "until": self.live_until()}

    def live_running(self) -> bool:
        return self._live_lock.locked()

    def live(self, now: int | None = None) -> dict:
        """拉上次拉到的时间（往回多拉 LIVE_OVERLAP）到现在的人员和应用用量日志，替换库里这一段，再重算这一天的汇总。
        平时一次几百到一千来条、十来秒；第一次（或者停了一天以上）从今天 0 点拉起。跨过 0 点时前一天的尾巴也一起补上。

        可以和整天同步同时进行：整天替换一个还没过完的日子，会把游标退回到它开始拉的时刻（见 _write）。
        游标只在本次拉的过程中没有发生这种替换时才往前推，所以「游标之前的数据都在库里」始终成立。"""
        if not self._live_lock.acquire(blocking=False):
            return {**self.live_state(), "skipped": True}
        try:
            return self._live(int(now or time.time()))
        finally:
            self._live_lock.release()

    def _live(self, now: int) -> dict:
        began = time.monotonic()
        cursor = loads(self.db.meta(LIVE_CURSOR), {})
        base, gen = int(cursor.get("until", 0)), int(cursor.get("gen", 0))
        day_start = day_range(datetime.fromtimestamp(now, CST).date().isoformat())[0]
        start = max(base - LIVE_OVERLAP, day_start - LIVE_OVERLAP) if base else day_start
        result = {"at": now, "start": start, "rows": 0, "pages": 0, "seconds": 0, "error": ""}
        try:
            for day, a, b in segments(start, now):
                for subject in ("member", "app"):
                    rows, pages = self._live_slice(day, subject, a, b)
                    result["rows"] += rows
                    result["pages"] += pages
        except FeishuError as e:
            result["error"] = str(e)
            log.warning("当天增量同步失败：%s", e)
        except Exception as e:  # 没想到的错误也记下来，下一轮照常再试
            log.exception("当天增量同步异常")
            result["error"] = f"内部错误：{type(e).__name__} {e}"
        else:
            with self.db.tx() as c:
                cur = loads(_meta(c, LIVE_CURSOR), {})
                if int(cur.get("gen", 0)) == gen:
                    _set_meta(c, LIVE_CURSOR, dumps({"until": now, "gen": gen}))
        result["seconds"] = round(time.monotonic() - began)
        self.db.set_meta(LIVE_LAST, dumps(result))
        return {**result, "until": self.live_until()}

    def _live_slice(self, day: str, subject: str, a: int, b: int) -> tuple[int, int]:
        """一天里 [a, b] 这一段：拉下来、删掉库里这一段、写进去、重算这一天的汇总，在一个事务里。返回 (写入条数, 页数)。"""
        collected, pages, app_types, _ = self._fetch(0, "log", subject, day, APP_ENTITY_TYPES, window=(a, b))
        rows = [r for r in collected if r[2] and a <= r[2] <= b]  # 只认这一段里的，删和写的范围一致才不会重复
        scope, params = self._scope(subject, day, app_types)
        piece, piece_params = f"{scope} AND ts BETWEEN ? AND ?", [*params, a, b]
        if not rows:
            # 这一段飞书一条都没给：库里本来没有就不用动；库里有的话多半是接口的问题，不覆盖（和整天同步的规则一样）
            return 0, pages
        with self.db.tx() as c:
            c.execute(f"DELETE FROM usage_logs WHERE {piece}", piece_params)
            c.executemany(LOG_INSERT, rows)
            c.execute(f"DELETE FROM usage_agg WHERE {scope}", params)
            c.execute(AGG_INSERT.format(where=scope), params)
        return len(rows), pages

    # ---------- 数据核对 ----------
    def reconcile(self, start: str, end: str) -> list[dict]:
        """核对 [start, end] 的每一天：每个人（应用）的用量日志合计，是否等于飞书统计详情里他这一天的合计。
        两份数据是飞书分别给的——日志是一条条的消耗记录（准实时），统计详情是飞书自己按人按天汇总的（T+1）——
        对得上，说明同步下来的日志没漏、没重复。结果写进 reconcile 表（同一天再核对就覆盖），返回这次核对的各行。

        - 当天不核对：统计详情第二天才有。
        - 一条日志都没有、又早于日志保留期的日子不核对：飞书的日志只保留一年，统计详情保留得更久。"""
        end = min(end, (today() - timedelta(days=1)).isoformat())
        if start > end:
            return []
        span = (start, end)
        logs: dict[tuple[str, str], dict[str, float]] = {}
        details: dict[tuple[str, str], dict[str, float]] = {}
        for r in self.db.q("SELECT usage_date, subject, entity_id, SUM(amount) FROM usage_agg WHERE usage_date BETWEEN ? AND ? GROUP BY 1, 2, 3", span):
            logs.setdefault((r[0], r[1]), {})[r[2]] = r[3]
        for r in self.db.q(
            "SELECT usage_date, subject, entity_id, SUM(q1+q2+q3+q4+q5+q6) FROM usage_details WHERE usage_date BETWEEN ? AND ? GROUP BY 1, 2, 3", span
        ):
            details.setdefault((r[0], r[1]), {})[r[2]] = r[3]
        log_days = {day for day, _subject in logs}
        oldest = (today() - timedelta(days=RETENTION_GUARD_DAYS)).isoformat()
        now = int(time.time())
        rows = []
        for day, subject in sorted(set(logs) | set(details)):
            if day not in log_days and day < oldest:
                continue
            lg, dt = logs.get((day, subject), {}), details.get((day, subject), {})
            gaps = sorted(((abs(lg.get(i, 0.0) - dt.get(i, 0.0)), i) for i in set(lg) | set(dt)), reverse=True)
            bad = [{"id": i, "log": round(lg.get(i, 0.0), 2), "detail": round(dt.get(i, 0.0), 2)} for gap, i in gaps if gap > RECONCILE_TOLERANCE]
            rows.append((day, subject, round(sum(lg.values()), 2), round(sum(dt.values()), 2), len(lg), len(dt), len(bad), dumps(bad[:5]), now))
        with self.db.tx() as c:
            c.execute("DELETE FROM reconcile WHERE usage_date BETWEEN ? AND ?", span)
            c.executemany(
                "INSERT INTO reconcile(usage_date, subject, log_points, detail_points, log_count, detail_count, mismatched, samples, checked_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                rows,
            )
        return [self._check_view(r) for r in self.db.q("SELECT * FROM reconcile WHERE usage_date BETWEEN ? AND ? ORDER BY usage_date DESC, subject DESC", span)]

    def checks(self, days: int = 30) -> dict:
        """同步管理页的「数据核对」：最近 days 天（到昨天）的核对结果。"""
        since = (today() - timedelta(days=days)).isoformat()
        rows = [self._check_view(r) for r in self.db.q("SELECT * FROM reconcile WHERE usage_date>=? ORDER BY usage_date DESC, subject DESC", (since,))]
        return {
            "days": days,
            "checked_at": self.db.val("SELECT MAX(checked_at) FROM reconcile"),
            "checked_days": len({r["usage_date"] for r in rows}),
            "diff": [r for r in rows if r["mismatched"]],
            "yesterday": (today() - timedelta(days=1)).isoformat(),
        }

    def _check_view(self, row: sqlite3.Row) -> dict:
        out = dict(row)
        out["subject_label"] = SUBJECT_LABELS.get(row["subject"], row["subject"])
        out["gap"] = round(row["log_points"] - row["detail_points"], 2)
        names = self._app_name if row["subject"] == "app" else self.directory.user_name
        out["samples"] = [{**s, "name": names(s["id"])} for s in loads(row["samples"], [])]
        return out

    def _app_name(self, app_id: str) -> str:
        return self.db.val("SELECT name FROM app_mapping WHERE app_id=?", (app_id,)) or app_id

    def _check(self, run_id: int, start: str, end: str) -> None:
        """同步任务的最后一步：核对这段日期，结果记一个执行步骤。核对出错不影响任务本身。"""
        try:
            checked = self.reconcile(start, end)
        except Exception as e:  # 核对是附带的检查，不能让它把同步任务弄成失败
            log.exception("同步任务 #%s 数据核对出错", run_id)
            self._step(run_id, end, "check", 0, 0, "failed", f"核对出错：{type(e).__name__} {e}")
            return
        if checked:
            self._step(run_id, checked[0]["usage_date"], "check", 0, 0, "diff" if any(r["mismatched"] for r in checked) else "ok", check_text(checked))

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
            ids = [r[0] for r in self.db.q("SELECT DISTINCT entity_id FROM usage_agg WHERE subject='member' AND usage_date BETWEEN ? AND ?", span)]
            missing = self.directory.missing("user", ids)
            found = self.feishu.user_names(missing) if missing else {}
            self.directory.remember("user", found)
            return len(found)

        def depts() -> int:
            ids = [
                r[0]
                for r in self.db.q(
                    "SELECT DISTINCT department_id FROM usage_agg WHERE subject='member' AND department_id<>'' AND usage_date BETWEEN ? AND ?", span
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

        def unnamed_apps(types: tuple[int, ...] | None = None) -> dict[str, str]:
            """用量数据里出现过（不限这次同步的日期）、还没有名称的应用 → 最早被用的那天。"""
            known = {r[0] for r in self.db.q("SELECT app_id FROM app_mapping")}
            where = f" AND entity_type IN ({','.join(str(t) for t in types)})" if types else ""
            rows = self.db.q(f"SELECT entity_id, MIN(usage_date) AS first FROM usage_agg WHERE subject='app'{where} GROUP BY entity_id")
            return {r["entity_id"]: r["first"] for r in rows if r["entity_id"] not in known}

        def aily() -> int:
            # 很久以前用过、最近没人用的智能体，最近一段的应用统计里没有：告诉接口还缺哪些、最早从哪天查起
            missing = unnamed_apps(AILY_ENTITY_TYPES)
            since = day_range(min(missing.values()))[0] if missing else None
            return self.save_app_names(self.feishu.aily_app_names(since=since, want=set(missing)), "aily")

        def builtin() -> int:
            missing = unnamed_apps()
            return self.save_app_names({i: labels.KNOWN_APP_NAMES[i] for i in missing if i in labels.KNOWN_APP_NAMES}, "builtin")

        attempt("人员名称", users)
        attempt("部门名称", depts)
        attempt("应用名称", apps)
        attempt("Aily 应用名称", aily)
        attempt("内置应用名称", builtin)
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
        try:  # 刚升级到有数据核对的版本：把已有的数据核对一遍，同步管理页上马上能看到结果
            first = self.db.val("SELECT MIN(usage_date) FROM usage_agg")
            if first and not self.db.val("SELECT 1 FROM reconcile LIMIT 1"):
                self.reconcile(first, today().isoformat())
        except Exception:
            log.exception("核对已有数据出错")
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
            # 最新执行的在最前面：一年的同步有上千个步骤，正序排的话最近的进展要翻到最底下才看得到
            for s in self.db.q("SELECT * FROM sync_steps WHERE run_id=? ORDER BY id DESC", (run_id,))
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
        out["progress"] = loads(row["progress"], None) if row["status"] == "running" else None
        return out

    def synced_dates(self, start: str, end: str) -> set[str]:
        """这段日期里哪些天已经有人员用量日志（发起同步前提醒用）。"""
        return {r[0] for r in self.db.q("SELECT DISTINCT usage_date FROM usage_agg WHERE usage_date BETWEEN ? AND ?", (start, end))}

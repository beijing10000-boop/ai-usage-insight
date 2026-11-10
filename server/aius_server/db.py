"""SQLite 存储。这里只有表结构和通用的读写方法；各功能自己的 SQL 写在各自的模块里（sync.py、analytics.py……）。

每个线程一个连接（uvicorn 的线程池 + 后台同步线程），WAL 模式下读写互不阻塞。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA_VERSION = 1

SCHEMA = """
-- 登录过的用户
CREATE TABLE IF NOT EXISTS users (
    open_id    TEXT PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    email      TEXT NOT NULL DEFAULT '',
    updated_at INTEGER NOT NULL
);
-- 角色：operations_admin（运营管理员）。没有记录的人是 regular_user（普通用户）
CREATE TABLE IF NOT EXISTS roles (
    open_id    TEXT PRIMARY KEY,
    role       TEXT NOT NULL,
    granted_by TEXT NOT NULL DEFAULT '',
    granted_at INTEGER NOT NULL
);
-- 通讯录快照（后台定时整表替换）：把用量数据里的 ID 显示成姓名和部门
CREATE TABLE IF NOT EXISTS directory (
    open_id  TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    en_name  TEXT NOT NULL DEFAULT '',
    email    TEXT NOT NULL DEFAULT '',
    dept_ids TEXT NOT NULL DEFAULT '[]'    -- JSON：直属部门（department_id）
);
-- 部门用 department_id（不是 open_department_id）：AI 用量接口返回的就是它
CREATE TABLE IF NOT EXISTS departments (
    id        TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    parent_id TEXT NOT NULL DEFAULT '0',   -- 顶级部门的上级是 '0'
    leader_id TEXT NOT NULL DEFAULT '',    -- 部门负责人的 open_id
    headcount INTEGER NOT NULL DEFAULT 0   -- 含子部门的在职人数（去重）
);
-- 通讯录快照里没有的名称（离职的人、已删除的部门），按需查一次后记下来。kind: user | dept
CREATE TABLE IF NOT EXISTS name_cache (
    kind       TEXT NOT NULL,
    id         TEXT NOT NULL,
    name       TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (kind, id)
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- 页面上能改的运营配置（定时同步、单价、周报推送），value 是 JSON
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_by TEXT NOT NULL DEFAULT '',
    updated_at INTEGER NOT NULL
);
-- 每个人自己保存的页面选项（部门圈选）
CREATE TABLE IF NOT EXISTS user_prefs (
    open_id TEXT NOT NULL,
    key     TEXT NOT NULL,
    value   TEXT NOT NULL,
    PRIMARY KEY (open_id, key)
);
-- 同步任务。status: queued | running | success | partial | failed | stopped
CREATE TABLE IF NOT EXISTS sync_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger        TEXT NOT NULL,              -- manual | schedule
    actor          TEXT NOT NULL,              -- 触发人 open_id；定时任务是 'system'
    start_date     TEXT NOT NULL,
    end_date       TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'queued',
    created_at     INTEGER NOT NULL,
    started_at     INTEGER,
    finished_at    INTEGER,
    heartbeat_at   INTEGER,
    done_units     TEXT NOT NULL DEFAULT '[]', -- 断点：已经完成的「日期|数据集」
    stats          TEXT NOT NULL DEFAULT '{}', -- JSON：各数据集写入条数、接口分页数、失败数
    error          TEXT NOT NULL DEFAULT '',
    resume_count   INTEGER NOT NULL DEFAULT 0,
    stop_requested INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS sync_runs_status ON sync_runs(status);
-- 同步任务里每个「日期 × 数据集」的执行情况，执行日志详情用。
-- dataset: log_member | log_app | detail_member | detail_app | names
CREATE TABLE IF NOT EXISTS sync_steps (
    id         INTEGER PRIMARY KEY,
    run_id     INTEGER NOT NULL,
    at         INTEGER NOT NULL,
    usage_date TEXT NOT NULL,
    dataset    TEXT NOT NULL,
    pages      INTEGER NOT NULL DEFAULT 0,
    rows       INTEGER NOT NULL DEFAULT 0,
    status     TEXT NOT NULL,                  -- ok | failed
    message    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS sync_steps_run ON sync_steps(run_id, id);
-- AI 用量日志（admin/v1/ai_usage_log/query）：一行 = 一次使用记录。
-- 同一个「日期 × subject」重新同步时整批替换，所以重复同步不会产生重复数据
CREATE TABLE IF NOT EXISTS usage_logs (
    id            INTEGER PRIMARY KEY,
    usage_date    TEXT NOT NULL,               -- YYYY-MM-DD（北京时间的自然日）
    subject       TEXT NOT NULL,               -- member | app
    ts            INTEGER,                     -- 使用时间（秒）
    entity_type   INTEGER,                     -- 见 labels.ENTITY_TYPES（2 用户、5 自建应用、6 Aily 工作流……）
    entity_id     TEXT NOT NULL,               -- 人员是 open_id，应用是应用 ID
    entity_name   TEXT NOT NULL DEFAULT '',    -- 接口返回的名称（多数情况为空，显示时再解析）
    department_id TEXT NOT NULL DEFAULT '',    -- 人员记录：使用当时所在的部门（接口没返回时按通讯录补）
    usage_type    INTEGER,                     -- 1 企业付费额度 2 试用免费额度
    feature_key   INTEGER,                     -- 额度类型，见 labels.FEATURE_KEYS（1 AI 通用额度……）
    used_quota    REAL NOT NULL DEFAULT 0,     -- 消耗点数
    scenario      TEXT NOT NULL DEFAULT '',    -- 场景路径，如「知识问答>主对话>网页版问答」
    notes         TEXT NOT NULL DEFAULT '',    -- JSON：接口返回的备注 descriptions [{key_name,key_type,value}]
    run_id        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_logs_date ON usage_logs(usage_date, subject);
CREATE INDEX IF NOT EXISTS usage_logs_entity ON usage_logs(entity_id, usage_date);
CREATE INDEX IF NOT EXISTS usage_logs_dept ON usage_logs(department_id, usage_date);
-- AI 用量统计详情（admin/v1/ai_usage_detail/query，T+1）：一行 = 某天某个对象各类额度的用量汇总。
-- q1..q6 对应额度类型 1..6（labels.FEATURE_KEYS）
CREATE TABLE IF NOT EXISTS usage_details (
    id          INTEGER PRIMARY KEY,
    usage_date  TEXT NOT NULL,
    subject     TEXT NOT NULL,                 -- member | app
    entity_type INTEGER,
    entity_id   TEXT NOT NULL,
    q1          REAL NOT NULL DEFAULT 0,
    q2          REAL NOT NULL DEFAULT 0,
    q3          REAL NOT NULL DEFAULT 0,
    q4          REAL NOT NULL DEFAULT 0,
    q5          REAL NOT NULL DEFAULT 0,
    q6          REAL NOT NULL DEFAULT 0,
    run_id      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_details_date ON usage_details(usage_date, subject);
CREATE INDEX IF NOT EXISTS usage_details_entity ON usage_details(entity_id, usage_date);
-- 应用名称映射。source: api（开放平台应用信息）| aily（Aily 应用统计）| manual（手工维护，优先级最高）
CREATE TABLE IF NOT EXISTS app_mapping (
    app_id     TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    source     TEXT NOT NULL,
    updated_by TEXT NOT NULL DEFAULT '',
    updated_at INTEGER NOT NULL
);
-- 额度分配：给部门 / 人员 / 应用在一个周期里分配的点数
CREATE TABLE IF NOT EXISTS quota_allocations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    dimension    TEXT NOT NULL,                -- department | member | app
    target_id    TEXT NOT NULL,
    target_name  TEXT NOT NULL DEFAULT '',
    amount       REAL NOT NULL,
    period_start TEXT NOT NULL,
    period_end   TEXT NOT NULL,
    enabled      INTEGER NOT NULL DEFAULT 1,
    note         TEXT NOT NULL DEFAULT '',
    updated_by   TEXT NOT NULL DEFAULT '',
    updated_at   INTEGER NOT NULL
);
-- 个人访问令牌（OpenAPI 和 MCP 用），只存哈希
CREATE TABLE IF NOT EXISTS api_tokens (
    token_hash   TEXT PRIMARY KEY,
    owner        TEXT NOT NULL,
    label        TEXT NOT NULL DEFAULT '',
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER,                      -- NULL = 不过期
    last_used_at INTEGER
);
CREATE INDEX IF NOT EXISTS api_tokens_owner ON api_tokens(owner);
CREATE TABLE IF NOT EXISTS audit_log (
    id     INTEGER PRIMARY KEY,
    at     INTEGER NOT NULL,
    actor  TEXT NOT NULL,                      -- open_id；系统自动执行的记 'system'
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    via    TEXT NOT NULL DEFAULT ''            -- web | api | mcp | system
);
CREATE INDEX IF NOT EXISTS audit_at ON audit_log(at);
-- 周报推送记录：一行 = 一次推送（定时、手动全量或测试）
CREATE TABLE IF NOT EXISTS report_runs (
    id          INTEGER PRIMARY KEY,
    week_start  TEXT NOT NULL,
    week_end    TEXT NOT NULL,
    trigger     TEXT NOT NULL,                 -- schedule | manual | test
    actor       TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL,                 -- running | success | partial | failed
    total       INTEGER NOT NULL DEFAULT 0,
    sent        INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    error       TEXT NOT NULL DEFAULT '',
    started_at  INTEGER NOT NULL,
    finished_at INTEGER
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        c = self._conn()
        c.executescript(SCHEMA)
        c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            # isolation_level=None：自动提交；需要事务时用 tx() 显式 BEGIN
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = c
        return c

    # ---- 通用 ----
    def q(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self._conn().execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self._conn().execute(sql, tuple(params)).fetchone()

    def val(self, sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
        row = self.one(sql, params)
        return default if row is None or row[0] is None else row[0]

    def x(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self._conn().execute(sql, tuple(params))

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """一组写操作要么全做、要么全不做。"""
        c = self._conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        c.execute("COMMIT")

    def size(self) -> int:
        try:
            return Path(self.path).stat().st_size
        except OSError:
            return 0

    # ---- meta ----
    def meta(self, key: str, default: str = "") -> str:
        return self.val("SELECT value FROM meta WHERE key=?", (key,), default)

    def set_meta(self, key: str, value: str) -> None:
        self.x("INSERT INTO meta(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # ---- 用户 ----
    def upsert_user(self, open_id: str, name: str, email: str) -> None:
        self.x(
            "INSERT INTO users(open_id, name, email, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(open_id) DO UPDATE SET name=excluded.name, email=excluded.email, updated_at=excluded.updated_at",
            (open_id, name, email, int(time.time())),
        )

    def pref(self, open_id: str, key: str, default: Any = None) -> Any:
        return loads(self.val("SELECT value FROM user_prefs WHERE open_id=? AND key=?", (open_id, key)), default)

    def set_pref(self, open_id: str, key: str, value: Any) -> None:
        self.x(
            "INSERT INTO user_prefs(open_id, key, value) VALUES(?,?,?) ON CONFLICT(open_id, key) DO UPDATE SET value=excluded.value",
            (open_id, key, dumps(value)),
        )

    # ---- 个人访问令牌 ----
    def add_token(self, token_hash: str, owner: str, label: str, expires_at: int | None = None) -> None:
        self.x(
            "INSERT INTO api_tokens(token_hash, owner, label, created_at, expires_at) VALUES(?,?,?,?,?)",
            (token_hash, owner, label, int(time.time()), expires_at),
        )

    def token_owner(self, token_hash: str) -> str | None:
        row = self.one("SELECT owner, expires_at, last_used_at FROM api_tokens WHERE token_hash=?", (token_hash,))
        now = int(time.time())
        if not row or (row["expires_at"] is not None and row["expires_at"] <= now):
            return None
        if not row["last_used_at"] or now - row["last_used_at"] > 60:  # 每分钟最多写一次
            self.x("UPDATE api_tokens SET last_used_at=? WHERE token_hash=?", (now, token_hash))
        return row["owner"]

    def tokens_of(self, owner: str) -> list[sqlite3.Row]:
        return self.q(
            "SELECT token_hash, label, created_at, expires_at, last_used_at FROM api_tokens WHERE owner=? ORDER BY created_at DESC", (owner,)
        )

    def revoke_token(self, owner: str, token_hash: str) -> bool:
        return self.x("DELETE FROM api_tokens WHERE owner=? AND token_hash=?", (owner, token_hash)).rowcount > 0

    # ---- 审计 ----
    def audit(self, actor: str, action: str, detail: str = "", via: str = "web") -> None:
        self.x("INSERT INTO audit_log(at, actor, action, detail, via) VALUES(?,?,?,?,?)", (int(time.time()), actor, action, detail[:500], via))


def loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def marks(n: int) -> str:
    """IN (?,?,?) 里的占位符。"""
    return ",".join("?" * n)

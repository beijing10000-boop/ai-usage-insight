"""通讯录快照：部门树 + 在职成员。把用量数据里的 ID 显示成姓名和部门，按部门树向上汇总，判断谁是部门负责人。

飞书用应用身份只能按部门逐个拉成员（每个部门一次调用），所以放在后台做：启动时若快照过期就同步一次，
之后每 interval 秒一次。同步失败不动旧快照（宁可旧也不要残缺）。查询都走内存里的副本。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .db import Database, dumps, loads
from .feishu import ROOT, Dept, DirUser, FeishuError

log = logging.getLogger("aius")

MAX_DEPTH = 64  # 防止脏数据里的环
PATH_SEP = " > "
_LOOSE = re.compile(r"[\s._\-·]+")


def _loose(text: str) -> str:
    """找人时比较用的写法：去掉空格、点、横线、下划线和间隔号。"""
    return _LOOSE.sub("", text)


class Directory:
    def __init__(self, feishu, db: Database, interval: int = 6 * 3600, workers: int = 4, retries: int = 3):
        self.feishu = feishu
        self.db = db
        self.interval = interval
        self.workers = workers
        self.retries = retries
        self._sync_lock = threading.Lock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._depts: dict[str, Dept] = {}
        self._children: dict[str, list[str]] = {}
        self._headcount: dict[str, int] = {}
        self._users: dict[str, DirUser] = {}
        self._led: dict[str, list[str]] = {}
        self._names: dict[tuple[str, str], str] = {}
        self.last_error = ""
        self.load()

    # ---------- 内存副本 ----------
    def load(self) -> None:
        depts = {r["id"]: Dept(r["id"], r["name"], r["parent_id"], r["leader_id"]) for r in self.db.q("SELECT * FROM departments")}
        children: dict[str, list[str]] = {}
        led: dict[str, list[str]] = {}
        for d in depts.values():
            children.setdefault(d.parent_id, []).append(d.id)
            if d.leader_id:
                led.setdefault(d.leader_id, []).append(d.id)
        users = {
            r["open_id"]: DirUser(r["open_id"], r["name"], r["en_name"], r["email"], tuple(loads(r["dept_ids"], [])))
            for r in self.db.q("SELECT * FROM directory")
        }
        headcount = {r["id"]: r["headcount"] for r in self.db.q("SELECT id, headcount FROM departments")}
        names = {(r["kind"], r["id"]): r["name"] for r in self.db.q("SELECT kind, id, name FROM name_cache")}
        # 先都算好，再一次换上去：读的线程看到的要么是旧的一整套，要么是新的一整套
        self._depts, self._children, self._led, self._users, self._headcount, self._names = depts, children, led, users, headcount, names

    # ---------- 部门 ----------
    def dept(self, dept_id: str) -> Dept | None:
        return self._depts.get(dept_id)

    def depts(self) -> list[Dept]:
        return list(self._depts.values())

    def chain(self, dept_id: str) -> list[str]:
        """部门自己 + 全部上级（不含根），从自己到最上级。"""
        out: list[str] = []
        cur = dept_id
        while cur and cur != ROOT and cur not in out and len(out) < MAX_DEPTH:
            out.append(cur)
            d = self._depts.get(cur)
            if not d:
                break
            cur = d.parent_id
        return out

    def top_level(self, dept_id: str) -> str:
        """所属的一级部门；不在部门树里的原样返回。"""
        chain = self.chain(dept_id)
        return chain[-1] if chain else dept_id

    def subtree(self, dept_id: str) -> set[str]:
        """部门自己 + 全部下级。"""
        out, stack = set(), [dept_id]
        while stack and len(out) < 100000:
            cur = stack.pop()
            if cur in out:
                continue
            out.add(cur)
            stack.extend(self._children.get(cur, ()))
        return out

    def children(self, dept_id: str = ROOT) -> list[Dept]:
        return sorted((self._depts[c] for c in self._children.get(dept_id, ())), key=lambda d: d.name)

    def dept_name(self, dept_id: str) -> str:
        if not dept_id:
            return "未归属部门"
        d = self._depts.get(dept_id)
        return d.name if d and d.name else self._names.get(("dept", dept_id), dept_id)

    def dept_path(self, dept_id: str) -> str:
        """集团 > 事业部 > 部门。"""
        if not dept_id:
            return "未归属部门"
        names = [self.dept_name(d) for d in reversed(self.chain(dept_id))]
        return PATH_SEP.join(names) or self.dept_name(dept_id)

    def headcount(self, dept_id: str) -> int:
        return self._headcount.get(dept_id, 0)

    def total_headcount(self) -> int:
        return len(self._users)

    def led_depts(self, open_id: str) -> list[str]:
        """这个人负责的部门（只算直接负责的，不含它们的下级）。"""
        return list(self._led.get(open_id, ()))

    def search_depts(self, query: str, limit: int = 10) -> list[Dept]:
        q = query.strip().casefold()
        if not q:
            return []
        hits = [d for d in self._depts.values() if q in self.dept_path(d.id).casefold() or q == d.id.casefold()]
        return sorted(hits, key=lambda d: (len(self.dept_path(d.id)), self.dept_path(d.id)))[:limit]

    # ---------- 人 ----------
    def user(self, open_id: str) -> DirUser | None:
        return self._users.get(open_id)

    def users(self) -> list[DirUser]:
        return list(self._users.values())

    def user_name(self, open_id: str) -> str:
        u = self._users.get(open_id)
        if u and u.name:
            return u.name
        return self._names.get(("user", open_id)) or self.db.val("SELECT name FROM users WHERE open_id=? AND name<>''", (open_id,)) or open_id

    def user_dept(self, open_id: str) -> str:
        u = self._users.get(open_id)
        return u.dept_ids[0] if u and u.dept_ids else ""

    def search_users(self, query: str, limit: int = 10) -> list[DirUser]:
        """按姓名、英文名、邮箱的任意一段找人，不分大小写；比较姓名时不管空格和点（「anna smith」「annasmith」都能找到 Anna.Smith）。
        完全同名的排最前，其次是姓名以输入开头的。"""
        q = query.strip().casefold()
        if not q:
            return []
        loose = _loose(q)

        def hit(u: DirUser) -> bool:
            name, en = u.name.casefold(), u.en_name.casefold()
            return (q in name or q in en or q in u.email.casefold() or q == u.open_id.casefold()
                    or bool(loose) and (loose in _loose(name) or loose in _loose(en)))  # fmt: skip

        def rank(u: DirUser) -> tuple:
            name = u.name.casefold()
            return (name != q, not (name.startswith(q) or _loose(name).startswith(loose)), u.name)

        return sorted((u for u in self._users.values() if hit(u)), key=rank)[:limit]

    # ---------- 快照里没有的名称 ----------
    def missing(self, kind: str, ids: list[str]) -> list[str]:
        known = self._users if kind == "user" else self._depts
        return [i for i in dict.fromkeys(ids) if i and i not in known and (kind, i) not in self._names]

    def remember(self, kind: str, names: dict[str, str]) -> None:
        now = int(time.time())
        for key, name in names.items():
            if key and name:
                self.db.x(
                    "INSERT INTO name_cache(kind, id, name, updated_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(kind, id) DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at",
                    (kind, key, name, now),
                )
                self._names[(kind, key)] = name

    # ---------- 同步 ----------
    def synced_at(self) -> int | None:
        raw = self.db.meta("directory_synced_at")
        return int(raw) if raw else None

    def _fetch(self, dept_id: str) -> list[DirUser]:
        for attempt in range(self.retries):
            try:
                return self.feishu.list_department_users(dept_id)
            except FeishuError:
                if attempt == self.retries - 1:
                    raise
                time.sleep(1 + attempt)
        return []

    def sync(self) -> tuple[int, int]:
        """全量同步，返回 (人数, 部门数)。失败抛 FeishuError，且不会动旧快照。"""
        with self._sync_lock:
            started = time.time()
            depts = {d.id: d for d in self.feishu.list_departments()}
            users: dict[str, DirUser] = {}
            with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="dir-sync") as pool:
                for batch in pool.map(self._fetch, [ROOT, *depts]):
                    for u in batch:
                        old = users.get(u.open_id)
                        if old:  # 同一个人在多个部门：合并部门
                            u = DirUser(u.open_id, u.name, u.en_name, u.email, tuple(dict.fromkeys(old.dept_ids + u.dept_ids)))
                        users[u.open_id] = u
            # 每个部门含子部门的人数（一个人在多个子部门里只算一次）
            members: dict[str, set[str]] = {d: set() for d in depts}
            parent = {d.id: d.parent_id for d in depts.values()}
            for u in users.values():
                for dept_id in u.dept_ids:
                    cur, seen = dept_id, 0
                    while cur in members and seen < MAX_DEPTH:
                        members[cur].add(u.open_id)
                        cur, seen = parent.get(cur, ROOT), seen + 1
            with self.db.tx() as c:
                c.execute("DELETE FROM departments")
                c.executemany(
                    "INSERT INTO departments(id, name, parent_id, leader_id, headcount) VALUES(?,?,?,?,?)",
                    [(d.id, d.name, d.parent_id, d.leader_id, len(members[d.id])) for d in depts.values()],
                )
                c.execute("DELETE FROM directory")
                c.executemany(
                    "INSERT INTO directory(open_id, name, en_name, email, dept_ids) VALUES(?,?,?,?,?)",
                    [(u.open_id, u.name, u.en_name, u.email, dumps(list(u.dept_ids))) for u in users.values()],
                )
            self.db.set_meta("directory_synced_at", str(int(time.time())))
            self.load()
            self.last_error = ""
            log.info("通讯录同步完成：%d 人，%d 个部门，用时 %.1fs", len(users), len(depts), time.time() - started)
            return len(users), len(depts)

    def sync_now(self) -> None:
        """管理员手动触发：后台线程立刻同步一次。"""
        self._wake.set()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="directory-sync", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while True:
            age = time.time() - (self.synced_at() or 0)
            if age >= self.interval or self._wake.is_set():
                self._wake.clear()
                try:
                    self.sync()
                except Exception as e:
                    self.last_error = str(e)
                    log.warning("通讯录同步失败，10 分钟后重试", exc_info=True)
                    self._wake.wait(600)
                    continue
                age = 0
            self._wake.wait(max(60, self.interval - age))

"""谁能看什么。

两个角色（和教程一致）：
- operations_admin 运营管理员：全部功能和全量数据。
- regular_user 普通用户：自己的用量；如果是部门负责人，还能看自己负责的部门（含下级部门）的运营数据。

运营管理员有两个来源：环境变量 AIUS_ADMINS 里列出的人（不能在页面上撤销），和「角色权限」页面里加的人。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .config import Settings
from .db import Database
from .directory import Directory

ADMIN = "operations_admin"
REGULAR = "regular_user"
ROLE_LABELS = {ADMIN: "运营管理员", REGULAR: "普通用户"}
ROLE_NOTES = {
    ADMIN: "拥有全量功能访问权限，含数据看板、同步管理、额度分配、应用映射等",
    REGULAR: "查看个人用量与负责部门的运营数据",
}


@dataclass(frozen=True)
class Viewer:
    open_id: str
    name: str
    email: str
    role: str = REGULAR
    led: tuple[str, ...] = ()  # 直接负责的部门

    @property
    def is_admin(self) -> bool:
        return self.role == ADMIN

    @property
    def label(self) -> str:
        return self.name or self.email or self.open_id

    @property
    def role_label(self) -> str:
        if self.is_admin:
            return ROLE_LABELS[ADMIN]
        return "部门负责人" if self.led else ROLE_LABELS[REGULAR]


class Access:
    def __init__(self, settings: Settings, db: Database, directory: Directory):
        self.s = settings
        self.db = db
        self.directory = directory

    def _env_admin(self, open_id: str, email: str = "") -> bool:
        if not self.s.admins:
            return False
        mine = {open_id.lower(), email.lower()}
        if entry := self.directory.user(open_id):
            mine.add(entry.email.lower())
        return bool(mine.intersection(self.s.admins) - {""})

    def role(self, open_id: str, email: str = "") -> str:
        if self._env_admin(open_id, email):
            return ADMIN
        return ADMIN if self.db.val("SELECT 1 FROM roles WHERE open_id=? AND role=?", (open_id, ADMIN)) else REGULAR

    def viewer(self, open_id: str, name: str = "", email: str = "") -> Viewer:
        return Viewer(open_id, name or self.directory.user_name(open_id), email, self.role(open_id, email), tuple(self.directory.led_depts(open_id)))

    # ---- 数据范围 ----
    def led_tree(self, viewer: Viewer) -> set[str]:
        """这个人负责的部门，连同它们的全部下级。"""
        out: set[str] = set()
        for dept in viewer.led:
            out |= self.directory.subtree(dept)
        return out

    def can_see_dept(self, viewer: Viewer, dept_id: str) -> bool:
        return viewer.is_admin or dept_id in self.led_tree(viewer)

    def can_see_member(self, viewer: Viewer, open_id: str) -> bool:
        if viewer.is_admin or open_id == viewer.open_id:
            return True
        member = self.directory.user(open_id)
        return bool(member and set(member.dept_ids) & self.led_tree(viewer))

    # ---- 角色管理 ----
    def admins(self) -> list[dict]:
        rows = {r["open_id"]: r for r in self.db.q("SELECT open_id, granted_by, granted_at FROM roles WHERE role=?", (ADMIN,))}
        out = []
        for key in self.s.admins:
            user = next((u for u in self.directory.users() if key in (u.open_id.lower(), u.email)), None)
            known = user.open_id if user else self.db.val("SELECT open_id FROM users WHERE lower(open_id)=? OR lower(email)=?", (key, key))
            out.append({"open_id": known or "", "key": key, "name": self.directory.user_name(known) if known else "", "fixed": True, "granted_at": None, "granted_by": ""})
            rows.pop(known or "", None)
        for open_id, r in rows.items():
            out.append(
                {
                    "open_id": open_id,
                    "key": open_id,
                    "name": self.directory.user_name(open_id),
                    "fixed": False,
                    "granted_at": r["granted_at"],
                    "granted_by": self.directory.user_name(r["granted_by"]) if r["granted_by"] else "",
                }
            )
        return out

    def grant(self, open_id: str, actor: str) -> None:
        self.db.x(
            "INSERT INTO roles(open_id, role, granted_by, granted_at) VALUES(?,?,?,?) "
            "ON CONFLICT(open_id) DO UPDATE SET role=excluded.role, granted_by=excluded.granted_by, granted_at=excluded.granted_at",
            (open_id, ADMIN, actor, int(time.time())),
        )

    def revoke(self, open_id: str) -> bool:
        return self.db.x("DELETE FROM roles WHERE open_id=?", (open_id,)).rowcount > 0

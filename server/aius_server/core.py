"""各个路由模块共用的东西：服务对象、登录态、数据范围、表单提交的统一检查、日期范围的解析。"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import web
from .access import Access, Viewer
from .analytics import Analytics, Filter
from .config import Settings
from .db import Database
from .directory import Directory
from .feishu import ROOT
from .insight import Insight
from .prefs import Config
from .quota import Quotas
from .report import WeeklyReport
from .sync import MAX_DAYS, SyncEngine
from .vault import Vault

SESSION_COOKIE = "aius_session"
OAUTH_COOKIE = "aius_oauth"
SETUP_COOKIE = "aius_setup"
SETUP_TTL = 1800  # 完成首次配置（或更换飞书应用）后，多久之内登录的那个人成为运营管理员
TOKEN_PREFIX = "aius_"
PRESETS = (7, 30, 90)
ASK_GAP = 3  # 同一个人两次向洞察助手提问至少间隔几秒

# 操作成功后跳回页面时的提示：?done=<键> -> (类型, 文字)
FLASH = {
    "sync_started": ("success", "同步任务已开始，下面可以看到进度"),
    "sync_stopped": ("info", "已通知任务停止，拉完当前这一页就会停下"),
    "saved": ("success", "已保存"),
    "connected": ("success", "已连接：飞书应用凭证可用"),
    "directory": ("success", "已通知后台同步通讯录，通常一两分钟内完成，稍后刷新查看"),
    "quota_saved": ("success", "额度已保存"),
    "quota_deleted": ("success", "额度记录已删除"),
    "quota_toggled": ("success", "已更新状态"),
    "mapping_saved": ("success", "应用名称已保存"),
    "mapping_deleted": ("success", "已删除手工映射"),
    "report_test": ("success", "测试周报已发给你，去飞书里看一下"),
    "report_pushed": ("success", "周报已开始推送，稍后刷新可以看到推送记录"),
    "revoked": ("success", "令牌已撤销"),
    "granted": ("success", "已设为运营管理员"),
    "role_removed": ("success", "已取消运营管理员"),
    "rollup_saved": ("success", "部门圈选已保存"),
    "credentials": ("success", "飞书应用凭证已保存，已用新凭证连通飞书"),
    "llm_saved": ("success", "AI 模型配置已保存，模型连接验证通过"),
    "llm_cleared": ("success", "已清除 AI 模型配置"),
}


def hash_secret(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class Range:
    start: str
    end: str
    preset: int | None  # 7 / 30 / 90，自定义范围是 None
    error: str = ""

    @property
    def label(self) -> str:
        return f"近 {self.preset} 天" if self.preset else f"{self.start} ~ {self.end}"

    @property
    def query(self) -> dict:
        """带着当前范围跳到别的页面时用的参数。"""
        return {"days": self.preset} if self.preset else {"start": self.start, "end": self.end}

    @property
    def days(self) -> int:
        return (date.fromisoformat(self.end) - date.fromisoformat(self.start)).days + 1


@dataclass
class Ctx:
    s: Settings
    db: Database
    fs: object
    directory: Directory
    config: Config
    access: Access
    engine: SyncEngine
    analytics: Analytics
    quotas: Quotas
    insight: Insight
    report: WeeklyReport
    vault: Vault

    def __post_init__(self) -> None:
        self.signer = URLSafeTimedSerializer(self.s.secret_key, salt="aius-session")
        self._asked: dict[str, float] = {}

    # ---------- 首次配置之后的第一位运营管理员 ----------
    def arm_admin_claim(self, response: Response) -> None:
        """在这个浏览器上留一个记号：接下来 SETUP_TTL 秒内用它登录的人成为运营管理员。
        用在两处：刚做完首次配置（还没有任何管理员）；管理员把飞书应用换成另一个（所有人的 open_id 都变了，原来的角色对不上）。"""
        nonce = secrets.token_urlsafe(16)
        self.db.set_meta("setup_nonce", f"{nonce}|{int(time.time())}")
        response.set_cookie(SETUP_COOKIE, self.signer.dumps({"nonce": nonce}), max_age=SETUP_TTL, httponly=True, secure=self.s.cookie_secure, samesite="lax")

    def claim_admin(self, request: Request, open_id: str) -> bool:
        """登录成功时调用：浏览器带着有效的记号就把这个人设为运营管理员。记号只能用一次。"""
        nonce, _, at = self.db.meta("setup_nonce").partition("|")
        if not nonce or not at.isdigit() or time.time() - int(at) > SETUP_TTL:
            return False
        try:
            data = self.signer.loads(request.cookies.get(SETUP_COOKIE, ""), max_age=SETUP_TTL)
        except BadSignature:
            return False
        if not secrets.compare_digest(str(data.get("nonce", "")), nonce):
            return False
        self.db.set_meta("setup_nonce", "")
        self.access.grant(open_id, "setup")
        self.db.audit(open_id, "role_grant", "完成首次配置后第一个登录，成为运营管理员", via="system")
        return True

    # ---------- 登录态 ----------
    def user(self, request: Request) -> Viewer | None:
        raw = request.cookies.get(SESSION_COOKIE)
        if not raw:
            return None
        try:
            data = self.signer.loads(raw, max_age=self.s.session_days * 86400)
        except BadSignature:
            return None
        return self.access.viewer(data["oid"], data.get("name", ""), data.get("email", ""))

    def need_login(self, request: Request) -> Response:
        """没登录：就在被访问的地址上显示登录页（不直接跳飞书），登录后回到这个地址。
        平台还没有配置飞书应用凭证时没法登录，去首次配置页面。"""
        if not self.vault.configured:
            return RedirectResponse("/setup", status_code=302)
        return web.login_page(request.url.path + (f"?{request.url.query}" if request.url.query else ""))

    def origin_ok(self, request: Request) -> bool:
        origin = request.headers.get("origin")
        return origin is None or origin == self.s.origin

    def bearer(self, authorization: str | None) -> Viewer | None:
        """个人访问令牌 → 它绑定的那个人（OpenAPI 和 MCP 用）。令牌只认身份，能看什么数据仍按这个人的角色算。"""
        if not authorization or not authorization.lower().startswith("bearer "):
            return None
        owner = self.db.token_owner(hash_secret(authorization[7:].strip()))
        return self.access.viewer(owner) if owner else None

    def new_token(self, owner: str, label: str, days: int | None) -> str:
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        self.db.add_token(hash_secret(token), owner, label, int(time.time()) + days * 86400 if days else None)
        self.db.audit(owner, "token_create", label)
        return token

    # ---------- 页面 ----------
    def shell(self, viewer: Viewer, active: str) -> web.Shell:
        return web.Shell(viewer, active, self.analytics.latest_date(), self.engine.active() is not None, web.navigation(viewer))

    def page(self, template: str, viewer: Viewer, active: str, request: Request | None = None, status: int = 200, **ctx) -> Response:
        flash = FLASH.get(request.query_params.get("done", "")) if request else None
        return web.render(template, status, shell=self.shell(viewer, active), flash=flash, **ctx)

    def notice(self, viewer: Viewer, title: str, text: str, status: int, back: tuple[str, str], active: str = "") -> Response:
        """控制台里的整页提示：说明哪里不行，给一个去处。"""
        return web.render("notice.html", status, shell=self.shell(viewer, active), title=title, text=text, back=back)

    def admin_only(self, viewer: Viewer, active: str = "") -> Response | None:
        if viewer.is_admin:
            return None
        return self.notice(viewer, "需要运营管理员权限", "这个页面只有运营管理员能打开。你可以在「我的用量」里看自己的数据；需要更多权限请联系运营管理员。", 403, ("去我的用量", "/self-usage"), active)

    def done(self, target: str, code: str) -> RedirectResponse:
        path, _, fragment = web.safe_next(target).partition("#")
        url = f"{path}{'&' if '?' in path else '?'}done={code}" + (f"#{fragment}" if fragment else "")
        return RedirectResponse(url, status_code=303)

    def came_from(self, request: Request) -> str:
        """表单是从哪个页面提交的（同源的 Referer）：重新登录后回到那里。"""
        ref = urlsplit(request.headers.get("referer", ""))
        if f"{ref.scheme}://{ref.netloc}" != self.s.origin:
            return "/"
        query = urlencode([(k, v) for k, v in parse_qsl(ref.query) if k != "done"])
        return ref.path + (f"?{query}" if query else "")

    def poster(self, request: Request, admin: bool = True) -> tuple[Viewer | None, Response | None]:
        """表单 POST 的统一检查：登录、来源、（默认）运营管理员。不通过时返回要显示的页面，这次操作不执行。"""
        viewer = self.user(request)
        if not viewer:
            return None, web.login_page(self.came_from(request), 401, error="登录已过期，这次操作没有执行。请重新登录后再试。")
        if not self.origin_ok(request):
            return None, web.message("请求被拒绝", "来源校验失败。", 403, viewer.label)
        if admin and not viewer.is_admin:
            return None, self.admin_only(viewer)
        return viewer, None

    # ---------- 日期范围 ----------
    def pick_range(self, request: Request, default_days: int = 30) -> Range:
        """从地址参数里取日期范围：?days=7|30|90，或 ?start=…&end=…；都没有就是默认的近 30 天。"""
        q = request.query_params
        if q.get("start") or q.get("end"):
            try:
                first, last = date.fromisoformat(q.get("start", "")), date.fromisoformat(q.get("end", ""))
            except ValueError:
                return self._preset(default_days, "日期要写成 2026-01-31 这样的格式，已改回默认范围")
            if first > last:
                return self._preset(default_days, "开始日期不能晚于结束日期，已改回默认范围")
            if first.year < 2020 or last.year > 2100:
                return self._preset(default_days, "日期超出了可查询的范围，已改回默认范围")
            if (last - first).days + 1 > MAX_DAYS:
                return self._preset(default_days, f"一次最多看 {MAX_DAYS} 天，已改回默认范围")
            return Range(first.isoformat(), last.isoformat(), None)
        try:
            days = int(q.get("days", default_days))
        except ValueError:
            days = default_days
        return self._preset(days if days in PRESETS else default_days)

    def _preset(self, days: int, error: str = "") -> Range:
        start, end = self.analytics.default_range(days)
        return Range(start, end, days, error)

    def week_ago(self, day: str) -> str:
        return (date.fromisoformat(day) - timedelta(days=6)).isoformat()

    # ---------- 数据范围 ----------
    def scope(self, viewer: Viewer, start: str, end: str) -> "Scope":
        """这个人默认能看的数据范围：运营管理员看全企业，部门负责人看负责的部门，其他人看自己。"""
        if viewer.is_admin:
            return Scope(Filter(start, end), "全企业", ROOT, True)
        if viewer.led:
            names = "、".join(self.directory.dept_name(d) for d in viewer.led[:3])
            label = f"你负责的部门（{names}{'等' if len(viewer.led) > 3 else ''}）"
            # 只负责一个部门：部门排行、部门增长按它的下级部门拆。负责好几个互不相干的部门时没有共同的上级可拆
            one = len(viewer.led) == 1
            return Scope(Filter(start, end, depts=frozenset(self.access.led_tree(viewer))), label, viewer.led[0] if one else ROOT, one)
        return Scope(Filter(start, end, member=viewer.open_id), f"{viewer.label} 个人", ROOT, False)

    def may_ask(self, open_id: str) -> bool:
        """洞察助手的提问间隔（网页和 MCP 共用）：同一个人两次提问至少隔 ASK_GAP 秒，免得被脚本拿来刷模型。"""
        now = time.monotonic()
        if now - self._asked.get(open_id, -ASK_GAP) < ASK_GAP:
            return False
        self._asked[open_id] = now
        return True


@dataclass(frozen=True)
class Scope:
    filter: Filter
    label: str
    parent: str  # 部门排行 / 部门增长按哪个部门的下级拆（根 = 一级部门）
    by_dept: bool  # 这个范围里要不要看部门维度

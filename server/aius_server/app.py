"""AI 运营洞察平台。

启动：uvicorn --factory aius_server.app:create_app

这里是组装、首次配置和登录；分析页面在 pages.py，运营管理页面在 manage.py，开放接口在 openapi.py，远程 MCP 在 remote_mcp.py。
"""

from __future__ import annotations

import logging
import secrets
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature
from starlette.routing import Route

from . import __version__, manage, openapi, pages, web
from .access import Access
from .analytics import Analytics
from .config import Settings
from .core import OAUTH_COOKIE, SESSION_COOKIE, SETUP_COOKIE, Ctx
from .db import Database
from .directory import Directory
from .feishu import FakeFeishu, FeishuClient, FeishuError
from .insight import Insight
from .prefs import Config
from .quota import Quotas
from .remote_mcp import build_remote_mcp
from .report import WeeklyReport
from .scheduler import Scheduler
from .sync import SyncEngine
from .vault import Vault

log = logging.getLogger("aius")

SETUP_MAX_FAILS = 10  # 初始化口令连续输错这么多次，就要等一会儿再试
SETUP_LOCK_SECONDS = 600


class Assets(StaticFiles):
    """样式和脚本。每次都向服务器确认有没有变（304 很便宜），升级后不会沿用旧文件。"""

    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers.setdefault("Cache-Control", "no-cache")
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response


def create_app(settings: Settings | None = None, feishu=None) -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    s = settings or Settings.from_env()
    if s.dev_fake_login:
        log.warning("!!! AIUS_DEV_FAKE_LOGIN 已开启：登录不经过飞书、数据是演示数据，任何人可冒充任何邮箱，只能用于本地开发 !!!")
    db = Database(s.data_dir / "aius.db")
    vault = Vault(db, s)
    fs = feishu or (FakeFeishu(demo=True) if s.dev_fake_login else FeishuClient(s, vault))
    directory = Directory(fs, db, interval=s.directory_sync_hours * 3600)
    config = Config(db)
    engine = SyncEngine(db, fs, directory)
    analytics = Analytics(db, directory, config)
    report = WeeklyReport(db, fs, directory, config, s.public_url)
    ctx = Ctx(
        s=s, db=db, fs=fs, directory=directory, config=config, access=Access(s, db, directory), engine=engine,
        analytics=analytics, quotas=Quotas(db, directory), insight=Insight(s, analytics, vault), report=report, vault=vault,
    )  # fmt: skip
    scheduler = Scheduler(db, config, engine, report)
    mcp_gate, mcp_server = build_remote_mcp(ctx)
    if not vault.configured:
        vault.bootstrap_token()
        log.warning("还没有配置飞书应用凭证。打开 %s/setup 完成首次配置；初始化口令在数据目录的 bootstrap.token 文件里", s.public_url)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        engine.start_worker()  # 同步线程总是要有，不然手动同步会一直「等待中」；启动时先把上次没跑完的任务从断点续上
        if s.scheduler:  # AIUS_SCHEDULER=0 只关掉「到点自动做」的事：定时同步、巡检、周报、通讯录定时同步
            directory.start()  # 快照过期（或从没同步过）时立即同步一次
            scheduler.start()
        async with mcp_server.session_manager.run():
            yield

    app = FastAPI(title="ai-usage-insight", version=__version__, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.ctx = ctx
    app.state.scheduler = scheduler

    # ---------- 首次配置 ----------
    # 平台还没有飞书应用凭证时，谁都登录不了，所以这个页面不需要登录；提交时要填初始化口令
    # （数据目录里的 bootstrap.token，只有能登上服务器的人看得到）。配置完成后这个页面就关了，之后在「同步管理」里改。
    fails: list[float] = []

    def setup_view(status: int = 200, error: str = "", app_id: str = "", aily_app_id: str = "") -> Response:
        return web.render("setup.html", status, callback_url=s.callback_url, error=error, app_id=app_id, aily_app_id=aily_app_id)

    @app.get("/setup")
    def setup_page():
        if vault.configured:
            return RedirectResponse("/login", status_code=302)
        vault.bootstrap_token()
        return setup_view()

    @app.post("/setup")
    def setup_submit(
        request: Request, token: str = Form(""), app_id: str = Form(""), app_secret: str = Form(""),
        aily_app_id: str = Form(""), aily_app_secret: str = Form(""),
    ):  # fmt: skip
        if vault.configured:
            return RedirectResponse("/login", status_code=303)
        if not ctx.origin_ok(request):
            return web.message("请求被拒绝", "来源校验失败。", 403)
        app_id, app_secret, aily_app_id, aily_app_secret = (v.strip() for v in (app_id, app_secret, aily_app_id, aily_app_secret))
        now = time.time()
        fails[:] = [t for t in fails if now - t < SETUP_LOCK_SECONDS]
        if len(fails) >= SETUP_MAX_FAILS:
            return setup_view(429, "初始化口令输错的次数太多了，请 10 分钟后再试。", app_id, aily_app_id)
        if not vault.check_bootstrap(token):
            fails.append(now)
            return setup_view(403, "初始化口令不对。口令在服务器数据目录的 bootstrap.token 文件里，注意不要带空格。", app_id, aily_app_id)
        if not app_id or not app_secret:
            return setup_view(400, "App ID 和 App Secret 都要填。", app_id, aily_app_id)
        if bool(aily_app_id) != bool(aily_app_secret):
            return setup_view(400, "Aily 应用的 App ID 和 App Secret 要么都填，要么都不填。", app_id, aily_app_id)
        try:
            fs.verify(app_id, app_secret)
        except FeishuError as e:
            return setup_view(400, f"用这对凭证连不上飞书：{e}。请核对 App ID 和 App Secret，并确认应用已经发布。", app_id, aily_app_id)
        if aily_app_id:
            try:
                fs.verify(aily_app_id, aily_app_secret)
            except FeishuError as e:
                return setup_view(400, f"Aily 应用的凭证连不上飞书：{e}。可以先不填，之后在「同步管理」里补。", app_id, aily_app_id)
        vault.set({"feishu_app_id": app_id, "feishu_app_secret": app_secret, "aily_app_id": aily_app_id, "aily_app_secret": aily_app_secret}, "setup")
        vault.close_bootstrap()
        fs.forget_tokens()
        db.audit("system", "credentials", "首次配置：保存飞书应用凭证", via="web")
        log.info("首次配置完成")
        if s.scheduler:
            directory.sync_now()
        resp = RedirectResponse("/login?next=%2Fsync&done=setup", status_code=303)  # 跳走，刷新不会重复提交
        ctx.arm_admin_claim(resp)
        return resp

    # ---------- 登录 ----------
    @app.get("/auth/login")
    def auth_login(next: str | None = None):
        if not vault.configured:
            return RedirectResponse("/setup", status_code=302)
        state = secrets.token_urlsafe(16)
        resp = RedirectResponse(fs.authorize_url(s.callback_url, state), status_code=302)
        resp.set_cookie(
            OAUTH_COOKIE, ctx.signer.dumps({"state": state, "next": web.safe_next(next)}), max_age=600, httponly=True, secure=s.cookie_secure, samesite="lax"
        )
        return resp

    @app.get("/auth/callback")
    def auth_callback(request: Request, state: str = "", code: str = "", error: str = "", email: str = "", name: str = ""):
        # 登录没成功都回到登录页，写明原因；再点一次登录还是回到原来要去的地址
        try:
            pending = ctx.signer.loads(request.cookies.get(OAUTH_COOKIE, ""), max_age=600)
        except BadSignature:
            pending = None
        target = pending["next"] if pending else "/"
        if error:
            return web.login_page(target, 400, error="登录没有完成：你取消了飞书授权。")
        if not pending or not state or not secrets.compare_digest(pending["state"], state):
            return web.login_page(target, 400, error="登录已过期：登录状态校验失败，请重新登录。")
        if s.dev_fake_login and email:
            code = f"{email}|{name}"
        if not code:
            return web.login_page(target, 400, error="登录失败：飞书没有返回授权码。")
        try:
            user = fs.login(code, s.callback_url)
        except FeishuError as e:
            log.warning("飞书登录失败: %s", e)
            return web.login_page(target, 502, error=f"登录失败：{e}")
        if s.allowed_tenant_key and user.tenant_key != s.allowed_tenant_key:
            return web.login_page(target, 403, error="登录失败：只允许本企业的飞书账号访问。")
        db.upsert_user(user.open_id, user.name, user.email)
        db.audit(user.open_id, "login")
        log.info("登录 %s %s tenant_key=%s", user.open_id, user.name, user.tenant_key)
        resp = RedirectResponse(pending["next"], status_code=302)
        resp.delete_cookie(OAUTH_COOKIE)
        if ctx.claim_admin(request, user.open_id):
            log.info("首次配置后的第一位运营管理员：%s %s", user.open_id, user.name)
        resp.delete_cookie(SETUP_COOKIE)
        resp.set_cookie(
            SESSION_COOKIE,
            ctx.signer.dumps({"oid": user.open_id, "name": user.name, "email": user.email}),
            max_age=s.session_days * 86400,
            httponly=True,
            secure=s.cookie_secure,
            samesite="lax",
        )
        return resp

    @app.get("/auth/logout")
    def auth_logout():
        resp = web.login_page("/", 200, notice="你已退出登录。")
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    @app.get("/login")
    def login(request: Request, next: str | None = None, done: str = ""):
        """登录页的固定地址。已经登录的直接去 next（没给就是首页）。"""
        if not vault.configured:
            return RedirectResponse("/setup", status_code=302)
        notices = {
            "setup": "配置完成。现在用飞书账号登录——你会成为第一位运营管理员（30 分钟内、在这个浏览器上登录有效）。",
            "app": "飞书应用已更换，所有人都需要重新登录。请现在登录——你会继续是运营管理员（30 分钟内、在这个浏览器上登录有效）。",
        }
        if done in notices:  # 刚做完配置：即使浏览器里还留着旧的登录态，也要重新登录一次
            resp = web.login_page(next, 200, notice=notices[done])
            resp.delete_cookie(SESSION_COOKIE)
            return resp
        if ctx.user(request):
            return RedirectResponse(web.safe_next(next), status_code=302)
        return web.login_page(next, 200)

    if s.dev_fake_login:

        @app.get("/auth/dev-login")
        def dev_login(state: str):
            return web.render("dev_login.html", state=state)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "version": __version__, "configured": vault.configured}

    pages.register(app, ctx)
    manage.register(app, ctx)
    openapi.register(app, ctx)
    app.mount("/static", Assets(directory=web.STATIC_DIR), name="static")
    app.router.routes.append(Route("/mcp", endpoint=mcp_gate))
    return app

"""AI 运营洞察平台。

启动：uvicorn --factory aius_server.app:create_app

这里是组装和登录；分析页面在 pages.py，运营管理页面在 manage.py，开放接口在 openapi.py，远程 MCP 在 remote_mcp.py。
"""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature
from starlette.routing import Route

from . import __version__, manage, openapi, pages, web
from .access import Access
from .analytics import Analytics
from .config import Settings
from .core import OAUTH_COOKIE, SESSION_COOKIE, Ctx
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

log = logging.getLogger("aius")


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
    fs = feishu or (FakeFeishu(demo=True) if s.dev_fake_login else FeishuClient(s))
    directory = Directory(fs, db, interval=s.directory_sync_hours * 3600)
    config = Config(db)
    engine = SyncEngine(db, fs, directory)
    analytics = Analytics(db, directory, config)
    report = WeeklyReport(db, fs, directory, config, s.public_url)
    ctx = Ctx(
        s=s, db=db, fs=fs, directory=directory, config=config, access=Access(s, db, directory), engine=engine,
        analytics=analytics, quotas=Quotas(db, directory), insight=Insight(s, analytics), report=report,
    )  # fmt: skip
    scheduler = Scheduler(db, config, engine, report)
    mcp_gate, mcp_server = build_remote_mcp(ctx)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if s.scheduler:
            directory.start()  # 快照过期（或从没同步过）时立即同步一次
            engine.start_worker()  # 先把上次没跑完的任务从断点续上
            scheduler.start()
        async with mcp_server.session_manager.run():
            yield

    app = FastAPI(title="ai-usage-insight", version=__version__, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.ctx = ctx
    app.state.scheduler = scheduler

    # ---------- 登录 ----------
    @app.get("/auth/login")
    def auth_login(next: str | None = None):
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
    def login(request: Request, next: str | None = None):
        """登录页的固定地址。已经登录的直接去 next（没给就是首页）。"""
        if ctx.user(request):
            return RedirectResponse(web.safe_next(next), status_code=302)
        return web.login_page(next, 200)

    if s.dev_fake_login:

        @app.get("/auth/dev-login")
        def dev_login(state: str):
            return web.render("dev_login.html", state=state)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "version": __version__}

    pages.register(app, ctx)
    manage.register(app, ctx)
    openapi.register(app, ctx)
    app.mount("/static", Assets(directory=web.STATIC_DIR), name="static")
    app.router.routes.append(Route("/mcp", endpoint=mcp_gate))
    return app

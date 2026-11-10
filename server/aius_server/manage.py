"""运营管理页面：额度分配、同步管理、执行日志、应用映射与周报推送、接入管理、角色权限、操作审计。

除了「接入管理」（每个人管自己的令牌），其余都只有运营管理员能用。
改数据的请求都是表单 POST：校验登录、来源和角色，成功后跳回 GET 页面并带 ?done=xxx 显示提示，刷新、后退都不会重复提交。
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .access import ADMIN, REGULAR, ROLE_LABELS, ROLE_NOTES
from .core import Ctx
from .db import dumps, loads
from .feishu import FeishuError
from .labels import entity_type
from .openapi import ENDPOINTS
from .pages import Pager, page_number
from .prefs import ConfigError
from .quota import DIMENSIONS, QuotaError
from .remote_mcp import connect_prompt
from .report import WEEKDAYS
from .sync import STATUS_LABELS, SyncError, today

AUDIT_ACTIONS = {
    "login": "登录",
    "sync_start": "发起同步",
    "sync_stop": "停止同步",
    "check": "测试连接",
    "directory": "同步通讯录",
    "config": "修改配置",
    "quota": "额度分配",
    "mapping": "应用映射",
    "report": "周报推送",
    "token_create": "生成令牌",
    "token_revoke": "撤销令牌",
    "role_grant": "授予管理员",
    "role_revoke": "取消管理员",
    "export": "导出数据",
    "insight_ask": "洞察提问",
}
TOKEN_DAYS = {30: "30 天", 90: "90 天", 365: "1 年", 0: "不过期"}


def mask(value: str) -> str:
    return value if len(value) <= 8 else f"{value[:6]}…{value[-4:]}"


def register(app: FastAPI, c: Ctx) -> None:
    def guard(request: Request, active: str):
        """GET 页面的统一检查：没登录 → 登录页；不是运营管理员 → 提示页。"""
        viewer = c.user(request)
        if not viewer:
            return None, c.need_login(request)
        return viewer, c.admin_only(viewer, active)

    def oops(viewer, title: str, text: str, back: tuple[str, str], active: str):
        return c.notice(viewer, title, text, 400, back, active)

    # ---------- 额度分配 ----------
    def resolve_target(dimension: str, text: str) -> str:
        """表单里填的对象：从联想列表选的是 ID；直接打名字时，只有唯一匹配才认。"""
        text = text.strip()
        if dimension == "department":
            hits = [text] if c.directory.dept(text) else [d.id for d in c.directory.search_depts(text, 2)]
        elif dimension == "member":
            hits = [text] if c.directory.user(text) else [u.open_id for u in c.directory.search_users(text, 2)]
        else:
            return text  # 应用：直接认填的应用 ID（应用不一定有名称映射）
        if len(hits) == 1:
            return hits[0]
        raise QuotaError(f"没找到唯一匹配「{text}」的{DIMENSIONS[dimension]}，请从联想列表里选一个")

    @app.get("/quota")
    def quota_page(request: Request, edit: int = 0):
        viewer, bad = guard(request, "quota")
        if bad:
            return bad
        rows = c.quotas.list()
        month = today().replace(day=1)
        month_end = (month.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        return c.page(
            "quota.html", viewer, "quota", request, rows=rows, summary=c.quotas.summary(rows), dimensions=DIMENSIONS,
            editing=c.quotas.get(edit) if edit else None, default_start=month.isoformat(), default_end=month_end.isoformat(), price=c.config.point_price,
        )  # fmt: skip

    @app.post("/quota")
    def quota_post(
        request: Request, action: str = Form(...), id: int = Form(0), dimension: str = Form("department"), target: str = Form(""),
        amount: str = Form(""), period_start: str = Form(""), period_end: str = Form(""), note: str = Form(""),
    ):  # fmt: skip
        viewer, bad = c.poster(request)
        if bad:
            return bad
        if action == "delete":
            c.quotas.delete(id)
            c.db.audit(viewer.open_id, "quota", f"删除额度记录 #{id}")
            return c.done("/quota", "quota_deleted")
        if action in ("enable", "disable"):
            c.quotas.set_enabled(id, action == "enable", viewer.open_id)
            c.db.audit(viewer.open_id, "quota", f"{'启用' if action == 'enable' else '停用'}额度记录 #{id}")
            return c.done("/quota", "quota_toggled")
        try:
            if dimension not in DIMENSIONS:
                raise QuotaError("对象类型只能是部门、人员或应用")
            saved = c.quotas.save(viewer.open_id, dimension, resolve_target(dimension, target), amount, period_start, period_end, note, id or None)
        except QuotaError as e:
            return oops(viewer, "额度没有保存", str(e), ("回到额度分配", "/quota"), "quota")
        c.db.audit(viewer.open_id, "quota", f"{'修改' if id else '新增'}额度记录 #{saved}：{DIMENSIONS[dimension]} {c.quotas.target_name(dimension, c.quotas.get(saved)['target_id'])} {amount} 点")
        return c.done("/quota", "quota_saved")

    # ---------- 同步管理 ----------
    @app.get("/sync")
    def sync_page(request: Request):
        viewer, bad = guard(request, "sync")
        if bad:
            return bad
        runs, _total = c.engine.runs(limit=1)
        yesterday = (today() - timedelta(days=1)).isoformat()
        return c.page(
            "sync.html", viewer, "sync", request,
            app_id=mask(c.s.feishu_app_id) or "开发模式，未配置", aily=mask(c.s.aily_app_id) if c.s.aily_ready else "",
            check=loads(c.db.meta("feishu_check"), None), running=c.engine.run(c.engine.active()["id"]) if c.engine.active() else None,
            last=runs[0] if runs else None, schedule=c.config.get("sync"), yesterday=yesterday, today=today().isoformat(),
            directory={"at": c.directory.synced_at(), "users": c.directory.total_headcount(), "depts": len(c.directory.depts()), "error": c.directory.last_error,
                       "hours": c.s.directory_sync_hours},
            latest=c.analytics.latest_date(), earliest=c.analytics.earliest_date(), dev=c.s.dev_fake_login,
        )  # fmt: skip

    @app.post("/sync")
    def sync_post(
        request: Request, action: str = Form(...), start: str = Form(""), end: str = Form(""), id: int = Form(0),
        enabled: str = Form(""), hour: str = Form(""), range_days: str = Form(""),
    ):  # fmt: skip
        viewer, bad = c.poster(request)
        if bad:
            return bad
        back = ("回到同步管理", "/sync")
        if action == "start":
            try:
                run_id = c.engine.start(start, end or start, "manual", viewer.open_id)
            except SyncError as e:
                return oops(viewer, "同步没有开始", str(e), back, "sync")
            c.db.audit(viewer.open_id, "sync_start", f"手动同步 #{run_id}：{start} ~ {end or start}")
            return c.done("/sync", "sync_started")
        if action == "stop":
            if c.engine.stop(id):
                c.db.audit(viewer.open_id, "sync_stop", f"停止同步任务 #{id}")
            return c.done("/sync", "sync_stopped")
        if action == "check":
            try:
                result = {"ok": True, **c.fs.check(), "at": int(time.time())}
            except FeishuError as e:
                result = {"ok": False, "error": str(e), "at": int(time.time())}
            c.db.set_meta("feishu_check", dumps(result))
            c.db.audit(viewer.open_id, "check", "已连接" if result["ok"] else f"失败：{result['error']}")
            return c.done("/sync", "connected") if result["ok"] else RedirectResponse("/sync#feishu", status_code=303)
        if action == "schedule":
            try:
                saved = c.config.save("sync", {"enabled": enabled == "1", "hour": hour, "range_days": range_days}, viewer.open_id)
            except ConfigError as e:
                return oops(viewer, "定时同步没有保存", str(e), back, "sync")
            c.db.audit(viewer.open_id, "config", f"定时同步：{'开启' if saved['enabled'] else '关闭'}，每天 {saved['hour']} 点，同步前 {saved['range_days']} 天")
            return c.done("/sync", "saved")
        if action == "directory":
            if c.s.scheduler:
                c.directory.sync_now()
            else:  # 没有后台线程时（测试、调度被关掉）直接同步
                try:
                    c.directory.sync()
                except FeishuError as e:
                    return oops(viewer, "通讯录同步失败", str(e), back, "sync")
            c.db.audit(viewer.open_id, "directory", "手动同步通讯录")
            return c.done("/sync", "directory")
        return oops(viewer, "不认识的操作", "这个操作不存在。", back, "sync")

    @app.get("/sync/status")
    def sync_status(request: Request):
        """同步进度（页面每隔几秒来问一次）。"""
        viewer = c.user(request)
        if not viewer or not viewer.is_admin:
            return JSONResponse({"error": "没有权限"}, status_code=403 if viewer else 401)
        row = c.engine.active()
        if not row:
            return {"active": None}
        run = c.engine.run(row["id"])
        return {
            "active": {
                "id": run["id"], "status": run["status"], "status_label": run["status_label"], "done": run["done"], "units": run["stats"]["units"],
                "rows": run["total_rows"], "pages": run["stats"]["pages"], "failed": run["stats"]["failed"], "stop_requested": bool(run["stop_requested"]),
            }
        }  # fmt: skip

    # ---------- 执行日志 ----------
    @app.get("/logs")
    def logs_page(request: Request, status: str = "", start: str = "", end: str = ""):
        viewer, bad = guard(request, "logs")
        if bad:
            return bad
        page = page_number(request)
        runs, total = c.engine.runs(status, start, end, 30, (page - 1) * 30)
        return c.page("logs.html", viewer, "logs", request, runs=runs, pager=Pager(page, 30, total), statuses=STATUS_LABELS,
                      keep={"status": status if status in STATUS_LABELS else "", "start": start, "end": end})  # fmt: skip

    @app.get("/logs/{run_id}")
    def log_detail(request: Request, run_id: int):
        viewer, bad = guard(request, "logs")
        if bad:
            return bad
        run = c.engine.run(run_id)
        if not run:
            return c.notice(viewer, "没有这个任务", "这个同步任务不存在。", 404, ("回到执行日志", "/logs"), "logs")
        return c.page("log_detail.html", viewer, "logs", request, run=run)

    # ---------- 应用映射与周报推送 ----------
    def app_rows() -> list[dict]:
        """用量数据里出现过的应用，加上映射表里有的，各自现在显示什么名称。"""
        mapping = {r["app_id"]: r for r in c.db.q("SELECT * FROM app_mapping")}
        seen = {r["entity_id"]: r for r in c.db.q(
            "SELECT entity_id, MAX(entity_type) AS t, SUM(used_quota) AS amount, MAX(usage_date) AS last FROM usage_logs WHERE subject='app' GROUP BY entity_id"
        )}  # fmt: skip
        out = []
        for app_id in {*mapping, *seen}:
            m, s = mapping.get(app_id), seen.get(app_id)
            out.append({
                "id": app_id, "name": m["name"] if m else "", "source": m["source"] if m else "", "updated_at": m["updated_at"] if m else None,
                "type": entity_type(s["t"]) if s else "", "amount": round(s["amount"], 2) if s else 0, "last": s["last"] if s else "",
            })  # fmt: skip
        return sorted(out, key=lambda r: (bool(r["name"]), -r["amount"]))

    @app.get("/config")
    def config_page(request: Request):
        viewer, bad = guard(request, "config")
        if bad:
            return bad
        apps = app_rows()
        return c.page(
            "config.html", viewer, "config", request, apps=apps, unnamed=sum(1 for a in apps if not a["name"]), aily=c.s.aily_ready,
            price=c.config.point_price, report=c.config.get("report"), period=c.report.period(), runs=c.report.runs(), weekdays=WEEKDAYS,
            sources={"manual": "手工", "api": "开放平台", "aily": "Aily"},
        )  # fmt: skip

    @app.post("/config")
    def config_post(
        request: Request, action: str = Form(...), app_id: str = Form(""), name: str = Form(""), point_price: str = Form(""),
        enabled: str = Form(""), weekday: str = Form(""), hour: str = Form(""), scope: str = Form(""), since: str = Form(""),
    ):  # fmt: skip
        viewer, bad = c.poster(request)
        if bad:
            return bad
        back = ("回到映射与推送", "/config")
        if action == "mapping_save":
            if not app_id.strip() or not name.strip():
                return oops(viewer, "应用名称没有保存", "应用 ID 和名称都要填。", back, "config")
            c.engine.save_app_names({app_id.strip(): name}, "manual", viewer.open_id)
            c.db.audit(viewer.open_id, "mapping", f"{app_id.strip()} → {name.strip()}")
            return c.done("/config#apps", "mapping_saved")
        if action == "mapping_delete":
            c.db.x("DELETE FROM app_mapping WHERE app_id=?", (app_id,))
            c.db.audit(viewer.open_id, "mapping", f"删除 {app_id} 的名称映射")
            return c.done("/config#apps", "mapping_deleted")
        try:
            if action == "pricing":
                saved = c.config.save("pricing", {"point_price": point_price}, viewer.open_id)
                c.db.audit(viewer.open_id, "config", f"点数单价改为 {saved['point_price']} 元/点")
                return c.done("/config#pricing", "saved")
            if action == "report":
                saved = c.config.save("report", {"enabled": enabled == "1", "weekday": weekday, "hour": hour, "scope": scope, "since": since}, viewer.open_id)
                c.db.audit(viewer.open_id, "config", f"周报推送：{'开启' if saved['enabled'] else '关闭'}，{WEEKDAYS[saved['weekday']]} {saved['hour']} 点")
                return c.done("/config#report", "saved")
        except ConfigError as e:
            return oops(viewer, "没有保存", str(e), back, "config")
        if action == "report_test":
            run = c.report.push("test", viewer.open_id, only=[viewer.open_id])
            c.db.audit(viewer.open_id, "report", f"测试发送：{run['status']}")
            if run["status"] != "success":
                return oops(viewer, "测试周报没有发出去", run["error"] or "发送失败。请确认应用已开启机器人能力，并开通 im:message:send_as_bot 权限。", back, "config")
            return c.done("/config#report", "report_test")
        if action == "report_push":
            threading.Thread(target=c.report.push, args=("manual", viewer.open_id), name="weekly-report", daemon=True).start()
            c.db.audit(viewer.open_id, "report", "手动全量推送")
            return c.done("/config#report", "report_pushed")
        return oops(viewer, "不认识的操作", "这个操作不存在。", back, "config")

    # ---------- 接入管理 ----------
    def connect_view(request: Request, viewer, secret: str = "", status: int = 200):
        tokens = [{**dict(t), "expired": t["expires_at"] is not None and t["expires_at"] <= time.time()} for t in c.db.tokens_of(viewer.open_id)]
        return c.page(
            "connect.html", viewer, "connect", request, status, tokens=tokens, secret=secret, endpoints=ENDPOINTS, base=c.s.public_url,
            prompt=connect_prompt(c.s.public_url, secret) if secret else "", token_days=TOKEN_DAYS, role_label=viewer.role_label,
        )  # fmt: skip

    @app.get("/connect")
    def connect_page(request: Request):
        viewer = c.user(request)
        if not viewer:
            return c.need_login(request)
        return connect_view(request, viewer)

    @app.post("/connect")
    def connect_post(request: Request, action: str = Form(...), label: str = Form(""), days: int = Form(90), token: str = Form("")):
        viewer, bad = c.poster(request, admin=False)
        if bad:
            return bad
        if action == "revoke":
            if c.db.revoke_token(viewer.open_id, token):
                c.db.audit(viewer.open_id, "token_revoke", "")
            return c.done("/connect", "revoked")
        if len(c.db.tokens_of(viewer.open_id)) >= 10:
            return c.notice(viewer, "令牌太多了", "每个人最多保留 10 个令牌，请先撤销不用的。", 400, ("回到接入管理", "/connect"), "connect")
        name = label.strip()[:40] or f"令牌 {time.strftime('%Y-%m-%d %H:%M')}"
        secret = c.new_token(viewer.open_id, name, days if days in TOKEN_DAYS and days else None)
        # 令牌明文只在这一次响应里出现（不跳转、不进地址栏、不落库）
        return connect_view(request, viewer, secret)

    # ---------- 角色权限 ----------
    @app.get("/roles")
    def roles_page(request: Request):
        viewer, bad = guard(request, "roles")
        if bad:
            return bad
        leaders = sum(1 for d in c.directory.depts() if d.leader_id)
        return c.page("roles.html", viewer, "roles", request, admins=c.access.admins(), labels=ROLE_LABELS, notes=ROLE_NOTES, admin=ADMIN, regular=REGULAR,
                      headcount=c.directory.total_headcount(), leaders=leaders)  # fmt: skip

    @app.post("/roles")
    def roles_post(request: Request, action: str = Form(...), user: str = Form("")):
        viewer, bad = c.poster(request)
        if bad:
            return bad
        back = ("回到角色权限", "/roles")
        if action == "grant":
            hits = [user] if c.directory.user(user) else [u.open_id for u in c.directory.search_users(user, 2)]
            if len(hits) != 1:
                return oops(viewer, "没有加上", f"没找到唯一匹配「{user}」的人，请从联想列表里选一个。", back, "roles")
            c.access.grant(hits[0], viewer.open_id)
            c.db.audit(viewer.open_id, "role_grant", c.directory.user_name(hits[0]))
            return c.done("/roles", "granted")
        if action == "revoke":
            if user == viewer.open_id:
                return oops(viewer, "不能取消自己", "为了不把自己锁在外面，不能取消自己的运营管理员角色。请让另一位运营管理员来操作。", back, "roles")
            if c.access.revoke(user):
                c.db.audit(viewer.open_id, "role_revoke", c.directory.user_name(user))
            return c.done("/roles", "role_removed")
        return oops(viewer, "不认识的操作", "这个操作不存在。", back, "roles")

    # ---------- 操作审计 ----------
    @app.get("/audit")
    def audit_page(request: Request, action: str = ""):
        viewer, bad = guard(request, "audit")
        if bad:
            return bad
        page = page_number(request)
        where, params = ("action=?", [action]) if action in AUDIT_ACTIONS else ("1=1", [])
        total = c.db.val(f"SELECT COUNT(*) FROM audit_log WHERE {where}", params, 0)
        rows = [
            {**dict(r), "actor_name": "系统" if r["actor"] == "system" else c.directory.user_name(r["actor"]), "action_label": AUDIT_ACTIONS.get(r["action"], r["action"])}
            for r in c.db.q(f"SELECT * FROM audit_log WHERE {where} ORDER BY id DESC LIMIT 100 OFFSET ?", [*params, (page - 1) * 100])
        ]
        return c.page("audit.html", viewer, "audit", request, rows=rows, pager=Pager(page, 100, total), actions=AUDIT_ACTIONS, keep={"action": action if action in AUDIT_ACTIONS else ""})

"""开放接口（只读）：/openapi/v1/...，用个人访问令牌（Authorization: Bearer aius_...）。

令牌绑定的是一个人；每个接口能返回什么数据，都按这个人的角色重新判断（和网页上一样）：
- self：每个人都能用，只返回自己的数据；
- dept：部门负责人能看自己负责的部门（含下级），运营管理员能看所有部门；
- admin：只有运营管理员能用。
所以不存在「换一个 open_id 参数就能看别人数据」的接口。远程 MCP（remote_mcp.py）调用的是同一套方法。
"""

from __future__ import annotations

from datetime import date

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import labels
from .access import Viewer
from .analytics import Filter
from .core import Ctx
from .feishu import ROOT
from .sync import MAX_DAYS

# (路径, 谁能用, 说明)：接入管理页面按这个列表生成接口文档
ENDPOINTS = [
    ("/openapi/v1/me", "self", "当前令牌对应的身份、角色、负责的部门"),
    ("/openapi/v1/self-usage/summary", "self", "我的用量汇总：点数、折算费用、使用次数、活跃天数、环比"),
    ("/openapi/v1/self-usage/trend", "self", "我每天的用量"),
    ("/openapi/v1/self-usage/scenarios", "self", "我的用量按场景和额度类型的分布"),
    ("/openapi/v1/self-usage/logs", "self", "我的用量日志明细（分页）"),
    ("/openapi/v1/self-usage/details", "self", "我的统计详情：每天各额度类型的用量（T+1）"),
    ("/openapi/v1/self-usage/department", "self", "我所在部门的整体用量、人均和我的占比（不含其他人的个人数据）"),
    ("/openapi/v1/departments", "dept", "我能查看的部门列表"),
    ("/openapi/v1/departments/{id}/summary", "dept", "一个部门（含下级）的指标、每日趋势和下级部门汇总"),
    ("/openapi/v1/departments/{id}/members", "dept", "一个部门（含下级）成员的用量排行"),
    ("/openapi/v1/departments/{id}/scenarios", "dept", "一个部门（含下级）的场景分布"),
    ("/openapi/v1/analytics/overview", "admin", "全企业指标和本期解读"),
    ("/openapi/v1/analytics/trend", "admin", "全企业每天的用量、活跃人数、人均用量"),
    ("/openapi/v1/analytics/ranking", "admin", "排行：dimension=department | member | app | scenario"),
    ("/openapi/v1/analytics/anomalies", "admin", "异常提醒：突增、快速增长、用量集中、单次用量偏高"),
    ("/openapi/v1/analytics/breakdown", "admin", "拆分：by=feature（额度类型）| usage_type（付费/免费）| hour（时段）"),
    ("/openapi/v1/analytics/dept-tree", "admin", "部门汇总与下钻：parent 的下级部门各自的用量（不传 parent 是一级部门）"),
    ("/openapi/v1/analytics/doubao", "admin", "豆包工作的用量：指标、趋势、部门、场景"),
    ("/openapi/v1/usage-logs", "admin", "用量日志明细（分页；可按 subject、q、member、dept 筛选）"),
    ("/openapi/v1/usage-details", "admin", "统计详情（分页；可按 subject 筛选）"),
    ("/openapi/v1/quotas", "admin", "额度分配及使用率"),
    ("/openapi/v1/sync-runs", "admin", "最近的同步任务"),
]
WHO = {"self": "所有人", "dept": "部门负责人 / 运营管理员", "admin": "运营管理员"}


class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _span(start: str, end: str) -> dict:
    return {"start": start, "end": end}


class Api:
    """开放接口和 MCP 共用的只读查询。每个方法的第一个参数都是调用者（令牌绑定的人），权限在方法里判断。"""

    def __init__(self, c: Ctx):
        self.c = c
        self.an = c.analytics

    # ---------- 参数 ----------
    def span(self, days: int | None = None, start: str = "", end: str = "") -> tuple[str, str]:
        if start or end:
            try:
                first, last = date.fromisoformat(start), date.fromisoformat(end)
            except ValueError:
                raise ApiError(400, "start 和 end 要同时给，格式是 YYYY-MM-DD") from None
            if first > last or (last - first).days + 1 > MAX_DAYS:
                raise ApiError(400, f"start 不能晚于 end，而且一次最多查 {MAX_DAYS} 天")
            return first.isoformat(), last.isoformat()
        days = 30 if days is None else days
        if not 1 <= days <= MAX_DAYS:
            raise ApiError(400, f"days 要在 1 到 {MAX_DAYS} 之间")
        return self.an.default_range(days)

    @staticmethod
    def paging(page: int, size: int) -> tuple[int, int]:
        if page < 1 or not 1 <= size <= 200:
            raise ApiError(400, "page 从 1 开始，size 在 1 到 200 之间")
        return size, (page - 1) * size

    def admin(self, v: Viewer) -> None:
        if not v.is_admin:
            raise ApiError(403, "这个接口只有运营管理员能用")

    def dept(self, v: Viewer, dept_id: str) -> None:
        if not self.c.directory.dept(dept_id):
            raise ApiError(404, "没有这个部门")
        if not self.c.access.can_see_dept(v, dept_id):
            raise ApiError(403, "这个部门不在你负责的范围内")

    def dept_filter(self, dept_id: str, start: str, end: str) -> Filter:
        return Filter(start, end, depts=frozenset(self.c.directory.subtree(dept_id)))

    # ---------- self ----------
    def me(self, v: Viewer) -> dict:
        d = self.c.directory
        return {
            "open_id": v.open_id,
            "name": v.label,
            "role": v.role,
            "role_label": v.role_label,
            "department": d.dept_path(d.user_dept(v.open_id)),
            "led_departments": [{"id": i, "path": d.dept_path(i)} for i in v.led],
            "data_latest_date": self.an.latest_date(),
            "server": self.c.s.public_url,
        }

    def my_summary(self, v: Viewer, start: str, end: str) -> dict:
        p = self.an.member_profile(v.open_id, start, end)
        return {k: p[k] for k in ("name", "dept_path", "start", "end", "days", "total", "cost", "records", "active_days", "prev_total", "change")}

    def my_trend(self, v: Viewer, start: str, end: str) -> dict:
        cur = self.an.slice(Filter(start, end, member=v.open_id))
        return {**_span(start, end), "daily": [{"date": d["date"], "amount": d["total"]} for d in self.an.daily(cur)]}

    def my_scenarios(self, v: Viewer, start: str, end: str) -> dict:
        f = Filter(start, end, member=v.open_id)
        return {
            **_span(start, end),
            "products": self.an.scenarios(f, level=1, limit=None),
            "scenarios": self.an.scenarios(f, level=0, limit=30),
            "features": self.an.split(f, "feature_key"),
        }

    def my_logs(self, v: Viewer, start: str, end: str, page: int = 1, size: int = 50) -> dict:
        limit, offset = self.paging(page, size)
        rows, total, amount = self.an.logs(Filter(start, end, member=v.open_id), limit=limit, offset=offset)
        return {**_span(start, end), "total": total, "amount": amount, "page": page, "size": size, "items": [_log(r) for r in rows]}

    def my_details(self, v: Viewer, start: str, end: str, page: int = 1, size: int = 50) -> dict:
        limit, offset = self.paging(page, size)
        rows, total = self.an.details(start, end, member=v.open_id, limit=limit, offset=offset)
        return {**_span(start, end), "total": total, "page": page, "size": size, "quota_types": list(labels.FEATURE_KEYS.values()), "items": [_detail(r) for r in rows]}

    def my_department(self, v: Viewer, start: str, end: str) -> dict:
        p = self.an.member_profile(v.open_id, start, end)
        if not p["dept"]:
            raise ApiError(404, "通讯录里没有你的部门信息")
        return {**_span(start, end), "path": p["dept_path"], **p["dept"]}

    # ---------- dept ----------
    def departments(self, v: Viewer) -> dict:
        d = self.c.directory
        depts = d.children(ROOT) if v.is_admin else [x for x in (d.dept(i) for i in v.led) if x]
        return {"items": [{"id": x.id, "name": x.name, "path": d.dept_path(x.id), "headcount": d.headcount(x.id), "leader": d.user_name(x.leader_id) if x.leader_id else ""} for x in depts]}

    def dept_summary(self, v: Viewer, dept_id: str, start: str, end: str) -> dict:
        self.dept(v, dept_id)
        f = self.dept_filter(dept_id, start, end)
        cur, prev = self.an.slice(f), self.an.previous(f)
        return {
            "id": dept_id,
            "path": self.c.directory.dept_path(dept_id),
            "headcount": self.c.directory.headcount(dept_id),
            "kpis": self.an.kpis(cur, prev),
            "daily": self.an.daily(cur),
            "children": self.an.rank_depts(cur, prev, dept_id),
        }

    def dept_members(self, v: Viewer, dept_id: str, start: str, end: str, limit: int = 50) -> dict:
        self.dept(v, dept_id)
        f = self.dept_filter(dept_id, start, end)
        cur = self.an.slice(f)
        main = self.an.top_scenario_by_member(f)
        rows = self.an.rank_members(cur, self.an.previous(f), max(1, min(limit, 500)))
        return {**_span(start, end), "active_members": cur.active_members, "items": [{**_member(m), "main_scenario": main.get(m["id"], "")} for m in rows]}

    def dept_scenarios(self, v: Viewer, dept_id: str, start: str, end: str) -> dict:
        self.dept(v, dept_id)
        f = self.dept_filter(dept_id, start, end)
        return {**_span(start, end), "products": self.an.scenarios(f, level=1, limit=None), "scenarios": self.an.scenarios(f, level=0, limit=30)}

    # ---------- admin ----------
    def overview(self, v: Viewer, start: str, end: str) -> dict:
        self.admin(v)
        o = self.an.overview(Filter(start, end))
        return {"kpis": o["kpis"], "narrative": o["narrative"], "anomalies": [_anomaly(a) for a in o["anomalies"]]}

    def trend(self, v: Viewer, start: str, end: str) -> dict:
        self.admin(v)
        return {**_span(start, end), "daily": self.an.daily(self.an.slice(Filter(start, end)))}

    def ranking(self, v: Viewer, dimension: str, start: str, end: str, limit: int = 20) -> dict:
        self.admin(v)
        f = Filter(start, end)
        limit = max(1, min(limit, 500))
        if dimension == "scenario":
            items = self.an.scenarios(f, level=0, limit=limit)
        else:
            cur, prev = self.an.slice(f), self.an.previous(f)
            if dimension == "department":
                items = [{k: d[k] for k in ("id", "name", "path", "amount", "share", "users", "headcount", "per_capita", "change")} for d in self.an.rank_depts(cur, prev)][:limit]
            elif dimension == "member":
                items = [_member(m) for m in self.an.rank_members(cur, prev, limit)]
            elif dimension == "app":
                items = [{k: a[k] for k in ("id", "name", "type", "amount", "share", "records", "change")} for a in self.an.rank_apps(cur, prev, limit)]
            else:
                raise ApiError(400, "dimension 只能是 department、member、app 或 scenario")
        return {**_span(start, end), "dimension": dimension, "items": items}

    def anomalies(self, v: Viewer, start: str, end: str) -> dict:
        self.admin(v)
        f = Filter(start, end)
        return {**_span(start, end), "items": [_anomaly(a) for a in self.an.anomalies(self.an.slice(f), self.an.previous(f))]}

    def breakdown(self, v: Viewer, by: str, start: str, end: str) -> dict:
        self.admin(v)
        f = Filter(start, end)
        if by == "feature":
            items = self.an.split(f, "feature_key")
        elif by == "usage_type":
            items = self.an.split(f, "usage_type")
        elif by == "hour":
            items = [{"hour": h["hour"], "amount": h["total"], "records": h["records"]} for h in self.an.hourly(f)]
        else:
            raise ApiError(400, "by 只能是 feature、usage_type 或 hour")
        return {**_span(start, end), "by": by, "items": items}

    def dept_tree(self, v: Viewer, parent: str, start: str, end: str) -> dict:
        self.admin(v)
        parent = parent or ROOT
        if parent != ROOT and not self.c.directory.dept(parent):
            raise ApiError(404, "没有这个部门")
        f = Filter(start, end) if parent == ROOT else self.dept_filter(parent, start, end)
        return {**_span(start, end), "parent": parent, "items": self.an.rank_depts(self.an.slice(f), self.an.previous(f), parent)}

    def doubao(self, v: Viewer, start: str, end: str) -> dict:
        self.admin(v)
        f = Filter(start, end, doubao=True)
        cur, prev = self.an.slice(f), self.an.previous(f)
        k = self.an.kpis(cur, prev)
        return {
            **_span(start, end),
            "kpis": {x: k[x] for x in ("total", "cost", "active_members", "avg_dau", "per_capita", "records", "prev_total", "change")},
            "daily": [{x: d[x] for x in ("date", "total", "users", "per_capita")} for d in self.an.daily(cur)],
            "departments": self.an.rank_depts(cur, prev),
            "scenarios": self.an.scenarios(f, level=0, limit=30),
        }

    def usage_logs(self, v: Viewer, start: str, end: str, subject: str = "", q: str = "", member: str = "", dept: str = "", page: int = 1, size: int = 50) -> dict:
        self.admin(v)
        limit, offset = self.paging(page, size)
        if dept and not self.c.directory.dept(dept):
            raise ApiError(404, "没有这个部门")
        f = Filter(start, end, member=member or None, depts=frozenset(self.c.directory.subtree(dept)) if dept else None)
        rows, total, amount = self.an.logs(f, subject=subject, query=q, limit=limit, offset=offset)
        return {**_span(start, end), "total": total, "amount": amount, "page": page, "size": size, "items": [_log(r) for r in rows]}

    def usage_details(self, v: Viewer, start: str, end: str, subject: str = "", page: int = 1, size: int = 50) -> dict:
        self.admin(v)
        limit, offset = self.paging(page, size)
        rows, total = self.an.details(start, end, subject, limit=limit, offset=offset)
        return {**_span(start, end), "total": total, "page": page, "size": size, "quota_types": list(labels.FEATURE_KEYS.values()), "items": [_detail(r) for r in rows]}

    def quotas(self, v: Viewer) -> dict:
        self.admin(v)
        rows = self.c.quotas.list()
        keys = ("id", "dimension_label", "name", "target_id", "amount", "used", "remaining", "rate", "state_label", "period_start", "period_end", "period", "note")
        return {"summary": self.c.quotas.summary(rows), "items": [{k: r[k] for k in keys} for r in rows]}

    def sync_runs(self, v: Viewer, limit: int = 10) -> dict:
        self.admin(v)
        runs, total = self.c.engine.runs(limit=max(1, min(limit, 100)))
        keys = ("id", "trigger", "actor_name", "start_date", "end_date", "status", "status_label", "stats", "total_rows", "seconds", "error", "created_at", "finished_at")
        return {"total": total, "items": [{k: r[k] for k in keys} for r in runs]}


def _log(r: dict) -> dict:
    return {k: r[k] for k in ("date", "ts", "subject", "entity_id", "name", "entity_type", "dept", "scenario", "feature", "usage_type", "amount", "cost", "notes")}


def _detail(r: dict) -> dict:
    return {k: r[k] for k in ("date", "subject", "entity_id", "name", "entity_type", "quotas", "total")}


def _member(m: dict) -> dict:
    return {k: m[k] for k in ("id", "name", "dept_path", "amount", "share", "records", "active_days", "last_used", "change")}


def _anomaly(a: dict) -> dict:
    return {k: a[k] for k in ("type", "level", "title", "dimension", "text")}


def register(app: FastAPI, c: Ctx) -> None:
    api = Api(c)
    app.state.api = api

    def run(request: Request, fn, *args, **kwargs):
        """统一的认证和出错格式：{"error": 说明}。"""
        viewer = c.bearer(request.headers.get("authorization"))
        if not viewer:
            return JSONResponse({"error": "缺少或无效的访问令牌。请在平台的「接入管理」里生成令牌，放在请求头 Authorization: Bearer <令牌> 里。"}, status_code=401)
        try:
            return fn(viewer, *args, **kwargs)
        except ApiError as e:
            return JSONResponse({"error": e.detail}, status_code=e.status)

    def ranged(request: Request, fn, *args, days: int | None, start: str, end: str, **kwargs):
        try:
            first, last = api.span(days, start, end)
        except ApiError as e:
            return JSONResponse({"error": e.detail}, status_code=e.status)
        return run(request, fn, *args, first, last, **kwargs)

    @app.get("/openapi/v1/me")
    def me(request: Request):
        return run(request, api.me)

    @app.get("/openapi/v1/self-usage/summary")
    def my_summary(request: Request, days: int | None = None, start: str = "", end: str = ""):
        return ranged(request, api.my_summary, days=days, start=start, end=end)

    @app.get("/openapi/v1/self-usage/trend")
    def my_trend(request: Request, days: int | None = None, start: str = "", end: str = ""):
        return ranged(request, api.my_trend, days=days, start=start, end=end)

    @app.get("/openapi/v1/self-usage/scenarios")
    def my_scenarios(request: Request, days: int | None = None, start: str = "", end: str = ""):
        return ranged(request, api.my_scenarios, days=days, start=start, end=end)

    @app.get("/openapi/v1/self-usage/logs")
    def my_logs(request: Request, days: int | None = None, start: str = "", end: str = "", page: int = 1, size: int = 50):
        return ranged(request, api.my_logs, days=days, start=start, end=end, page=page, size=size)

    @app.get("/openapi/v1/self-usage/details")
    def my_details(request: Request, days: int | None = None, start: str = "", end: str = "", page: int = 1, size: int = 50):
        return ranged(request, api.my_details, days=days, start=start, end=end, page=page, size=size)

    @app.get("/openapi/v1/self-usage/department")
    def my_department(request: Request, days: int | None = None, start: str = "", end: str = ""):
        return ranged(request, api.my_department, days=days, start=start, end=end)

    @app.get("/openapi/v1/departments")
    def departments(request: Request):
        return run(request, api.departments)

    @app.get("/openapi/v1/departments/{dept_id}/summary")
    def dept_summary(request: Request, dept_id: str, days: int | None = None, start: str = "", end: str = ""):
        return ranged(request, api.dept_summary, dept_id, days=days, start=start, end=end)

    @app.get("/openapi/v1/departments/{dept_id}/members")
    def dept_members(request: Request, dept_id: str, days: int | None = None, start: str = "", end: str = "", limit: int = 50):
        return ranged(request, api.dept_members, dept_id, days=days, start=start, end=end, limit=limit)

    @app.get("/openapi/v1/departments/{dept_id}/scenarios")
    def dept_scenarios(request: Request, dept_id: str, days: int | None = None, start: str = "", end: str = ""):
        return ranged(request, api.dept_scenarios, dept_id, days=days, start=start, end=end)

    @app.get("/openapi/v1/analytics/overview")
    def overview(request: Request, days: int | None = None, start: str = "", end: str = ""):
        return ranged(request, api.overview, days=days, start=start, end=end)

    @app.get("/openapi/v1/analytics/trend")
    def trend(request: Request, days: int | None = None, start: str = "", end: str = ""):
        return ranged(request, api.trend, days=days, start=start, end=end)

    @app.get("/openapi/v1/analytics/ranking")
    def ranking(request: Request, dimension: str = "department", days: int | None = None, start: str = "", end: str = "", limit: int = 20):
        return ranged(request, api.ranking, dimension, days=days, start=start, end=end, limit=limit)

    @app.get("/openapi/v1/analytics/anomalies")
    def anomalies(request: Request, days: int | None = None, start: str = "", end: str = ""):
        return ranged(request, api.anomalies, days=days, start=start, end=end)

    @app.get("/openapi/v1/analytics/breakdown")
    def breakdown(request: Request, by: str = "feature", days: int | None = None, start: str = "", end: str = ""):
        return ranged(request, api.breakdown, by, days=days, start=start, end=end)

    @app.get("/openapi/v1/analytics/dept-tree")
    def dept_tree(request: Request, parent: str = "", days: int | None = None, start: str = "", end: str = ""):
        return ranged(request, api.dept_tree, parent, days=days, start=start, end=end)

    @app.get("/openapi/v1/analytics/doubao")
    def doubao(request: Request, days: int | None = None, start: str = "", end: str = ""):
        return ranged(request, api.doubao, days=days, start=start, end=end)

    @app.get("/openapi/v1/usage-logs")
    def usage_logs(
        request: Request, days: int | None = None, start: str = "", end: str = "", subject: str = "", q: str = "", member: str = "", dept: str = "",
        page: int = 1, size: int = 50,
    ):  # fmt: skip
        return ranged(request, api.usage_logs, days=days, start=start, end=end, subject=subject, q=q, member=member, dept=dept, page=page, size=size)

    @app.get("/openapi/v1/usage-details")
    def usage_details(request: Request, days: int | None = None, start: str = "", end: str = "", subject: str = "", page: int = 1, size: int = 50):
        return ranged(request, api.usage_details, days=days, start=start, end=end, subject=subject, page=page, size=size)

    @app.get("/openapi/v1/quotas")
    def quotas(request: Request):
        return run(request, api.quotas)

    @app.get("/openapi/v1/sync-runs")
    def sync_runs(request: Request, limit: int = 10):
        return run(request, api.sync_runs, limit)

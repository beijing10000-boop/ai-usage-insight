"""分析页面（浏览器 Cookie 会话）：AI 门户、运营驾驶舱、部门运营、豆包运营、用量明细、我的用量。

能看什么数据由角色决定（见 access.py）：运营管理员看全量；部门负责人看自己负责的部门；其他人只看自己。
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse, Response

from . import excel, labels, md, web
from .access import Viewer
from .analytics import Filter, month_name, r2
from .core import Ctx, Range
from .feishu import ROOT
from .insight import MAX_QUESTION, PERSONAL_SUGGESTIONS, SUGGESTIONS
from .sync import today

PAGE_SIZE = 50
EXPORT_LIMIT = 100000
MAX_PAGE = 100000


@dataclass
class Pager:
    page: int
    size: int
    total: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.size))

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size

    @property
    def first(self) -> int:
        return self.offset + 1 if self.total else 0

    @property
    def last(self) -> int:
        return min(self.total, self.offset + self.size)


def page_number(request: Request) -> int:
    try:
        return min(MAX_PAGE, max(1, int(request.query_params.get("page", 1))))
    except ValueError:
        return 1


def small_int(raw: str | None) -> int | None:
    """地址参数里的小整数（额度类型、用量类型）；不是 1–99 的整数就当没给。"""
    return int(raw) if raw and raw.isascii() and raw.isdigit() and len(raw) <= 2 else None


GRANULARITY = ("day", "week", "month")


def granularity(request: Request) -> str:
    by = request.query_params.get("by", "day")
    return by if by in GRANULARITY else "day"


# ---------- 图表数据：每个点是 {label 横轴文字, title 提示里的标题, value 数值, lines 提示里的补充行} ----------
# 按周 / 按月汇总时 title 里写这一周 / 这个月（范围两头不完整时写明只算了哪几天）
def usage_chart(daily: list[dict], split: bool = True) -> dict:
    points = []
    for d in daily:
        lines = [f"人员 {web.fmt_num(d['member'])} 点 · 应用 {web.fmt_num(d['app'])} 点"] if split else []
        lines.append(f"{d['users']} 人使用")
        points.append({"label": d["label"], "title": d.get("title", d["date"]), "value": d["total"], "lines": lines})
    return {"kind": "bar", "unit": "点", "points": points}


def people_chart(daily: list[dict]) -> dict:
    return {"kind": "line", "unit": "人", "points": [{"label": d["label"], "title": d.get("title", d["date"]), "value": d["users"], "lines": []} for d in daily]}


def per_capita_chart(daily: list[dict]) -> dict:
    return {
        "kind": "line",
        "unit": "点",
        "points": [{"label": d["label"], "title": d.get("title", d["date"]), "value": d["per_capita"], "lines": [f"{d['users']} 人使用"]} for d in daily],
    }


def hourly_chart(hourly: list[dict]) -> dict:
    return {
        "kind": "bar",
        "unit": "点",
        "points": [{"label": str(h["hour"]), "title": f"{h['hour']}:00 – {h['hour']}:59", "value": h["total"], "lines": [f"{h['records']} 次使用"]} for h in hourly],
    }


def register(app: FastAPI, c: Ctx) -> None:
    an = c.analytics

    def crumbs(dept_id: str) -> list[tuple[str, str]]:
        """部门下钻的路径：[(部门 ID, 名称)]，从最上级到当前。"""
        return [(d, c.directory.dept_name(d)) for d in reversed(c.directory.chain(dept_id))]

    # ---------- AI 门户 ----------
    @app.get("/")
    @app.get("/overview")
    def portal(request: Request):
        viewer = c.user(request)
        if not viewer:
            return c.need_login(request)
        rng = c.pick_range(request)
        sc = c.scope(viewer, rng.start, rng.end, rng.monthly)
        f = sc.filter
        cur, prev = an.slice(f), an.previous(f)
        personal = f.member is not None
        # 「查看使用分布」去哪：运营管理员去驾驶舱，部门负责人去部门运营，其他人去我的用量
        more = "/dashboard#scenarios" if viewer.is_admin else ("/dept-ops" if viewer.led else "/self-usage")
        return c.page(
            "portal.html", viewer, "portal", request,
            rng=rng, scope=sc.label, personal=personal, k=an.kpis(cur, prev), more=more,
            allowance=an.allowance(c.allowance_month(rng), depts=f.depts, member=f.member, limit=1),
            anomalies=[] if personal else an.anomalies(cur, prev, sc.parent, sc.by_dept),
            scenarios=an.scenarios(f, level=1, limit=5),
            suggestions=PERSONAL_SUGGESTIONS if personal else SUGGESTIONS,
            llm=c.insight.ready, max_question=MAX_QUESTION,
        )  # fmt: skip

    @app.post("/insight/ask")
    async def insight_ask(request: Request):
        try:
            body = await request.json()
            question = str(body.get("question", "")).strip() if isinstance(body, dict) else None
        except Exception:
            question = None
        # 登录态、取数、调模型、记审计都要读写数据库或等网络，放到线程里做，不占事件循环
        return await run_in_threadpool(answer, request, question)

    def answer(request: Request, question: str | None):
        viewer = c.user(request)
        if not viewer:
            return JSONResponse({"error": "登录已过期，请刷新页面重新登录。"}, status_code=401)
        if not c.origin_ok(request):
            return JSONResponse({"error": "来源校验失败。"}, status_code=403)
        if question is None:
            return JSONResponse({"error": "请求格式不对。"}, status_code=400)
        if not question:
            return JSONResponse({"error": "请先输入问题。"}, status_code=400)
        if not c.may_ask(viewer.open_id):
            return JSONResponse({"error": "问得太快了，稍等几秒再问。"}, status_code=429)
        rng = c.pick_range(request)
        sc = c.scope(viewer, rng.start, rng.end, rng.monthly)
        result = c.insight.ask(question, sc.filter, sc.label, sc.parent)
        c.db.audit(viewer.open_id, "insight_ask", question[:200])
        return {"html": md.render(result["answer"]), "model": result["model"], "scope": sc.label, "range": rng.label}

    # ---------- 运营驾驶舱 ----------
    def cockpit(viewer: Viewer, rng: Range, dept: str, by: str = "day") -> dict:
        parent = dept if dept and c.directory.dept(dept) else ROOT
        f = rng.filter(depts=frozenset(c.directory.subtree(parent))) if parent != ROOT else rng.filter()
        o = an.overview(f, parent, by)
        picked = [d for d in c.db.pref(viewer.open_id, "rollup", []) if c.directory.dept(d)]
        rollup = an.rollup(an.slice(rng.filter()), picked) if picked else None
        return {"o": o, "f": f, "parent": parent, "crumbs": crumbs(parent) if parent != ROOT else [], "rollup": rollup, "picked": picked}

    @app.get("/dashboard")
    def dashboard(request: Request, dept: str = ""):
        viewer = c.user(request)
        if not viewer:
            return c.need_login(request)
        if denied := c.admin_only(viewer, "dashboard"):
            return denied
        rng, by = c.pick_range(request), granularity(request)
        data = cockpit(viewer, rng, dept, by)
        o = data["o"]
        quotas = [q for q in c.quotas.list() if q["current"]]
        return c.page(
            "dashboard.html", viewer, "dashboard", request, rng=rng, by=by, **data,
            usage=usage_chart(o["trend"], split=data["parent"] == ROOT), people=people_chart(o["trend"]), hours=hourly_chart(o["hourly"]),
            quotas=sorted(quotas, key=lambda q: -(q["rate"] or 0))[:5], quota_summary=c.quotas.summary(quotas),
            allowance=an.allowance(c.allowance_month(rng), depts=data["f"].depts),
        )  # fmt: skip

    @app.post("/dashboard/rollup")
    def dashboard_rollup(request: Request, action: str = Form(...), dept: str = Form(""), next: str = Form("/dashboard")):
        viewer, bad = c.poster(request)
        if bad:
            return bad
        picked = [d for d in c.db.pref(viewer.open_id, "rollup", []) if c.directory.dept(d)]
        if action == "add":
            hits = [dept] if c.directory.dept(dept) else [d.id for d in c.directory.search_depts(dept, 2)]
            if len(hits) != 1:
                return c.notice(viewer, "没有加上", f"没找到唯一匹配「{dept}」的部门，请从联想列表里选一个。", 400, ("回到驾驶舱", web.safe_next(next)), "dashboard")
            if hits[0] not in picked:
                picked.append(hits[0])
        elif action == "remove":
            picked = [d for d in picked if d != dept]
        else:
            picked = []
        c.db.set_pref(viewer.open_id, "rollup", picked[:30])
        return c.done(web.safe_next(next) + "#rollup", "rollup_saved")

    @app.get("/dashboard/export.xlsx")
    def dashboard_export(request: Request, dept: str = ""):
        viewer = c.user(request)
        if not viewer:
            return c.need_login(request)
        if denied := c.admin_only(viewer, "dashboard"):
            return denied
        rng = c.pick_range(request)
        data = cockpit(viewer, rng, dept)
        o, k = data["o"], data["o"]["kpis"]
        cur = an.slice(data["f"])
        sheets = [
            ("指标", ["指标", "数值"], [
                ["时间范围", f"{rng.start} ~ {rng.end}"], ["总用量（点）", k["total"]], ["人员用量（点）", k["member_total"]], ["应用用量（点）", k["app_total"]],
                ["折算费用（元）", k["cost"]], ["点数单价（元）", k["point_price"]], [f"环比（%，和{k['prev_label']} {k['prev_start']} ~ {k['prev_end']} 比）", k.get("change")],
                ["活跃人数", k["active_members"]], ["活跃部门数", k["active_depts"]], ["活跃应用数", k["active_apps"]], ["日均用量（点）", k["daily_avg"]],
                ["人均用量（点）", k["per_capita"]], ["用量最高 10 人占比（%）", k["top10_share"]], ["使用记录数", k["records"]],
            ]),
            ("每日趋势", ["日期", "总用量", "人员用量", "应用用量", "活跃人数", "人均用量"], [[d["date"], d["total"], d["member"], d["app"], d["users"], d["per_capita"]] for d in o["daily"]]),
            ("每月汇总", ["月份", "从", "到", "总用量", "人员用量", "应用用量", "活跃人数", "人均用量"],
             [[d["date"], d["start"], d["end"], d["total"], d["member"], d["app"], d["users"], d["per_capita"]] for d in an.periods(cur, "month")]),
            ("部门", ["部门", "路径", "用量", "占比（%）", "活跃人数", "部门人数", "人均用量", "环比（%）"], [[d["name"], d["path"], d["amount"], d["share"], d["users"], d["headcount"], d["per_capita"], d["change"]] for d in o["depts"]]),
            ("人员", ["姓名", "部门", "用量", "占比（%）", "使用次数", "活跃天数", "最近使用"], [[m["name"], m["dept_path"], m["amount"], m["share"], m["records"], m["active_days"], m["last_used"]] for m in an.rank_members(cur, None, None)]),
            ("应用", ["应用", "应用 ID", "类型", "用量", "占比（%）", "使用次数", "环比（%）"], [[a["name"], a["id"], a["type"], a["amount"], a["share"], a["records"], a["change"]] for a in o["apps"]]),
            ("场景", ["场景", "用量", "占比（%）", "使用人数", "使用次数"], [[s["name"], s["amount"], s["share"], s["users"], s["records"]] for s in an.scenarios(cur.f, level=0, limit=None)]),
        ]  # fmt: skip
        if allow := an.allowance(c.allowance_month(rng), depts=data["f"].depts, limit=None):
            sheets.append((
                "月度额度", ["姓名", "部门", f"{allow['label']} 已用（点）", "每人每月基础额度（点）", "占基础额度（%）", "基础额度剩余（点）", "超出基础额度（点）"],
                [[m["name"], m["dept_path"], m["amount"], allow["per_member"], m["rate"], m["remaining"], m["over"]] for m in allow["members"]],
            ))  # fmt: skip
        c.db.audit(viewer.open_id, "export", f"驾驶舱 {rng.start} ~ {rng.end}")
        return xlsx(excel.workbook(sheets), f"AI用量驾驶舱_{rng.start}_{rng.end}.xlsx")

    # ---------- 部门运营 ----------
    @app.get("/dept-ops")
    def dept_ops(request: Request, dept: str = ""):
        viewer = c.user(request)
        if not viewer:
            return c.need_login(request)
        if not (viewer.is_admin or viewer.led):
            return c.notice(viewer, "这里是部门负责人的页面", "你不是部门负责人，所以没有部门运营数据可看。自己的用量在「我的用量」里。", 403, ("去我的用量", "/self-usage"), "dept_ops")
        rng, by = c.pick_range(request), granularity(request)
        # 可以切换的部门：负责人是自己负责的部门；运营管理员是一级部门
        options = [c.directory.dept(d) for d in viewer.led] if not viewer.is_admin else c.directory.children(ROOT)
        options = [d for d in options if d]
        if not dept:
            if not options:
                return c.page("dept_ops.html", viewer, "dept_ops", request, rng=rng, by=by, dept=None, options=[])
            dept = options[0].id
        info = c.directory.dept(dept)
        if not info or not c.access.can_see_dept(viewer, dept):
            return c.notice(viewer, "看不了这个部门", "这个部门不存在，或者不在你负责的范围内。", 403, ("回到部门运营", "/dept-ops"), "dept_ops")
        f = rng.filter(depts=frozenset(c.directory.subtree(dept)))
        o = an.overview(f, dept, by)
        cur = an.slice(f)
        main = an.top_scenario_by_member(f)
        members = an.rank_members(cur, an.previous(f), None)
        for m in members:
            m["main"] = main.get(m["id"], "")
        # 负责人只能沿着自己负责的部门往下看，面包屑里不在范围内的上级不给链接
        trail = [(d, name, c.access.can_see_dept(viewer, d)) for d, name in crumbs(dept)]
        return c.page(
            "dept_ops.html", viewer, "dept_ops", request, rng=rng, by=by, dept=info, options=options, trail=trail, o=o, members=members,
            path=c.directory.dept_path(dept), leader=c.directory.user_name(info.leader_id) if info.leader_id else "",
            headcount=c.directory.headcount(dept), usage=usage_chart(o["trend"], split=False), people=people_chart(o["trend"]),
            allowance=an.allowance(c.allowance_month(rng), depts=f.depts),
        )  # fmt: skip

    # ---------- 豆包运营 ----------
    @app.get("/doubao-ops")
    def doubao_ops(request: Request, dept: str = "", by: str = "day"):
        viewer = c.user(request)
        if not viewer:
            return c.need_login(request)
        if denied := c.admin_only(viewer, "doubao"):
            return denied
        rng = c.pick_range(request)
        parent = dept if dept and c.directory.dept(dept) else ROOT
        f = rng.filter(doubao=True, depts=frozenset(c.directory.subtree(parent)) if parent != ROOT else None)
        cur, prev = an.slice(f), an.previous(f)
        by = by if by in GRANULARITY else "day"
        trend = an.periods(cur, by)
        k = an.kpis(cur, prev)
        scenes = an.scenarios(f, level=0, limit=None)
        pager = Pager(page_number(request), 20, cur.active_members)
        main = an.top_scenario_by_member(f)
        members = an.rank_members(cur, prev, None)[pager.offset : pager.offset + pager.size]
        for m in members:
            m["main"] = main.get(m["id"], "")
        return c.page(
            "doubao.html", viewer, "doubao", request, rng=rng, by=by, k=k, parent=parent, crumbs=crumbs(parent) if parent != ROOT else [],
            depts=an.rank_depts(cur, prev, parent), members=members, pager=pager, scenes=scenes, scene_count=len(scenes),
            usage=usage_chart(trend, split=False), people=people_chart(trend), per_capita=per_capita_chart(trend), trend=trend,
        )  # fmt: skip

    # ---------- 用量明细 ----------
    def log_filter(request: Request, rng: Range) -> tuple[Filter, dict]:
        q = request.query_params
        member, dept = q.get("member", ""), q.get("dept", "")
        f = Filter(
            rng.start, rng.end,
            member=member or None,
            depts=frozenset(c.directory.subtree(dept)) if dept and c.directory.dept(dept) else None,
        )  # fmt: skip
        opts = {
            "subject": q.get("subject", "") if q.get("subject") in ("member", "app") else "",
            "query": q.get("q", "").strip()[:100],
            "feature_key": small_int(q.get("feature")),
            "usage_type": small_int(q.get("usage")),
        }
        return f, opts

    @app.get("/data")
    def data(request: Request, tab: str = "logs"):
        viewer = c.user(request)
        if not viewer:
            return c.need_login(request)
        if denied := c.admin_only(viewer, "data"):
            return denied
        rng = c.pick_range(request, 7)
        f, opts = log_filter(request, rng)
        page = page_number(request)
        q = request.query_params
        # keep：除了日期范围和页码之外，翻页、切换时要带着走的筛选条件
        keep = {"subject": opts["subject"], "q": opts["query"], "feature": opts["feature_key"] or "", "usage": opts["usage_type"] or "",
                "member": f.member or "", "dept": q.get("dept", "") if f.depts is not None else ""}  # fmt: skip
        shared = dict(rng=rng, tab="details" if tab == "details" else "logs", opts=opts, keep=keep, features=labels.FEATURE_KEYS, usage_types=labels.USAGE_TYPES,
                      member_name=c.directory.user_name(f.member) if f.member else "",
                      dept_name=c.directory.dept_path(q.get("dept", "")) if f.depts is not None else "")  # fmt: skip
        if tab == "details":
            rows, total, sums = an.details(rng.start, rng.end, opts["subject"], f.member, PAGE_SIZE, (page - 1) * PAGE_SIZE)
            return c.page("data.html", viewer, "data", request, rows=rows, pager=Pager(page, PAGE_SIZE, total), amount=sums["total"], sums=sums, **shared)
        rows, total, amount = an.logs(f, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE, **opts)
        return c.page("data.html", viewer, "data", request, rows=rows, pager=Pager(page, PAGE_SIZE, total), amount=amount, **shared)

    def logs_sheet(rows: list[dict]) -> tuple[str, list[str], list[list]]:
        head = ["日期", "时间", "类型", "对象", "对象 ID", "部门", "场景", "额度类型", "用量类型", "消耗点数", "折算费用（元）", "备注"]
        body = [
            [r["date"], web.fmt_clock(r["ts"]), labels.SUBJECTS.get(r["subject"], r["subject"]), r["name"], r["entity_id"], r["dept"], r["scenario"],
             r["feature"], r["usage_type"], r["amount"], r["cost"], "；".join(f"{n.get('key_name', '')}：{n.get('value', '')}" for n in r["notes"])]
            for r in rows
        ]  # fmt: skip
        return "用量日志", head, body

    @app.get("/data/export.xlsx")
    def data_export(request: Request, tab: str = "logs"):
        viewer = c.user(request)
        if not viewer:
            return c.need_login(request)
        if denied := c.admin_only(viewer, "data"):
            return denied
        rng = c.pick_range(request, 7)
        f, opts = log_filter(request, rng)
        if tab == "details":
            rows, total, sums = an.details(rng.start, rng.end, opts["subject"], f.member, EXPORT_LIMIT, 0)
            head = ["日期", "类型", "对象", "对象 ID", *labels.FEATURE_KEYS.values(), "合计"]
            body = [[r["date"], labels.SUBJECTS.get(r["subject"], r["subject"]), r["name"], r["entity_id"], *r["quotas"], r["total"]] for r in rows]
            # 最后一行是整个筛选范围的合计（超过导出上限时只导出前面的行，合计仍是全部的）
            body.append([f"合计（{rng.start} ~ {rng.end}，{total} 条）", "", "", "", *sums["quotas"], sums["total"]])
            sheets = [("统计详情", head, body)]
        else:
            rows, _total, _amount = an.logs(f, limit=EXPORT_LIMIT, offset=0, **opts)
            sheets = [logs_sheet(rows)]
        c.db.audit(viewer.open_id, "export", f"用量明细（{'统计详情' if tab == 'details' else '用量日志'}）{rng.start} ~ {rng.end}，{len(rows)} 行")
        return xlsx(excel.workbook(sheets), f"AI用量明细_{rng.start}_{rng.end}.xlsx")

    # ---------- 我的用量 ----------
    def whose(viewer: Viewer, member: str) -> tuple[str | None, Response | None]:
        """这次看谁的用量：默认自己；运营管理员和部门负责人可以看范围内的其他人。"""
        if not member or member == viewer.open_id:
            return viewer.open_id, None
        if not c.access.can_see_member(viewer, member):
            return None, c.notice(viewer, "看不了这个人的用量", "只能看自己的用量；部门负责人可以看本部门成员的。", 403, ("去我的用量", "/self-usage"), "self")
        return member, None

    @app.get("/self-usage")
    def self_usage(request: Request, member: str = ""):
        viewer = c.user(request)
        if not viewer:
            return c.need_login(request)
        open_id, denied = whose(viewer, member)
        if denied:
            return denied
        rng, by = c.pick_range(request), granularity(request)
        # 运营管理员、这个人所在部门的负责人本来就能看部门成员的用量；其他人只能看到不会泄露同事个人数据的部门汇总
        profile = an.member_profile(open_id, rng.start, rng.end, dept_visible=c.access.can_see_dept(viewer, c.directory.user_dept(open_id)), monthly=rng.monthly)
        trend = profile["daily"] if by == "day" else an.periods(an.slice(rng.filter(member=open_id)), by)
        page = page_number(request)
        rows, total, _amount = an.logs(Filter(rng.start, rng.end, member=open_id), limit=20, offset=(page - 1) * 20)
        mine = open_id == viewer.open_id
        # 看别人的：运营管理员是从「成员用量」来的，部门负责人是从「部门运营」来的，导航停在那一项上
        active = "self" if mine else ("members" if viewer.is_admin else "dept_ops")
        return c.page(
            "self_usage.html", viewer, active, request, rng=rng, by=by, p=profile, mine=mine, rows=rows, pager=Pager(page, 20, total),
            keep={"member": member if open_id != viewer.open_id else ""}, usage=usage_chart(trend, split=False),
            allowance=an.allowance(c.allowance_month(rng), member=open_id, limit=1),
        )  # fmt: skip

    @app.get("/self-usage/export.xlsx")
    def self_export(request: Request, member: str = ""):
        viewer = c.user(request)
        if not viewer:
            return c.need_login(request)
        open_id, denied = whose(viewer, member)
        if denied:
            return denied
        rng = c.pick_range(request)
        rows, _total, _amount = an.logs(Filter(rng.start, rng.end, member=open_id), limit=EXPORT_LIMIT, offset=0)
        c.db.audit(viewer.open_id, "export", f"个人用量 {c.directory.user_name(open_id)} {rng.start} ~ {rng.end}")
        return xlsx(excel.workbook([logs_sheet(rows)]), f"AI用量_{rng.start}_{rng.end}.xlsx")

    # ---------- 成员用量（运营管理员） ----------
    @app.get("/members")
    def members(request: Request, q: str = ""):
        """查任何一个人的用量：按姓名、英文名、邮箱搜，只搜到一个人就直接打开他的用量页——和他自己在「我的用量」里看到的一样。
        没有输入时列出本月用量最多的人。"""
        viewer = c.user(request)
        if not viewer:
            return c.need_login(request)
        if denied := c.admin_only(viewer, "members"):
            return denied
        q = q.strip()[:100]
        matches = c.directory.search_users(q, limit=30) if q else []
        if len(matches) == 1:
            return RedirectResponse(web.url_with("/self-usage", member=matches[0].open_id), status_code=303)
        month = today().isoformat()[:7]
        start, end, complete = an.month_span(month)
        cur = an.slice(Filter(start, end, monthly=True))
        found = [
            {"id": u.open_id, "name": u.name, "en_name": u.en_name, "dept_path": c.directory.dept_path(u.dept_ids[0]) if u.dept_ids else "未归属部门",
             "amount": r2(cur.by_member.get(u.open_id, {}).get("amount", 0.0))}
            for u in matches
        ]  # fmt: skip
        return c.page(
            "members.html", viewer, "members", request, q=q, found=found, top=an.rank_members(cur, limit=20), per=c.config.monthly_allowance,
            month_label=month_name(month) + ("" if complete else f"（截至 {int(end[5:7])}/{int(end[8:10])}）"),
        )  # fmt: skip

    # ---------- 输入联想 ----------
    @app.get("/suggest")
    def suggest(request: Request, kind: str = "dept", q: str = ""):
        """部门、人员、应用的输入联想（给运营管理员的表单用）。"""
        viewer = c.user(request)
        if not viewer or not viewer.is_admin:
            return JSONResponse({"items": []}, status_code=403 if viewer else 401)
        if kind == "user":
            items = [{"value": u.open_id, "label": f"{u.name}（{c.directory.dept_path(u.dept_ids[0]) if u.dept_ids else '未归属部门'}）"} for u in c.directory.search_users(q)]
        elif kind == "app":
            query = q.strip().casefold()
            items = [{"value": k, "label": v} for k, v in an.app_names().items() if query and (query in v.casefold() or query in k.casefold())][:10]
        else:
            items = [{"value": d.id, "label": c.directory.dept_path(d.id)} for d in c.directory.search_depts(q)]
        return {"items": items}


def xlsx(content: bytes, filename: str) -> Response:
    return Response(
        content,
        media_type=excel.XLSX_TYPE,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )

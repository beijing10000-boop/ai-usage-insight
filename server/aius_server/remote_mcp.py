"""远程 MCP（Streamable HTTP，挂在 /mcp）：AI 工具直接连平台查用量，本机不用装任何东西。

认证用个人访问令牌（Authorization: Bearer aius_...）。23 个 aius_* 工具全部只读，和开放接口走同一套查询（openapi.Api），
能看到什么数据由令牌绑定的那个人的角色决定。
"""

# 注意：这个模块不要加 from __future__ import annotations——MCP 要在运行时读工具函数的参数类型。
import json
from urllib.parse import urlsplit

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from . import __version__
from .access import Viewer
from .core import Ctx
from .openapi import Api, ApiError

INSTRUCTIONS = """查询公司的飞书 AI 用量数据（点数、折算费用、人员 / 部门 / 应用 / 场景分布、异常、额度）。所有工具都是只读的。

能看到的数据取决于当前令牌对应的人：
- 所有人：自己的用量（aius_my_*）；
- 部门负责人：还能看自己负责的部门（aius_department_*）；
- 运营管理员：还能看全企业的数据（aius_overview、aius_ranking、aius_usage_logs 等）。
不确定身份时先调 aius_whoami。工具报「只有运营管理员能用」时，如实告诉用户他没有这个权限，不要换别的方式去凑数据。

时间范围：默认是最近 30 天（到最近一个有数据的日子为止）；可以用 days 指定天数，或者用 start、end（YYYY-MM-DD）指定起止日期。
用量的单位是「点」；cost 是按平台设置的单价折算的费用（元）；change 是和上一个同样长度的时间段相比的百分比。
数据由后台定时从飞书同步，aius_whoami 返回的 data_latest_date 是数据截至的日期；用户问到这天之后的情况时要说明数据还没同步到。"""


def connect_prompt(public_url: str, token: str) -> str:
    """给用户复制给 AI 工具的那段话：让 AI 自己把远程 MCP 配好。"""
    url = f"{public_url}/mcp"
    cursor = json.dumps({"url": url, "headers": {"Authorization": f"Bearer {token}"}}, ensure_ascii=False)
    return f"""请帮我把公司的 AI 用量洞察平台接入你自己。它是远程 MCP 服务（Streamable HTTP 传输），不需要安装任何软件：
- 地址：{url}
- 认证：请求头 Authorization: Bearer {token}

按你自己的类型配置（配置写到用户级，不要写进项目目录或提交到 git——这个令牌等同于我的身份）：
- 如果你是 Claude Code：运行 claude mcp add --transport http --scope user ai-usage {url} --header "Authorization: Bearer {token}"，再用 claude mcp get ai-usage 确认
- 如果你是 Cursor：在 ~/.cursor/mcp.json 的 mcpServers 里加入 "ai-usage": {cursor}
- 如果你是 OpenAI Codex：在 ~/.codex/config.toml 里加入
    [mcp_servers.ai-usage]
    url = "{url}"
    http_headers = {{ "Authorization" = "Bearer {token}" }}
  然后用 codex mcp list 确认
- 其他支持远程 MCP 的工具：按它自己的方式配置上面的地址和请求头

配置完成后告诉我是否需要重启工具或新开会话；生效后调用 aius_whoami 工具，确认返回的是我的名字。"""


class BearerGate:
    """挡在 MCP 前面：没有有效令牌直接 401，不进 MCP 会话。"""

    def __init__(self, app, c: Ctx):
        self.app = app
        self.c = c

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            auth = next((v.decode("latin-1") for k, v in scope["headers"] if k == b"authorization"), None)
            if not await anyio.to_thread.run_sync(self.c.bearer, auth):
                body = json.dumps(
                    {"error": "unauthorized", "detail": "缺少或无效的访问令牌：请在平台的「接入管理」页面生成令牌，并配置请求头 Authorization: Bearer <令牌>"},
                    ensure_ascii=False,
                ).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"application/json; charset=utf-8"), (b"content-length", str(len(body)).encode())],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def build_remote_mcp(c: Ctx) -> tuple[BearerGate, MCPServer]:
    server = MCPServer("ai-usage-insight", instructions=INSTRUCTIONS, version=__version__)
    api = Api(c)

    def who(ctx: Context) -> Viewer:
        viewer = c.bearer((ctx.headers or {}).get("authorization"))
        if not viewer:
            raise ToolError("令牌无效、已撤销或已过期，请在平台的「接入管理」页面重新生成")
        return viewer

    async def call(ctx: Context, fn, *args, **kwargs):
        """查询里有 SQLite 访问，放到线程里，别卡住事件循环；业务错误原样告诉模型。"""

        def work():
            return fn(who(ctx), *args, **kwargs)

        try:
            return await anyio.to_thread.run_sync(work)
        except ApiError as e:
            raise ToolError(e.detail) from None

    async def ranged(ctx: Context, fn, *args, days: int, start: str, end: str, **kwargs):
        try:
            first, last = api.span(days, start, end)
        except ApiError as e:
            raise ToolError(e.detail) from None
        return await call(ctx, fn, *args, first, last, **kwargs)

    # ---------- 所有人 ----------
    @server.tool()
    async def aius_whoami(ctx: Context) -> dict:
        """当前连接对应的飞书身份、角色（运营管理员 / 部门负责人 / 普通用户）、负责的部门，以及数据截至哪一天。"""
        return await call(ctx, api.me)

    @server.tool()
    async def aius_my_summary(ctx: Context, days: int = 30, start: str = "", end: str = "") -> dict:
        """我的 AI 用量汇总：消耗点数、折算费用、使用次数、活跃天数、和上一个周期相比的变化。"""
        return await ranged(ctx, api.my_summary, days=days, start=start, end=end)

    @server.tool()
    async def aius_my_trend(ctx: Context, days: int = 30, start: str = "", end: str = "") -> dict:
        """我每天的 AI 用量（点）。"""
        return await ranged(ctx, api.my_trend, days=days, start=start, end=end)

    @server.tool()
    async def aius_my_scenarios(ctx: Context, days: int = 30, start: str = "", end: str = "") -> dict:
        """我的用量用在哪里：按一级场景（products）、完整场景路径（scenarios）和额度类型（features）的分布。"""
        return await ranged(ctx, api.my_scenarios, days=days, start=start, end=end)

    @server.tool()
    async def aius_my_logs(ctx: Context, days: int = 7, start: str = "", end: str = "", page: int = 1, size: int = 50) -> dict:
        """我的用量日志明细：每次使用的时间、场景、额度类型、消耗点数。分页，size 最大 200。"""
        return await ranged(ctx, api.my_logs, days=days, start=start, end=end, page=page, size=size)

    @server.tool()
    async def aius_my_details(ctx: Context, days: int = 30, start: str = "", end: str = "", page: int = 1, size: int = 50) -> dict:
        """我的统计详情：飞书按天汇总的各额度类型用量（T+1，今天的还没有）。quotas 数组的顺序见 quota_types。"""
        return await ranged(ctx, api.my_details, days=days, start=start, end=end, page=page, size=size)

    @server.tool()
    async def aius_my_department(ctx: Context, days: int = 30, start: str = "", end: str = "") -> dict:
        """我所在部门的整体用量：部门总量、活跃人数、人均用量、我在部门里的占比和排名、部门的场景分布。不含其他同事的个人数据。"""
        return await ranged(ctx, api.my_department, days=days, start=start, end=end)

    # ---------- 部门负责人 / 运营管理员 ----------
    @server.tool()
    async def aius_departments(ctx: Context) -> dict:
        """我能查看的部门：部门负责人是自己负责的部门，运营管理员是全部一级部门。返回的 id 用于 aius_department_* 工具。"""
        return await call(ctx, api.departments)

    @server.tool()
    async def aius_department_summary(ctx: Context, department_id: str, days: int = 30, start: str = "", end: str = "") -> dict:
        """一个部门（含下级部门）的用量：指标、每日趋势、各下级部门的用量（children，可以继续用它们的 id 下钻）。"""
        return await ranged(ctx, api.dept_summary, department_id, days=days, start=start, end=end)

    @server.tool()
    async def aius_department_members(ctx: Context, department_id: str, days: int = 30, start: str = "", end: str = "", limit: int = 50) -> dict:
        """一个部门（含下级部门）成员的用量排行：点数、占比、使用次数、活跃天数、最近使用日期、主要场景。"""
        return await ranged(ctx, api.dept_members, department_id, days=days, start=start, end=end, limit=limit)

    @server.tool()
    async def aius_department_scenarios(ctx: Context, department_id: str, days: int = 30, start: str = "", end: str = "") -> dict:
        """一个部门（含下级部门）的用量按场景的分布。"""
        return await ranged(ctx, api.dept_scenarios, department_id, days=days, start=start, end=end)

    # ---------- 运营管理员 ----------
    @server.tool()
    async def aius_overview(ctx: Context, days: int = 30, start: str = "", end: str = "") -> dict:
        """全企业的 AI 用量概况（只有运营管理员能用）：总用量、折算费用、活跃人数 / 部门 / 应用、日均、人均、环比，本期解读和异常提醒。"""
        return await ranged(ctx, api.overview, days=days, start=start, end=end)

    @server.tool()
    async def aius_trend(ctx: Context, days: int = 30, start: str = "", end: str = "") -> dict:
        """全企业每天的用量：总量、人员用量、应用用量、活跃人数、人均用量（只有运营管理员能用）。"""
        return await ranged(ctx, api.trend, days=days, start=start, end=end)

    @server.tool()
    async def aius_ranking(ctx: Context, dimension: str = "department", days: int = 30, start: str = "", end: str = "", limit: int = 20) -> dict:
        """用量排行（只有运营管理员能用）。dimension：department（一级部门，含下级）| member（人员）| app（应用）| scenario（场景）。"""
        return await ranged(ctx, api.ranking, dimension, days=days, start=start, end=end, limit=limit)

    @server.tool()
    async def aius_anomalies(ctx: Context, days: int = 30, start: str = "", end: str = "") -> dict:
        """异常提醒（只有运营管理员能用）：单日突增、部门或应用快速增长、用量集中在个别人、单次用量偏高。"""
        return await ranged(ctx, api.anomalies, days=days, start=start, end=end)

    @server.tool()
    async def aius_breakdown(ctx: Context, by: str = "feature", days: int = 30, start: str = "", end: str = "") -> dict:
        """用量拆分（只有运营管理员能用）。by：feature（额度类型）| usage_type（企业付费 / 试用免费）| hour（一天里各小时）。"""
        return await ranged(ctx, api.breakdown, by, days=days, start=start, end=end)

    @server.tool()
    async def aius_dept_tree(ctx: Context, parent: str = "", days: int = 30, start: str = "", end: str = "") -> dict:
        """部门汇总与下钻（只有运营管理员能用）：parent 的各个下级部门的用量（各自含下级）、活跃人数、人均、环比。不传 parent 是一级部门。"""
        return await ranged(ctx, api.dept_tree, parent, days=days, start=start, end=end)

    @server.tool()
    async def aius_doubao(ctx: Context, days: int = 30, start: str = "", end: str = "") -> dict:
        """豆包工作的用量（只有运营管理员能用）：总量、日均活跃人数、人均用量、每日趋势、各部门用量、场景分布。"""
        return await ranged(ctx, api.doubao, days=days, start=start, end=end)

    @server.tool()
    async def aius_usage_logs(
        ctx: Context, days: int = 7, start: str = "", end: str = "", subject: str = "", q: str = "", member: str = "", dept: str = "",
        page: int = 1, size: int = 50,
    ) -> dict:  # fmt: skip
        """全企业的用量日志明细（只有运营管理员能用）。subject：member | app；q：按场景、人名或应用名搜索；
        member：某个人的 open_id；dept：某个部门的 id（含下级）。分页，size 最大 200。"""
        return await ranged(ctx, api.usage_logs, days=days, start=start, end=end, subject=subject, q=q, member=member, dept=dept, page=page, size=size)

    @server.tool()
    async def aius_usage_details(ctx: Context, days: int = 30, start: str = "", end: str = "", subject: str = "", page: int = 1, size: int = 50) -> dict:
        """全企业的统计详情（只有运营管理员能用）：飞书按天汇总的每个人 / 应用各额度类型的用量（T+1）。"""
        return await ranged(ctx, api.usage_details, days=days, start=start, end=end, subject=subject, page=page, size=size)

    @server.tool()
    async def aius_quotas(ctx: Context) -> dict:
        """额度分配和使用情况（只有运营管理员能用）：给部门 / 人员 / 应用分配的点数、已用、剩余、使用率、是否超额。"""
        return await call(ctx, api.quotas)

    @server.tool()
    async def aius_sync_runs(ctx: Context, limit: int = 10) -> dict:
        """最近的数据同步任务（只有运营管理员能用）：日期范围、状态、写入条数、失败原因。用来判断数据是不是最新的。"""
        return await call(ctx, api.sync_runs, limit)

    @server.tool()
    async def aius_ask(ctx: Context, question: str, days: int = 30, start: str = "", end: str = "") -> dict:
        """把一个关于 AI 用量的问题交给平台的洞察助手回答（它会按我的数据范围取数后作答）。
        能直接用上面的查询工具回答的问题，优先用查询工具；需要平台给一段成文的分析时再用这个。"""
        try:
            first, last = api.span(days, start, end)
        except ApiError as e:
            raise ToolError(e.detail) from None

        def work():
            viewer = who(ctx)
            if not question.strip():
                raise ToolError("question 不能为空")
            if not c.may_ask(viewer.open_id):
                raise ToolError("问得太快了，稍等几秒再问")
            sc = c.scope(viewer, first, last)
            result = c.insight.ask(question, sc.filter, sc.label, sc.parent)
            c.db.audit(viewer.open_id, "insight_ask", question[:200], via="mcp")
            return {"answer": result["answer"], "by_model": result["model"], "scope": sc.label, "start": first, "end": last}

        return await anyio.to_thread.run_sync(work)

    netloc = urlsplit(c.s.public_url).netloc
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[netloc, "127.0.0.1:*", "localhost:*"],
        allowed_origins=[c.s.origin, "http://127.0.0.1:*", "http://localhost:*"],
    )
    inner = server.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True, transport_security=security)
    return BearerGate(inner, c), server

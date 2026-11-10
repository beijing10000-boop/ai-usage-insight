"""远程 MCP：真实 uvicorn + 真实 MCP HTTP 客户端，走完整链路（lifespan、鉴权、Host 校验、工具）。"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from aius_server.remote_mcp import connect_prompt
from conftest import D1, Env


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live(tmp_path):
    port = _free_port()
    env = Env(tmp_path, public_url=f"http://127.0.0.1:{port}")
    for open_id in ("ou_admin", "ou_bob", "ou_dave"):
        env.api(open_id)  # 生成 aius_test_<open_id> 令牌
    server = uvicorn.Server(uvicorn.Config(env.app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    env.base = f"http://127.0.0.1:{port}"
    yield env
    server.should_exit = True
    t.join(5)


def session(base: str, open_id: str):
    """返回一个函数：在一个 MCP 会话里依次执行 calls=[(tool, args), ...]。"""

    async def run(calls):
        async with create_mcp_http_client(headers={"Authorization": f"Bearer aius_test_{open_id}"}) as http:
            async with streamable_http_client(f"{base}/mcp", http_client=http) as (r, w):
                async with ClientSession(r, w) as s:
                    init = await s.initialize()
                    out = {"instructions": init.instructions, "tools": [t.name for t in (await s.list_tools()).tools], "results": []}
                    for tool, args in calls:
                        res = await s.call_tool(tool, args)
                        text = "".join(getattr(c, "text", "") for c in res.content)
                        out["results"].append((res.is_error, json.loads(text) if not res.is_error else text))
                    return out

    return lambda calls: asyncio.run(run(calls))


def test_tools_and_instructions(live):
    out = session(live.base, "ou_admin")([("aius_whoami", {})])
    assert len(out["tools"]) == 23 and all(name.startswith("aius_") for name in out["tools"])
    assert "只读" in out["instructions"]
    failed, me = out["results"][0]
    assert not failed and me["name"] == "管理员" and me["role"] == "operations_admin" and me["data_latest_date"] == D1


def test_admin_tools(live):
    span = {"start": D1, "end": D1}
    out = session(live.base, "ou_admin")(
        [
            ("aius_overview", span),
            ("aius_ranking", {**span, "dimension": "member", "limit": 2}),
            ("aius_usage_logs", {**span, "q": "豆包"}),
            ("aius_dept_tree", {**span, "parent": "d-tech"}),
            ("aius_ask", {**span, "question": "用量合理吗"}),
            ("aius_ranking", {"dimension": "nope"}),
            ("aius_trend", {"days": 0}),
        ]
    )
    (_, overview), (_, ranking), (_, logs), (_, tree), (_, ask), bad_dim, bad_days = out["results"]
    assert overview["kpis"]["total"] == 206
    assert [m["name"] for m in ranking["items"]] == ["卡萝", "鲍勃"]
    assert logs["total"] == 2 and {d["name"] for d in tree["items"]} == {"研发部", "运维部"}
    assert ask["by_model"] is False and "206 点" in ask["answer"] and ask["scope"] == "全企业"
    assert bad_dim[0] and "dimension" in bad_dim[1]
    assert bad_days[0] and "days" in bad_days[1]


def test_what_a_token_can_see_follows_its_owner(live):
    span = {"start": D1, "end": D1}
    dave = session(live.base, "ou_dave")(
        [("aius_my_summary", span), ("aius_my_department", span), ("aius_overview", span), ("aius_department_members", {**span, "department_id": "d-rd"}), ("aius_ask", {**span, "question": "我用了多少"})]
    )
    (_, mine), (_, dept), overview, members, (_, ask) = dave["results"]
    assert mine["total"] == 1 and dept["hidden"] is True and "total" not in dept  # 部门里用的人太少，不给合计
    assert overview[0] and "运营管理员" in overview[1]
    assert members[0] and "不在你负责的范围内" in members[1]
    assert "戴维 个人" in ask["scope"] and "鲍勃" not in ask["answer"]
    bob = session(live.base, "ou_bob")([("aius_departments", {}), ("aius_department_members", {**span, "department_id": "d-rd"}), ("aius_usage_logs", span)])
    (_, depts), (_, team), logs = bob["results"]
    assert [d["id"] for d in depts["items"]] == ["d-rd"] and [m["name"] for m in team["items"]] == ["鲍勃", "戴维"]
    assert logs[0] and "运营管理员" in logs[1]


def test_requests_without_a_valid_token_are_rejected_before_mcp(live):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert httpx.post(f"{live.base}/mcp", json=body).status_code == 401
    r = httpx.post(f"{live.base}/mcp", json=body, headers={"Authorization": "Bearer aius_nope"})
    assert r.status_code == 401 and "接入管理" in r.json()["detail"]


def test_connect_prompt_mentions_the_endpoint_and_token():
    text = connect_prompt("http://ai.example.com", "aius_abc")
    assert "http://ai.example.com/mcp" in text and "Bearer aius_abc" in text and "aius_whoami" in text

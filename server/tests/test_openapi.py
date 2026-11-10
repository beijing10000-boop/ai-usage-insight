"""开放接口：令牌认证，以及按令牌绑定的那个人的角色决定能看什么。"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from aius_server.core import hash_secret
from aius_server.openapi import ENDPOINTS
from conftest import D1, D2

SPAN = {"start": D1, "end": D1}
ADMIN_ONLY = [
    "/openapi/v1/analytics/overview", "/openapi/v1/analytics/trend", "/openapi/v1/analytics/ranking", "/openapi/v1/analytics/anomalies",
    "/openapi/v1/analytics/breakdown", "/openapi/v1/analytics/dept-tree", "/openapi/v1/analytics/doubao", "/openapi/v1/usage-logs",
    "/openapi/v1/usage-details", "/openapi/v1/quotas", "/openapi/v1/sync-runs",
]  # fmt: skip


def test_token_is_required(env):
    c = TestClient(env.app)
    assert c.get("/openapi/v1/me").status_code == 401
    assert c.get("/openapi/v1/me", headers={"Authorization": "Bearer aius_wrong"}).status_code == 401
    assert "接入管理" in c.get("/openapi/v1/analytics/overview").json()["error"]


def test_expired_tokens_stop_working(env):
    env.db.add_token(hash_secret("aius_old"), "ou_dave", "旧的", int(time.time()) - 1)
    env.db.add_token(hash_secret("aius_new"), "ou_dave", "新的", int(time.time()) + 3600)
    c = TestClient(env.app)
    assert c.get("/openapi/v1/me", headers={"Authorization": "Bearer aius_old"}).status_code == 401
    assert c.get("/openapi/v1/me", headers={"Authorization": "Bearer aius_new"}).json()["name"] == "戴维"


def test_documented_endpoints_are_exactly_the_registered_ones(env):
    registered = {r.path.replace("{dept_id}", "{id}") for r in env.app.routes if getattr(r, "path", "").startswith("/openapi/")}
    assert registered == {path for path, _who, _text in ENDPOINTS}
    assert all("GET" in r.methods and len(r.methods) == 1 for r in env.app.routes if getattr(r, "path", "").startswith("/openapi/"))


def test_self_endpoints_return_only_the_callers_data(env):
    dave = env.api("ou_dave")
    me = dave.get("/openapi/v1/me").json()
    assert me["role"] == "regular_user" and me["department"] == "技术中心 > 研发部" and me["data_latest_date"] == D1
    summary = dave.get("/openapi/v1/self-usage/summary", params=SPAN).json()
    assert summary["total"] == 1 and summary["records"] == 1 and summary["name"] == "戴维"
    assert dave.get("/openapi/v1/self-usage/trend", params=SPAN).json()["daily"] == [{"date": D1, "amount": 1}]
    assert [s["name"] for s in dave.get("/openapi/v1/self-usage/scenarios", params=SPAN).json()["products"]] == ["智能纪要"]
    logs = dave.get("/openapi/v1/self-usage/logs", params=SPAN).json()
    assert logs["total"] == 1 and logs["items"][0]["scenario"] == "智能纪要>会议纪要>纪要生成"
    dept = dave.get("/openapi/v1/self-usage/department", params=SPAN).json()
    # 研发部只有两个人用过：不给部门合计（31 减掉戴维自己的 1 就是鲍勃的个人用量）
    assert dept["hidden"] is True and "total" not in dept and "scenarios" not in dept and "反推" in dept["note"]
    mine = env.api("ou_bob").get("/openapi/v1/self-usage/department", params=SPAN).json()  # 鲍勃是研发部负责人，本来就能看
    assert mine["hidden"] is False and mine["total"] == 31 and mine["my_rank"] == 1
    bob_details = env.api("ou_bob").get("/openapi/v1/self-usage/details", params=SPAN).json()
    assert bob_details["total"] == 1 and bob_details["items"][0]["quotas"][3] == 20


def test_regular_users_cannot_use_admin_or_department_endpoints(env):
    dave = env.api("ou_dave")
    for path in ADMIN_ONLY:
        r = dave.get(path, params=SPAN)
        assert r.status_code == 403 and "运营管理员" in r.json()["error"], path
    assert dave.get("/openapi/v1/departments").json() == {"items": []}
    assert dave.get("/openapi/v1/departments/d-rd/summary").status_code == 403
    assert dave.get("/openapi/v1/departments/d-rd/members").status_code == 403


def test_leaders_see_their_departments(env):
    bob = env.api("ou_bob")
    assert [d["id"] for d in bob.get("/openapi/v1/departments").json()["items"]] == ["d-rd"]
    summary = bob.get("/openapi/v1/departments/d-rd/summary", params=SPAN).json()
    assert summary["kpis"]["total"] == 31 and summary["headcount"] == 2
    members = bob.get("/openapi/v1/departments/d-rd/members", params=SPAN).json()
    assert [(m["name"], m["amount"]) for m in members["items"]] == [("鲍勃", 30), ("戴维", 1)]
    assert members["items"][0]["main_scenario"] == "豆包工作>主对话>深度思考"
    assert bob.get("/openapi/v1/departments/d-rd/scenarios", params=SPAN).json()["products"][0]["name"] == "豆包工作"
    assert bob.get("/openapi/v1/departments/d-sales/summary").status_code == 403
    assert bob.get("/openapi/v1/departments/nope/summary").status_code == 404
    assert bob.get("/openapi/v1/analytics/overview").status_code == 403
    alice = env.api("ou_alice")  # 上级部门的负责人能看下级部门
    assert alice.get("/openapi/v1/departments/d-rd/summary", params=SPAN).status_code == 200


def test_admin_endpoints(env):
    admin = env.api("ou_admin")
    overview = admin.get("/openapi/v1/analytics/overview", params=SPAN).json()
    assert overview["kpis"]["total"] == 206 and "206 点" in overview["narrative"]
    assert admin.get("/openapi/v1/analytics/trend", params={"start": D2, "end": D1}).json()["daily"][0]["total"] == 8
    ranking = {dim: admin.get("/openapi/v1/analytics/ranking", params={**SPAN, "dimension": dim}).json()["items"] for dim in ("department", "member", "app", "scenario")}
    assert ranking["department"][0]["name"] == "销售部" and ranking["member"][0]["name"] == "卡萝"
    assert ranking["app"][0]["id"] == "spring_b__c" and ranking["scenario"][0]["amount"] == 100
    assert admin.get("/openapi/v1/analytics/ranking", params={"dimension": "x"}).status_code == 400
    assert admin.get("/openapi/v1/analytics/breakdown", params={**SPAN, "by": "hour"}).json()["items"][10]["amount"] == 116
    assert admin.get("/openapi/v1/analytics/breakdown", params={"by": "x"}).status_code == 400
    tree = admin.get("/openapi/v1/analytics/dept-tree", params={**SPAN, "parent": "d-tech"}).json()
    assert {d["name"]: d["amount"] for d in tree["items"]} == {"研发部": 31, "运维部": 5}
    assert admin.get("/openapi/v1/analytics/doubao", params=SPAN).json()["kpis"]["total"] == 20
    logs = admin.get("/openapi/v1/usage-logs", params={**SPAN, "q": "豆包", "size": 1}).json()
    assert logs["total"] == 2 and len(logs["items"]) == 1
    assert admin.get("/openapi/v1/usage-logs", params={**SPAN, "dept": "d-tech"}).json()["amount"] == 36
    assert admin.get("/openapi/v1/usage-details", params={**SPAN, "subject": "app"}).json()["total"] == 1
    assert admin.get("/openapi/v1/quotas").json()["items"] == []
    assert admin.get("/openapi/v1/sync-runs").json()["items"][0]["status"] == "success"
    assert admin.get("/openapi/v1/analytics/anomalies", params=SPAN).status_code == 200


def test_bad_ranges_and_paging_are_rejected(env):
    admin = env.api("ou_admin")
    for params in ({"start": D1}, {"start": D1, "end": D2}, {"days": 0}, {"days": 999}, {"start": "x", "end": "y"}):
        assert admin.get("/openapi/v1/analytics/trend", params=params).status_code == 400, params
    assert admin.get("/openapi/v1/usage-logs", params={"size": 500}).status_code == 400
    assert admin.get("/openapi/v1/usage-logs", params={"page": 0}).status_code == 400


def test_openapi_is_read_only(env):
    admin = env.api("ou_admin")
    for method in ("post", "put", "delete", "patch"):
        assert getattr(admin, method)("/openapi/v1/quotas").status_code == 405

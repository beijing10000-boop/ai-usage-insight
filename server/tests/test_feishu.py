"""飞书对接层：请求长什么样、限频重试、错误怎么报。用假的 HTTP 传输层，不连网。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from aius_server.config import Settings
from aius_server.feishu import DETAIL_PATH, LOG_PATH, FeishuClient, FeishuError, day_range, subjects_for


class EnvVault:
    """只从 Settings 里取凭证的保管箱（这里不测页面保存，那在 test_setup.py）。"""

    def __init__(self, s: Settings):
        self.s = s

    def feishu(self) -> tuple[str, str]:
        return self.s.feishu_app_id, self.s.feishu_app_secret

    def aily(self) -> tuple[str, str]:
        return self.s.aily_app_id, self.s.aily_app_secret


def client(handler, **kw) -> tuple[FeishuClient, list[float]]:
    slept: list[float] = []
    s = Settings(public_url="http://x", data_dir=Path("."), secret_key="k" * 40, feishu_app_id="cli_app", feishu_app_secret="sec", **kw)

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            body = json.loads(request.content)
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t-" + body["app_id"], "expire": 7200})
        return handler(request)

    return FeishuClient(s, EnvVault(s), http=httpx.Client(transport=httpx.MockTransport(route)), sleep=slept.append), slept


def test_day_range_is_the_beijing_calendar_day():
    start, end = day_range("2026-05-15")
    assert start == 1778774400 and end - start == 86399  # 2026-05-15 00:00:00 +08:00


def test_subjects():
    assert subjects_for("member") == (1, [{"entity_type": 1, "entity_ids": ["0"]}])
    subject_type, subjects = subjects_for("app")
    assert subject_type == 3 and [s["entity_type"] for s in subjects] == [5, 6, 7, 8, 10, 13]
    assert all(s["entity_ids"] == ["-1"] for s in subjects)


def test_usage_log_request_and_paging():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        more = "page_token" not in request.url.params
        return httpx.Response(200, json={"code": 0, "data": {"items": [{"entity_id": "ou_1"}], "has_more": more, "page_token": "next" if more else ""}})

    fs, _ = client(handler)
    items, token = fs.usage_page("log", "member", "2026-05-15")
    assert items == [{"entity_id": "ou_1"}] and token == "next"
    items, token = fs.usage_page("log", "member", "2026-05-15", token)
    assert token == ""
    first, second = seen
    assert first.method == "POST" and first.url.path == LOG_PATH
    assert dict(first.url.params) == {"user_id_type": "open_id", "department_id_type": "department_id", "page_size": "100"}
    assert second.url.params["page_token"] == "next"
    assert first.headers["authorization"] == "Bearer t-cli_app"
    body = json.loads(first.content)
    assert body == {"date_start": 1778774400, "date_end": 1778860799, "subject_type": 1, "subjects": [{"entity_type": 1, "entity_ids": ["0"]}], "locale": "zh-CN"}


def test_usage_detail_request():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"code": 0, "data": {"items": [], "has_more": False}})

    fs, _ = client(handler)
    assert fs.usage_page("detail", "app", "2026-05-15", app_types=(5, 6)) == ([], "")
    body = json.loads(seen[0].content)
    assert seen[0].url.path == DETAIL_PATH and body["subject_type"] == 3 and "locale" not in body
    assert body["subjects"] == [{"entity_type": 5, "entity_ids": ["-1"]}, {"entity_type": 6, "entity_ids": ["-1"]}]


def test_usage_response_shapes():
    def serving(payload):
        return client(lambda r: httpx.Response(200, json=payload))[0]

    # 这一天没有数据时飞书可能不带 items：当成空
    assert serving({"code": 0, "data": {"has_more": False}}).usage_page("log", "member", "2026-05-15") == ([], "")
    assert serving({"code": 0, "data": {}}).usage_page("detail", "app", "2026-05-15") == ([], "")
    # 数据直接放在最外层也认
    assert serving({"code": 0, "items": [{"entity_id": "a"}], "has_more": False}).usage_page("log", "member", "2026-05-15") == ([{"entity_id": "a"}], "")
    # 连 data 都没有：返回格式变了，报错，不能当成「没有数据」
    with pytest.raises(FeishuError, match="返回格式可能变了"):
        serving({"code": 0, "msg": "ok"}).usage_page("log", "member", "2026-05-15")
    # 说还有下一页却不给分页标记：报错，不能只写第一页就当成完成
    with pytest.raises(FeishuError, match="没有给分页标记"):
        serving({"code": 0, "data": {"items": [{"entity_id": "a"}], "has_more": True}}).usage_page("log", "member", "2026-05-15")
    with pytest.raises(FeishuError, match="不是列表"):
        serving({"code": 0, "data": {"items": {"a": 1}}}).usage_page("log", "member", "2026-05-15")


def test_usage_calls_are_spaced_out_for_the_one_per_second_limit():
    fs, slept = client(lambda r: httpx.Response(200, json={"code": 0, "data": {"items": [], "has_more": False}}))
    fs.usage_page("log", "member", "2026-05-15")
    fs.usage_page("log", "app", "2026-05-15")
    assert len(slept) == 1 and 0 < slept[0] <= 1.05  # 两次请求「开始」之间隔够 1.05 秒


def test_rate_limit_is_retried_with_backoff():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, json={"code": 99991400, "msg": "request trigger frequency limit"})
        return httpx.Response(200, json={"code": 0, "data": {"items": [{"entity_id": "x"}], "has_more": False}})

    fs, slept = client(handler)
    assert fs.usage_page("log", "member", "2026-05-15")[0] == [{"entity_id": "x"}]
    assert len(calls) == 3 and slept[:2] == [2, 4]


def test_permission_errors_are_not_retried_and_keep_the_message():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, json={"code": 99991672, "msg": "Access denied. One of the following scopes is required: [admin:ai_usage_log:read]"})

    fs, slept = client(handler)
    with pytest.raises(FeishuError, match="admin:ai_usage_log:read") as err:
        fs.usage_page("log", "member", "2026-05-15")
    assert len(calls) == 1 and not slept and err.value.code == 99991672 and not err.value.retryable


def test_network_errors_become_feishu_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    fs, slept = client(handler)
    with pytest.raises(FeishuError, match="连接飞书失败"):
        fs.user_names(["ou_1"])
    assert slept == [2, 4, 8, 16]  # 网络抖动会重试，最后才报错


def test_departments_use_department_id_and_report_leaders():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/open-apis/contact/v3/departments/0/children"
        assert request.url.params["department_id_type"] == "department_id" and request.url.params["fetch_child"] == "true"
        items = [
            {"department_id": "D1", "open_department_id": "od-1", "name": "研发部", "parent_department_id": "0", "leader_user_id": "ou_boss"},
            {"department_id": "D2", "name": "后端组", "parent_department_id": "D1", "leaders": [{"leaderType": 2, "leaderID": "ou_b"}, {"leaderType": 1, "leaderID": "ou_a"}]},
            {"department_id": "D3", "name": "已删除", "parent_department_id": "0", "status": {"is_deleted": True}},
        ]
        return httpx.Response(200, json={"code": 0, "data": {"items": items, "has_more": False}})

    fs, _ = client(handler)
    depts = fs.list_departments()
    assert [(d.id, d.parent_id, d.leader_id) for d in depts] == [("D1", "0", "ou_boss"), ("D2", "D1", "ou_a")]


def test_missing_parent_department_field_is_an_error_not_a_silent_top_level():
    fs, _ = client(lambda r: httpx.Response(200, json={"code": 0, "data": {"items": [{"department_id": "D1", "name": "x"}]}}))
    with pytest.raises(FeishuError, match="contact:department.organize:readonly"):
        fs.list_departments()


def test_department_members_belong_to_the_department_that_was_listed():
    def handler(request: httpx.Request) -> httpx.Response:
        items = [
            {"open_id": "ou_1", "name": "张三", "enterprise_email": "ZS@Example.com"},
            {"open_id": "ou_2", "name": "已离职", "status": {"is_resigned": True}},
        ]
        return httpx.Response(200, json={"code": 0, "data": {"items": items, "has_more": False}})

    fs, _ = client(handler)
    users = fs.list_department_users("D1")
    assert [(u.open_id, u.email, u.dept_ids) for u in users] == [("ou_1", "zs@example.com", ("D1",))]
    assert fs.list_department_users("0")[0].dept_ids == ()


def test_app_names_from_the_list_then_one_by_one_skipping_aily_ids():
    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        asked.append(path)
        if path.endswith("/applications"):
            return httpx.Response(200, json={"code": 0, "data": {"items": [{"app_id": "cli_a", "app_name": "甲"}, {"app_id": "cli_other", "app_name": "别的"}]}})
        return httpx.Response(200, json={"code": 0, "data": {"app": {"app_id": "cli_b", "app_name": "乙"}}})

    fs, _ = client(handler)
    assert fs.app_names(["cli_a", "cli_b", "spring_x__c", "agent_y"]) == {"cli_a": "甲", "cli_b": "乙"}
    assert [p for p in asked if "/applications/" in p] == ["/open-apis/application/v6/applications/cli_b"]


def test_aily_names_use_the_aily_app_credentials():
    tokens = []

    def handler(request: httpx.Request) -> httpx.Response:
        tokens.append(request.headers["authorization"])
        return httpx.Response(200, json={"code": 0, "data": {"items": [{"id": "spring_x__c", "name": "合同审核"}], "has_more": False}})

    fs, _ = client(handler, aily_app_id="cli_aily", aily_app_secret="s2")
    assert fs.aily_app_names() == {"spring_x__c": "合同审核"}
    assert set(tokens) == {"Bearer t-cli_aily"}
    plain, _ = client(handler)
    assert plain.aily_app_names() == {}


def test_check_reports_connection_and_tenant():
    fs, _ = client(lambda r: httpx.Response(200, json={"code": 0, "data": {"tenant": {"name": "示例公司"}}}))
    assert fs.check() == {"connected": True, "tenant": "示例公司", "aily": None}


def test_verify_checks_a_pair_of_credentials_without_caching():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("verify 只换 token，不该调别的接口")

    fs, _ = client(handler)
    fs.verify("cli_new", "new-secret")  # 假的传输层对任何凭证都发 token
    assert "cli_new" not in fs._tokens

    def reject(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 10014, "msg": "app secret invalid"})

    s = Settings(public_url="http://x", data_dir=Path("."), secret_key="k" * 40)
    bad = FeishuClient(s, EnvVault(s), http=httpx.Client(transport=httpx.MockTransport(reject)), sleep=lambda _s: None)
    with pytest.raises(FeishuError, match="app secret invalid"):
        bad.verify("cli_new", "wrong")


def test_send_card():
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append((dict(request.url.params), json.loads(request.content)))
        return httpx.Response(200, json={"code": 0, "data": {}})

    fs, _ = client(handler)
    fs.send_card("ou_1", {"header": {"title": "周报"}})
    params, body = sent[0]
    assert params == {"receive_id_type": "open_id"} and body["receive_id"] == "ou_1" and body["msg_type"] == "interactive"
    assert json.loads(body["content"]) == {"header": {"title": "周报"}}

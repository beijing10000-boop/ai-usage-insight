"""网页：登录、每个角色能打开哪些页面、表单提交的检查、页面安全头。"""

from __future__ import annotations

import io
import re
import time
import zipfile
from datetime import datetime

import pytest

from aius_server.core import hash_secret
from aius_server.feishu import CST
from conftest import D0, D1, D2, log, login, stamped

ADMIN_PAGES = ["/dashboard", "/doubao-ops", "/data", "/data?tab=details", "/members", "/members?q=example", "/quota", "/sync", "/logs", "/logs/1", "/reports", "/config", "/roles", "/audit"]
EVERYONE_PAGES = ["/", "/overview", "/self-usage", "/connect"]


def test_anonymous_visitors_get_the_login_page_in_place(env):
    c = env.browser()
    for path in ["/", "/dashboard", "/self-usage?days=7"]:
        r = c.get(path)
        assert r.status_code == 401 and "使用飞书账号登录" in r.text
    assert "/auth/login?next=%2Fself-usage%3Fdays%3D7" in c.get("/self-usage?days=7").text
    assert c.get("/sync/status").status_code == 401 and c.get("/suggest?q=x").status_code == 401
    assert c.post("/insight/ask", json={"question": "x"}).status_code == 401


def test_login_returns_to_the_page_and_logout_clears_the_session(env):
    c = env.browser()
    login(c, "bob@example.com", "/self-usage?days=7")
    assert c.get("/self-usage").status_code == 200
    assert c.get("/login?next=/connect").headers["location"] == "/connect"
    assert "你已退出登录" in c.get("/auth/logout").text
    assert c.get("/self-usage").status_code == 401


def test_login_never_redirects_off_site(env):
    c = env.browser()
    r = c.get("/auth/login", params={"next": "//evil.example.com/x"})
    state = re.search(r"state=([\w-]+)", r.headers["location"]).group(1)
    assert c.get("/auth/callback", params={"state": state, "email": "bob@example.com"}).headers["location"] == "/"
    assert env.browser().get("/auth/callback", params={"state": "forged", "email": "bob@example.com"}).status_code == 400


def test_admin_can_open_every_page(env):
    c = env.browser("admin@example.com")
    for path in ADMIN_PAGES + EVERYONE_PAGES + ["/dept-ops", "/dept-ops?dept=d-rd", "/dashboard?dept=d-tech", "/doubao-ops?by=week&dept=d-tech"]:
        r = c.get(path)
        assert r.status_code == 200, path


@pytest.mark.parametrize("email", ["dave@example.com", "bob@example.com"])
def test_other_roles_cannot_open_admin_pages(env, email):
    c = env.browser(email)
    for path in ADMIN_PAGES + ["/dashboard/export.xlsx", "/data/export.xlsx"]:
        r = c.get(path)
        assert r.status_code == 403 and "需要运营管理员权限" in r.text, path
    for path in EVERYONE_PAGES:
        assert c.get(path).status_code == 200, path
    assert c.get("/sync/status").status_code == 403 and c.get("/suggest?q=研发").status_code == 403


def test_regular_user_only_sees_own_usage(env):
    c = env.browser("dave@example.com")
    page = c.get(f"/self-usage?start={D1}&end={D1}").text
    assert "智能纪要" in page and "鲍勃" not in page
    assert c.get("/self-usage?member=ou_bob").status_code == 403
    assert c.get("/self-usage/export.xlsx?member=ou_bob").status_code == 403
    assert c.get("/dept-ops").status_code == 403
    portal = c.get(f"/?start={D1}&end={D1}").text
    assert "戴维 个人" in portal and "异常线索" not in portal


def test_department_leader_sees_own_department_tree_only(env):
    bob = env.browser("bob@example.com")  # 研发部负责人
    page = bob.get(f"/dept-ops?start={D1}&end={D1}")
    assert page.status_code == 200 and "研发部" in page.text and "戴维" in page.text
    assert bob.get("/dept-ops?dept=d-sales").status_code == 403 and bob.get("/dept-ops?dept=d-tech").status_code == 403
    assert bob.get("/self-usage?member=ou_dave").status_code == 200  # 本部门成员
    assert bob.get("/self-usage?member=ou_carol").status_code == 403
    alice = env.browser("alice@example.com")  # 技术中心负责人：能往下看研发部、运维部
    assert alice.get("/dept-ops?dept=d-rd").status_code == 200 and alice.get("/self-usage?member=ou_erin").status_code == 200
    assert "你负责的部门（技术中心）" in alice.get("/").text


def test_pages_carry_a_strict_csp_and_have_no_inline_code(env):
    c = env.browser("admin@example.com")
    for path in ADMIN_PAGES + EVERYONE_PAGES + ["/dept-ops"]:
        r = c.get(path)
        csp = r.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "unsafe-inline" not in csp and r.headers["x-frame-options"] == "DENY"
        assert not re.search(r"<script(?![^>]*\bsrc=)", r.text), path
        assert not re.search(r"\sstyle=|<style|\son\w+=\"", r.text), path


def test_names_from_feishu_are_escaped(empty):
    from conftest import log, stamped

    empty.feishu.usage[("log", "member", D1)] = stamped([log("ou_bob", "d-rd", "<img src=x onerror=alert(1)>场景", 5)], D1)
    empty.sync(D1, D1)
    c = empty.browser("admin@example.com")
    for path in (f"/dashboard?start={D1}&end={D1}", f"/data?start={D1}&end={D1}"):
        text = c.get(path).text
        assert "<img src=x" not in text and "&lt;img src=x" in text


def test_posts_need_a_session_the_right_origin_and_the_admin_role(env):
    assert "登录已过期" in env.browser().post("/sync", data={"action": "check"}).text
    admin = env.browser("admin@example.com")
    r = admin.post("/sync", data={"action": "check"}, headers={"Origin": "http://evil.example.com"})
    assert r.status_code == 403 and "来源校验失败" in r.text
    dave = env.browser("dave@example.com")
    for path, data in (("/sync", {"action": "start", "start": D1, "end": D1}), ("/quota", {"action": "delete", "id": "1"}), ("/roles", {"action": "grant", "user": "ou_dave"}),
                       ("/config", {"action": "pricing", "point_price": "9"}), ("/dashboard/rollup", {"action": "clear"})):  # fmt: skip
        assert dave.post(path, data=data).status_code == 403, path
    assert env.ctx.engine.active() is None and env.ctx.config.point_price == 0.05 and not env.ctx.access.viewer("ou_dave").is_admin


def test_sync_page_shows_the_live_sync(env):
    env.ctx.config.save("sync", {"enabled": True}, "ou_admin")
    c = env.browser("admin@example.com")
    assert "等待第一次更新" in c.get("/sync").text
    env.feishu.usage[("log", "member", D0)] = stamped([log("ou_bob", "d-rd", "知识问答", 1, hour=0)], D0)
    now = int(time.time())
    env.ctx.engine.live(now=now)
    clock = datetime.fromtimestamp(now, CST).strftime("%H:%M")
    page = c.get("/sync").text
    assert f"数据截至 {clock}" in page and "新拉到 1 条" in page and "每 10 分钟更新" in page
    assert f"数据截至 {D0} {clock}" in c.get("/dashboard").text  # 侧栏带上几点


def test_member_search_matches_while_typing(env):
    from aius_server.feishu import DirUser

    env.ctx.directory._users["ou_anna"] = DirUser("ou_anna", "Anna.Smith", "Anna.Smith", "anna@example.com", ("d-rd",))
    admin = env.browser("admin@example.com")
    page = admin.get("/members").text
    assert "data-member-search" in page and 'role="combobox"' in page and 'aria-controls="member-q-results"' in page
    items = admin.get("/members/search", params={"q": "bo"}).json()["items"]  # 英文名的一部分
    bob = next(i for i in items if i["id"] == "ou_bob")
    assert bob["name"] == "鲍勃" and bob["href"] == "/self-usage?member=ou_bob" and bob["dept_path"].endswith("研发部") and "点 · ¥" in bob["usage"]
    for q in ("anna", "ANNA S", "anna smith", "annasmith", "smith", "Anna.S"):  # 不用输全名，空格、点都不要紧
        assert [i["id"] for i in admin.get("/members/search", params={"q": q}).json()["items"]] == ["ou_anna"], q
    assert admin.get("/members/search", params={"q": "  "}).json() == {"items": [], "more": False}
    many = admin.get("/members/search", params={"q": "example.com"}).json()
    assert len(many["items"]) == 8 and not many["more"]
    assert env.browser("bob@example.com").get("/members/search", params={"q": "bo"}).status_code == 403
    assert env.browser().get("/members/search", params={"q": "bo"}).status_code == 401


def test_money_is_shown_next_to_points(env):
    admin = env.browser("admin@example.com")
    page = admin.get("/dashboard").text  # 两天合计 214 点（人员 144 + 应用 70），0.05 元/点
    assert "消耗金额" in page and "¥10.70" in page and "按 0.05 元/点折算" in page
    assert "¥5.00" in page and "¥2.00" in page  # 人员排行里卡萝 100 点，应用排行里 spring_b__c 40 点
    page = admin.get("/dept-ops?dept=d-rd").text  # 研发部：鲍勃 38 + 戴维 1
    assert "部门消耗金额" in page and "¥1.95" in page and "¥1.90" in page
    page = env.browser("bob@example.com").get("/self-usage").text
    assert "消耗金额" in page and "¥1.90" in page
    assert "<th class=\"num\">金额</th>" in admin.get("/members").text


def test_admin_looks_up_anyone_and_sees_their_usage_page(env):
    c = env.browser("admin@example.com")
    page = c.get("/members").text
    assert 'aria-current="page"' in page and "成员用量" in page and "本月用量最多的人" in page
    if D1[:7] == D0[:7]:  # 测试数据在昨天：昨天还在本月时，排行里有人
        assert "卡萝" in page and "/self-usage?member=ou_carol" in page
    # 只搜到一个人：直接打开他的用量页
    for q in ("艾琳", "Erin", "erin@example.com", "ou_erin"):
        r = c.get("/members", params={"q": q})
        assert r.status_code == 303 and r.headers["location"] == "/self-usage?member=ou_erin", q
    page = c.get("/members", params={"q": "example.com"}).text  # 搜到好几个：列出来让人选
    assert "找到 7 个人" in page and "/self-usage?member=ou_bob" in page
    assert "没有找到「不存在的人」" in c.get("/members", params={"q": "不存在的人"}).text
    # 他的用量页：和他自己看到的一样，导航停在「成员用量」，可以直接换一个人
    page = c.get("/self-usage?member=ou_bob").text
    assert "鲍勃 的用量" in page and "和他自己在「我的用量」里看到的一样" in page and 'id="switch-member"' in page
    assert re.search(r'class="nav-link active" href="/members"', page)
    own = env.browser("bob@example.com").get("/self-usage").text
    for part in ("消耗点数", "用在哪里", "额度类型", "使用明细"):
        assert part in page and part in own
    # 部门负责人看本部门成员：导航停在「部门运营」，没有换人的搜索框（那是运营管理员的）
    page = env.browser("bob@example.com").get("/self-usage?member=ou_dave").text
    assert re.search(r'class="nav-link active" href="/dept-ops"', page) and 'id="switch-member"' not in page


def test_sync_page_shows_the_data_check(env):
    c = env.browser("admin@example.com")
    page = c.get("/sync").text
    assert "数据核对" in page and "有差异" in page and "艾琳（日志 5 / 详情 0）" in page
    run = env.ctx.engine.runs()[0][0]
    assert "对不上" in c.get(f"/logs/{run['id']}").text
    env.db.x("DELETE FROM reconcile")
    assert "未核对" in c.get("/sync").text
    r = c.post("/sync", data={"action": "reconcile"})
    assert r.headers["location"] == "/sync?done=reconciled#reconcile" and "有差异" in c.get("/sync").text
    assert "对不上" in env.db.val("SELECT detail FROM audit_log WHERE action='reconcile'")
    env.db.x("UPDATE reconcile SET mismatched=0")
    assert "全部一致" in c.get("/sync").text


def test_sync_page_actions(env):
    c = env.browser("admin@example.com")
    assert c.post("/sync", data={"action": "check"}).headers["location"] == "/sync?done=connected"
    assert "已连接" in c.get("/sync").text
    r = c.post("/sync", data={"action": "schedule", "enabled": "1", "hour": "6", "range_days": "3"})
    assert r.status_code == 303 and env.ctx.config.get("sync") == {"enabled": True, "hour": 6, "range_days": 3, "live_minutes": 10, "last_auto_date": ""}
    assert c.post("/sync", data={"action": "schedule", "enabled": "1", "hour": "99", "range_days": "3"}).status_code == 400
    assert c.post("/sync", data={"action": "schedule", "enabled": "1", "hour": "6", "range_days": "3", "live_minutes": "2"}).status_code == 400
    c.post("/sync", data={"action": "schedule", "enabled": "1", "hour": "6", "range_days": "3", "live_minutes": "30"})
    assert env.ctx.config.get("sync")["live_minutes"] == 30
    assert "当天数据每 30 分钟更新" in env.db.val("SELECT detail FROM audit_log WHERE action='config' ORDER BY id DESC LIMIT 1")
    assert c.post("/sync", data={"action": "start", "start": D1, "end": D2}).status_code == 400
    assert c.post("/sync", data={"action": "start", "start": D1, "end": D1}).headers["location"] == "/sync?done=sync_started"
    status = c.get("/sync/status").json()["active"]
    assert status["status"] == "queued" and "任务 #" in c.get("/sync").text
    assert c.post("/sync", data={"action": "stop", "id": str(status["id"])}).status_code == 303
    assert c.get("/sync/status").json() == {"active": None}
    assert c.get(f"/logs/{status['id']}").status_code == 200 and c.get("/logs/999").status_code == 404
    assert c.get("/logs?status=stopped").text.count("已停止") >= 1


def test_quota_form(env):
    c = env.browser("admin@example.com")
    form = {"action": "save", "dimension": "department", "target": "研发部", "amount": "50", "period_start": D2, "period_end": D1, "note": "测试"}
    assert c.post("/quota", data=form).headers["location"] == "/quota?done=quota_saved"
    page = c.get("/quota").text
    assert "技术中心 &gt; 研发部" in page and "偏高" in page  # 已用 39 / 50 = 78%
    quota_id = env.db.val("SELECT id FROM quota_allocations")
    assert c.post("/quota", data={**form, "target": "不存在的部门"}).status_code == 400
    assert c.post("/quota", data={**form, "amount": "abc"}).status_code == 400
    assert "修改额度" in c.get(f"/quota?edit={quota_id}").text
    c.post("/quota", data={**form, "id": str(quota_id), "amount": "20"})
    assert env.ctx.quotas.list()[0]["rate"] == 195.0
    c.post("/quota", data={"action": "disable", "id": str(quota_id)})
    assert env.ctx.quotas.list()[0]["state_label"] == "已停用"
    c.post("/quota", data={"action": "delete", "id": str(quota_id)})
    assert env.ctx.quotas.list() == []


def test_config_page_actions(env):
    c = env.browser("admin@example.com")
    assert "还没有名称" in c.get("/config").text  # spring_b__c 没解析出名称
    c.post("/config", data={"action": "mapping_save", "app_id": "spring_b__c", "name": "合同审核"})
    assert env.ctx.analytics.app_names()["spring_b__c"] == "合同审核"
    c.post("/config", data={"action": "mapping_delete", "app_id": "spring_b__c"})
    assert "spring_b__c" not in env.ctx.analytics.app_names()
    c.post("/config", data={"action": "pricing", "point_price": "0.1"})
    assert env.ctx.config.point_price == 0.1
    assert c.post("/config", data={"action": "pricing", "point_price": "-1"}).status_code == 400


def test_reports_page_actions(env):
    c = env.browser("admin@example.com")
    page = c.get("/reports").text
    assert all(f'id="{k}"' in page for k in ("daily", "weekly", "monthly")) and page.count("已关闭") == 3  # 默认三个都关着
    assert 'class="nav-link active" href="/reports"' in page
    tomorrow = datetime.now(CST).isoweekday() % 7 + 1  # 不是今天：保存后今天不会到点
    r = c.post("/reports", data={"action": "save", "kind": "weekly", "enabled": "1", "weekday": str(tomorrow), "hour": "10"})
    assert r.headers["location"] == "/reports?done=saved#weekly"
    assert env.ctx.config.get("report_weekly") == {"enabled": True, "weekday": tomorrow, "hour": 10, "last_push_date": "", "last_period": ""}
    # 打开开关时今天已经过了发送时间：这一期不补发，提示从下一个发送时间开始
    r = c.post("/reports", data={"action": "save", "kind": "daily", "enabled": "1", "hour": "0"})
    assert r.headers["location"] == "/reports?done=report_saved_skip#daily" and not env.ctx.report.due("daily", datetime.now(CST))
    assert "定时不会再发" in c.get("/reports").text and "不自动补发" in env.db.val("SELECT detail FROM audit_log WHERE action='report' ORDER BY id DESC LIMIT 1")
    c.post("/reports", data={"action": "save", "kind": "monthly", "enabled": "0", "day": "5", "hour": "8"})
    assert env.ctx.config.get("report_monthly")["day"] == 5 and not env.ctx.config.get("report_monthly")["enabled"]
    assert c.post("/reports", data={"action": "save", "kind": "monthly", "day": "31", "hour": "8"}).status_code == 400
    assert c.post("/reports", data={"action": "save", "kind": "yearly"}).status_code == 400
    c.post("/reports", data={"action": "scope", "scope": "all"})
    assert env.ctx.config.get("report")["scope"] == "all"
    assert c.post("/reports", data={"action": "test", "kind": "daily"}).headers["location"] == "/reports?done=report_test#daily"
    assert [oid for oid, _card in env.feishu.sent] == ["ou_admin"]
    runs = c.get("/reports").text
    assert "日报" in runs and "测试" in runs and "已开启" in runs
    assert "日报测试发送给自己" in env.db.val("SELECT detail FROM audit_log WHERE action='report' ORDER BY id DESC LIMIT 1")
    assert env.browser("bob@example.com").post("/reports", data={"action": "test", "kind": "daily"}).status_code == 403


def test_roles_page(env):
    c = env.browser("admin@example.com")
    assert c.post("/roles", data={"action": "grant", "user": "戴维"}).headers["location"] == "/roles?done=granted"
    assert env.ctx.access.viewer("ou_dave").is_admin and env.browser("dave@example.com").get("/dashboard").status_code == 200
    assert c.post("/roles", data={"action": "grant", "user": "没有这个人"}).status_code == 400
    assert c.post("/roles", data={"action": "revoke", "user": "ou_admin"}).status_code == 400  # 不能取消自己
    c.post("/roles", data={"action": "revoke", "user": "ou_dave"})
    assert not env.ctx.access.viewer("ou_dave").is_admin
    page = c.get("/roles").text
    assert "固定（环境变量）" in page and "operations_admin" in page and "regular_user" in page


def test_tokens_are_shown_once_and_can_be_revoked(env):
    c = env.browser("dave@example.com")
    r = c.post("/connect", data={"action": "create", "label": "我的工具", "days": "30"})
    token = re.search(r'value="(aius_[\w-]+)"', r.text).group(1)
    assert r.status_code == 200 and "claude mcp add" in r.text
    assert env.db.token_owner(hash_secret(token)) == "ou_dave"
    page = c.get("/connect").text
    assert token not in page and "我的工具" in page  # 明文只出现一次
    assert env.db.val("SELECT COUNT(*) FROM api_tokens WHERE token_hash=?", (token,)) == 0  # 库里只有哈希
    other = env.browser("bob@example.com")
    other.post("/connect", data={"action": "revoke", "token": hash_secret(token)})
    assert env.db.token_owner(hash_secret(token)) == "ou_dave"  # 别人撤销不了我的令牌
    c.post("/connect", data={"action": "revoke", "token": hash_secret(token)})
    assert env.db.token_owner(hash_secret(token)) is None


def test_rollup_selection_is_saved_per_user(env):
    c = env.browser("admin@example.com")
    c.post("/dashboard/rollup", data={"action": "add", "dept": "研发部", "next": "/dashboard"})
    c.post("/dashboard/rollup", data={"action": "add", "dept": "d-sales", "next": "/dashboard"})
    assert env.db.pref("ou_admin", "rollup") == ["d-rd", "d-sales"]
    page = c.get(f"/dashboard?start={D1}&end={D1}").text
    assert "圈选合计" in page and "移出" in page
    c.post("/dashboard/rollup", data={"action": "remove", "dept": "d-rd", "next": "/dashboard"})
    assert env.db.pref("ou_admin", "rollup") == ["d-sales"]


def test_exports_are_real_xlsx_files(env):
    c = env.browser("admin@example.com")
    # 驾驶舱：指标、每日趋势、每月汇总、部门、人员、应用、场景（设置了每人每月额度时再加一张月度额度）
    for path, sheet_count in ((f"/dashboard/export.xlsx?start={D1}&end={D1}", 7), (f"/data/export.xlsx?start={D2}&end={D1}", 1), ("/data/export.xlsx?tab=details", 1)):
        r = c.get(path)
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/vnd.openxmlformats")
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            sheets = [n for n in z.namelist() if n.startswith("xl/worksheets/")]
            assert len(sheets) == sheet_count and z.testzip() is None
    with zipfile.ZipFile(io.BytesIO(c.get(f"/data/export.xlsx?start={D2}&end={D1}").content)) as z:
        body = z.read("xl/worksheets/sheet1.xml").decode()
    assert "鲍勃" in body and "豆包工作&gt;主对话&gt;深度思考" in body and body.count("<row ") == 9


def test_insight_answers_with_a_data_summary_when_no_model_is_configured(env):
    c = env.browser("admin@example.com")
    r = c.post(f"/insight/ask?start={D1}&end={D1}", json={"question": "哪些部门用得多？"})
    data = r.json()
    assert r.status_code == 200 and data["model"] is False and data["scope"] == "全企业"
    assert "<strong>206 点</strong>" in data["html"] and "销售部" in data["html"]
    assert c.post("/insight/ask", json={"question": "再问一次"}).status_code == 429  # 问得太快
    assert c.post("/insight/ask", json={"question": " "}).status_code == 400
    dave = env.browser("dave@example.com").post(f"/insight/ask?start={D1}&end={D1}", json={"question": "我用了多少"}).json()
    assert "戴维 个人" in dave["scope"] and "鲍勃" not in dave["html"] and "销售部" not in dave["html"]


def test_range_errors_fall_back_to_the_default_with_a_notice(env):
    c = env.browser("admin@example.com")
    assert "开始日期不能晚于结束日期" in c.get(f"/dashboard?start={D1}&end={D2}").text
    assert "日期要写成" in c.get("/dashboard?start=abc&end=def").text
    assert c.get("/dashboard?days=5").status_code == 200  # 不认识的天数按默认


def test_odd_parameters_do_not_break_pages(env):
    c = env.browser("admin@example.com")
    for path in ("/data?feature=%C2%B2&usage=%C2%B2", "/data?feature=99999999999999999999", "/data?page=99999999999999999999999", "/logs?page=-5",
                 "/dashboard?start=0001-01-01&end=0001-01-02", "/dashboard?start=9999-12-30&end=9999-12-31", "/dashboard?dept=no-such-dept",
                 "/self-usage?member=", "/audit?page=abc", "/doubao-ops?by=year"):  # fmt: skip
        assert c.get(path).status_code == 200, path
    assert "日期超出了可查询的范围" in c.get("/dashboard?start=0001-01-01&end=0001-01-02").text


def test_admin_can_revoke_anyones_token(env):
    env.api("ou_dave")  # 戴维有一个令牌
    token_hash = hash_secret("aius_test_ou_dave")
    admin = env.browser("admin@example.com")
    page = admin.get("/roles").text
    assert "全部访问令牌" in page and "戴维" in page and "aius_test_ou_dave" not in page
    assert env.browser("bob@example.com").post("/roles", data={"action": "token_revoke", "token": token_hash}).status_code == 403
    assert env.db.token_owner(token_hash) == "ou_dave"
    assert admin.post("/roles", data={"action": "token_revoke", "token": token_hash}).headers["location"] == "/roles?done=revoked#tokens"
    assert env.db.token_owner(token_hash) is None


def test_leader_portal_stays_inside_their_departments(env):
    bob = env.browser("bob@example.com")
    page = bob.get(f"/?start={D1}&end={D1}").text
    assert 'href="/dept-ops?' in page and 'href="/dashboard' not in page  # 负责人打不开驾驶舱，链接不能指过去
    assert "占在职 100%" in page  # 研发部 2 人都用过；不是按全企业 7 人算的 28.6%


def test_self_usage_hides_the_department_total_when_too_few_colleagues_use_ai(env):
    page = env.browser("dave@example.com").get(f"/self-usage?start={D1}&end={D1}").text
    assert "不到 3 位" in page and "部门用量" not in page
    leader = env.browser("bob@example.com").get(f"/self-usage?start={D1}&end={D1}").text  # 负责人本来就能看本部门
    assert "部门用量" in leader


def test_dept_ops_header_uses_a_dropdown_and_never_calls_direct_members_a_department(env):
    from conftest import log, stamped

    admin = env.browser("admin@example.com")
    page = admin.get(f"/dept-ops?dept=d-rd&start={D1}&end={D1}").text
    # 运营管理员能切换全部一级部门：用下拉框；下钻到研发部时，选中的是它所在的一级部门
    assert '<select id="dept-pick" name="dept">' in page and '<option value="d-tech" selected>技术中心</option>' in page
    assert '<option value="d-sales">销售部</option>' in page and 'class="segmented" role="group" aria-label="切换部门"' not in page
    assert "负责人 鲍勃" in page and "个下级部门有用量" not in page  # 研发部没有下级部门
    # 只有直属成员有用量时，本期解读不能说「用量最高的部门是直属成员」
    env.feishu.usage[("log", "member", D1)] = stamped([log("ou_alice", "d-tech", "知识问答", 7)], D1)
    env.sync(D1, D1)
    assert "直属成员」" not in admin.get(f"/dept-ops?dept=d-tech&start={D1}&end={D1}").text.split("本期解读")[1].split("</p>")[0]
    bob = env.browser("bob@example.com").get("/dept-ops").text  # 只负责一个部门：不出现切换
    assert 'id="dept-pick"' not in bob and "研发部" in bob


def test_health(env):
    assert env.browser().get("/healthz").json()["ok"] is True

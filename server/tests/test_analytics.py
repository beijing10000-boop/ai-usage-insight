"""统计口径：总量、部门向上汇总、环比、场景、豆包、异常、明细、个人、额度。"""

from __future__ import annotations

from datetime import timedelta

from aius_server.analytics import DIRECT, Filter
from aius_server.sync import today
from conftest import D0, D1, D2, log, stamped


def test_total_is_member_logs_plus_app_logs(env):
    an = env.ctx.analytics
    cur = an.slice(Filter(D1, D1))
    assert (cur.member_total, cur.app_total, cur.total) == (136, 70, 206)
    k = an.kpis(cur, None)
    assert (k["active_members"], k["active_depts"], k["active_apps"], k["records"]) == (4, 3, 2, 7)
    assert k["cost"] == 10.3 and k["last_day"] == 206 and k["per_capita"] == 34
    assert k["comparable"] is False and k["change"] is None


def test_departments_roll_up_to_their_parents(env):
    an = env.ctx.analytics
    cur = an.slice(Filter(D1, D1))
    top = {d["name"]: d for d in an.rank_depts(cur)}
    assert top["技术中心"]["amount"] == 36 and top["技术中心"]["users"] == 3  # 研发部 31 + 运维部 5
    assert top["销售部"]["amount"] == 100
    assert top["技术中心"]["headcount"] == 5 and top["技术中心"]["share"] == 26.5
    below = {d["name"]: d for d in an.rank_depts(an.slice(Filter(D1, D1, depts=frozenset(env.ctx.directory.subtree("d-tech")))), None, "d-tech")}
    assert below["研发部"]["amount"] == 31 and below["运维部"]["amount"] == 5 and "直属成员" not in below


def test_direct_members_get_their_own_row_when_drilling_down(empty):
    empty.feishu.usage[("log", "member", D1)] = stamped([log("ou_alice", "d-tech", "知识问答", 7), log("ou_bob", "d-rd", "知识问答", 3)], D1)
    empty.sync(D1, D1)
    an = empty.ctx.analytics
    rows = an.rank_depts(an.slice(Filter(D1, D1, depts=frozenset(empty.ctx.directory.subtree("d-tech")))), None, "d-tech")
    assert [(r["id"], r["amount"], r["drill"]) for r in rows] == [(DIRECT, 7, False), ("d-rd", 3, True)]


def test_change_is_only_computed_when_the_previous_period_is_fully_synced(env):
    an = env.ctx.analytics
    assert an.previous(Filter(D1, D1)) is not None  # 前一天（D2）同步过
    k = an.kpis(an.slice(Filter(D1, D1)), an.previous(Filter(D1, D1)))
    assert k["prev_total"] == 8 and k["change"] == 2475.0
    assert an.previous(Filter(D2, D1)) is None  # 再往前两天没同步过：不比较


def test_scenarios_by_product_and_by_full_path(env):
    an = env.ctx.analytics
    products = {s["name"]: s for s in an.scenarios(Filter(D1, D1), level=1)}
    assert products["知识问答"]["amount"] == 15 and products["知识问答"]["users"] == 2
    assert products["应用调用"]["users"] == 0  # 使用人数只数人，应用不算
    paths = [s["name"] for s in an.scenarios(Filter(D1, D1), level=0)]
    assert paths[0] == "豆包工作伙伴>专属伙伴>对话" and "豆包工作>主对话>深度思考" in paths


def test_doubao_counts_doubao_work_but_not_doubao_partner(env):
    cur = env.ctx.analytics.slice(Filter(D1, D1, doubao=True))
    assert cur.total == 20 and list(cur.by_member) == ["ou_bob"]


def test_splits_by_quota_type_and_paid_or_free(env):
    an = env.ctx.analytics
    features = {x["name"]: x["amount"] for x in an.split(Filter(D1, D1), "feature_key")}
    assert features == {"飞书 Aily 专用额度": 100, "AI 通用额度": 85, "团队订阅席位额度": 20, "智能纪要专用额度": 1}
    usage = {x["name"]: x["amount"] for x in an.split(Filter(D1, D1), "usage_type")}
    assert usage == {"企业付费额度": 201, "试用免费额度": 5}


def test_hourly_uses_beijing_time(env):
    hours = {h["hour"]: h["total"] for h in env.ctx.analytics.hourly(Filter(D1, D1))}
    assert hours[10] == 116 and hours[15] == 20 and hours[3] == 70 and hours[0] == 0


def test_anomalies_flag_concentration_and_spikes(empty):
    days = [(today() - timedelta(days=i)).isoformat() for i in range(1, 6)]
    for day in days:
        amount = 500 if day == days[0] else 10
        empty.feishu.usage[("log", "member", day)] = stamped(
            [log("ou_bob", "d-rd", "知识问答", amount), log("ou_dave", "d-rd", "知识问答", 2), log("ou_erin", "d-ops", "知识问答", 2), log("ou_carol", "d-sales", "知识问答", 2)], day
        )
    empty.sync(days[-1], days[0])
    an = empty.ctx.analytics
    f = Filter(days[-1], days[0])
    found = {a["type"]: a for a in an.anomalies(an.slice(f), an.previous(f))}
    assert "spike" in found and days[0] in found["spike"]["text"]
    assert "concentration" in found and "鲍勃" in found["concentration"]["text"]
    assert "growth" not in found  # 上一个周期没有同步过，不报「增长」


def test_logs_can_be_searched_by_scenario_person_or_app(env):
    an = env.ctx.analytics
    f = Filter(D2, D1)
    rows, total, amount = an.logs(f)
    assert total == 8 and amount == 214 and rows[0]["date"] == D1
    assert an.logs(f, query="豆包")[1] == 2
    assert an.logs(f, query="鲍勃")[1] == 3
    assert an.logs(f, query="客服机器人")[1] == 1
    assert an.logs(f, subject="app")[1] == 2
    assert an.logs(f, usage_type=2)[1] == 1 and an.logs(f, feature_key=4)[1] == 1
    assert an.logs(f, query="100%")[1] == 0  # 通配符只当普通字符
    carol = an.logs(Filter(D1, D1, member="ou_carol"))[0][0]
    assert carol["name"] == "卡萝" and carol["dept"] == "销售部" and carol["notes"][0]["value"] == "标准模型" and carol["cost"] == 5
    app = an.logs(f, query="客服机器人")[0][0]
    assert app["name"] == "客服机器人" and app["dept"] == ""


def test_details_list(env):
    rows, total = env.ctx.analytics.details(D1, D1)
    assert total == 3
    bob = next(r for r in rows if r["entity_id"] == "ou_bob")
    assert bob["quotas"] == [10, 0, 0, 20, 0, 0] and bob["total"] == 30 and bob["name"] == "鲍勃"
    assert env.ctx.analytics.details(D1, D1, member="ou_carol")[1] == 1


def test_rollup_counts_a_department_once_even_if_its_parent_is_also_picked(env):
    an = env.ctx.analytics
    cur = an.slice(Filter(D1, D1))
    for picked in (["d-tech", "d-rd", "d-sales"], ["d-rd", "d-sales", "d-tech"]):  # 结果和加入的先后无关
        r = an.rollup(cur, picked)
        assert r["total"] == 136 and r["users"] == 4 and r["share_of_all"] == 100  # 研发部含在技术中心里，合计不重复算
        assert {row["id"]: row["amount"] for row in r["rows"]} == {"d-tech": 36, "d-rd": 31, "d-sales": 100}


def test_member_profile_shows_own_usage_and_department_aggregate(env):
    an = env.ctx.analytics
    p = an.member_profile("ou_bob", D1, D1, dept_visible=True)  # 负责人 / 管理员看：部门汇总完整
    assert p["total"] == 30 and p["records"] == 2 and p["active_days"] == 1 and p["dept_path"] == "技术中心 > 研发部"
    assert [s["name"] for s in p["products"]] == ["豆包工作", "知识问答"]
    assert p["dept"]["total"] == 31 and p["dept"]["active_members"] == 2 and p["dept"]["my_share"] == 96.8 and p["dept"]["my_rank"] == 1
    idle = an.member_profile("ou_frank", D1, D1, dept_visible=True)
    assert idle["total"] == 0 and idle["dept"]["name"] == "销售部" and idle["dept"]["total"] == 100 and idle["dept"]["my_rank"] is None


def test_small_departments_do_not_reveal_colleagues_to_regular_users(env, tmp_path):
    an = env.ctx.analytics
    # 研发部只有鲍勃和戴维用过：戴维要是看到部门合计 31 点，减掉自己的 1 点就是鲍勃的 30 点
    dave = an.member_profile("ou_dave", D1, D1)["dept"]
    assert dave == {"id": "d-rd", "name": "研发部", "headcount": 2, "hidden": True}
    # 弗兰克自己没用过，部门里只有卡萝一个人用过：部门合计就是卡萝的个人数据
    assert an.member_profile("ou_frank", D1, D1)["dept"]["hidden"] is True
    # 除了自己还有 3 位同事用过：可以看部门汇总
    from conftest import Env

    big = Env(tmp_path / "big", synced=False)
    big.feishu.usage[("log", "member", D1)] = stamped([log(u, "d-ops", "知识问答", n) for u, n in (("ou_erin", 1), ("ou_admin", 2), ("ou_alice", 3), ("ou_bob", 4))], D1)
    big.sync(D1, D1)
    dept = big.ctx.analytics.member_profile("ou_erin", D1, D1)["dept"]
    assert dept["hidden"] is False and dept["total"] == 10 and dept["my_share"] == 10 and dept["my_rank"] == 4


def test_share_in_department_ignores_usage_from_a_previous_department(empty):
    # 戴维上半天在运维部用了 9 点，调到研发部后用了 1 点：他在研发部的占比按 1 点算，不会超过 100%
    empty.feishu.usage[("log", "member", D1)] = stamped(
        [log("ou_dave", "d-ops", "知识问答", 9, hour=9), log("ou_dave", "d-rd", "知识问答", 1, hour=15), log("ou_bob", "d-rd", "知识问答", 3)], D1
    )
    empty.sync(D1, D1)
    p = empty.ctx.analytics.member_profile("ou_dave", D1, D1, dept_visible=True)
    assert p["total"] == 10 and p["dept"]["total"] == 4 and p["dept"]["my_share"] == 25 and p["dept"]["my_rank"] == 2


def test_change_needs_every_day_of_the_previous_period_to_be_synced(empty):
    days = [(today() - timedelta(days=i)).isoformat() for i in range(1, 7)]  # days[0] 是昨天
    for day in days:
        empty.feishu.usage[("log", "member", day)] = stamped([log("ou_bob", "d-rd", "知识问答", 10)], day)
    empty.sync(days[5], days[4])  # 最早的两天
    empty.sync(days[1], days[0])  # 最近的两天；中间两天没同步（比如服务停了）
    an = empty.ctx.analytics
    assert an.previous(Filter(days[1], days[0])) is None  # 上一个周期（中间那两天）没同步过：不比较
    empty.sync(days[3], days[2])
    assert an.previous(Filter(days[1], days[0])).total == 20


def test_department_views_use_the_departments_own_headcount_and_children(empty):
    days = [(today() - timedelta(days=i)).isoformat() for i in (1, 2)]
    empty.feishu.usage[("log", "member", days[1])] = stamped([log("ou_bob", "d-rd", "知识问答", 10), log("ou_erin", "d-ops", "知识问答", 10)], days[1])
    empty.feishu.usage[("log", "member", days[0])] = stamped([log("ou_bob", "d-rd", "知识问答", 40), log("ou_erin", "d-ops", "知识问答", 10)], days[0])
    empty.sync(days[1], days[0])
    an = empty.ctx.analytics
    f = Filter(days[0], days[0], depts=frozenset(empty.ctx.directory.subtree("d-tech")))
    k = an.kpis(an.slice(f), an.previous(f))
    assert k["headcount"] == 5 and k["coverage"] == 40  # 技术中心 5 人里 2 人用过，不是全企业 7 人
    found = an.anomalies(an.slice(f), an.previous(f), "d-tech")
    growth = next(a for a in found if a["type"] == "growth")
    assert "研发部" in growth["text"] and "技术中心" not in growth["text"] and growth["link"] == "/dept-ops?dept=d-rd"
    assert an.kpis(an.slice(Filter(days[0], days[0], member="ou_bob")))["coverage"] is None


def test_default_range_ends_at_the_latest_synced_day(env, empty):
    assert env.ctx.analytics.default_range(7) == ((today() - timedelta(days=7)).isoformat(), D1)
    assert empty.ctx.analytics.default_range(7)[1] == D1  # 没同步过：到昨天
    assert D0 > D1


def test_quota_usage_includes_sub_departments(env):
    q = env.ctx.quotas
    q.save("ou_admin", "department", "d-tech", 40, D2, D1)
    q.save("ou_admin", "member", "ou_carol", 50, D1, D1)
    q.save("ou_admin", "app", "cli_a", 300, D1, D1, note="测试")
    rows = {r["name"]: r for r in q.list()}
    assert rows["技术中心"]["used"] == 44 and rows["技术中心"]["rate"] == 110 and rows["技术中心"]["state_label"] == "已超额"
    assert rows["卡萝"]["used"] == 100 and rows["卡萝"]["remaining"] == -50
    assert rows["客服机器人"]["used"] == 30 and rows["客服机器人"]["state_label"] == "正常"


def test_quota_summary_does_not_count_nested_allocations_twice(env):
    q = env.ctx.quotas
    q.save("ou_admin", "department", "d-tech", 100, D2, D0)
    q.save("ou_admin", "department", "d-rd", 60, D2, D0)  # 含在技术中心里
    q.save("ou_admin", "member", "ou_bob", 50, D2, D0)  # 鲍勃在研发部，也含在技术中心里
    q.save("ou_admin", "member", "ou_carol", 200, D2, D0)  # 销售部的人，不在上面任何部门里
    summary = q.summary(q.list())
    assert summary["count"] == 4 and summary["allocated"] == 300 and summary["used"] == 144  # 技术中心 44 + 卡萝 100

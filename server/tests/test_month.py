"""按自然月看：本月 / 上月 / 按月选、和上个月同一段比、按月汇总的趋势、每人每月基础额度。"""

from __future__ import annotations

from datetime import date, timedelta

from conftest import D1, D2

from aius_server.analytics import Filter, month_aligned, month_end
from aius_server.sync import today

MONTH = D1[:7]  # 测试数据（昨天、前天）所在的月份：昨天所在的那个月


def first_of(month: str) -> str:
    return month + "-01"


# ---------- 环比：上个月的同一段 ----------
def test_previous_month_same_days():
    f = Filter("2026-11-01", "2026-11-10", monthly=True)
    p = f.previous()
    assert (p.start, p.end) == ("2026-10-01", "2026-10-10") and f.prev_label == "上月同期"


def test_full_month_compares_with_full_previous_month():
    for start, end, prev in (
        ("2026-10-01", "2026-10-31", ("2026-09-01", "2026-09-30")),
        ("2026-03-01", "2026-03-31", ("2026-02-01", "2026-02-28")),
        ("2024-03-01", "2024-03-31", ("2024-02-01", "2024-02-29")),  # 闰年
        ("2026-01-01", "2026-01-31", ("2025-12-01", "2025-12-31")),  # 跨年
        ("2026-02-01", "2026-02-28", ("2026-01-01", "2026-01-31")),  # 短月整月比长月整月
    ):
        f = Filter(start, end, monthly=True)
        assert (f.previous().start, f.previous().end) == prev and f.prev_label == "上个月", start


def test_partial_month_is_clipped_to_the_shorter_previous_month():
    p = Filter("2026-03-01", "2026-03-30", monthly=True).previous()
    assert (p.start, p.end) == ("2026-02-01", "2026-02-28")


def test_not_month_aligned_falls_back_to_equal_length():
    for f in (Filter("2026-11-02", "2026-11-10", monthly=True), Filter("2026-10-25", "2026-11-05", monthly=True), Filter("2026-11-01", "2026-11-10")):
        p = f.previous()
        assert (date.fromisoformat(f.start) - date.fromisoformat(p.end)).days == 1 and p.days == f.days and f.prev_label == "上一个周期"


def test_month_helpers():
    assert month_aligned("2026-11-01", "2026-11-01") and month_aligned("2026-02-01", "2026-02-28")
    assert not month_aligned("2026-11-01", "2026-12-01") and not month_aligned("2026-11-02", "2026-11-03") and not month_aligned("bad", "2026-11-03")
    assert month_end(date(2024, 2, 10)) == date(2024, 2, 29) and month_end(date(2026, 12, 5)) == date(2026, 12, 31)


def test_kpis_say_what_they_compare_with(env):
    an = env.ctx.analytics
    f = Filter(first_of(MONTH), D1, monthly=True)
    k = an.kpis(an.slice(f), an.previous(f))
    assert k["prev_label"] in ("上月同期", "上个月") and k["prev_start"] == f.previous().start
    assert k["comparable"] is False and k["change"] is None  # 上个月没同步过，不比
    assert f"{k['prev_label']}没有完整的数据可比" in an.narrative(k, [], [], [], [])


# ---------- 按月汇总的趋势 ----------
def test_monthly_periods_cover_every_month_and_add_up(env):
    an = env.ctx.analytics
    start = (date.fromisoformat(first_of(MONTH)) - timedelta(days=40)).replace(day=1)
    cur = an.slice(Filter(start.isoformat(), D1))
    months = an.periods(cur, "month")
    assert [m["date"] for m in months][-1] == MONTH and len(months) in (2, 3)
    assert sum(m["total"] for m in months) == round(cur.total, 2)
    assert months[0]["total"] == 0 and months[0]["partial"] is False  # 没有用量的月份也画出来
    last = months[-1]
    assert last["end"] == D1 and last["partial"] == (date.fromisoformat(D1) != month_end(date.fromisoformat(D1)))
    # 活跃人数按月去重：鲍勃两天都用了，只算一个人
    assert sum(m["users"] for m in months) == len({(r["usage_date"][:7], r["entity_id"]) for r in cur.rows if r["subject"] == "member"})
    weeks = an.periods(cur, "week")
    assert all(date.fromisoformat(w["date"]).weekday() == 0 for w in weeks) and sum(w["total"] for w in weeks) == round(cur.total, 2)


# ---------- 页面上的自然月 ----------
def test_month_presets_on_pages(env):
    c = env.browser("admin@example.com")
    r = c.get("/dashboard?month=this")
    assert r.status_code == 200 and 'href="/dashboard?month=this" aria-current="true"' in r.text and "按自然月和" in r.text
    this = today().isoformat()[:7]
    # 在下拉框里选中本月，记成 this：「本月」那个按钮亮着，链接明天打开还是本月
    r = c.get(f"/dashboard?month={this}")
    assert 'href="/dashboard?month=this" aria-current="true"' in r.text and f'<option value="{this}" selected>' in r.text
    r = c.get("/dashboard?month=last")
    last = (today().replace(day=1) - timedelta(days=1))
    assert f"{last.year} 年 {last.month} 月" in r.text and "比上个月" in r.text
    for bad, text in (("2099-01", "这个月还没到"), ("2026-13", "月份要写成"), ("abc", "月份要写成"), ("2019-12", "超出了可查询的范围")):
        assert text in c.get(f"/dashboard?month={bad}").text, bad


def test_custom_range_from_the_first_counts_as_month_but_presets_do_not(env):
    c = env.browser("admin@example.com")
    assert "比上月同期" in c.get(f"/?start={first_of(MONTH)}&end={first_of(MONTH)}").text
    assert "比上一个周期" in c.get("/?days=7").text


def test_granularity_toggle(env):
    c = env.browser("admin@example.com")
    for path in ("/dashboard", "/dept-ops", "/self-usage", "/doubao-ops"):
        for by in ("day", "week", "month"):
            r = c.get(f"{path}?by={by}&month=this")
            assert r.status_code == 200, (path, by)
    r = c.get(f"/dashboard?by=month&start={D2}&end={D1}")
    assert "每月的 AI 用量" in r.text and "by=month" in r.text


# ---------- 每人每月基础额度 ----------
def set_allowance(env, value: str):
    c = env.browser("admin@example.com")
    r = c.post("/quota", data={"action": "allowance", "per_member": value}, headers={"origin": "http://testserver"})
    return c, r


def test_allowance_off_by_default(env):
    assert env.ctx.config.monthly_allowance == 0 and env.ctx.analytics.allowance(MONTH) is None
    assert "月度额度" not in env.browser("admin@example.com").get(f"/dashboard?month={MONTH}").text


def test_allowance_counts_people_who_used_it_up(env):
    _c, r = set_allowance(env, "100")
    assert r.status_code == 303 and env.ctx.config.monthly_allowance == 100
    a = env.ctx.analytics.allowance(MONTH)
    # 卡萝昨天用了 100 点 = 用完；其他人都远没到
    assert a["used_up"] == 1 and a["near"] == 0 and a["over_points"] == 0 and a["members"][0]["name"] == "卡萝"
    assert a["members"][0]["state_label"] == "已用完" and a["members"][0]["remaining"] == 0
    dave = env.ctx.analytics.allowance(MONTH, member="ou_dave")["members"][0]
    assert dave["amount"] == 1 and dave["remaining"] == 99
    nobody = env.ctx.analytics.allowance(MONTH, member="ou_frank")["members"][0]  # 这个月没用过：0 / 100
    assert nobody["amount"] == 0 and nobody["remaining"] == 100
    rd = env.ctx.analytics.allowance(MONTH, depts=frozenset(env.ctx.directory.subtree("d-rd")))
    assert rd["used_up"] == 0 and {m["name"] for m in rd["members"]} <= {"鲍勃", "戴维"}


def test_allowance_on_pages(env):
    c, _r = set_allowance(env, "100")
    r = c.get(f"/dashboard?month={MONTH}")
    assert "月度额度" in r.text and "卡萝" in r.text and "已用完" in r.text
    assert "每人每月基础额度" in c.get("/quota").text and "卡萝" in c.get(f"/quota?month={MONTH}").text
    dave = env.browser("dave@example.com")
    r = dave.get(f"/self-usage?month={MONTH}")
    assert "基础额度" in r.text and "剩余 99 点" in r.text
    assert "剩余 99 点" in dave.get(f"/?month={MONTH}").text
    data = env.api("ou_dave").get(f"/openapi/v1/self-usage/summary?start={first_of(MONTH)}&end={D1}").json()
    assert data["month_allowance"]["remaining"] == 99 and data["prev_label"] in ("上月同期", "上个月")


def test_allowance_validation_and_clear(env):
    _c, r = set_allowance(env, "-5")
    assert r.status_code == 400 and "负数" in r.text
    _c, r = set_allowance(env, "abc")
    assert r.status_code == 400
    set_allowance(env, "2000")
    set_allowance(env, "")
    assert env.ctx.config.monthly_allowance == 0

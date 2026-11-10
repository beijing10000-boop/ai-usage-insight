"""零散的小模块：调度、配置校验、Excel、Markdown、通讯录、洞察助手、ui-kit。"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from aius_server import excel, md
from aius_server.analytics import Filter
from aius_server.config import Settings
from aius_server.feishu import CST, FeishuError
from aius_server.insight import Insight
from aius_server.prefs import ConfigError, clean
from aius_server.sync import today
from aius_server.vault import Vault
from conftest import D1, make_settings

STATIC = Path(__file__).resolve().parents[1] / "aius_server" / "static"


# ---------- 调度 ----------
def test_daily_sync_runs_once_after_the_configured_hour(empty):
    scheduler, config = empty.app.state.scheduler, empty.ctx.config
    now = datetime.now(CST).replace(hour=6, minute=1)
    scheduler.tick(now)
    assert empty.ctx.engine.active() is None  # 没开启
    config.save("sync", {"enabled": True, "hour": 7, "range_days": 3}, "ou_admin")
    scheduler.tick(now)
    assert empty.ctx.engine.active() is None  # 还没到点
    scheduler.tick(now.replace(hour=9))  # 7 点那一分钟没赶上：过后补上
    run = empty.ctx.engine.active()
    assert (run["trigger"], run["actor"]) == ("schedule", "system")
    assert run["start_date"] == (today() - timedelta(days=3)).isoformat() and run["end_date"] == D1
    empty.ctx.engine.run_pending()
    scheduler.tick(now.replace(hour=10))
    assert empty.ctx.engine.active() is None and len(empty.ctx.engine.runs()[0]) == 1  # 一天只自动同步一次


def test_daily_sync_waits_while_a_manual_run_is_active(empty):
    scheduler, config, engine = empty.app.state.scheduler, empty.ctx.config, empty.ctx.engine
    config.save("sync", {"enabled": True, "hour": 0, "range_days": 1}, "ou_admin")
    manual = engine.start(D1, D1, "manual", "ou_admin")
    scheduler.tick()
    assert config.get("sync")["last_auto_date"] == ""  # 没发起成功就不记，等下一分钟再试
    engine.run_pending()
    scheduler.tick()
    assert config.get("sync")["last_auto_date"] == today().isoformat() and engine.active()["id"] != manual


# ---------- 配置校验 ----------
def test_config_validation():
    assert clean("sync", {"enabled": 1, "hour": "5", "range_days": "2"}) == {"enabled": True, "hour": 5, "range_days": 2}
    assert clean("pricing", {"point_price": "0.05"}) == {"point_price": 0.05}
    assert clean("report", {"since": ""}) == {"since": ""}
    for key, patch in (("sync", {"hour": 24}), ("sync", {"range_days": 0}), ("pricing", {"point_price": 0}), ("pricing", {"point_price": "x"}),
                       ("report", {"weekday": 8}), ("report", {"scope": "nobody"}), ("report", {"since": "2026-13-01"}), ("nope", {})):  # fmt: skip
        with pytest.raises(ConfigError):
            clean(key, patch)


def test_settings_from_env(monkeypatch, tmp_path):
    for name in ("AIUS_PUBLIC_URL", "AIUS_SECRET_KEY", "FEISHU_APP_ID", "FEISHU_APP_SECRET", "AIUS_DEV_FAKE_LOGIN"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="AIUS_PUBLIC_URL"):
        Settings.from_env()
    monkeypatch.setenv("AIUS_PUBLIC_URL", "https://ai.example.com/")
    with pytest.raises(RuntimeError, match="AIUS_SECRET_KEY"):
        Settings.from_env()
    monkeypatch.setenv("AIUS_SECRET_KEY", "s" * 40)
    assert Settings.from_env().feishu_app_id == ""  # 飞书凭证可以不放在环境变量里，在页面上填
    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "sec")
    monkeypatch.setenv("AIUS_ADMINS", "A@Example.com, ou_ABC；b@example.com")
    s = Settings.from_env()
    assert s.public_url == "https://ai.example.com" and s.callback_url == "https://ai.example.com/auth/callback" and s.cookie_secure
    assert s.admins == ("a@example.com", "ou_abc", "b@example.com") and s.feishu_app_id == "cli_x"


# ---------- Excel ----------
def test_workbook_is_valid_and_keeps_text_literal():
    data = excel.workbook([("明细/1", ["名称", "数值"], [["=1+1", 3.5], ["<b>&\x00", None], [True, 7]])])
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert z.testzip() is None
        sheet = z.read("xl/worksheets/sheet1.xml").decode()
        book = z.read("xl/workbook.xml").decode()
    assert 'name="明细 1"' in book  # 工作表名里不能有 /
    assert '<c r="A2" s="0" t="inlineStr"><is><t xml:space="preserve">=1+1</t></is></c>' in sheet  # 是文字，不是公式
    assert "<v>3.5</v>" in sheet and "&lt;b&gt;&amp;" in sheet and "\x00" not in sheet and "<v>7</v>" in sheet and ">是<" in sheet
    assert excel._column(0) == "A" and excel._column(26) == "AA"


# ---------- Markdown ----------
def test_markdown_escapes_html_and_renders_the_common_bits():
    html = md.render("## 结论\n用量 **很高**，见 `表`。\n\n- 第一 <script>alert(1)</script>\n- 第二\n\n| 部门 | 用量 |\n|---|---|\n| 研发 | 10 |\n\n1. 步骤\n\n---\n> ⚠️ 注意")
    assert "<h4>结论</h4>" in html and "<strong>很高</strong>" in html and "<code>表</code>" in html
    assert "<script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<ul><li>第一" in html and "<th>部门</th>" in html and "<td>10</td>" in html and "<ol><li>步骤</li></ol>" in html and "<hr />" in html
    assert md.render("```\n<b>x</b>\n```") == "<pre>&lt;b&gt;x&lt;/b&gt;</pre>"


# ---------- 通讯录 ----------
def test_directory_tree_helpers(env):
    d = env.ctx.directory
    assert d.dept_path("d-rd") == "技术中心 > 研发部" and d.top_level("d-rd") == "d-tech" and d.chain("d-rd") == ["d-rd", "d-tech"]
    assert d.subtree("d-tech") == {"d-tech", "d-rd", "d-ops"} and [c.id for c in d.children()] == ["d-tech", "d-sales"]
    assert d.headcount("d-tech") == 5 and d.headcount("d-rd") == 2 and d.total_headcount() == 7
    assert d.led_depts("ou_alice") == ["d-tech"] and d.user_name("ou_nobody") == "ou_nobody" and d.dept_name("") == "未归属部门"
    assert [u.name for u in d.search_users("鲍")] == ["鲍勃"] and [x.id for x in d.search_depts("研发")] == ["d-rd"]


def test_directory_keeps_the_old_snapshot_when_a_sync_fails(env):
    def boom():
        raise FeishuError("获取部门列表失败")

    env.feishu.list_departments = boom
    with pytest.raises(FeishuError):
        env.ctx.directory.sync()
    assert env.ctx.directory.total_headcount() == 7 and env.ctx.directory.dept("d-rd") is not None


def test_env_admins_match_by_email_or_open_id(tmp_path):
    from conftest import Env

    env = Env(tmp_path, synced=False, admins=("ou_dave", "carol@example.com"))
    access = env.ctx.access
    assert access.viewer("ou_dave").is_admin and access.viewer("ou_carol").is_admin and not access.viewer("ou_admin").is_admin
    assert {a["name"] for a in access.admins()} == {"戴维", "卡萝"} and all(a["fixed"] for a in access.admins())


# ---------- 洞察助手 ----------
def test_insight_sends_only_scoped_data_to_the_model(env, tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["auth"], seen["body"] = str(request.url), request.headers["authorization"], request.content.decode()
        return httpx.Response(200, json={"choices": [{"message": {"content": "**结论**：用得不多。"}}]})

    s = make_settings(tmp_path / "x", llm_base_url="https://llm.example.com/v1", llm_api_key="sk-test", llm_model="demo-model")
    insight = Insight(s, env.ctx.analytics, Vault(env.db, s), http=httpx.Client(transport=httpx.MockTransport(handler)))
    out = insight.ask("我用了多少？", Filter(D1, D1, member="ou_dave"), "戴维 个人")
    assert out == {"answer": "**结论**：用得不多。", "model": True}
    assert seen["url"] == "https://llm.example.com/v1/chat/completions" and seen["auth"] == "Bearer sk-test"
    body = seen["body"].encode().decode("unicode_escape") if "\\u" in seen["body"] else seen["body"]
    assert "demo-model" in body and "我用了多少？" in body and "智能纪要" in body
    assert "鲍勃" not in body and "卡萝" not in body and "人员排行" not in body  # 个人范围里没有别人的数据


def test_insight_falls_back_when_the_model_fails(env, tmp_path):
    s = make_settings(tmp_path / "y", llm_base_url="https://llm.example.com/v1", llm_api_key="k", llm_model="m")
    insight = Insight(s, env.ctx.analytics, Vault(env.db, s), http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom"))))
    out = insight.ask("怎么样", Filter(D1, D1), "全企业")
    assert out["model"] is False and "206 点" in out["answer"] and "暂时调用失败" in out["answer"]
    assert "还没有用量数据" in insight.ask("怎么样", Filter("2020-01-01", "2020-01-02"), "全企业")["answer"]


# ---------- ui-kit ----------
def test_ui_kit_files_match_the_manifest():
    """ui-kit 是运维控制台共用的设计系统，这里的两份文件必须和上游逐字节相同（换行按 LF 算）。
    升级方法：从 AI Ops Platform 仓库把 ui-kit.css、ui-kit.js、ui-kit.sha256 三个文件一起拷过来；本站自己的样式写在 app.css。"""
    lines = (STATIC / "ui-kit.sha256").read_text("utf-8").splitlines()
    listed = {name: digest for digest, name in (line.split() for line in lines if line and not line.startswith("#"))}
    assert set(listed) == {"ui-kit.css", "ui-kit.js"}
    for name, digest in listed.items():
        data = (STATIC / name).read_bytes().replace(b"\r\n", b"\n")
        assert hashlib.sha256(data).hexdigest() == digest, f"{name} 被改过了：页面自己的样式请写到 app.css"
    versions = {re.search(r"ui-kit (\d+\.\d+\.\d+)", (STATIC / n).read_text("utf-8")).group(1) for n in ("ui-kit.css", "ui-kit.js", "ui-kit.sha256")}
    assert len(versions) == 1

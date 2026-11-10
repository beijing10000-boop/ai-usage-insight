"""在页面上填写凭证：首次配置、第一位运营管理员、之后的修改、AI 模型配置、加密存储。"""

from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from aius_server.app import create_app
from aius_server.feishu import FeishuClient, FeishuError
from aius_server.vault import Vault
from conftest import DEPARTMENTS, PEOPLE, TestFeishu, make_settings, usage_fixture

SECRET = "app-secret-do-not-show"


class Site:
    """一个没开开发模式的站点：飞书是假的，但登录、凭证都走真实的流程。"""

    def __init__(self, tmp_path: Path, **kw):
        self.settings = make_settings(tmp_path, dev_fake_login=False, admins=(), **kw)
        self.feishu = TestFeishu(DEPARTMENTS, PEOPLE, usage_fixture())
        self.app = create_app(self.settings, feishu=self.feishu)
        self.ctx = self.app.state.ctx
        self.vault: Vault = self.ctx.vault
        self.token_file = tmp_path / "bootstrap.token"

    def browser(self) -> TestClient:
        return TestClient(self.app, follow_redirects=False)

    def token(self) -> str:
        return self.token_file.read_text("utf-8").strip()

    def setup(self, c: TestClient, **form):
        data = {"token": self.token(), "app_id": "cli_real", "app_secret": SECRET, **form}
        return c.post("/setup", data=data)

    def login(self, c: TestClient, email: str):
        r = c.get("/auth/login", params={"next": "/"})
        state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
        return c.get("/auth/callback", params={"state": state, "code": f"{email}|"})


@pytest.fixture
def site(tmp_path):
    return Site(tmp_path)


def test_unconfigured_site_only_offers_the_setup_page(site):
    c = site.browser()
    assert c.get("/healthz").json()["configured"] is False
    for path in ("/", "/dashboard", "/login", "/auth/login", "/self-usage"):
        r = c.get(path)
        assert r.status_code == 302 and r.headers["location"] == "/setup", path
    page = c.get("/setup")
    assert page.status_code == 200 and "首次配置" in page.text and "http://testserver/auth/callback" in page.text
    assert len(site.token()) >= 20 and site.token() not in page.text  # 口令在服务器上，不在页面里
    assert c.get("/openapi/v1/me").status_code == 401 and c.post("/insight/ask", json={"question": "x"}).status_code == 401


def test_setup_needs_the_bootstrap_token(site):
    c = site.browser()
    r = site.setup(c, token="wrong")
    assert r.status_code == 403 and "初始化口令不对" in r.text and 'value="cli_real"' in r.text and SECRET not in r.text
    assert not site.vault.configured and not [x for x in site.feishu.calls if x[0] == "verify"]
    for _ in range(9):
        site.setup(c, token="wrong")
    assert site.setup(c).status_code == 429  # 输错太多次：连正确的也要等一会儿
    assert not site.vault.configured


def test_setup_rejects_credentials_that_cannot_reach_feishu(site):
    c = site.browser()
    site.feishu.verify_error = FeishuError("获取 tenant_access_token 失败：code=10014 app secret invalid")
    r = site.setup(c)
    assert r.status_code == 400 and "app secret invalid" in r.text and not site.vault.configured and site.token_file.exists()
    site.feishu.verify_error = None
    assert site.setup(c, app_secret="").status_code == 400
    assert site.setup(c, aily_app_id="cli_aily").status_code == 400  # Aily 只填了一半
    assert c.post("/setup", data={"token": site.token(), "app_id": "x", "app_secret": "y"}, headers={"Origin": "http://evil.example.com"}).status_code == 403
    assert not site.vault.configured


def test_setup_saves_encrypted_credentials_and_closes_itself(site):
    c = site.browser()
    r = site.setup(c, aily_app_id="cli_aily", aily_app_secret="aily-secret")
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Fsync&done=setup"
    assert site.vault.configured and site.vault.feishu() == ("cli_real", SECRET) and site.vault.aily() == ("cli_aily", "aily-secret")
    assert site.vault.feishu_source() == "page" and not site.token_file.exists()
    stored = " ".join(row["value"] for row in site.ctx.db.q("SELECT value FROM secrets"))
    assert SECRET not in stored and "cli_real" not in stored and "aily-secret" not in stored  # 库里是密文
    assert c.get("/healthz").json()["configured"] is True
    assert c.get("/setup").headers["location"] == "/login"  # 配置完就关了
    again = c.post("/setup", data={"token": "whatever", "app_id": "cli_attacker", "app_secret": "s"})
    assert again.status_code == 303 and again.headers["location"] == "/login" and site.vault.feishu()[0] == "cli_real"
    assert "配置完成" in c.get("/login?next=%2Fsync&done=setup").text


def test_whoever_finishes_setup_becomes_the_first_admin_once(site):
    owner, other = site.browser(), site.browser()
    site.setup(owner)
    assert site.login(other, "bob@example.com").status_code == 302  # 别的浏览器先登录：不是管理员
    assert not site.ctx.access.viewer("ou_bob").is_admin
    assert site.login(owner, "dave@example.com").status_code == 302
    assert site.ctx.access.viewer("ou_dave").is_admin and owner.get("/sync").status_code == 200
    assert "首次配置" in owner.get("/roles").text
    owner.get("/auth/logout")
    site.login(owner, "carol@example.com")  # 记号只能用一次
    assert not site.ctx.access.viewer("ou_carol").is_admin


def test_admin_claim_expires(site):
    owner = site.browser()
    site.setup(owner)
    nonce = site.ctx.db.meta("setup_nonce").split("|")[0]
    site.ctx.db.set_meta("setup_nonce", f"{nonce}|{int(time.time()) - 3600}")
    site.login(owner, "dave@example.com")
    assert not site.ctx.access.viewer("ou_dave").is_admin


def configured(site: Site) -> TestClient:
    admin = site.browser()
    site.setup(admin)
    site.login(admin, "dave@example.com")
    return admin


def test_admin_can_change_the_secret_without_retyping_the_app_id(site):
    admin = configured(site)
    page = admin.get("/sync").text
    assert "cli_real" in page and "页面填写" in page and SECRET not in page and "修改凭证" in page
    r = admin.post("/sync", data={"action": "credentials", "app_secret": "rotated-secret"})
    assert r.headers["location"] == "/sync?done=credentials#feishu" and site.vault.feishu() == ("cli_real", "rotated-secret")
    assert site.feishu.calls[-1] == ("verify", "cli_real")
    assert "rotated-secret" not in admin.get("/sync").text
    assert admin.post("/sync", data={"action": "credentials"}).status_code == 400  # 什么都没填
    assert admin.post("/sync", data={"action": "credentials", "aily_app_id": "cli_aily"}).status_code == 400  # Aily 只填一半


def test_bad_new_credentials_are_not_saved(site):
    admin = configured(site)
    site.feishu.verify_error = FeishuError("获取 tenant_access_token 失败：code=10014 app secret invalid")
    r = admin.post("/sync", data={"action": "credentials", "app_secret": "typo"})
    assert r.status_code == 400 and "原来的凭证没有动" in r.text and site.vault.feishu() == ("cli_real", SECRET)


def test_switching_to_another_app_forces_relogin_and_keeps_the_admin(site):
    admin = configured(site)
    assert admin.post("/sync", data={"action": "credentials", "app_id": "cli_other"}).status_code == 400  # 换应用必须带 Secret
    r = admin.post("/sync", data={"action": "credentials", "app_id": "cli_other", "app_secret": "other-secret"})
    assert r.headers["location"] == "/login?next=%2Fsync&done=app" and site.vault.feishu() == ("cli_other", "other-secret")
    page = admin.get(r.headers["location"])
    assert "飞书应用已更换" in page.text
    assert admin.get("/sync").status_code == 401  # 旧的登录态作废，要重新登录
    site.ctx.access.revoke("ou_dave")  # 换了应用，原来的 open_id 对不上了
    site.login(admin, "dave@example.com")
    assert site.ctx.access.viewer("ou_dave").is_admin


def test_aily_credentials_can_be_added_and_cleared(site):
    admin = configured(site)
    admin.post("/sync", data={"action": "credentials", "aily_app_id": "cli_aily", "aily_app_secret": "aily-secret"})
    assert site.vault.aily() == ("cli_aily", "aily-secret") and "cli_aily" in admin.get("/sync").text
    admin.post("/sync", data={"action": "credentials", "aily_clear": "1"})
    assert site.vault.aily() == ("", "") and site.vault.feishu() == ("cli_real", SECRET)


def test_only_admins_can_change_credentials(site):
    configured(site)
    bob = site.browser()
    site.login(bob, "bob@example.com")
    assert bob.post("/sync", data={"action": "credentials", "app_secret": "x"}).status_code == 403
    assert bob.post("/config", data={"action": "llm_clear"}).status_code == 403
    assert site.vault.feishu() == ("cli_real", SECRET)


def test_llm_settings_are_verified_then_saved(site):
    admin = configured(site)
    answers = {"status": 401}

    def handler(request: httpx.Request) -> httpx.Response:
        if answers["status"] != 200:
            return httpx.Response(answers["status"], text="invalid api key")
        return httpx.Response(200, json={"choices": [{"message": {"content": "收到"}}]})

    site.ctx.insight.http = httpx.Client(transport=httpx.MockTransport(handler))
    form = {"action": "llm_save", "llm_base_url": "https://llm.example.com/v1/", "llm_api_key": "sk-secret-key", "llm_model": "demo"}
    r = admin.post("/config", data=form)
    assert r.status_code == 400 and "HTTP 401" in r.text and not site.ctx.insight.ready
    assert admin.post("/config", data={**form, "llm_base_url": "ftp://x"}).status_code == 400
    answers["status"] = 200
    assert admin.post("/config", data=form).headers["location"] == "/config?done=llm_saved#llm"
    assert site.vault.llm() == {"base_url": "https://llm.example.com/v1", "api_key": "sk-secret-key", "model": "demo", "source": "page"}
    page = admin.get("/config").text
    assert "demo" in page and "已配置" in page and "sk-secret-key" not in page
    admin.post("/config", data={**form, "llm_api_key": "", "llm_model": "demo-2"})  # Key 留空 = 不改
    assert site.vault.llm()["model"] == "demo-2" and site.vault.llm()["api_key"] == "sk-secret-key"
    admin.post("/config", data={"action": "llm_clear"})
    assert not site.ctx.insight.ready


def test_vault_prefers_page_values_and_falls_back_to_env(tmp_path):
    site = Site(tmp_path, feishu_app_id="cli_env", feishu_app_secret="env-secret", llm_base_url="https://e/v1", llm_api_key="k", llm_model="m")
    assert site.vault.configured and site.vault.feishu() == ("cli_env", "env-secret") and site.vault.feishu_source() == "env"
    assert site.vault.llm()["source"] == "env" and not site.token_file.exists()
    assert site.browser().get("/setup").headers["location"] == "/login"  # 环境变量里有凭证：不需要首次配置
    site.vault.set({"feishu_app_id": "cli_page", "feishu_app_secret": "page-secret", "unknown": "x"}, "ou_x")
    assert site.vault.feishu() == ("cli_page", "page-secret") and site.vault.feishu_source() == "page"
    assert site.ctx.db.val("SELECT COUNT(*) FROM secrets") == 2  # 不认识的名字不存


def test_saved_credentials_are_useless_with_a_different_secret_key(tmp_path):
    site = Site(tmp_path)
    site.setup(site.browser())
    other = Vault(site.ctx.db, make_settings(tmp_path, dev_fake_login=False, secret_key="y" * 40))
    assert other.feishu() == ("", "") and not other.configured


def test_feishu_client_without_credentials_says_so(tmp_path):
    site = Site(tmp_path)
    client = FeishuClient(site.settings, site.vault)
    with pytest.raises(FeishuError, match="还没有配置飞书应用凭证"):
        client.list_departments()
    with pytest.raises(FeishuError, match="还没有配置飞书应用凭证"):
        client.authorize_url("http://x/cb", "s")

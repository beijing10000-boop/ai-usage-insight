from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from aius_server.app import create_app
from aius_server.config import Settings
from aius_server.core import hash_secret
from aius_server.feishu import Dept, DirUser, FakeFeishu
from aius_server.sync import today

# 技术中心（负责人 alice）/ 研发部（负责人 bob）、技术中心 / 运维部；销售部（负责人 carol）
DEPARTMENTS = [
    Dept("d-tech", "技术中心", "0", "ou_alice"),
    Dept("d-rd", "研发部", "d-tech", "ou_bob"),
    Dept("d-ops", "运维部", "d-tech", ""),
    Dept("d-sales", "销售部", "0", "ou_carol"),
]
PEOPLE = [
    DirUser("ou_alice", "爱丽丝", "Alice", "alice@example.com", ("d-tech",)),
    DirUser("ou_bob", "鲍勃", "Bob", "bob@example.com", ("d-rd",)),
    DirUser("ou_dave", "戴维", "Dave", "dave@example.com", ("d-rd",)),
    DirUser("ou_erin", "艾琳", "Erin", "erin@example.com", ("d-ops",)),
    DirUser("ou_carol", "卡萝", "Carol", "carol@example.com", ("d-sales",)),
    DirUser("ou_frank", "弗兰克", "Frank", "frank@example.com", ("d-sales",)),
    DirUser("ou_admin", "管理员", "Admin", "admin@example.com", ("d-ops",)),
]
OPEN_IDS = {p.email: p.open_id for p in PEOPLE}

D0 = today().isoformat()
D1 = (today() - timedelta(days=1)).isoformat()
D2 = (today() - timedelta(days=2)).isoformat()


def log(open_id: str, dept: str, scenario: str, amount: float, feature: int = 1, usage: int = 1, hour: int = 10, **extra) -> dict:
    """一条人员用量日志（接口返回的样子）。time 先随便填，usage_for() 里按日期改成当天的毫秒时间戳。"""
    return {"entity_type": 2, "entity_id": open_id, "department_id": dept, "scenario_translate": scenario, "used_quota": amount,
            "feature_key": feature, "usage_type": usage, "_hour": hour, **extra}  # fmt: skip


def app_log(app_id: str, amount: float, etype: int = 5, scenario: str = "应用调用>模型调用>对话补全") -> dict:
    return {"entity_type": etype, "entity_id": app_id, "scenario_translate": scenario, "used_quota": amount, "feature_key": 1, "usage_type": 1, "_hour": 3}


# 昨天（D1）的用量：人员 136 点，应用 70 点，合计 206 点
MEMBER_D1 = [
    log("ou_bob", "d-rd", "知识问答>主对话>网页版问答", 10),
    log("ou_bob", "d-rd", "豆包工作>主对话>深度思考", 20, feature=4, hour=15),
    log("ou_dave", "d-rd", "智能纪要>会议纪要>纪要生成", 1, feature=2),
    log("ou_erin", "d-ops", "知识问答>主对话>网页版问答", 5, usage=2),
    log("ou_carol", "d-sales", "豆包工作伙伴>专属伙伴>对话", 100, feature=3, descriptions=[{"key_name": "模型", "key_type": 1, "value": "标准模型"}]),
]
APP_D1 = [app_log("cli_a", 30), app_log("spring_b__c", 40, etype=6, scenario="飞书 Aily>工作流>节点执行")]
# 前天（D2）：人员 8 点
MEMBER_D2 = [log("ou_bob", "d-rd", "知识问答>主对话>网页版问答", 8)]


def stamped(items: list[dict], day: str) -> list[dict]:
    from aius_server.feishu import day_range

    start, _ = day_range(day)
    out = []
    for it in items:
        it = dict(it)
        it["time"] = (start + it.pop("_hour") * 3600) * 1000
        out.append(it)
    return out


def detail(entity_id: str, etype: int = 2, **quotas) -> dict:
    return {"entity_type": etype, "entity_id": entity_id, **quotas}


def usage_fixture() -> dict:
    return {
        ("log", "member", D1): stamped(MEMBER_D1, D1),
        ("log", "app", D1): stamped(APP_D1, D1),
        ("log", "member", D2): stamped(MEMBER_D2, D2),
        ("detail", "member", D1): [
            detail("ou_bob", usage_value_general_ai_quota=10, usage_value_doubao_team_subscription_quota=20),
            detail("ou_carol", usage_value_feishu_aily_quota=100),
        ],
        ("detail", "app", D1): [detail("cli_a", 5, usage_value_general_ai_quota=30)],
    }


def make_settings(tmp_path: Path, **kw) -> Settings:
    base = dict(public_url="http://testserver", data_dir=tmp_path, secret_key="x" * 40, dev_fake_login=True, scheduler=False, admins=("admin@example.com",))
    base.update(kw)
    return Settings(**base)


class TestFeishu(FakeFeishu):
    """测试里登录用的 open_id 和通讯录里的一致（ou_alice 这种），不是按邮箱算出来的哈希。"""

    __test__ = False

    def open_id_for(self, key: str) -> str:  # type: ignore[override]
        return OPEN_IDS.get(key.lower(), "ou_x_" + key.split("@")[0])


class Env:
    def __init__(self, tmp_path: Path, synced: bool = True, **kw):
        self.settings = make_settings(tmp_path, **kw)
        self.feishu = TestFeishu(DEPARTMENTS, PEOPLE, usage_fixture(), app_names={"cli_a": "客服机器人"})
        self.app = create_app(self.settings, feishu=self.feishu)
        self.ctx = self.app.state.ctx
        self.db = self.ctx.db
        self.ctx.directory.sync()
        if synced:
            self.sync(D2, D1)

    def sync(self, start: str, end: str) -> dict:
        run_id = self.ctx.engine.start(start, end, "manual", "ou_admin")
        self.ctx.engine.run_pending()
        return self.ctx.engine.run(run_id)

    def browser(self, email: str | None = None) -> TestClient:
        c = TestClient(self.app, follow_redirects=False)
        if email:
            login(c, email)
        return c

    def api(self, open_id: str, **kw) -> TestClient:
        token = "aius_test_" + open_id
        if not self.db.token_owner(hash_secret(token)):
            self.db.add_token(hash_secret(token), open_id, "test")
        return TestClient(self.app, headers={"Authorization": f"Bearer {token}"}, follow_redirects=False, **kw)


def login(c: TestClient, email: str, next_path: str = "/") -> None:
    r = c.get("/auth/login", params={"next": next_path})
    assert r.status_code == 302
    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    r = c.get("/auth/callback", params={"state": state, "email": email})
    assert r.status_code == 302, r.text


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


@pytest.fixture
def empty(tmp_path):
    """通讯录同步了、用量还没同步。"""
    return Env(tmp_path, synced=False)

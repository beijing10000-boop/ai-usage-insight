"""在页面上填写的凭证：飞书应用、Aily 应用、AI 模型。

加密后存在数据库的 secrets 表里（Fernet，密钥由 AIUS_SECRET_KEY 派生），所以只拿到数据库文件或它的备份看不到 App Secret。
页面上保存过的优先；没保存过的用环境变量里的（FEISHU_APP_ID 等，可选）。保存之后页面只显示 App ID 和「已设置」，不再显示密钥本身。

AIUS_SECRET_KEY 换了以后，已保存的凭证解不开，等于没有配置，要重新走一遍首次配置。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .config import Settings
from .db import Database

log = logging.getLogger("aius")

FEISHU = ("feishu_app_id", "feishu_app_secret")
AILY = ("aily_app_id", "aily_app_secret")
LLM = ("llm_base_url", "llm_api_key", "llm_model")
NAMES = (*FEISHU, *AILY, *LLM)


class Vault:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.s = settings
        key = hashlib.sha256(b"aius-vault:" + settings.secret_key.encode()).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(key))
        self._token_file = Path(settings.data_dir) / "bootstrap.token"

    # ---------- 读写 ----------
    def get(self, name: str) -> str:
        raw = self.db.val("SELECT value FROM secrets WHERE name=?", (name,))
        if not raw:
            return ""
        try:
            return self._fernet.decrypt(raw.encode()).decode()
        except InvalidToken:
            log.error("凭证 %s 解密失败：AIUS_SECRET_KEY 和保存时的不一样，需要重新配置", name)
            return ""

    def set(self, values: dict[str, str], actor: str) -> None:
        """保存一组凭证（只收认识的名字）；值为空串的删掉。"""
        now = int(time.time())
        with self.db.tx() as c:
            for name, value in values.items():
                if name not in NAMES:
                    continue
                if value:
                    c.execute(
                        "INSERT INTO secrets(name, value, updated_by, updated_at) VALUES(?,?,?,?) "
                        "ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                        (name, self._fernet.encrypt(value.encode()).decode(), actor, now),
                    )
                else:
                    c.execute("DELETE FROM secrets WHERE name=?", (name,))

    def saved_at(self, name: str) -> int | None:
        return self.db.val("SELECT updated_at FROM secrets WHERE name=?", (name,))

    def _pair(self, names: tuple[str, str], env: tuple[str, str]) -> tuple[str, str, str]:
        """(id, secret, 来源)：页面上保存过完整的一对就用它，否则用环境变量里的。来源是 page / env / 空串。"""
        stored = (self.get(names[0]), self.get(names[1]))
        if all(stored):
            return (*stored, "page")
        if all(env):
            return (*env, "env")
        return "", "", ""

    # ---------- 各组凭证 ----------
    def feishu(self) -> tuple[str, str]:
        return self._pair(FEISHU, (self.s.feishu_app_id, self.s.feishu_app_secret))[:2]

    def feishu_source(self) -> str:
        return self._pair(FEISHU, (self.s.feishu_app_id, self.s.feishu_app_secret))[2]

    def aily(self) -> tuple[str, str]:
        return self._pair(AILY, (self.s.aily_app_id, self.s.aily_app_secret))[:2]

    def aily_source(self) -> str:
        return self._pair(AILY, (self.s.aily_app_id, self.s.aily_app_secret))[2]

    def llm(self) -> dict[str, str]:
        """AI 模型配置：页面上三项都保存过就用页面的，否则用环境变量的。"""
        stored = {k: self.get(f"llm_{k}") for k in ("base_url", "api_key", "model")}
        if all(stored.values()):
            return {**stored, "source": "page"}
        env = {"base_url": self.s.llm_base_url, "api_key": self.s.llm_api_key, "model": self.s.llm_model}
        return {**env, "source": "env"} if all(env.values()) else {"base_url": "", "api_key": "", "model": "", "source": ""}

    @property
    def configured(self) -> bool:
        """飞书应用凭证有没有。没有的话平台还不能登录，只开放首次配置页面。"""
        return self.s.dev_fake_login or bool(self.feishu()[0])

    # ---------- 首次配置的初始化口令 ----------
    def bootstrap_token(self) -> str:
        """没有配置飞书凭证时，首次配置页面要凭这个口令才能提交。口令放在数据目录的 bootstrap.token 里
        （只有能登上服务器的人看得到），配置完成后删除。"""
        try:
            token = self._token_file.read_text("utf-8").strip()
        except OSError:
            token = ""
        if len(token) < 20:
            token = secrets.token_urlsafe(18)
            self._token_file.parent.mkdir(parents=True, exist_ok=True)
            self._token_file.write_text(token + "\n", "utf-8")
            try:
                os.chmod(self._token_file, 0o600)
            except OSError:
                pass
        return token

    def check_bootstrap(self, given: str) -> bool:
        return secrets.compare_digest(self.bootstrap_token().encode(), given.strip().encode())

    def close_bootstrap(self) -> None:
        try:
            self._token_file.unlink()
        except OSError:
            pass

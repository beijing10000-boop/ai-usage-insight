"""运行配置，全部来自环境变量（见 .env.example）。

密钥类的配置（飞书应用凭证、Aily 应用凭证、模型 API Key）只放在环境变量里，不进数据库、不在页面上回显；
能在页面上改的运营配置（同步时间、单价、异常阈值、周报推送）存在数据库的 settings 表里，见 settings.py。
"""

from __future__ import annotations

import logging
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger("aius")


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _list(name: str) -> tuple[str, ...]:
    """逗号 / 分号 / 空白分隔的列表，统一小写（open_id 和邮箱都不区分大小写）。"""
    return tuple(dict.fromkeys(v.lower() for v in re.split(r"[,;，；\s]+", os.environ.get(name, "")) if v))


@dataclass(frozen=True)
class Settings:
    public_url: str
    data_dir: Path
    secret_key: str
    # 企业自建应用：飞书登录、通讯录、AI 用量接口都用它
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_open_base: str = "https://open.feishu.cn"
    feishu_accounts_base: str = "https://accounts.feishu.cn"
    allowed_tenant_key: str = ""
    # Aily 应用（可选）：用来把 Aily 应用的 ID 显示成名称
    aily_app_id: str = ""
    aily_app_secret: str = ""
    cookie_secure: bool = False
    session_days: int = 7
    # 运营管理员（open_id 或飞书邮箱，小写）。这里列出的人永远是运营管理员；其余管理员在「角色权限」页面里加
    admins: tuple[str, ...] = ()
    # AI 洞察助手用的模型（OpenAI 兼容的 /chat/completions）；不配置时助手只给出数据摘要
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout: int = 60
    directory_sync_hours: int = 6
    # 后台调度（定时同步、巡检、周报推送、通讯录同步）；测试里关掉
    scheduler: bool = True
    dev_fake_login: bool = False

    @property
    def origin(self) -> str:
        parts = urlsplit(self.public_url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def callback_url(self) -> str:
        return f"{self.public_url}/auth/callback"

    @property
    def llm_ready(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)

    @property
    def aily_ready(self) -> bool:
        return bool(self.aily_app_id and self.aily_app_secret)

    @classmethod
    def from_env(cls) -> "Settings":
        public_url = os.environ.get("AIUS_PUBLIC_URL", "").strip().rstrip("/")
        if not public_url:
            raise RuntimeError("必须设置 AIUS_PUBLIC_URL，例如 https://ai-usage.example.com")
        dev = _bool("AIUS_DEV_FAKE_LOGIN")
        secret = os.environ.get("AIUS_SECRET_KEY", "")
        if len(secret) < 32:
            if not dev:
                raise RuntimeError('AIUS_SECRET_KEY 至少 32 个字符（可用 python -c "import secrets;print(secrets.token_urlsafe(48))" 生成）')
            secret = secrets.token_urlsafe(48)
            log.warning("开发模式：未设置 AIUS_SECRET_KEY，使用临时密钥，重启后登录态失效")
        app_id = os.environ.get("FEISHU_APP_ID", "").strip()
        app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
        if not dev and not (app_id and app_secret):
            raise RuntimeError("必须设置 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
        return cls(
            public_url=public_url,
            data_dir=Path(os.environ.get("AIUS_DATA_DIR", "/data")),
            secret_key=secret,
            feishu_app_id=app_id,
            feishu_app_secret=app_secret,
            feishu_open_base=os.environ.get("FEISHU_OPEN_BASE", cls.feishu_open_base).rstrip("/"),
            feishu_accounts_base=os.environ.get("FEISHU_ACCOUNTS_BASE", cls.feishu_accounts_base).rstrip("/"),
            allowed_tenant_key=os.environ.get("FEISHU_TENANT_KEY", "").strip(),
            aily_app_id=os.environ.get("AILY_APP_ID", "").strip(),
            aily_app_secret=os.environ.get("AILY_APP_SECRET", "").strip(),
            cookie_secure=_bool("AIUS_COOKIE_SECURE", public_url.startswith("https://")),
            session_days=_int("AIUS_SESSION_DAYS", 7),
            admins=_list("AIUS_ADMINS"),
            llm_base_url=os.environ.get("AIUS_LLM_BASE_URL", "").strip().rstrip("/"),
            llm_api_key=os.environ.get("AIUS_LLM_API_KEY", "").strip(),
            llm_model=os.environ.get("AIUS_LLM_MODEL", "").strip(),
            llm_timeout=_int("AIUS_LLM_TIMEOUT", 60),
            directory_sync_hours=_int("AIUS_DIRECTORY_SYNC_HOURS", 6),
            scheduler=_bool("AIUS_SCHEDULER", True),
            dev_fake_login=dev,
        )

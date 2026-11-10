"""飞书开放平台的对接层：网页登录、通讯录、应用名称、AI 用量日志 / 统计详情、发卡片消息。

平台里所有访问飞书的请求都从这里出去。登录和取数用同一个企业自建应用，所以登录拿到的 open_id
和用量数据里的 open_id 是同一套。
"""

from __future__ import annotations

import hashlib
import json
import random
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, urlencode

import httpx

from .config import Settings
from .labels import APP_ENTITY_TYPES

CST = timezone(timedelta(hours=8))  # 用量数据按北京时间的自然日划分
ROOT = "0"

LOG_PATH = "/open-apis/admin/v1/ai_usage_log/query"
DETAIL_PATH = "/open-apis/admin/v1/ai_usage_detail/query"
USAGE_MIN_GAP = 1.1  # 两个用量接口限频 1 次/秒
RATE_LIMIT_CODES = {99991400, 11232, 1000004}
# 较早开通灰度的企业可能还不认 10（飞书项目）、13（CodeM 沙箱）这两种应用类型
LEGACY_APP_ENTITY_TYPES = (5, 6, 7, 8)


class FeishuError(Exception):
    def __init__(self, message: str, code: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass
class FeishuUser:
    open_id: str
    name: str
    email: str
    tenant_key: str


@dataclass(frozen=True)
class Dept:
    id: str  # department_id（AI 用量接口返回的就是这种 ID）
    name: str
    parent_id: str  # 顶级部门的上级是 "0"
    leader_id: str = ""  # 部门负责人的 open_id


@dataclass(frozen=True)
class DirUser:
    open_id: str
    name: str
    en_name: str = ""
    email: str = ""
    dept_ids: tuple[str, ...] = ()


def day_range(day: str) -> tuple[int, int]:
    """YYYY-MM-DD → 这一天在北京时间下的起止秒级时间戳。"""
    start = datetime.combine(date.fromisoformat(day), datetime.min.time(), CST)
    return int(start.timestamp()), int(start.timestamp()) + 86399


def subjects_for(subject: str, app_types: tuple[int, ...] = APP_ENTITY_TYPES) -> tuple[int, list[dict]]:
    """(subject_type, subjects)。人员：从根部门全选；应用：每种应用类型传 -1 全选。"""
    if subject == "member":
        return 1, [{"entity_type": 1, "entity_ids": [ROOT]}]
    return 3, [{"entity_type": t, "entity_ids": ["-1"]} for t in app_types]


def _json(resp: httpx.Response, what: str) -> dict:
    try:
        body = resp.json()
    except ValueError:
        raise FeishuError(f"{what}失败：HTTP {resp.status_code}，返回不是 JSON", retryable=resp.status_code >= 500) from None
    code = body.get("code")
    if code != 0:
        msg = body.get("error_description") or body.get("msg") or body.get("error") or ""
        limited = resp.status_code == 429 or code in RATE_LIMIT_CODES or "frequency limit" in str(msg).lower()
        raise FeishuError(f"{what}失败：code={code} {msg}".rstrip(), code=code, retryable=limited or resp.status_code >= 500)
    return body


class FeishuClient:
    def __init__(self, settings: Settings, vault, http: httpx.Client | None = None, sleep=time.sleep):
        self.s = settings
        self.vault = vault  # 应用凭证从这里取（页面上填的，或环境变量里的），每次调用现取，改了立即生效
        self.http = http or httpx.Client(timeout=30)
        self.sleep = sleep
        self._tokens: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()
        self._usage_lock = threading.Lock()
        self._usage_last = 0.0

    # ---------- 基础 ----------
    def _call(self, method: str, url: str, **kwargs) -> httpx.Response:
        """连不上、超时这类网络错误也算 FeishuError（可重试），调用方只认这一种异常。"""
        try:
            return self.http.request(method, url, **kwargs)
        except httpx.HTTPError as e:
            raise FeishuError(f"连接飞书失败：{type(e).__name__} {e}".rstrip(), retryable=True) from None

    def _retry(self, fn, retries: int = 4):
        """限频和网络抖动：退避 2、4、8、16 秒重试；权限不足、参数错误这类不重试。"""
        for attempt in range(retries + 1):
            try:
                return fn()
            except FeishuError as e:
                if not e.retryable or attempt == retries:
                    raise
                self.sleep(2 ** (attempt + 1))

    def _app(self) -> tuple[str, str]:
        app_id, app_secret = self.vault.feishu()
        if not app_id:
            raise FeishuError("还没有配置飞书应用凭证")
        return app_id, app_secret

    def _exchange(self, app_id: str, app_secret: str) -> dict:
        return _json(
            self._call(
                "POST",
                f"{self.s.feishu_open_base}/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": app_id, "app_secret": app_secret},
            ),
            "获取 tenant_access_token ",
        )

    def _token(self, app_id: str = "", app_secret: str = "") -> str:
        if not app_id:
            app_id, app_secret = self._app()
        with self._lock:
            cached = self._tokens.get(app_id)
            if cached and time.time() < cached[1]:
                return cached[0]
            body = self._exchange(app_id, app_secret)
            self._tokens[app_id] = (body["tenant_access_token"], time.time() + int(body.get("expire", 7200)) - 300)
            return body["tenant_access_token"]

    def verify(self, app_id: str, app_secret: str) -> None:
        """保存凭证之前先验一下：这对 App ID / App Secret 能不能换到 tenant_access_token。不能就抛 FeishuError。"""
        self._exchange(app_id, app_secret)

    def forget_tokens(self) -> None:
        """凭证改了之后，丢掉缓存的 tenant_access_token。"""
        with self._lock:
            self._tokens.clear()

    def _api(self, method: str, path: str, what: str, params=None, body=None, token: str | None = None, whole: bool = False) -> dict:
        """调一个飞书接口，返回响应里的 data；whole=True 时返回整个响应体（调用方自己找数据在哪一层）。"""

        def once() -> dict:
            headers = {"Authorization": f"Bearer {token or self._token()}"}
            if body is not None:
                headers["Content-Type"] = "application/json; charset=utf-8"
            resp = self._call(method, f"{self.s.feishu_open_base}{path}", params=params, json=body, headers=headers)
            payload = _json(resp, what)
            return payload if whole else (payload.get("data") or {})

        return self._retry(once)

    # ---------- 登录 ----------
    def authorize_url(self, redirect_uri: str, state: str) -> str:
        query = urlencode(
            {"client_id": self._app()[0], "response_type": "code", "redirect_uri": redirect_uri, "state": state},
            quote_via=quote,
        )
        return f"{self.s.feishu_accounts_base}/open-apis/authen/v1/authorize?{query}"

    def login(self, code: str, redirect_uri: str) -> FeishuUser:
        app_id, app_secret = self._app()
        body = _json(
            self._call(
                "POST",
                f"{self.s.feishu_open_base}/open-apis/authen/v2/oauth/token",
                json={
                    "grant_type": "authorization_code",
                    "client_id": app_id,
                    "client_secret": app_secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
            ),
            "换取 user_access_token ",
        )
        token = body.get("access_token") or (body.get("data") or {}).get("access_token")
        if not token:
            raise FeishuError("换取 user_access_token 失败：响应中没有 access_token")
        info = _json(
            self._call("GET", f"{self.s.feishu_open_base}/open-apis/authen/v1/user_info", headers={"Authorization": f"Bearer {token}"}),
            "获取用户信息",
        )["data"]
        return FeishuUser(
            open_id=info["open_id"],
            name=info.get("name") or info.get("en_name") or "",
            email=info.get("enterprise_email") or info.get("email") or "",
            tenant_key=info.get("tenant_key") or "",
        )

    # ---------- 连接测试 ----------
    def check(self) -> dict:
        """测试连接：能换到 tenant_access_token 就算已连接；企业名称拿不到不算失败。
        AI 用量权限是否开通，要到第一次真实同步才知道。"""
        self.forget_tokens()  # 不用缓存，真的去换一次
        self._token()
        out = {"connected": True, "tenant": "", "aily": None}
        try:
            out["tenant"] = (self._api("GET", "/open-apis/tenant/v2/tenant/query", "获取企业信息").get("tenant") or {}).get("name") or ""
        except FeishuError:
            pass
        aily_id, aily_secret = self.vault.aily()
        if aily_id:
            try:
                self._token(aily_id, aily_secret)
                out["aily"] = True
            except FeishuError as e:
                out["aily"] = str(e)
        return out

    # ---------- 通讯录 ----------
    def list_departments(self) -> list[Dept]:
        """通讯录权限范围内的全部部门（递归子部门），ID 用 department_id。"""
        out: list[Dept] = []
        params = {"department_id_type": "department_id", "user_id_type": "open_id", "fetch_child": "true", "page_size": 50}
        while True:
            data = self._api("GET", f"/open-apis/contact/v3/departments/{ROOT}/children", "获取部门列表", params=params)
            for it in data.get("items") or []:
                if (it.get("status") or {}).get("is_deleted") or not it.get("department_id"):
                    continue
                # 上级部门字段要单独授权；缺了就没法汇总到上级部门，宁可报错也不能静默当成顶级部门
                if "parent_department_id" not in it:
                    raise FeishuError("获取部门列表失败：返回里没有 parent_department_id，请确认应用已开通 contact:department.organize:readonly")
                out.append(Dept(it["department_id"], it.get("name") or "", it["parent_department_id"] or ROOT, _leader(it)))
            if not data.get("has_more") or not data.get("page_token"):
                return out
            params["page_token"] = data["page_token"]

    def list_department_users(self, dept_id: str) -> list[DirUser]:
        """部门的直属成员（不含子部门），跳过已离职 / 未加入的人。dept_id="0" 是根部门。
        成员所属部门就记这次查询的部门（不依赖 department_ids 字段，那个字段要额外的权限）。"""
        out: list[DirUser] = []
        params = {"department_id": dept_id, "department_id_type": "department_id", "user_id_type": "open_id", "page_size": 50}
        while True:
            data = self._api("GET", "/open-apis/contact/v3/users/find_by_department", "获取部门成员", params=params)
            for it in data.get("items") or []:
                if "name" not in it:
                    raise FeishuError("获取部门成员失败：返回里没有 name，请确认应用已开通 contact:user.base:readonly")
                st = it.get("status") or {}
                if st.get("is_resigned") or st.get("is_unjoin") or not it.get("open_id"):
                    continue
                out.append(
                    DirUser(
                        open_id=it["open_id"],
                        name=it.get("name") or "",
                        en_name=it.get("en_name") or "",
                        email=(it.get("enterprise_email") or it.get("email") or "").lower(),
                        dept_ids=() if dept_id == ROOT else (dept_id,),
                    )
                )
            if not data.get("has_more") or not data.get("page_token"):
                return out
            params["page_token"] = data["page_token"]

    def user_names(self, open_ids: list[str]) -> dict[str, str]:
        """open_id → 姓名（通讯录快照里没有的人，比如已离职的）。查不到的不出现在结果里。"""
        out: dict[str, str] = {}
        for i in range(0, len(open_ids), 50):
            params = [("user_id_type", "open_id"), *[("user_ids", v) for v in open_ids[i : i + 50]]]
            data = self._api("GET", "/open-apis/contact/v3/users/batch", "批量获取用户信息", params=params)
            for it in data.get("items") or []:
                if it.get("open_id") and it.get("name"):
                    out[it["open_id"]] = it["name"]
        return out

    def dept_infos(self, dept_ids: list[str]) -> dict[str, Dept]:
        """department_id → 部门信息（通讯录快照里没有的部门）。"""
        out: dict[str, Dept] = {}
        for i in range(0, len(dept_ids), 50):
            params = [
                ("department_id_type", "department_id"),
                ("user_id_type", "open_id"),
                *[("department_ids", v) for v in dept_ids[i : i + 50]],
            ]
            data = self._api("GET", "/open-apis/contact/v3/departments/batch", "批量获取部门信息", params=params)
            for it in data.get("items") or []:
                if it.get("department_id"):
                    out[it["department_id"]] = Dept(it["department_id"], it.get("name") or "", it.get("parent_department_id") or ROOT, _leader(it))
        return out

    # ---------- 应用名称 ----------
    def app_names(self, app_ids: list[str]) -> dict[str, str]:
        """应用 ID → 名称：先翻企业的应用列表，列表里没有的再逐个查。
        Aily / 智能体应用（spring_、agent_ 开头）开放平台查不到，要靠 aily_app_names 或手工映射。"""
        out: dict[str, str] = {}
        params = {"page_size": 50, "lang": "zh_cn"}
        while True:
            data = self._api("GET", "/open-apis/application/v6/applications", "获取应用列表", params=params)
            for it in data.get("items") or []:
                if it.get("app_id") and it.get("app_name"):
                    out[it["app_id"]] = it["app_name"]
            if not data.get("has_more") or not data.get("page_token"):
                break
            params["page_token"] = data["page_token"]
        for app_id in app_ids:
            if app_id in out or app_id.startswith(("spring_", "agent_")):
                continue
            try:
                app = self._api("GET", f"/open-apis/application/v6/applications/{quote(app_id, safe='')}", "获取应用信息", params={"lang": "zh_cn"}).get("app") or {}
            except FeishuError:
                continue
            if app.get("app_name"):
                out[app_id] = app["app_name"]
        return {k: v for k, v in out.items() if k in set(app_ids)}

    def aily_app_names(self) -> dict[str, str]:
        """Aily 应用和智能体的 ID → 名称（要配置 Aily 应用凭证，权限 aily:stats:read）。"""
        aily_id, aily_secret = self.vault.aily()
        if not aily_id:
            return {}
        token = self._token(aily_id, aily_secret)
        now = int(time.time())
        out: dict[str, str] = {}
        for kind in ("app", "agent"):
            params = {"start_at": str(now - 90 * 86400), "end_at": str(now), "filter_type": kind, "page_size": 100}
            while True:
                data = self._api("GET", "/open-apis/aily/v1/app_stats", "获取 Aily 应用统计", params=params, token=token)
                for it in data.get("items") or []:
                    if it.get("id") and it.get("name"):
                        out[it["id"]] = it["name"]
                if not data.get("has_more") or not data.get("page_token"):
                    break
                params["page_token"] = data["page_token"]
                self.sleep(1.1)
            self.sleep(1.1)
        return out

    # ---------- AI 用量 ----------
    def usage_page(
        self, kind: str, subject: str, day: str, page_token: str = "", app_types: tuple[int, ...] = APP_ENTITY_TYPES
    ) -> tuple[list[dict], str]:
        """取一页用量数据，返回 (这一页的记录, 下一页的 page_token；没有下一页是空串)。

        kind: log（用量日志，准实时）| detail（统计详情，T+1）；subject: member | app；day: YYYY-MM-DD。
        """
        start, end = day_range(day)
        subject_type, subjects = subjects_for(subject, app_types)
        # entity_ids 传 0（根部门）时，接口要求 department_id_type=department_id
        params = {"user_id_type": "open_id", "department_id_type": "department_id", "page_size": 100}
        if page_token:
            params["page_token"] = page_token
        body = {"date_start": start, "date_end": end, "subject_type": subject_type, "subjects": subjects}
        if kind == "log":
            body["locale"] = "zh-CN"
        with self._usage_lock:
            wait = self._usage_last + USAGE_MIN_GAP - time.monotonic()
            if wait > 0:
                self.sleep(wait)
            what = "查询 AI 用量" + ("日志" if kind == "log" else "统计详情")
            try:
                data = self._api("POST", LOG_PATH if kind == "log" else DETAIL_PATH, what, params=params, body=body, whole=True)
            finally:
                self._usage_last = time.monotonic()
        # 用量数据通常在 data 里；也认直接放在最外层的写法。这一天没有数据时飞书可能不带 items（当成空列表）；
        # 但连 data 这一层都没有就是返回格式变了，宁可报错也不要当成「没有数据」把这一天清空
        if isinstance(data.get("items"), list):
            inner = data
        elif isinstance(data.get("data"), dict):
            inner = data["data"]
        else:
            raise FeishuError(f"{what}失败：返回里既没有 data 也没有 items，接口的返回格式可能变了")
        items = inner.get("items") or []
        if not isinstance(items, list):
            raise FeishuError(f"{what}失败：返回的 items 不是列表")
        token = inner.get("page_token") or ""
        if inner.get("has_more") and not token:
            raise FeishuError(f"{what}失败：飞书说还有下一页，但没有给分页标记")
        return items, token if inner.get("has_more") else ""

    # ---------- 消息 ----------
    def send_card(self, open_id: str, card: dict) -> None:
        """以应用机器人的身份给一个人发卡片消息（应用要开机器人能力和 im:message:send_as_bot）。"""
        self._api(
            "POST",
            "/open-apis/im/v1/messages",
            "发送飞书消息",
            params={"receive_id_type": "open_id"},
            body={"receive_id": open_id, "msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)},
        )


def _leader(item: dict) -> str:
    """部门负责人的 open_id：先看 leader_user_id，没有再从 leaders 里找主负责人。"""
    if item.get("leader_user_id"):
        return item["leader_user_id"]
    leaders = item.get("leaders") or []
    main = next((x for x in leaders if (x.get("leaderType") or x.get("leader_type")) == 1), leaders[0] if leaders else {})
    return main.get("leaderID") or main.get("leader_id") or main.get("leaderId") or ""


# ---------------------------------------------------------------------------
# 开发 / 测试用的假飞书
# ---------------------------------------------------------------------------

DEMO_DEPARTMENTS = [
    Dept("d-tech", "技术中心", ROOT, ""),
    Dept("d-rd", "研发部", "d-tech"),
    Dept("d-rd-be", "后端组", "d-rd"),
    Dept("d-rd-fe", "前端组", "d-rd"),
    Dept("d-ops", "运维部", "d-tech"),
    Dept("d-data", "数据部", "d-tech"),
    Dept("d-biz", "业务中心", ROOT),
    Dept("d-sales", "销售部", "d-biz"),
    Dept("d-cs", "客户服务部", "d-biz"),
    Dept("d-mkt", "市场部", "d-biz"),
    Dept("d-func", "职能中心", ROOT),
    Dept("d-hr", "人力资源部", "d-func"),
    Dept("d-fin", "财务部", "d-func"),
]
# (姓名, 邮箱前缀, 部门)；每个部门的第一个人是负责人
DEMO_PEOPLE = [
    ("周明远", "admin", "d-tech"),
    ("陈思雨", "chensiyu", "d-rd"), ("李浩然", "lihaoran", "d-rd-be"), ("王梓涵", "wangzihan", "d-rd-be"),
    ("赵一鸣", "zhaoyiming", "d-rd-be"), ("刘欣妍", "liuxinyan", "d-rd-be"), ("孙宇航", "sunyuhang", "d-rd-fe"),
    ("吴佳怡", "wujiayi", "d-rd-fe"), ("郑子墨", "zhengzimo", "d-rd-fe"),
    ("钱文博", "qianwenbo", "d-ops"), ("冯雨桐", "fengyutong", "d-ops"), ("蒋俊杰", "jiangjunjie", "d-ops"),
    ("沈诗涵", "shenshihan", "d-data"), ("韩天宇", "hantianyu", "d-data"), ("杨可馨", "yangkexin", "d-data"),
    ("朱志强", "zhuzhiqiang", "d-biz"),
    ("秦梦瑶", "qinmengyao", "d-sales"), ("许嘉豪", "xujiahao", "d-sales"), ("何雅婷", "heyating", "d-sales"),
    ("吕子轩", "lvzixuan", "d-sales"), ("施雨欣", "shiyuxin", "d-sales"), ("张博文", "zhangbowen", "d-sales"),
    ("孔思琪", "kongsiqi", "d-cs"), ("曹宇轩", "caoyuxuan", "d-cs"), ("严语嫣", "yanyuyan", "d-cs"), ("华晨曦", "huachenxi", "d-cs"),
    ("金梓萱", "jinzixuan", "d-mkt"), ("魏浩宇", "weihaoyu", "d-mkt"), ("陶欣怡", "taoxinyi", "d-mkt"),
    ("姜海涛", "jianghaitao", "d-func"),
    ("戚雨泽", "qiyuze", "d-hr"), ("谢婉婷", "xiewanting", "d-hr"), ("邹子健", "zouzijian", "d-hr"),
    ("喻诗雨", "yushiyu", "d-fin"), ("柏俊熙", "baijunxi", "d-fin"), ("水若曦", "shuiruoxi", "d-fin"),
]
DEMO_APPS = [
    (5, "cli_demo_helpdesk", "IT 服务台机器人"),
    (5, "cli_demo_report", "经营日报助手"),
    (6, "spring_demo_contract__c", "合同审核工作流"),
    (8, "agent_demo_onboarding", "新人入职智能体"),
    (7, "apaas_demo_crm", "客户跟进 aPaaS"),
]
# (场景路径, 额度类型, 权重, 单次点数范围)
DEMO_SCENARIOS = [
    ("知识问答>主对话>网页版问答", 1, 20, (1, 6)),
    ("知识问答>主对话>移动端问答", 1, 8, (1, 4)),
    ("知识问答>深度研究>研究报告", 1, 3, (20, 60)),
    ("云文档>AI 写作>续写与润色", 1, 10, (1, 5)),
    ("云文档>AI 写作>文档总结", 1, 6, (2, 8)),
    ("多维表格>AI 字段捷径>信息提取", 1, 7, (1, 10)),
    ("多维表格>AI 字段捷径>智能分类", 1, 4, (1, 8)),
    ("妙搭>应用开发>对话生成应用", 1, 3, (30, 120)),
    ("智能纪要>会议纪要>纪要生成", 2, 9, (1, 1)),
    ("智能纪要>会议纪要>智能章节", 2, 4, (1, 1)),
    ("飞书 Aily>智能伙伴>日常对话", 3, 6, (1, 5)),
    ("豆包工作>主对话>深度思考", 4, 12, (2, 12)),
    ("豆包工作>主对话>联网搜索", 4, 9, (1, 6)),
    ("豆包工作>办公任务>文档生成", 5, 5, (5, 25)),
    ("豆包工作>办公任务>数据分析", 5, 3, (8, 40)),
    ("豆包工作伙伴>专属伙伴>对话", 3, 2, (1, 5)),
]
APP_SCENARIOS = [
    ("应用调用>模型调用>对话补全", 1, (2, 20)),
    ("应用调用>模型调用>文本向量化", 1, (1, 5)),
    ("飞书 Aily>工作流>节点执行", 3, (1, 15)),
]


class FakeFeishu:
    """开发 / 测试用：不连飞书。登录页让你随便填一个邮箱；通讯录和用量数据可以注入，也可以用内置的演示数据。

    只在 AIUS_DEV_FAKE_LOGIN=1 时启用，生产环境绝不能打开。
    """

    def __init__(
        self,
        departments: list[Dept] | None = None,
        people: list[DirUser] | None = None,
        usage: dict[tuple[str, str, str], list[dict]] | None = None,
        app_names: dict[str, str] | None = None,
        demo: bool = False,
    ):
        self.demo = demo
        if demo and departments is None:
            first: dict[str, str] = {}
            people = []
            for name, prefix, dept in DEMO_PEOPLE:
                oid = self.open_id_for(f"{prefix}@example.com")
                first.setdefault(dept, oid)
                people.append(DirUser(oid, name, "", f"{prefix}@example.com", (dept,)))
            departments = [Dept(d.id, d.name, d.parent_id, first.get(d.id, "")) for d in DEMO_DEPARTMENTS]
        self.departments = departments or []
        self.people = people or []
        # usage: (kind, subject, day) -> 记录列表；demo 模式下没注入的日子按规律生成
        self.usage = usage or {}
        self.apps = app_names if app_names is not None else ({a[1]: a[2] for a in DEMO_APPS if a[0] == 5} if demo else {})
        self.aily_apps: dict[str, str] = {a[1]: a[2] for a in DEMO_APPS if a[0] in (6, 8)} if demo else {}
        self.page_size = 100
        self.fail: dict[tuple[str, str], FeishuError] = {}  # (kind, subject) -> 调用时抛出的错误（测试用）
        self.calls: list[tuple] = []
        self.sent: list[tuple[str, dict]] = []
        self.send_error: FeishuError | None = None
        self.verify_error: FeishuError | None = None  # verify() 时抛出的错误（测试用）

    @staticmethod
    def open_id_for(key: str) -> str:
        return "ou_dev_" + hashlib.sha1(key.lower().encode()).hexdigest()[:16]

    # ---- 登录 ----
    def authorize_url(self, redirect_uri: str, state: str) -> str:
        return "/auth/dev-login?" + urlencode({"state": state})

    def login(self, code: str, redirect_uri: str) -> FeishuUser:
        email, _, name = code.partition("|")
        email = email.strip().lower()
        if "@" not in email:
            raise FeishuError("开发登录：请填写邮箱")
        oid = self.open_id_for(email)
        known = next((p for p in self.people if p.open_id == oid), None)
        return FeishuUser(open_id=oid, name=name.strip() or (known.name if known else email.split("@")[0]), email=email, tenant_key="dev")

    def check(self) -> dict:
        return {"connected": True, "tenant": "演示企业", "aily": None}

    def verify(self, app_id: str, app_secret: str) -> None:
        self.calls.append(("verify", app_id))
        if self.verify_error:
            raise self.verify_error

    def forget_tokens(self) -> None:
        pass

    # ---- 通讯录 ----
    def list_departments(self) -> list[Dept]:
        self.calls.append(("list_departments",))
        return list(self.departments)

    def list_department_users(self, dept_id: str) -> list[DirUser]:
        return [DirUser(p.open_id, p.name, p.en_name, p.email, () if dept_id == ROOT else (dept_id,)) for p in self.people if dept_id in p.dept_ids or (dept_id == ROOT and not p.dept_ids)]

    def user_names(self, open_ids: list[str]) -> dict[str, str]:
        self.calls.append(("user_names", tuple(open_ids)))
        known = {p.open_id: p.name for p in self.people}
        return {i: known[i] for i in open_ids if i in known}

    def dept_infos(self, dept_ids: list[str]) -> dict[str, Dept]:
        known = {d.id: d for d in self.departments}
        return {i: known[i] for i in dept_ids if i in known}

    def app_names(self, app_ids: list[str]) -> dict[str, str]:
        self.calls.append(("app_names", tuple(app_ids)))
        return {i: self.apps[i] for i in app_ids if i in self.apps}

    def aily_app_names(self) -> dict[str, str]:
        return dict(self.aily_apps)

    # ---- AI 用量 ----
    def usage_page(self, kind: str, subject: str, day: str, page_token: str = "", app_types: tuple[int, ...] = APP_ENTITY_TYPES) -> tuple[list[dict], str]:
        self.calls.append(("usage_page", kind, subject, day, page_token, tuple(app_types)))
        if (kind, subject) in self.fail:
            raise self.fail[(kind, subject)]
        items = self.usage.get((kind, subject, day))
        if items is None:
            items = self._demo_items(kind, subject, day) if self.demo else []
        start = int(page_token or 0)
        page = items[start : start + self.page_size]
        more = start + self.page_size < len(items)
        return page, str(start + self.page_size) if more else ""

    def _demo_items(self, kind: str, subject: str, day: str) -> list[dict]:
        logs = self._demo_logs(subject, day)
        if kind == "log":
            return logs
        if date.fromisoformat(day) >= date.today():  # 统计详情是 T+1
            return []
        sums: dict[tuple[int, str], dict[int, float]] = {}
        for it in logs:
            sums.setdefault((it["entity_type"], it["entity_id"]), {}).setdefault(it["feature_key"], 0)
            sums[(it["entity_type"], it["entity_id"])][it["feature_key"]] += it["used_quota"]
        fields = {1: "usage_value_general_ai_quota", 2: "usage_value_ai_notes_quota", 3: "usage_value_feishu_aily_quota",
                  4: "usage_value_doubao_team_subscription_quota", 5: "usage_value_doubao_team_gift_quota", 6: "usage_value_doubao_team_upgrade_quota"}
        return [{"entity_type": t, "entity_id": i, **{fields[k]: round(v, 2) for k, v in by.items()}} for (t, i), by in sums.items()]

    def _demo_logs(self, subject: str, day: str) -> list[dict]:
        """按日期和人算出来的固定演示数据：工作日多、周末少，整体缓慢上涨，个别部门偶尔有一天特别高。"""
        d = date.fromisoformat(day)
        if d > date.today():
            return []
        rnd = random.Random(f"{subject}:{day}")
        start, _ = day_range(day)
        weekday = 1.0 if d.weekday() < 5 else 0.18
        growth = 1.0 + max(-0.5, (d - date.today()).days / 120)  # 越近用得越多
        out: list[dict] = []
        if subject == "member":
            weights = [s[2] for s in DEMO_SCENARIOS]
            for person in self.people:
                seed = int(hashlib.sha1(person.open_id.encode()).hexdigest()[:6], 16)
                heavy = 0.4 + (seed % 100) / 25  # 每个人的活跃程度不一样
                dept = person.dept_ids[0] if person.dept_ids else ""
                spike = 4.0 if dept == "d-data" and d.toordinal() % 17 == 3 else 1.0
                if rnd.random() > min(0.95, 0.35 * heavy * weekday + 0.15 * weekday):
                    continue
                for _ in range(max(1, int(rnd.gauss(4, 2) * heavy * growth * spike))):
                    scenario, fkey, _weight, (lo, hi) = rnd.choices(DEMO_SCENARIOS, weights=weights)[0]
                    out.append(
                        {
                            "entity_type": 2,
                            "entity_id": person.open_id,
                            "department_id": dept,
                            "time": (start + rnd.randint(8 * 3600, 21 * 3600)) * 1000,
                            "scenario_translate": scenario,
                            "feature_key": fkey,
                            "usage_type": 2 if rnd.random() < 0.12 else 1,
                            "used_quota": float(rnd.randint(lo, hi)),
                            "descriptions": [{"key_name": "模型", "key_type": 1, "value": rnd.choice(["标准模型", "深度思考模型"])}],
                        }
                    )
        else:
            for etype, app_id, _name in DEMO_APPS:
                for _ in range(int(rnd.randint(3, 14) * weekday * growth)):
                    scenario, fkey, (lo, hi) = rnd.choice(APP_SCENARIOS)
                    out.append(
                        {
                            "entity_type": etype,
                            "entity_id": app_id,
                            "time": (start + rnd.randint(0, 86399)) * 1000,
                            "scenario_translate": scenario,
                            "feature_key": fkey,
                            "usage_type": 1,
                            "used_quota": float(rnd.randint(lo, hi)),
                        }
                    )
        out.sort(key=lambda it: it["time"])
        return out

    # ---- 消息 ----
    def send_card(self, open_id: str, card: dict) -> None:
        if self.send_error:
            raise self.send_error
        self.sent.append((open_id, card))

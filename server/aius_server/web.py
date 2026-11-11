"""页面渲染：Jinja2 模板（templates/）+ 静态资源（static/）。

外观沿用运维控制台共用的设计系统 ui-kit：static/ui-kit.css、ui-kit.js 与 AI Ops Platform 仓库里的逐字节相同
（tests/test_ui_kit.py 按 ui-kit.sha256 核对），不要在这里改；本站自己的样式和脚本写在 app.css、app.js。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup

from . import __version__
from .access import Viewer
from .feishu import CST

BASE = Path(__file__).parent
STATIC_DIR = BASE / "static"
SITE = "AI 运营洞察平台"

# 页面没有内联脚本和内联样式，策略收紧到只认本站资源
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
    "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)
HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
}

# ui-kit 图标集里没有的几个图标（同样的 24 格、1.7 线宽）；ui-kit 自带的用 data-icon 属性
ICONS = {
    "logo": '<path d="M4.5 19.5h15"/><path d="M7 16v-4m5 4V8.5m5 7.5V5"/><path d="m5.5 9.5 4-3 3.5 2 5-4.5"/>',
    "chart": '<path d="M4.5 4.5v15h15"/><path d="M8.5 15.5v-4m4 4v-7m4 7v-9.5"/>',
    "dept": '<rect x="8.5" y="3.5" width="7" height="5.5" rx="1.5"/><rect x="3" y="15.5" width="7" height="5" rx="1.5"/><rect x="14" y="15.5" width="7" height="5" rx="1.5"/><path d="M12 9v3.5M6.5 15.5v-3h11v3"/>',
    "bean": '<path d="M12 3.5c4.5 0 7.5 3.4 7.5 8s-3 9-7.5 9-7.5-4.4-7.5-9 3-8 7.5-8Z"/><path d="M9 10.5h.01M15 10.5h.01M9.5 14.5c1.4 1.2 3.6 1.2 5 0"/>',
    "table": '<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="M3.5 9.5h17M3.5 14.5h17M9.5 9.5v10"/>',
    "person": '<circle cx="12" cy="8" r="3.8"/><path d="M4.5 20a7.5 7.5 0 0 1 15 0"/>',
    "people": '<circle cx="9" cy="8.5" r="3.3"/><path d="M3 19.5a6 6 0 0 1 12 0"/><path d="M15.5 5.5a3.3 3.3 0 0 1 0 6.3M17.5 14.2a6 6 0 0 1 3.5 5.3"/>',
    "quota": '<path d="M4 15a8 8 0 1 1 16 0"/><path d="m12 15 3.5-5"/><path d="M4 19h16"/>',
    "sync": '<path d="M20 11.5A8 8 0 0 0 6.3 7"/><path d="M5.5 3.5V7.5h4"/><path d="M4 12.5A8 8 0 0 0 17.7 17"/><path d="M18.5 20.5v-4h-4"/>',
    "list": '<path d="M8.5 6.5h11M8.5 12h11M8.5 17.5h11M4.5 6.5h.01M4.5 12h.01M4.5 17.5h.01"/>',
    "bell": '<path d="M6 16V11a6 6 0 1 1 12 0v5l1.5 2h-15L6 16Z"/><path d="M10 20.5a2 2 0 0 0 4 0"/>',
    "plug": '<path d="M9 7H7a5 5 0 0 0 0 10h2m6-10h2a5 5 0 0 1 0 10h-2M8 12h8"/>',
    "shield": '<path d="m12 3 7.5 3.5v5c0 4.7-3.3 8-7.5 9.5-4.2-1.5-7.5-4.8-7.5-9.5v-5L12 3Z"/><path d="m8.5 12 2.5 2.5 4.5-5"/>',
    "download": '<path d="M12 4.5v11m-4-4 4 4 4-4M4.5 15.5v4h15v-4"/>',
    "copy": '<rect x="8.5" y="8.5" width="11" height="11" rx="2.5"/><path d="M15.5 8.5V6A1.5 1.5 0 0 0 14 4.5H6A1.5 1.5 0 0 0 4.5 6v8A1.5 1.5 0 0 0 6 15.5h2.5"/>',
    "send": '<path d="m20.5 3.5-17 7 6.5 2.5 2.5 6.5 8-16Z"/><path d="m10 13 4-4"/>',
}


def icon(name: str) -> Markup:
    return Markup(
        '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" '
        f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{ICONS[name]}</svg>'
    )


# ---------- 数字和时间的显示 ----------
def fmt_num(value, digits: int = 2) -> str:
    """1234.5 → 1,234.5；整数不带小数点；None → —。"""
    if value is None:
        return "—"
    number = round(float(value), digits)
    if number == int(number):
        return f"{int(number):,}"
    return f"{number:,.{digits}f}".rstrip("0").rstrip(".")


def fmt_compact(value) -> str:
    """指标卡上的大数字：一万以内写全，往上写成 12.9 万 / 1.2 亿。"""
    if value is None:
        return "—"
    number = float(value)
    if abs(number) >= 1e8:
        return f"{number / 1e8:.2f}".rstrip("0").rstrip(".") + " 亿"
    if abs(number) >= 1e4:
        return f"{number / 1e4:.2f}".rstrip("0").rstrip(".") + " 万"
    return fmt_num(number, 1)


def fmt_money(value) -> str:
    return "—" if value is None else f"¥{float(value):,.2f}"


def fmt_delta(value) -> str:
    """环比：+12.3% / -5% / —（上一个周期没有数据时）。"""
    if value is None:
        return "—"
    return f"{'+' if value > 0 else ''}{fmt_num(value, 1)}%"


def fmt_ts(ts: int | None, never: str = "—") -> str:
    return never if not ts else datetime.fromtimestamp(ts, CST).strftime("%Y-%m-%d %H:%M")


def fmt_clock(ts: int | None) -> str:
    return "" if not ts else datetime.fromtimestamp(ts, CST).strftime("%H:%M")


def fmt_ago(ts: int | None, never: str = "—") -> str:
    """刚刚 / 5 分钟前 / 3 小时前 / 6 天前；超过 30 天写日期。"""
    if not ts:
        return never
    gap = max(0, int(time.time()) - ts)
    if gap < 60:
        return "刚刚"
    if gap < 3600:
        return f"{gap // 60} 分钟前"
    if gap < 86400:
        return f"{gap // 3600} 小时前"
    if gap < 30 * 86400:
        return f"{gap // 86400} 天前"
    return datetime.fromtimestamp(ts, CST).strftime("%Y-%m-%d")


def fmt_duration(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分 {seconds % 60} 秒"
    return f"{seconds // 3600} 小时 {seconds % 3600 // 60} 分"


def url_with(path: str, **params) -> str:
    """拼带查询参数的站内地址；值为 None 或空串的参数不带。"""
    query = urlencode({k: v for k, v in params.items() if v not in (None, "")})
    return f"{path}?{query}" if query else path


env = Environment(loader=FileSystemLoader(BASE / "templates"), autoescape=True, trim_blocks=True, lstrip_blocks=True)
env.filters.update(num=fmt_num, compact=fmt_compact, money=fmt_money, delta=fmt_delta, ts=fmt_ts, clock=fmt_clock, ago=fmt_ago, duration=fmt_duration)
env.globals.update(icon=icon, version=__version__, site=SITE, url_with=url_with)


@dataclass
class Shell:
    """侧栏和顶栏要用的：当前用户、当前在哪个导航项、数据截至哪天。"""

    viewer: Viewer
    active: str = ""
    latest: str | None = None
    syncing: bool = False
    nav: list = field(default_factory=list)
    point_price: float = 0.0  # 1 点折算多少元：页面上的「消耗金额」都按它算

    @property
    def initial(self) -> str:
        return (self.viewer.label.strip()[:1] or "?").upper()


def navigation(viewer: Viewer) -> list[tuple[str, list[tuple[str, str, str, str]]]]:
    """按角色给出导航：[(分组名, [(键, 地址, 名称, 图标)])]。图标带 kit: 前缀的是 ui-kit 自带的。"""
    insight = [("portal", "/", "AI 门户", "kit:ai")]
    if viewer.is_admin:
        insight.append(("dashboard", "/dashboard", "运营驾驶舱", "chart"))
    if viewer.is_admin or viewer.led:
        insight.append(("dept_ops", "/dept-ops", "部门运营", "dept"))
    if viewer.is_admin:
        insight += [("doubao", "/doubao-ops", "豆包运营", "bean"), ("data", "/data", "用量明细", "table"), ("members", "/members", "成员用量", "people")]
    insight.append(("self", "/self-usage", "我的用量", "person"))
    groups = [("洞察", insight)]
    if viewer.is_admin:
        groups.append(
            (
                "运营",
                [
                    ("quota", "/quota", "额度分配", "quota"),
                    ("sync", "/sync", "同步管理", "sync"),
                    ("logs", "/logs", "执行日志", "list"),
                    ("reports", "/reports", "报表发送", "send"),
                    ("config", "/config", "映射与配置", "bell"),
                ],
            )
        )
    groups.append(("接入", [("connect", "/connect", "接入管理", "plug")]))
    if viewer.is_admin:
        groups.append(("治理", [("roles", "/roles", "角色权限", "shield"), ("audit", "/audit", "操作审计", "kit:audit")]))
    return groups


def render(template: str, status: int = 200, **ctx) -> HTMLResponse:
    return HTMLResponse(env.get_template(template).render(**ctx), status_code=status, headers=HEADERS)


def message(title: str, text: str, status: int = 200, user_name: str | None = None) -> HTMLResponse:
    """独立的提示页（请求被拒绝、页面不存在……）。"""
    return render("message.html", status, title=title, text=text, user_name=user_name)


def safe_next(target: str | None) -> str:
    """登录后、操作后要跳回的地址：只允许站内路径。"""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    if any(ord(ch) < 0x20 for ch in target):
        return "/"
    return target


def login_page(next_path: str | None = "/", status: int = 401, error: str | None = None, notice: str | None = None) -> HTMLResponse:
    """登录页，和运维控制台一样：先看到是哪个站点，点「使用飞书账号登录」才去飞书授权。
    没登录时就在被访问的地址上显示（401，不跳转），登录后回到 next。"""
    target = safe_next(next_path)
    return render("login.html", status, login_url=f"/auth/login?next={quote(target, safe='')}", error=error, notice=notice)

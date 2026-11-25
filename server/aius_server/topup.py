"""追加额度自动审批：盯着一个飞书审批（通用的资源申请单）里申请 AI 额度的单子，以指定审批人的身份自动处理。

怎么认出是申请 AI 额度的（两条都满足，别的单子一律不碰）：
- 表单「类别」里含设置的那个词；
- 「资源名称」去掉空格后含「额度」，并且含「豆包」或「AI」。

只处理落在指定审批人名下的任务，按任务所在的节点分两步：
- 审批节点：先看这个月是不是已经追加过（平台有追加记录，或者人已经在用户组里）→ 评论一次请他撤回；再看基础额度用完没有
  （这个月用量没到「算作用完」的点数）→ 评论一次，单子留着，之后每次检查再看，用完了自动往下走；都没问题 → 填预估费用和费用说明并同意。
- 办理节点：把使用人加进飞书用户组（额度由管理后台对这个用户组的限额规则决定），记一条追加记录（周报、月报、额度提醒按「基础 + 追加」算），
  再把办理任务办掉。加组失败不办理，通知审批人，下次检查重试。

留给人工、不自动处理的：「使用人」没填或填了不止一个人、这个人单独设置了追加额度、同一个人还有另一张申请在办理中、
到办理节点才发现他本月已经追加过。

三种模式：关闭；只预览（照样看单子、算出每张会怎么处理，记在 plan 里，但不评论、不审批、不动用户组）；自动处理。
每月 1 日把用户组清空（追加额度只在当月有效）；只在「自动处理」开着、跨月的时候清，刚打开时不清。
自动写的评论和审批意见都以 TAG 开头：是以审批人本人的身份发的，带上标注才分得清。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from urllib.parse import quote

from .db import Database, dumps, loads
from .directory import Directory
from .feishu import CST, FeishuError
from .prefs import Config
from .report import points

log = logging.getLogger("aius")

CHECK_SECONDS = 60  # 多久看一次审批单
LOOKBACK_DAYS = 60  # 只看最近这么多天发起的
TAG = "【自动处理】"
STATUS = {
    "pending": "审批中", "waiting": "等基础额度用完", "duplicate": "本月已追加过", "approved": "已同意，等办理",
    "granted": "已追加", "manual": "留给人工", "error": "出错了，会重试", "closed": "已结束",
}  # fmt: skip
ENDED = {"APPROVED": "审批已通过（不是自动追加的）", "REJECTED": "已拒绝", "CANCELED": "已撤回", "DELETED": "已删除"}
APPROVAL_APP = "cli_9cb844403dbb9108"  # 飞书「审批」这个应用，打开审批单的链接里要带


def instance_link(code: str) -> str:
    return f"https://applink.feishu.cn/client/mini_program/open?mode=appCenter&appId={APPROVAL_APP}&path=" + quote(f"pc/pages/in-process/index?instanceId={code}", safe="")


def form_fields(raw) -> list[dict]:
    value = loads(raw, []) if isinstance(raw, str) else raw
    return [f for f in value if isinstance(f, dict)] if isinstance(value, list) else []


def field(form: list[dict], part: str) -> dict:
    """表单里名称含 part 的那个字段，没有就是空字典。"""
    return next((f for f in form if part in str(f.get("name") or "")), {})


def text_of(value) -> str:
    if isinstance(value, list):
        return "、".join(str(v) for v in value)
    return "" if value is None else str(value)


def is_quota_request(form: list[dict], category: str) -> bool:
    name = "".join(text_of(field(form, "资源名称").get("value")).split()).lower()
    kinds = text_of(field(form, "类别").get("value"))
    return (not category or category in kinds) and "额度" in name and ("豆包" in name or "ai" in name)


class _Round:
    """一轮检查里要反复用的东西：用户组和它的成员、审批节点要填的费用字段。用到才去取，一轮只取一次。"""

    def __init__(self, feishu, cfg: dict):
        self.feishu, self.cfg = feishu, cfg
        self._gid: str | None = None
        self._members: set[str] | None = None
        self._fee: list[dict] | None = None

    def gid(self) -> str:
        if self._gid is None:
            if not self.cfg["group"]:
                raise FeishuError("还没有设置追加后要加进哪个用户组")
            self._gid = self.feishu.group_id(self.cfg["group"])
            if not self._gid:
                raise FeishuError(f"飞书里找不到用户组「{self.cfg['group']}」")
        return self._gid

    def members(self) -> set[str]:
        if self._members is None:
            self._members = set(self.feishu.group_members(self.gid())) if self.cfg["group"] else set()
        return self._members

    def fee_form(self, note: str) -> list[dict]:
        """审批节点要填的两个字段：预估费用、费用说明（字段 ID 从审批定义里按名称找）。"""
        if self._fee is None:
            form = form_fields(self.feishu.approval_definition(self.cfg["approval_code"]).get("form"))
            amount, text = field(form, "预估费用"), field(form, "费用说明")
            if not amount.get("id") or not text.get("id"):
                raise FeishuError("审批表单里找不到「预估费用」「费用说明」两个字段")
            self._fee = [{"id": amount["id"], "type": amount.get("type") or "amount", "value": self.cfg["fee"]},
                         {"id": text["id"], "type": text.get("type") or "textarea", "value": note}]  # fmt: skip
        return self._fee


class TopUps:
    def __init__(self, db: Database, feishu, directory: Directory, config: Config):
        self.db = db
        self.feishu = feishu
        self.directory = directory
        self.config = config
        self._lock = threading.Lock()
        self._last = 0.0

    # ---------- 文字 ----------
    @staticmethod
    def texts(cfg: dict, used: float = 0.0, per: float = 0.0) -> dict[str, str]:
        p, fee = f"{cfg['points']:g}", f"{cfg['fee']:g}"
        return {
            "duplicate": f"{TAG}本月已经追加额外 {p} 点，不再提供。请撤回审批单。",
            "waiting": f"{TAG}本月基础额度未用完（已用 {points(used)} / {points(per)} 点），此审批单等用完后处理。",
            "opinion": f"{TAG}已核对：本月基础额度已用完，本月未追加过，同意追加 {p} 点。",
            "fee_note": f"本月基础额度已经用完，额外追加{p}点，预计费用{fee}元，次月失效。",
            "handled": f"{TAG}已经追加。",
        }

    # ---------- 设置 ----------
    def save(self, patch: dict, actor: str, now: datetime | None = None) -> dict:
        """保存设置。切到「自动处理」时把「用户组上次清空的月份」记成本月：刚打开不清用户组，以后跨月才清。"""
        now = now or datetime.now(CST)
        before = self.config.get("topup")
        saved = self.config.save("topup", patch, actor)
        if saved["mode"] == "on" and before["mode"] != "on":
            self.config.mark("topup", "reset_month", now.strftime("%Y-%m"))
        if saved["mode"] != before["mode"]:
            self.db.x("UPDATE topup_requests SET plan='' WHERE plan<>''")  # 换了模式：预览的结果作废，下一轮重算
            self._last = 0.0
        return self.config.get("topup")

    def ready(self, cfg: dict | None = None) -> str:
        """还缺什么设置（空 = 齐了）。"""
        cfg = cfg or self.config.get("topup")
        missing = [name for key, name in (("approval_code", "审批编码"), ("approver", "以谁的身份处理"), ("group", "用户组")) if not cfg[key]]
        if not self.config.monthly_allowance:
            missing.append("每人每月基础额度（在「额度分配」里）")
        return "、".join(missing)

    # ---------- 数据 ----------
    def month_used(self, open_id: str, month: str) -> float:
        return float(
            self.db.val(
                "SELECT SUM(amount) FROM usage_agg WHERE subject='member' AND entity_id=? AND usage_date BETWEEN ? AND ?",
                (open_id, f"{month}-01", f"{month}-31"),
            )
            or 0.0
        )

    def state(self) -> dict:
        return loads(self.db.val("SELECT value FROM meta WHERE key='topup_last'"), {})

    def records(self, limit: int = 200) -> list[dict]:
        rows = self.db.q("SELECT * FROM topup_requests WHERE kind='quota' ORDER BY submitted_at DESC, instance_code LIMIT ?", (limit,))
        d = self.directory
        return [
            {**dict(r), "applicant_name": d.user_name(r["applicant"]) if r["applicant"] else "", "member_name": d.user_name(r["member"]) if r["member"] else "",
             "dept": d.dept_path(d.user_dept(r["member"] or r["applicant"])), "status_name": STATUS.get(r["status"], r["status"]), "link": instance_link(r["instance_code"])}
            for r in rows
        ]  # fmt: skip

    def grants(self, month: str) -> list[dict]:
        d = self.directory
        return [
            {**dict(r), "name": d.user_name(r["open_id"]), "dept": d.dept_path(d.user_dept(r["open_id"]))}
            for r in self.db.q("SELECT * FROM topups WHERE month=? ORDER BY created_at DESC", (month,))
        ]

    # ---------- 检查 ----------
    def tick(self, now: datetime) -> bool:
        """调度每分钟调一次：开着、设置齐了、离上次满 CHECK_SECONDS 秒，就在后台检查一轮。"""
        cfg = self.config.get("topup")
        if cfg["mode"] == "off" or self.ready(cfg) or now.timestamp() - self._last < CHECK_SECONDS - 5 or self._lock.locked():
            return False
        self._last = now.timestamp()
        threading.Thread(target=self.run, args=(now,), name="topup", daemon=True).start()
        return True

    def run(self, now: datetime | None = None) -> dict:
        """检查一轮：审批中的单子逐张看，该评论的评论、该同意的同意、该加用户组的加。返回这一轮的情况（也记在 meta 里给页面看）。"""
        now = now or datetime.now(CST)
        with self._lock:
            cfg = self.config.get("topup")
            state = {"at": int(time.time()), "mode": cfg["mode"], "pending": 0, "acted": 0, "error": ""}
            if cfg["mode"] == "off" or self.ready(cfg):
                return state
            live = cfg["mode"] == "on"
            rnd = _Round(self.feishu, cfg)
            try:
                if live:
                    self._monthly_reset(cfg, now, rnd)
                pending = self.feishu.approval_pending(cfg["approval_code"], LOOKBACK_DAYS)
                state["pending"] = len(pending)
                known = {r["instance_code"]: dict(r) for r in self.db.q("SELECT * FROM topup_requests")}
                for code in pending:
                    row = known.get(code)
                    if row and row["kind"] != "quota":
                        continue
                    try:
                        state["acted"] += bool(self._one(code, row, cfg, live, now, rnd))
                    except FeishuError as e:
                        self._failed(code, cfg, str(e))
                # 已经不在审批中的（通过了、撤回了、拒绝了）：收个尾
                for code, row in known.items():
                    if row["kind"] == "quota" and row["status"] not in ("granted", "closed") and code not in pending:
                        self._close(code)
            except FeishuError as e:
                state["error"] = str(e)[:300]
                log.warning("追加额度自动审批这一轮出错：%s", e)
            self.db.x("INSERT INTO meta(key, value) VALUES('topup_last', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (dumps(state),))
            return state

    def _one(self, code: str, row: dict | None, cfg: dict, live: bool, now: datetime, rnd: _Round) -> bool:
        inst = self.feishu.approval_instance(code)
        if row is None:
            row = self._classify(code, inst, cfg)
        if row["kind"] != "quota":
            return False
        task = next((t for t in inst.get("task_list") or [] if t.get("status") == "PENDING" and cfg["approver"] in (t.get("open_id"), t.get("user_id"))), None)
        if not task:
            return False  # 还没流转到这位审批人，或者在别人那儿
        node = str(task.get("node_name") or "")
        if cfg["approve_node"] and cfg["approve_node"] in node:
            return self._approve_stage(code, row, task, cfg, live, now, rnd)
        if cfg["handle_node"] and cfg["handle_node"] in node:
            return self._handle_stage(code, row, task, cfg, live, now, rnd)
        return False

    def _classify(self, code: str, inst: dict, cfg: dict) -> dict:
        form = form_fields(inst.get("form"))
        quota = is_quota_request(form, cfg["category"])
        users = field(form, "使用人").get("open_ids") or []
        row = {
            "instance_code": code, "serial": str(inst.get("serial_number") or ""), "kind": "quota" if quota else "other",
            "applicant": str(inst.get("open_id") or ""), "member": users[0] if len(users) == 1 else "",
            "resource": text_of(field(form, "资源名称").get("value"))[:100], "reason": text_of(field(form, "申请原因").get("value"))[:300],
            "submitted_at": int(int(inst.get("start_time") or 0) / 1000), "month": "", "status": "pending" if quota else "other",
            "used": None, "note": "", "plan": "", "commented": "", "approved_at": None, "granted_at": None, "updated_at": int(time.time()),
        }  # fmt: skip
        self.db.x(f"INSERT OR REPLACE INTO topup_requests({', '.join(row)}) VALUES({', '.join('?' * len(row))})", tuple(row.values()))
        return row

    def _set(self, code: str, live: bool, status: str, note: str, plan: str = "", **more) -> None:
        """记下这张单的处理结果。只预览时不改状态，只在 plan 里写「会怎么做」。"""
        fields = {"plan": plan or note, "updated_at": int(time.time()), **{k: v for k, v in more.items() if k in ("month", "used")}}
        if live:
            fields = {"status": status, "note": note, "plan": "", "updated_at": int(time.time()), **more}
        self.db.x(f"UPDATE topup_requests SET {', '.join(f'{k}=?' for k in fields)} WHERE instance_code=?", (*fields.values(), code))
        if not live:  # 只预览：之前出过错、这次能算出结果了，状态回到审批中
            self.db.x("UPDATE topup_requests SET status='pending', note='' WHERE instance_code=? AND status='error'", (code,))

    def _approve_stage(self, code: str, row: dict, task: dict, cfg: dict, live: bool, now: datetime, rnd: _Round) -> bool:
        month = now.strftime("%Y-%m")
        member = row["member"]
        if not member:
            self._set(code, live, "manual", "「使用人」没填或填了不止一个人，留给人工")
            return False
        used = self.month_used(member, month)
        if self.db.val("SELECT 1 FROM quota_extras WHERE open_id=?", (member,)):
            self._set(code, live, "manual", "这个人单独设置了追加额度，留给人工", month=month, used=used)
            return False
        other = self.db.val("SELECT serial FROM topup_requests WHERE member=? AND month=? AND status='approved' AND instance_code<>?", (member, month, code))
        if other:
            self._set(code, live, "manual", f"同一个人还有一张申请在办理中（单号 {other}），留给人工", month=month, used=used)
            return False
        text = self.texts(cfg, used, self.config.monthly_allowance)
        if self.config.topup_points(member, month) or member in rnd.members():
            return self._comment_once(code, row, cfg, live, "duplicate", month, used, text["duplicate"])
        if used < cfg["used_up"]:
            return self._comment_once(code, row, cfg, live, "waiting", month, used, text["waiting"])
        if not live:
            self._set(code, live, "", "", plan=f"会填预估费用 {cfg['fee']:g}、费用说明，并同意", month=month, used=used)
            return False
        self.feishu.approval_approve(cfg["approval_code"], code, str(task["id"]), cfg["approver"], text["opinion"], form=rnd.fee_form(text["fee_note"]))
        self._set(code, live, "approved", "基础额度已用完、本月未追加过：已填预估费用并同意", month=month, used=used, approved_at=int(time.time()))
        self.db.audit("system", "topup", f"自动同意追加申请 {row['serial'] or code}：{self.directory.user_name(member)}，本月已用 {points(used)} 点", via="system")
        return True

    def _comment_once(self, code: str, row: dict, cfg: dict, live: bool, kind: str, month: str, used: float, text: str) -> bool:
        """同一种评论一个月只发一次；之后每次检查只更新已用点数。"""
        key = f"{kind}:{month}"
        done = key in row["commented"].split(",")
        note = {"duplicate": "本月已经追加过：已评论，等申请人撤回", "waiting": "基础额度还没用完：已评论，用完后自动继续"}[kind]
        if not live:
            self._set(code, live, "", "", plan=f"会评论：{text}", month=month, used=used)
            return False
        if not done:
            self.feishu.approval_comment(code, cfg["approver"], text)
        commented = row["commented"] if done else ",".join(filter(None, [row["commented"], key]))
        self._set(code, live, kind, note, month=month, used=used, commented=commented)
        return not done

    def _handle_stage(self, code: str, row: dict, task: dict, cfg: dict, live: bool, now: datetime, rnd: _Round) -> bool:
        month = now.strftime("%Y-%m")
        member = row["member"]
        if not member:
            self._set(code, live, "manual", "「使用人」没填或填了不止一个人，留给人工")
            return False
        existing = self.db.one("SELECT instance_code FROM topups WHERE month=? AND open_id=?", (month, member))
        if existing and existing["instance_code"] != code:
            self._set(code, live, "manual", "这个人本月已经追加过（另一张单），留给人工", month=month)
            return False
        if not existing and member in rnd.members():
            self._set(code, live, "manual", "这个人已经在用户组里（不是这张单加的），留给人工", month=month)
            return False
        if not live:
            self._set(code, live, "", "", plan=f"会把他加进用户组「{cfg['group']}」并办理", month=month)
            return False
        if not existing:
            self.feishu.group_add(rnd.gid(), member)
            rnd.members().add(member)
            self.db.x(
                "INSERT OR REPLACE INTO topups(month, open_id, points, source, instance_code, created_at) VALUES(?,?,?,'approval',?,?)",
                (month, member, cfg["points"], code, int(time.time())),
            )
        self.feishu.approval_approve(cfg["approval_code"], code, str(task["id"]), cfg["approver"], self.texts(cfg)["handled"])
        self._set(code, live, "granted", f"已加进用户组「{cfg['group']}」，追加 {cfg['points']:g} 点", month=month, granted_at=int(time.time()))
        self.db.audit("system", "topup", f"自动追加 {cfg['points']:g} 点 {row['serial'] or code}：{self.directory.user_name(member)} 加进用户组「{cfg['group']}」", via="system")
        return True

    def _failed(self, code: str, cfg: dict, error: str) -> None:
        """这张单处理出错：记下来，下一轮重试；第一次出这个错时用机器人告诉审批人。"""
        row = self.db.one("SELECT serial, status, note FROM topup_requests WHERE instance_code=?", (code,))
        log.warning("追加申请 %s 处理出错：%s", code, error)
        if not row:
            return
        first = row["status"] != "error" or row["note"] != error[:300]
        self.db.x("UPDATE topup_requests SET status='error', note=?, updated_at=? WHERE instance_code=?", (error[:300], int(time.time()), code))
        if first and cfg["mode"] == "on":
            card = {
                "config": {"wide_screen_mode": True},
                "header": {"template": "red", "title": {"tag": "plain_text", "content": "追加额度自动审批出错"}},
                "elements": [{"tag": "markdown", "content": f"审批单 **{row['serial'] or code}** 没有处理成功，下一分钟会重试。\n原因：{error[:200]}"}],
            }
            try:
                self.feishu.send_card(cfg["approver"], card)
            except FeishuError:
                pass

    def _close(self, code: str) -> None:
        inst = self.feishu.approval_instance(code)
        status = str(inst.get("status") or "")
        note = ENDED.get(status)
        if not note and int(int(inst.get("start_time") or 0) / 1000) < time.time() - LOOKBACK_DAYS * 86400:
            note = f"发起超过 {LOOKBACK_DAYS} 天还没审完，不再跟"
        if note:
            self.db.x("UPDATE topup_requests SET status='closed', note=?, plan='', updated_at=? WHERE instance_code=?", (note, int(time.time()), code))

    def _monthly_reset(self, cfg: dict, now: datetime, rnd: _Round) -> None:
        """追加额度只在当月有效：跨月后的第一轮把用户组清空。reset_month 是空（从没记过）时只记下本月，不清。"""
        month = now.strftime("%Y-%m")
        if cfg["reset_month"] == month:
            return
        if cfg["reset_month"]:
            members = sorted(rnd.members())
            for oid in members:
                self.feishu.group_remove(rnd.gid(), oid)
                rnd.members().discard(oid)
            self.db.audit("system", "topup", f"月初清空用户组「{cfg['group']}」：移出 {len(members)} 人", via="system")
            log.info("月初清空用户组 %s：移出 %d 人", cfg["group"], len(members))
        self.config.mark("topup", "reset_month", month)

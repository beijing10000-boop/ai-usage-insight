"""飞书 AI 用量接口里的枚举值 → 中文名称，以及场景路径的处理。

取值来自《AI 用量查询 OpenAPI 使用指南》（admin/v1/ai_usage_log/query、ai_usage_detail/query）。
"""

from __future__ import annotations

ENTITY_TYPES = {
    1: "部门",
    2: "用户",
    4: "用户组",
    5: "企业自建应用",
    6: "Aily 工作流应用",
    7: "aPaaS 应用",
    8: "Aily 智能体",
    10: "飞书项目",
    13: "CodeM 沙箱应用",
}
# 应用维度要查的实体类型；entity_ids 传 -1 表示这一类全选
APP_ENTITY_TYPES = (5, 6, 7, 8, 10, 13)

# 消耗的 AI 额度类型
FEATURE_KEYS = {
    1: "AI 通用额度",
    2: "智能纪要专用额度",
    3: "飞书 Aily 专用额度",
    4: "团队订阅席位额度",
    5: "团队订阅共享额度",
    6: "团队订阅扩容额度",
}
# 统计详情接口里各额度类型对应的字段名
DETAIL_FIELDS = {
    1: ("usage_value_general_ai_quota",),
    2: ("usage_value_ai_notes_quota",),
    3: ("usage_value_feishu_aily_quota",),
    4: ("usage_value_doubao_team_subscription_quota",),
    5: ("usage_value_doubao_team_gift_quota",),
    # 文档里这个字段名中间多了一个空格，两种写法都认
    6: ("usage_value_doubao_team_upgrade_quota", "usage_value_doubao_team_upgrade _quota"),
}

USAGE_TYPES = {1: "企业付费额度", 2: "试用免费额度"}

SUBJECTS = {"member": "人员", "app": "应用"}

UNKNOWN_SCENARIO = "未标注场景"
# 豆包工作的用量：场景以「豆包工作」开头，但不含「豆包工作伙伴」
DOUBAO_PREFIX = "豆包工作"
DOUBAO_EXCLUDE = "豆包工作伙伴"


def entity_type(value: int | None) -> str:
    return ENTITY_TYPES.get(value or 0, "未知类型" if value is None else f"类型 {value}")


def feature_key(value: int | None) -> str:
    return FEATURE_KEYS.get(value or 0, "未知额度" if value is None else f"额度 {value}")


def usage_type(value: int | None) -> str:
    return USAGE_TYPES.get(value or 0, "未知" if value is None else f"类型 {value}")


def clean_scenario(raw: object) -> str:
    """「知识问答 > 主对话>网页版问答」→「知识问答>主对话>网页版问答」；空的返回空串。"""
    if not isinstance(raw, str):
        return ""
    return ">".join(part.strip() for part in raw.split(">") if part.strip())


def top_scenario(scenario: str) -> str:
    """一级场景（产品）：场景路径的第一段。"""
    return scenario.split(">", 1)[0] if scenario else UNKNOWN_SCENARIO


def is_doubao(scenario: str) -> bool:
    return scenario.startswith(DOUBAO_PREFIX) and not scenario.startswith(DOUBAO_EXCLUDE)


# SQL 里判断豆包工作用量的条件（和 is_doubao 一致）
DOUBAO_SQL = f"scenario LIKE '{DOUBAO_PREFIX}%' AND scenario NOT LIKE '{DOUBAO_EXCLUDE}%'"

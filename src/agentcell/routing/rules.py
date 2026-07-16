"""Deterministic multilingual intent rules for public task targets."""

from __future__ import annotations

import re
from dataclasses import dataclass

from agentcell.policy import Capability
from agentcell.routing.models import RouteBudgetProfile, TaskRouteMode

_CHANGE = re.compile(
    r"(?:修复|实现|新增|添加|开发|重构|删除).{0,24}"
    r"(?:代码|功能|特性|模块|接口|页面|测试|用例|问题|错误)|"
    r"(?:修改|更新).{0,16}(?:代码|文件|功能|模块|接口|页面|测试|用例|实现)|"
    r"\b(?:fix|repair|implement|add|create|build|refactor|change|update|remove)\b",
    re.IGNORECASE,
)
_TEST = re.compile(r"(?:测试|用例|pytest|单测|集成测试)|\btests?\b", re.IGNORECASE)
_REVIEW = re.compile(
    r"(?:独立审查|审查|审核|复核|代码审阅|回归分析|安全检查)|"
    r"\b(?:independent\s+review|review|audit|regression\s+analysis|security\s+check)\b",
    re.IGNORECASE,
)
_RESEARCH = re.compile(
    r"(?:调研|联网|检索资料|外部资料|官方文档|最新资料|最新信息)|"
    r"\b(?:research|web\s+search|search\s+the\s+web|external\s+sources?|"
    r"official\s+docs?|latest\s+information)\b",
    re.IGNORECASE,
)
_READ_ONLY = re.compile(
    r"(?:分析|解释|说明|检查|规划|总结|梳理|阅读)|"
    r"\b(?:analy[sz]e|explain|inspect|check|plan|summari[sz]e|read)\b",
    re.IGNORECASE,
)
_DIRECT_GREETING_OR_META = re.compile(
    r"^(?:(?:你|您)好|你好呀|嗨|哈喽|hello|hi|hey|"
    r"谢谢|多谢|thanks?|thank\s+you|再见|bye|"
    r"你是谁|你是什么|你能做什么|介绍(?:一下)?你自己|"
    r"who\s+are\s+you|what\s+can\s+you\s+do)[!！。.?？~～]*$",
    re.IGNORECASE,
)
_WORKSPACE_CONTEXT = re.compile(
    r"(?:代码库|仓库|项目|工作区|源码|文件|目录|模块|依赖|构建|测试|用例|"
    r"报错|错误|异常|日志|迁移|数据库|接口|页面|组件)|"
    r"\b(?:repo(?:sitory)?|workspace|project|codebase|source|file|directory|"
    r"module|dependency|build|tests?|error|exception|traceback|migration|database|"
    r"api|page|component)\b",
    re.IGNORECASE,
)
_ORDINARY_QUESTION = re.compile(
    r"(?:[?？]$|^(?:什么|为什么|怎么|如何|谁|哪里|何时|是否|能否|可以|请问)|"
    r"^(?:what|why|how|who|where|when|is|are|can|could|would|should)\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class IntentSignals:
    change: bool
    test: bool
    review: bool
    research: bool
    read_only: bool


@dataclass(frozen=True, slots=True)
class DeterministicRouteMatch:
    mode: TaskRouteMode
    target_id: str
    confidence: float
    reason_summary: str
    required_capabilities: frozenset[Capability]
    budget_profile: RouteBudgetProfile
    ambiguous: bool = False


def intent_signals(task: str) -> IntentSignals:
    """Extract only bounded public intent facts; never inspect a workspace."""

    return IntentSignals(
        change=bool(_CHANGE.search(task)),
        test=bool(_TEST.search(task)),
        review=bool(_REVIEW.search(task)),
        research=bool(_RESEARCH.search(task)),
        read_only=bool(_READ_ONLY.search(task)),
    )


def is_direct_conversation(task: str) -> bool:
    """Return whether auto chat can safely use one tool-free Agent directly."""

    normalized = " ".join(task.strip().split())
    if not normalized:
        return False
    signals = intent_signals(normalized)
    if any((signals.change, signals.test, signals.review, signals.research, signals.read_only)):
        return False
    if _WORKSPACE_CONTEXT.search(normalized):
        return False
    if _DIRECT_GREETING_OR_META.fullmatch(normalized):
        return True
    return len(normalized) <= 180 and bool(_ORDINARY_QUESTION.search(normalized))


def deterministic_route(task: str) -> DeterministicRouteMatch:
    """Choose an explicit intent or return a confirmation-only safe fallback."""

    signals = intent_signals(task)
    if signals.change and signals.test and signals.review:
        return DeterministicRouteMatch(
            mode=TaskRouteMode.TEAM,
            target_id="software",
            confidence=0.99,
            reason_summary="任务同时要求修改、测试和独立审查，适合确定性 software Team。",
            required_capabilities=frozenset(
                {
                    Capability.FILESYSTEM_READ,
                    Capability.FILESYSTEM_WRITE,
                    Capability.SHELL_EXECUTE,
                }
            ),
            budget_profile=RouteBudgetProfile.DELIVERY,
        )
    if signals.change:
        required = {Capability.FILESYSTEM_READ, Capability.FILESYSTEM_WRITE}
        if signals.test:
            required.add(Capability.SHELL_EXECUTE)
        return DeterministicRouteMatch(
            mode=TaskRouteMode.SINGLE_AGENT,
            target_id="coder",
            confidence=0.96,
            reason_summary="任务明确要求修改工作区内容，选择 Coder。",
            required_capabilities=frozenset(required),
            budget_profile=RouteBudgetProfile.CHANGE,
        )
    if signals.review:
        return DeterministicRouteMatch(
            mode=TaskRouteMode.SINGLE_AGENT,
            target_id="reviewer",
            confidence=0.95,
            reason_summary="任务是只读审查或回归判断，选择 Reviewer。",
            required_capabilities=frozenset({Capability.FILESYSTEM_READ}),
            budget_profile=RouteBudgetProfile.REVIEW,
        )
    if signals.research:
        return DeterministicRouteMatch(
            mode=TaskRouteMode.SINGLE_AGENT,
            target_id="researcher",
            confidence=0.94,
            reason_summary="任务需要工作区与外部资料证据，选择 Researcher。",
            required_capabilities=frozenset(
                {Capability.FILESYSTEM_READ, Capability.NETWORK_REQUEST}
            ),
            budget_profile=RouteBudgetProfile.RESEARCH,
        )
    if signals.read_only:
        return DeterministicRouteMatch(
            mode=TaskRouteMode.SINGLE_AGENT,
            target_id="coordinator",
            confidence=0.93,
            reason_summary="任务是只读分析、解释或规划，选择 Coordinator。",
            required_capabilities=frozenset({Capability.FILESYSTEM_READ}),
            budget_profile=RouteBudgetProfile.READ_ONLY,
        )
    return DeterministicRouteMatch(
        mode=TaskRouteMode.SINGLE_AGENT,
        target_id="coordinator",
        confidence=0.35,
        reason_summary="任务意图不足以由确定性规则可靠分类，需要确认或结构化模型回退。",
        required_capabilities=frozenset({Capability.FILESYSTEM_READ}),
        budget_profile=RouteBudgetProfile.READ_ONLY,
        ambiguous=True,
    )

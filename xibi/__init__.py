# xibi — AI assistant framework
# https://github.com/[owner]/xibi

"""
Xibi: roles-based AI assistant with observation cycles,
trust gradients, and local-first execution.

Usage:
    from xibi.router import get_model
    from xibi.react import run

    llm = get_model("text", "fast")     # extraction, triage
    llm = get_model("text", "think")    # reasoning, ReAct loop
    llm = get_model("text", "review")   # observation cycle, audit

    result = run(
        query="What is the weather in Tokyo?",
        config=config,
        skill_registry=registry
    )
"""

from xibi.alerting.rules import RuleEngine
from xibi.channels.telegram import TelegramAdapter
from xibi.dashboard.app import DashboardConfig, create_app
from xibi.db.migrations import SchemaManager
from xibi.executor import Executor
from xibi.heartbeat.poller import HeartbeatPoller
from xibi.react import run
from xibi.router import get_model
from xibi.routing.classifier import MessageModeClassifier, ModeScores
from xibi.routing.control_plane import ControlPlaneRouter, RoutingDecision
from xibi.routing.shadow import ShadowMatch, ShadowMatcher
from xibi.skills.registry import SkillRegistry
from xibi.trust.gradient import (
    DEFAULT_TRUST_CONFIG,
    FailureType,
    TrustConfig,
    TrustGradient,
    TrustRecord,
)
from xibi.types import ReActResult

__all__ = [
    "get_model",
    "run",
    "TelegramAdapter",
    "ReActResult",
    "SkillRegistry",
    "Executor",
    "MessageModeClassifier",
    "ModeScores",
    "ControlPlaneRouter",
    "RoutingDecision",
    "ShadowMatch",
    "ShadowMatcher",
    "RuleEngine",
    "HeartbeatPoller",
    "SchemaManager",
    "create_app",
    "DashboardConfig",
    "TrustGradient",
    "TrustRecord",
    "TrustConfig",
    "DEFAULT_TRUST_CONFIG",
    "FailureType",
]

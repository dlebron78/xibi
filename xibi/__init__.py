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

from xibi.executor import Executor
from xibi.react import run
from xibi.router import get_model
from xibi.routing.control_plane import ControlPlaneRouter, RoutingDecision
from xibi.skills.registry import SkillRegistry
from xibi.types import ReActResult

__all__ = ["get_model", "run", "ReActResult", "SkillRegistry", "Executor", "ControlPlaneRouter", "RoutingDecision"]

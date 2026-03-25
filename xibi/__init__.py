# xibi — AI assistant framework
# https://github.com/[owner]/xibi

"""
Xibi: roles-based AI assistant with observation cycles,
trust gradients, and local-first execution.

Usage:
    from xibi.router import get_model

    llm = get_model("text", "fast")     # extraction, triage
    llm = get_model("text", "think")    # reasoning, ReAct loop
    llm = get_model("text", "review")   # observation cycle, audit
"""

#!/usr/bin/python3
"""Agent hook: record which session runs in this Herdr terminal.

Registered as a SessionStart hook for Claude Code, Codex and Cursor Agent (and SessionEnd for
Claude Code). The agent passes its session as JSON on stdin; this terminal's address is in
HC_TERMINAL_ID. Outside Herdr nothing happens. It prints nothing (Claude adds a SessionStart
hook's output to the conversation) and always exits 0, so it can never hold an agent up.

    record_session.py <claude|codex|cursor> [start|end]
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def first(payload, *names):
    for name in names:
        value = payload.get(name)
        if isinstance(value, str) and value:
            return value
    return None


def workspace_root(payload):
    """Cursor sends its folders as workspace_roots (no cwd). Without either, record() asks the
    terminal for the agent's working directory."""
    roots = payload.get("workspace_roots") or payload.get("workspaceRoots")
    if isinstance(roots, list) and roots and isinstance(roots[0], str) and roots[0]:
        return roots[0]
    return None


def main():
    import srlib as S

    agent = sys.argv[1] if len(sys.argv) > 1 else ""
    mode = sys.argv[2] if len(sys.argv) > 2 else "start"
    addr = os.environ.get("HC_TERMINAL_ID")
    if not addr or agent not in S.RESUME:
        return
    raw = "" if sys.stdin.isatty() else sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    sid = first(payload, "session_id", "sessionId", "conversation_id", "conversationId")
    if mode in ("working", "idle"):
        # Cursor's prompt and stop hooks: keep the inventory right when a resumed session never ran
        # its start hook, and report the status Cursor itself doesn't send.
        if sid and agent == "cursor":
            S.ensure_recorded(addr, agent, sid, first(payload, "cwd") or workspace_root(payload), None, mode)
        info = S.terminal(addr)
        if info and agent == "cursor":
            S.report_status(info, mode, "cursor")
        return
    if mode == "end":
        reason = first(payload, "reason")
        # Only a deliberate exit ends the entry. A shutdown, a killed terminal or a signal reports
        # "other", and that is exactly when the session should come back; the sweeper handles the rest.
        deliberate = reason in ("prompt_input_exit", "logout")
        if deliberate:
            S.mark_ended(addr, sid, reason)
        S.log(
            f"hook {agent} end {addr} {sid} reason={reason}: {'ended' if deliberate else 'kept'}"
        )
        return
    if not sid:
        S.log(
            f"hook {agent} {addr}: no session id in the payload (keys: {sorted(payload)})"
        )
        return
    if agent == "claude" and payload.get("agent_id"):
        return  # a subagent's session, not the one in the pane
    if agent == "claude" and (
        "CURSOR_VERSION" in os.environ or "cursor_version" in payload
    ):
        return  # Cursor Agent also runs Claude Code's hooks: its own hook records that session
    if agent == "codex":
        inherited = os.environ.get("CODEX_THREAD_ID")
        if inherited and inherited != sid:
            return  # a child process of another session
    S.log(f"hook {agent} start {addr} {sid}")
    S.record(
        addr,
        agent,
        sid,
        first(payload, "cwd") or workspace_root(payload),
        first(payload, "transcript_path"),
        first(payload, "source"),
    )


try:
    main()
except Exception:
    pass
sys.exit(0)

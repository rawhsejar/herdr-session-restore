#!/usr/bin/python3
"""Tests for herdr-session-restore against an isolated state folder and a fake hc.

    /usr/bin/python3 tests/test_restore.py

Nothing here touches the real inventory, the real core or any real terminal.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
tmp = Path(tempfile.mkdtemp(prefix="sr-test-"))
(tmp / "state").mkdir()
sock_path = tmp / "core.sock"
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(str(sock_path))
srv.listen(5)
threading.Thread(
    target=lambda: [srv.accept() for _ in iter(int, 1)], daemon=True
).start()

terms = tmp / "terms.json"
sends = tmp / "sends.log"
fake = tmp / "hc"
fake.write_text(
    f"""#!/usr/bin/python3
import json, sys
a = sys.argv[1:]
terms = json.load(open("{terms}"))
if a[:1] == ["get"]:
    t = terms.get(a[1])
    if t is None:
        sys.exit(1)
    print(json.dumps(t))
elif a[:1] == ["send"]:
    open("{sends}", "a").write(a[1] + " | " + a[2] + "\\n")
elif a[:1] == ["ls"]:
    print(json.dumps([{{"address": k}} for k in terms]))
"""
)
fake.chmod(0o755)

env = {
    k: v
    for k, v in os.environ.items()
    if not k.startswith("HC_") and k != "CODEX_THREAD_ID"
}
env.update(
    HERDR_SR_STATE_DIR=str(tmp / "state"),
    HERDR_SR_HC=str(fake),
    HERDR_SR_CORE_SOCK=str(sock_path),
)
os.environ.clear()
os.environ.update(env)
sys.path.insert(0, str(ROOT))
import sessions_cli as C
import srlib as S

STARTS = {}  # fake process start times by pid
S.shell_start_ms = lambda pid: STARTS.get(pid)
S.STAGGER_SECONDS = 0
DAY = 86_400_000


def setterms(d):
    terms.write_text(json.dumps(d))


def shell(pid=100, created=7, cwd="/tmp", fgpid=None):
    return {
        "state": "live",
        "pid": pid,
        "argv": ["/bin/bash", "-l"],
        "created_ms": created,
        "foreground": {"pid": fgpid or pid, "cwd": cwd, "argv": ["bash"]},
        "labels": {"where": "test / 1"},
    }


def sent():
    return sends.read_text().splitlines() if sends.exists() else []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        sys.exit(1)


folder = str(tmp)  # a directory that exists, for the entries' cwd
hook = ROOT / "record_session.py"


def run_hook(agent, mode, payload, terminal="t1", extra=None, raw=None):
    e = dict(env, **(extra or {}))
    if terminal:
        e["HC_TERMINAL_ID"] = terminal
    return subprocess.run(
        ["/usr/bin/python3", str(hook), agent, mode],
        input=raw if raw is not None else json.dumps(payload),
        capture_output=True,
        text=True,
        env=e,
    )


def entry(addr="t1"):
    return S.read()["entries"][addr]


# ---- the hook ------------------------------------------------------------------------------
setterms({"t1": shell(pid=100, created=7)})
r = run_hook(
    "claude",
    "start",
    {
        "session_id": "S1",
        "cwd": folder,
        "transcript_path": "/x.jsonl",
        "source": "startup",
    },
)
check(
    "hook exits 0 and prints nothing",
    r.returncode == 0 and r.stdout == "" and r.stderr == "",
)
e = entry()
check(
    "hook records terminal, agent, session, folder, terminal and shell identity",
    (
        e["agent"],
        e["session_id"],
        e["cwd"],
        e["terminal_created_ms"],
        e["shell_pid"],
        e["active"],
    )
    == ("claude", "S1", folder, 7, 100, True),
)
run_hook("claude", "start", {"session_id": "SUB", "agent_id": "a1", "cwd": folder})
check("a subagent session is ignored", entry()["session_id"] == "S1")
run_hook(
    "claude",
    "start",
    {"session_id": "CUR", "cwd": folder},
    terminal="t11",
    extra={"CURSOR_VERSION": "2026.11.17"},
)
run_hook(
    "claude",
    "start",
    {"session_id": "CUR2", "cwd": folder, "cursor_version": "2026.11.17"},
    terminal="t12",
)
check(
    "Cursor running Claude Code's hook is not recorded as a Claude session",
    "t11" not in S.read()["entries"] and "t12" not in S.read()["entries"],
)
run_hook("claude", "start", {"session_id": "S2", "cwd": folder}, terminal=None)
check(
    "outside Herdr (no HC_TERMINAL_ID) nothing is recorded",
    "S2" not in json.dumps(S.read()),
)
run_hook(
    "codex",
    "start",
    {"session_id": "OTHER"},
    terminal="t9",
    extra={"CODEX_THREAD_ID": "PARENT"},
)
check("a Codex child of another thread is ignored", "t9" not in S.read()["entries"])
run_hook("claude", "end", {"session_id": "OLD", "reason": "clear"})
check("an end for an older session leaves the newer one active", entry()["active"])
run_hook("cursor", "start", {"conversation_id": "C1", "cwd": folder}, terminal="t2")
check("cursor accepts conversation_id", entry("t2")["session_id"] == "C1")
run_hook(
    "cursor",
    "start",
    {"conversation_id": "C2", "workspace_roots": ["/work/a", "/work/b"]},
    terminal="t13",
)
check("cursor's workspace_roots gives the folder", entry("t13")["cwd"] == "/work/a")
setterms({"t1": shell(pid=100, created=7, cwd="/agent/cwd")})
run_hook("cursor", "start", {"conversation_id": "C3"}, terminal="t1")
check(
    "without any folder in the payload, the terminal's foreground folder is used",
    entry("t1")["cwd"] == "/agent/cwd",
)
setterms({"t1": shell(pid=100, created=7)})
run_hook(
    "claude",
    "start",
    {
        "session_id": "S1",
        "cwd": folder,
        "transcript_path": "/x.jsonl",
        "source": "startup",
    },
)  # back to the claude entry
r = run_hook("claude", "start", None, raw="not json")
check(
    "garbage stdin still exits 0 quietly",
    r.returncode == 0 and r.stdout == "" and r.stderr == "",
)
run_hook("claude", "end", {"session_id": "S1", "reason": "prompt_input_exit"})
check(
    "SessionEnd for the current session ends the entry",
    not entry()["active"] and entry()["ended_reason"] == "prompt_input_exit",
)
run_hook(
    "claude", "start", {"session_id": "S1", "cwd": folder}
)  # active again, as after a resume
run_hook("claude", "end", {"session_id": "S1", "reason": "other"})
check(
    "SessionEnd reason other (a shutdown or a killed terminal) keeps the entry",
    entry()["active"],
)
run_hook("claude", "end", {"session_id": "S1", "reason": "clear"})
check(
    "SessionEnd reason clear keeps the entry (a new session follows)", entry()["active"]
)


def backdate_ending(addr, ms):
    """Pretend the sweeper first noticed the entry's end condition `ms` ago."""
    with S.locked() as d:
        d["entries"][addr]["ending_since_ms"] -= ms


# the session was recorded two minutes ago, in shell 100 which started an hour before that
now = S.now_ms()
with S.locked() as d:
    d["entries"]["t1"]["recorded_ms"] = now - 120_000
R = entry()["recorded_ms"]
STARTS[100] = R - 3_600_000

# ---- restore rules -------------------------------------------------------------------------
setterms({"t1": shell(pid=100, created=7)})
check(
    "same shell as when recorded: nothing to restore",
    C.restore_pass() == [] and sent() == [],
)
setterms({"t1": shell(pid=200, created=7, cwd=folder)})
STARTS[200] = S.now_ms() - 1000
check("shell replaced a second ago: still settling", C.restore_pass() == [])
STARTS[200] = S.now_ms() - 60_000
check(
    "dry run reports the command and sends nothing",
    [a["command"] for a in C.restore_pass(dry_run=True)] == ["claude --resume S1"]
    and sent() == [],
)
S.DISABLED.write_text("x")
check("disabled flag: nothing is sent", C.restore_pass() == [] and sent() == [])
S.DISABLED.unlink()
setterms({"t1": shell(pid=200, created=7, cwd=folder, fgpid=555)})
check(
    "something already running in the terminal: left alone",
    C.restore_pass() == [] and sent() == [],
)
setterms({"t1": shell(pid=200, created=7, cwd="/somewhere/else")})
a = C.restore_pass()
check(
    "recovered shell gets the resume command (with cd, its folder differs)",
    len(a) == 1 and sent() == [f"t1 | cd -- {folder} && claude --resume S1"],
)
e = entry()
check(
    "the resumed agent's shell becomes the baseline",
    e["shell_pid"] == 200 and e["recorded_ms"] >= now and e["active"],
)
check(
    "a second pass does not type it again", C.restore_pass() == [] and len(sent()) == 1
)

# ---- the sweeper after a restore -----------------------------------------------------------
with S.locked() as d:
    d["entries"]["t1"]["recorded_ms"] -= 60_000  # resumed a minute ago
setterms({"t1": shell(pid=200, created=7, cwd=folder, fgpid=555)})
C.sweep_pass()
check("agent still running after the restore: entry stays", entry()["active"])
setterms(
    {"t1": shell(pid=200, created=7, cwd=folder)}
)  # the user quit the agent: shell prompt again
C.sweep_pass()
check(
    "agent quit after the restore: the end is noticed but waits out the grace period",
    entry()["active"] and entry()["ending_reason"] == "agent-exited",
)
backdate_ending("t1", 60_000)
C.sweep_pass()
check(
    "agent quit after the restore: sweeper ends the entry once the grace period has passed",
    not entry()["active"] and entry()["ended_reason"] == "agent-exited",
)
setterms({"t1": shell(pid=300, created=7, cwd=folder)})
STARTS[300] = S.now_ms() - 60_000
check("a quit session is not revived by a later reboot", C.restore_pass() == [])

# ---- a second reboot restores again --------------------------------------------------------
run_hook(
    "claude", "start", {"session_id": "S1", "cwd": folder}
)  # the user starts it again in shell 300
with S.locked() as d:
    d["entries"]["t1"]["recorded_ms"] -= 120_000
STARTS[300] = entry()["recorded_ms"] - 10_000
setterms({"t1": shell(pid=400, created=7, cwd=folder)})
STARTS[400] = S.now_ms() - 60_000
before = len(sent())
check(
    "after another reboot the same entry is restored again",
    len(C.restore_pass()) == 1 and len(sent()) == before + 1,
)

# ---- identity guards -----------------------------------------------------------------------
run_hook("claude", "start", {"session_id": "S1", "cwd": folder})
with S.locked() as d:
    d["entries"]["t1"]["recorded_ms"] -= 120_000
STARTS[500] = S.now_ms() - 60_000
setterms({"t1": shell(pid=500, created=999, cwd=folder)})
check(
    "a terminal address reused by a different terminal is skipped",
    C.restore_pass(only="t1") == [],
)
with S.locked() as d:
    d["entries"]["t1"]["shell_pid"] = (
        500  # pid repeated after a reboot: only the start time can tell
    )
STARTS[500] = S.now_ms() - 30_000
setterms({"t1": shell(pid=500, created=7, cwd=folder)})
check(
    "a repeated pid is told apart by the shell's start time",
    len(C.restore_pass(dry_run=True)) == 1,
)

# ---- other agents and skips ----------------------------------------------------------------
S.record("t3", "codex", "X3", folder)
with S.locked() as d:
    d["entries"]["t3"].update(
        terminal_created_ms=None, shell_pid=1, recorded_ms=S.now_ms() - 120_000
    )
setterms({"t3": shell(pid=600, cwd=folder)})
STARTS[600] = S.now_ms() - 60_000
check(
    "codex resume line",
    [a["command"] for a in C.restore_pass(dry_run=True)]
    == ["codex --no-daemon resume X3"],
)
S.record("t8", "cursor", "C8", folder)
with S.locked() as d:
    d["entries"]["t8"].update(
        terminal_created_ms=None, shell_pid=1, recorded_ms=S.now_ms() - 120_000
    )
setterms({"t8": shell(pid=650, cwd=folder)})
STARTS[650] = S.now_ms() - 60_000
check(
    "cursor resume line",
    [a["command"] for a in C.restore_pass(dry_run=True)]
    == ["cursor-agent --resume C8"],
)
with S.locked() as d:
    d["entries"]["t3"]["recorded_ms"] = S.now_ms() - 31 * DAY
setterms({"t3": shell(pid=600, cwd=folder)})
STARTS[600] = S.now_ms() - 60_000
a = C.restore_pass()
check(
    "an entry older than 30 days is skipped and closed",
    len(a) == 1 and a[0]["skip"] == "stale" and not entry("t3")["active"],
)
gone = tmp / "removed-folder"
S.record("t10", "claude", "S10", str(gone))
with S.locked() as d:
    d["entries"]["t10"].update(
        terminal_created_ms=None, shell_pid=1, recorded_ms=S.now_ms() - 120_000
    )
setterms({"t10": shell(pid=700, cwd="/tmp")})
STARTS[700] = S.now_ms() - 60_000
a = C.restore_pass()
check(
    "a missing folder is skipped and closed",
    len(a) == 1 and a[0]["skip"] == "folder-missing" and not entry("t10")["active"],
)

# ---- sweeper -------------------------------------------------------------------------------
with S.locked() as d:
    for t in ("t4", "t5", "t6"):
        d["entries"][t] = {
            "terminal": t,
            "agent": "claude",
            "session_id": "S" + t,
            "cwd": folder,
            "recorded_ms": S.now_ms() - 60_000,
            "active": True,
            "terminal_created_ms": None,
            "shell_pid": 1,
        }
STARTS.update({1: 0})
setterms(
    {"t4": shell(pid=1), "t5": shell(pid=1, fgpid=777)}
)  # t4 at its prompt, t5 busy, t6 gone
C.sweep_pass()
check(
    "first sighting only starts the grace period",
    entry("t4")["active"]
    and entry("t6")["active"]
    and entry("t4")["ending_reason"] == "agent-exited",
)
setterms(
    {"t4": shell(pid=1, fgpid=888), "t5": shell(pid=1, fgpid=777)}
)  # t4 runs a program again, t6 is still gone
C.sweep_pass()
check(
    "a condition that goes away resets the grace period",
    "ending_since_ms" not in entry("t4")
    and entry("t6")["ending_reason"] == "terminal-gone",
)
setterms({"t4": shell(pid=1), "t5": shell(pid=1, fgpid=777)})
C.sweep_pass()
backdate_ending("t4", 60_000)
backdate_ending("t6", 120_000)
C.sweep_pass()
check(
    "sweeper ends an agent whose shell stayed at its prompt",
    not entry("t4")["active"] and entry("t4")["ended_reason"] == "agent-exited",
)
check(
    "sweeper keeps an entry whose terminal still runs a program", entry("t5")["active"]
)
check(
    "sweeper ends an entry whose terminal stayed gone",
    not entry("t6")["active"] and entry("t6")["ended_reason"] == "terminal-gone",
)

# ---- hooks that report Cursor's status and keep the inventory right ------------------------
fake_tty = tmp / "tty.out"
fake_tty.write_text("")
tty_env = {"HERDR_SR_TTY": str(fake_tty)}
setterms({"t20": shell(pid=2000, created=9, cwd=folder)})
run_hook(
    "cursor",
    "working",
    {"conversation_id": "CUR20", "workspace_roots": [folder], "prompt": "hi"},
    terminal="t20",
    extra=tty_env,
)
check(
    "Cursor's prompt hook records a session whose start hook never ran",
    entry("t20")["session_id"] == "CUR20" and entry("t20")["source"] == "working",
)
check(
    "Cursor's prompt hook reports working to the terminal",
    fake_tty.read_bytes() == b"\x1b]7501;state=working:app=cursor\x1b\\",
)
fake_tty.write_bytes(b"")
before = entry("t20")["recorded_ms"]
r = run_hook(
    "cursor", "idle", {"conversation_id": "CUR20"}, terminal="t20", extra=tty_env
)
check(
    "Cursor's stop hook reports idle and does not rewrite an unchanged record",
    fake_tty.read_bytes() == b"\x1b]7501;state=idle:app=cursor\x1b\\"
    and entry("t20")["recorded_ms"] == before
    and r.stdout == "",
)
run_hook("claude", "working", {"session_id": "NOPE"}, terminal="t21", extra=tty_env)
check("working and idle do nothing for other agents", "t21" not in S.read()["entries"])


# ---- discovery: sessions the start hook missed (resume) ------------------------------------
def agent_shell(agent, pid, fgpid, status=None):
    argv = {
        "claude": ["claude"],
        "codex": ["codex", "resume"],
        "cursor": [
            "/h/.local/bin/cursor-agent",
            "--use-system-ca",
            "/h/index.js",
            "resume",
        ],
    }[agent]
    t = shell(pid=pid, cwd=folder, fgpid=fgpid)
    t["foreground"]["argv"] = argv
    t["status"] = status
    return t


check(
    "agent_of tells the three agents apart",
    [C.agent_of(agent_shell(a, 1, 2)) for a in ("claude", "codex", "cursor")]
    == ["claude", "codex", "cursor"]
    and C.agent_of(shell()) is None,
)
found_ids = {
    "t30": ("CDX30", "/x/rollout-2026-11-18T10-00-00-CDX30.jsonl"),
    "t31": ("CUR31", None),
    "t32": ("CLA32", "/x/CLA32.jsonl"),
}
looked = []


def fake_discover(agent, info):
    looked.append(info["pid"])
    return found_ids[{3000: "t30", 3100: "t31", 3200: "t32"}[info["pid"]]]


C.discover_session = fake_discover
setterms(
    {
        "t30": agent_shell("codex", 3000, 3001),
        "t31": agent_shell("cursor", 3100, 3101),
        "t32": agent_shell("claude", 3200, 3201),
    }
)
rows = C.agent_terminals()
check(
    "agent_terminals lists the live agent terminals",
    sorted(r[0] for r in rows) == ["t30", "t31", "t32"],
)
seen = {}
check(
    "discovery records Codex, Cursor and Claude sessions found running",
    C.discover_pass(rows, seen) == 3,
)
check(
    "discovered entries carry their session, folder and source",
    (
        entry("t30")["session_id"],
        entry("t31")["session_id"],
        entry("t30")["cwd"],
        entry("t30")["source"],
    )
    == ("CDX30", "CUR31", folder, "discovered"),
)
looked.clear()
check(
    "a second pass right away looks nothing up",
    C.discover_pass(C.agent_terminals(), seen) == 0 and looked == [],
)
seen["t30"]["at"] -= 200_000
seen["t31"]["at"] -= 200_000
seen["t32"]["at"] -= 200_000
C.discover_pass(C.agent_terminals(), seen)
check(
    "later passes re-check Codex and Cursor, but not a recorded Claude session",
    sorted(looked) == [3000, 3100],
)
found_ids["t30"] = (
    "CDX30-NEW",
    "/x/rollout-2026-11-18T10-00-00-CDX30-NEW.jsonl",
)  # the user switched sessions inside Codex
seen["t30"]["at"] -= 200_000
check(
    "a Codex session switched inside the running agent is picked up",
    C.discover_pass(C.agent_terminals(), seen) == 1
    and entry("t30")["session_id"] == "CDX30-NEW",
)


def failing(agent, info):
    raise RuntimeError("no session open yet")


C.discover_session = failing
S.record("t33", "codex", "OLD33", folder)
setterms({"t33": agent_shell("codex", 3300, 3301)})
check(
    "a failed lookup is not fatal and keeps the existing record",
    C.discover_pass(C.agent_terminals(), {}) == 0
    and entry("t33")["session_id"] == "OLD33",
)

# ---- status for Cursor in the agents list --------------------------------------------------
reported = []
S.report_status = lambda info, state, app: (
    reported.append((info["pid"], state, app)) or True
)
rows = [
    ("t31", agent_shell("cursor", 3100, 3101), "cursor"),
    (
        "t34",
        agent_shell("cursor", 3400, 3401, status={"app": "cursor", "state": "working"}),
        "cursor",
    ),
    ("t35", agent_shell("codex", 3500, 3501), "codex"),
]
check(
    "a Cursor with no status gets idle, others are left alone",
    C.status_pass(rows) == 1 and reported == [(3100, "idle", "cursor")],
)

print("\nall checks passed")

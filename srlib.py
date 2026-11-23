"""Shared pieces of herdr-session-restore: the session inventory and the hc helpers.

The inventory maps a Herdr terminal address (HC_TERMINAL_ID, stable across a reboot) to the
agent session that ran in it. Hooks in Claude Code, Codex and Cursor write it; the watcher
reads it after the core brings lost terminals back as plain shells and types the agent's own
resume command into them. Python 3.9, standard library only.
"""

import contextlib
import fcntl
import json
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

HOME = Path.home()
STATE_DIR = Path(
    os.environ.get("HERDR_SR_STATE_DIR") or HOME / ".local/state/herdr-session-restore"
)
SESSIONS = STATE_DIR / "sessions.json"
LOCK = STATE_DIR / "sessions.lock"
DISABLED = (
    STATE_DIR / "disabled"
)  # exists: record and list, but never type a resume command
LOG = STATE_DIR / "watcher.log"
HC = os.environ.get("HERDR_SR_HC") or shutil.which("hc") or str(HOME / ".local/bin/hc")
CORE_SOCK = Path(
    os.environ.get("HERDR_SR_CORE_SOCK")
    or HOME / ".local/state/herdr-core/default/default.sock"
)

STALE_DAYS = (
    30  # Claude removes old transcripts after about a month: older records can't resume
)
SETTLE_MS = (
    3000  # a recovered shell is left alone this long before the resume command is typed
)
END_GRACE_MS = {  # how long a reason must hold before the sweeper closes an entry
    "agent-exited": 30_000,
    "terminal-gone": 60_000,
    "terminal-finished": 60_000,
}
STAGGER_SECONDS = (
    3  # pause between two resumes, so a login doesn't start every agent at once
)
SHELLS = {"bash", "zsh", "sh", "fish", "dash", "ksh", "tcsh", "csh", "nu", "xonsh"}

# The agent's own way to reopen a session (the same commands the Herdr TUI uses).
RESUME = {
    "claude": ["claude", "--resume", "{id}"],
    "codex": [
        "codex",
        "--no-daemon",
        "resume",
        "{id}",
    ],  # --no-daemon as Herdr launches Codex
    "cursor": ["cursor-agent", "--resume", "{id}"],
}


def now_ms():
    return int(time.time() * 1000)


def log(message):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if LOG.exists() and LOG.stat().st_size > 1_000_000:
            LOG.replace(LOG.with_suffix(".log.1"))
        with open(LOG, "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n")
    except Exception:
        pass


# ---- the inventory -----------------------------------------------------------------------


def _load():
    try:
        data = json.loads(SESSIONS.read_text())
        if isinstance(data, dict) and isinstance(data.get("entries"), dict):
            return data
    except Exception:
        pass
    return {"version": 1, "entries": {}}


def _save(data):
    tmp = SESSIONS.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, SESSIONS)


def read():
    """The inventory as it is now (no lock: the file is replaced atomically)."""
    return _load()


@contextlib.contextmanager
def locked():
    """Read-modify-write the inventory under an exclusive lock (hooks can fire together)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _load()
        yield data
        _save(data)


# ---- hc ----------------------------------------------------------------------------------


def hc(*args, timeout=10):
    try:
        r = subprocess.run([HC, *args], capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:  # missing binary, timeout
        return 127, "", str(e)


def hc_json(*args, timeout=10):
    rc, out, _ = hc(*args, "--json", timeout=timeout)
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except Exception:
        return None


def core_up():
    """True when herdr-core is listening. Checks the socket, so it never starts the core: a core
    started from launchd would keep launchd's stripped environment for every terminal."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            s.connect(str(CORE_SOCK))
        return True
    except Exception:
        return False


def terminal(addr):
    """hc get for one terminal, or None when it no longer exists."""
    return hc_json("get", addr)


def is_idle_shell(info):
    """The terminal's own login shell is in the foreground (a prompt, no agent running)."""
    fg = info.get("foreground") or {}
    argv = info.get("argv") or []
    shell = os.path.basename(str(argv[0])).lstrip("-") if argv else ""
    return shell in SHELLS and fg.get("pid") == info.get("pid")


def where(info):
    return ((info or {}).get("labels") or {}).get("where") or ""


def shell_start_ms(pid):
    """When process `pid` started (ms since the epoch), from ps, or None."""
    if not pid:
        return None
    try:
        r = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            env=dict(os.environ, LC_ALL="C"),
        )
        text = r.stdout.strip()
        if r.returncode != 0 or not text:
            return None
        return int(time.mktime(time.strptime(text, "%a %b %d %H:%M:%S %Y")) * 1000)
    except Exception:
        return None


def shell_replaced(entry, info):
    """The terminal's shell is not the one the agent was started from, so the core brought the
    terminal back after a loss (reboot, crash, core restart). The core's API shows only the shell's
    pid: compare it with the pid recorded with the session, and with the process start time (a pid
    can repeat after a reboot)."""
    pid = info.get("pid")
    if entry.get("shell_pid") and pid != entry["shell_pid"]:
        return True
    started = shell_start_ms(pid)
    return bool(started) and started > entry["recorded_ms"]


# ---- entries -----------------------------------------------------------------------------


def record(addr, agent, session_id, cwd, transcript=None, source=None):
    """An agent session started (or was resumed) in terminal `addr`."""
    info = terminal(addr) or {}
    # An agent that doesn't say where it works (Cursor): where its process is running now.
    cwd = cwd or (info.get("foreground") or {}).get("cwd") or info.get("cwd")
    with locked() as data:
        data["entries"][addr] = {
            "terminal": addr,
            "agent": agent,
            "session_id": session_id,
            "cwd": cwd,
            "transcript_path": transcript,
            "source": source,
            "recorded_ms": now_ms(),
            "terminal_created_ms": info.get("created_ms"),
            "shell_pid": info.get("pid"),
            "active": True,
            "ended_ms": None,
            "ended_reason": None,
            "restore": (data["entries"].get(addr) or {}).get("restore")
            if (data["entries"].get(addr) or {}).get("session_id") == session_id
            else None,
        }


def mark_ended(addr, session_id, reason):
    with locked() as data:
        e = data["entries"].get(addr)
        if not e or not e.get("active"):
            return
        if session_id and e.get("session_id") != session_id:
            return  # an older session ending after a newer one started (clear, resume)
        e["active"] = False
        e["ended_ms"] = now_ms()
        e["ended_reason"] = reason or "ended"
        e.pop("ending_since_ms", None)
        e.pop("ending_reason", None)


def update_entry(addr, session_id, **fields):
    """Set fields on an entry (a None value removes the field), if it still holds that session."""
    with locked() as data:
        e = data["entries"].get(addr)
        if not e or e.get("session_id") != session_id:
            return
        for k, v in fields.items():
            if v is None:
                e.pop(k, None)
            else:
                e[k] = v


def ensure_recorded(addr, agent, session_id, cwd, transcript=None, source=None):
    """Record a session unless the inventory already holds exactly it (so a hook that runs on every
    prompt does not rewrite the file each time)."""
    e = read()["entries"].get(addr) or {}
    if e.get("active") and e.get("agent") == agent and e.get("session_id") == session_id:
        return False
    record(addr, agent, session_id, cwd, transcript, source)
    return True


# ---- status for agents that don't report it ------------------------------------------------
# herdr-core takes an agent's status only from the agent itself, as OSC 7501 messages
# (`ESC ] 7501 ; state=idle:app=cursor ESC \`). Claude Code and Codex send them. Cursor Agent does
# not, so it never reaches the sidebar's agents list. Anything may write the message to the
# terminal's pty on the agent's behalf; the core clears the status when the shell is back in front.


def tty_path(pid):
    """The pty a process runs on (/dev/ttysNNN), from ps."""
    if os.environ.get("HERDR_SR_TTY"):  # tests: write to a file instead of a pty
        return os.environ["HERDR_SR_TTY"]
    try:
        r = subprocess.run(["ps", "-o", "tty=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        name = r.stdout.strip()
        if r.returncode == 0 and name and name != "??":
            return "/dev/" + name
    except Exception:
        pass
    return None


def report_status(info, state, app):
    """Send a status report for the terminal described by `info` (hc get). True when it was written."""
    path = tty_path(info.get("pid"))
    if not path:
        return False
    try:
        fd = os.open(path, os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            os.write(fd, f"\x1b]7501;state={state}:app={app}\x1b\\".encode())
        finally:
            os.close(fd)
        return True
    except OSError:
        return False


CURSOR_CHAT = re.compile(r"/\.cursor/chats/[0-9a-f]+/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/store\.db$")


def cursor_chat_id(pid):
    """The chat a running Cursor Agent has open: its store.db sits in ~/.cursor/chats/<workspace>/<chat id>/."""
    try:
        r = subprocess.run(["lsof", "-p", str(pid), "-Fn"], capture_output=True, text=True, timeout=15)
    except Exception:
        return None
    ids = []
    for line in r.stdout.splitlines():
        m = CURSOR_CHAT.search(line[1:]) if line.startswith("n") else None
        if m and m.group(1) not in ids:
            ids.append(m.group(1))
    return ids[0] if ids else None


def resume_command(entry, shell_cwd=None):
    """The line to type into the recovered shell."""
    import shlex

    template = RESUME.get(entry["agent"])
    if not template:
        return None
    cmd = " ".join(
        shlex.quote(p.replace("{id}", entry["session_id"])) for p in template
    )
    cwd = entry.get("cwd")
    if cwd and shell_cwd and os.path.realpath(cwd) != os.path.realpath(shell_cwd):
        cmd = f"cd -- {shlex.quote(cwd)} && {cmd}"
    return cmd

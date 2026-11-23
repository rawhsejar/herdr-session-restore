#!/usr/bin/python3
"""herdr-sessions: the inventory of agent sessions in Herdr terminals, and their restore.

herdr-sessions list [--all] [--json]     what runs where, with full session ids
herdr-sessions commands                  the resume line for each live session (copy and paste)
herdr-sessions bootstrap                 record agents already running (before the hooks existed)
herdr-sessions sweep                     end entries whose agent has exited
herdr-sessions restore [--dry-run] [--only tN]
                                         type the resume command into terminals the core
                                         brought back as plain shells
herdr-sessions enable | disable          turn automatic restore on or off (recording continues)
herdr-sessions forget tN                 drop one terminal's entry
herdr-sessions reopen tN                 make a closed entry active again
herdr-sessions install [--dry-run]       put the hooks into Claude Code, Codex and Cursor
herdr-sessions uninstall                 take them out again
herdr-sessions service install|uninstall|status
                                         the launchd agent that runs the watcher
herdr-sessions status                    everything above at a glance
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import srlib as S

HERE = Path(__file__).resolve().parent
PY = "/usr/bin/python3"
RECORD = HERE / "record_session.py"
MARK = "herdr-session-restore/record_session.py"
LABEL = "dev.local.herdr-session-restore"
PLIST = S.HOME / "Library/LaunchAgents" / f"{LABEL}.plist"
UUID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")
DAY_MS = 86_400_000


def short(path):
    p = str(path or "")
    home = str(S.HOME)
    return "~" + p[len(home) :] if p.startswith(home) else p


def age(ms):
    s = max(0, (S.now_ms() - (ms or 0)) // 1000)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            return f"{s // size}{unit}"
    return f"{s}s"


# ---- state of one entry -----------------------------------------------------------------


def state_of(entry, info):
    if not entry.get("active"):
        return "ended"
    if info is None or info.get("state") != "live":
        return "gone"
    if S.shell_replaced(entry, info):
        done = (entry.get("restore") or {}).get("for_shell_pid") == info.get("pid")
        return "restore-sent" if done else "needs-restore"
    return "running"


# ---- list / commands --------------------------------------------------------------------


def cmd_list(args):
    entries = sorted(
        S.read()["entries"].values(), key=lambda e: e["recorded_ms"], reverse=True
    )
    rows = []
    up = S.core_up()
    for e in entries:
        info = S.terminal(e["terminal"]) if up else None
        state = state_of(e, info) if up or not e.get("active") else "core-off"
        if state == "ended" and not args.all:
            continue
        rows.append({**e, "state": state, "where": S.where(info)})
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print(
            "No agent sessions recorded yet. Run `herdr-sessions bootstrap` for sessions already running."
        )
        return 0
    head = ["TERMINAL", "WHERE", "AGENT", "SESSION", "FOLDER", "STATE", "RECORDED"]
    table = [
        [
            r["terminal"],
            r["where"],
            r["agent"],
            r["session_id"],
            short(r["cwd"]),
            r["state"],
            age(r["recorded_ms"]) + " ago",
        ]
        for r in rows
    ]
    widths = [max(len(str(x[i])) for x in [head] + table) for i in range(len(head))]
    for line in [head] + table:
        print("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(line)).rstrip())
    return 0


def cmd_commands(args):
    shown = 0
    for e in sorted(S.read()["entries"].values(), key=lambda e: e["recorded_ms"]):
        if not e.get("active"):
            continue
        cmd = S.resume_command(
            e, shell_cwd="/"
        )  # always with the cd, so it works from any folder
        print(f"# {e['terminal']}  {e['agent']}  {short(e['cwd'])}\n{cmd}")
        shown += 1
    if not shown:
        print("Nothing recorded.")
    return 0


# ---- sweep ------------------------------------------------------------------------------


def sweep_pass():
    ended = 0
    if not S.core_up():
        return ended
    for e in list(S.read()["entries"].values()):
        if not e.get("active"):
            continue
        info = S.terminal(e["terminal"])
        reason = None
        if info is None:
            reason = "terminal-gone"
        elif info.get("state") != "live":
            reason = "terminal-finished"
        else:
            replaced = S.shell_replaced(e, info)
            # The agent ran in a shell that is still the same one, and that shell is back at its prompt.
            if (
                not replaced
                and S.is_idle_shell(info)
                and S.now_ms() - e["recorded_ms"] > 10_000
            ):
                reason = "agent-exited"
        addr, sid, now = e["terminal"], e["session_id"], S.now_ms()
        if reason is None:
            if e.get("ending_since_ms"):
                S.update_entry(addr, sid, ending_since_ms=None, ending_reason=None)
            continue
        # A glimpse is not enough: a machine shutting down kills the agent before its shell, and
        # a core that just restarted shows its terminals as finished before it recovers them. The
        # reason has to hold for a whole grace period before the entry is closed.
        since = e.get("ending_since_ms") if e.get("ending_reason") == reason else None
        if since is None:
            S.update_entry(addr, sid, ending_since_ms=now, ending_reason=reason)
            continue
        if now - since >= S.END_GRACE_MS[reason]:
            S.mark_ended(addr, sid, reason)
            S.log(f"ended {addr} {e['agent']} {sid}: {reason}")
            ended += 1
    return ended


def cmd_sweep(args):
    print(f"ended {sweep_pass()} entries")
    return 0


# ---- restore ----------------------------------------------------------------------------


def restore_pass(dry_run=False, only=None):
    actions = []
    if not S.core_up():
        return actions
    if S.DISABLED.exists() and not dry_run:
        return actions
    now = S.now_ms()
    entries = [e for e in S.read()["entries"].values() if e.get("active")]
    for e in sorted(entries, key=lambda e: e["recorded_ms"]):
        addr = e["terminal"]
        if only and addr != only:
            continue
        info = S.terminal(addr)
        if not info or info.get("state") != "live":
            continue
        if not S.shell_replaced(e, info):
            continue  # the shell the agent started from is still there
        pid = info.get("pid")
        if (
            e.get("terminal_created_ms")
            and info.get("created_ms") != e["terminal_created_ms"]
        ):
            continue  # the address now belongs to a different terminal
        if (e.get("restore") or {}).get("for_shell_pid") == pid:
            continue  # already tried in this shell
        started = S.shell_start_ms(pid) or now
        if now - started < S.SETTLE_MS or not S.is_idle_shell(info):
            continue  # still starting, or something is running in it already
        verdict = None
        if now - e["recorded_ms"] > S.STALE_DAYS * DAY_MS:
            verdict = "stale"
        elif not e.get("cwd") or not os.path.isdir(e["cwd"]):
            verdict = "folder-missing"
        cmd = (
            None
            if verdict
            else S.resume_command(e, (info.get("foreground") or {}).get("cwd"))
        )
        action = {
            "terminal": addr,
            "agent": e["agent"],
            "session": e["session_id"],
            "command": cmd,
            "skip": verdict,
        }
        actions.append(action)
        if dry_run:
            continue
        result = verdict or "sent"
        if cmd:
            rc, _, err = S.hc("send", addr, cmd)
            if rc != 0:
                result = f"send-failed: {err.strip()[:80]}"
        with S.locked() as data:
            entry = data["entries"].get(addr)
            if entry:
                entry["restore"] = {
                    "for_shell_pid": pid,
                    "attempted_ms": S.now_ms(),
                    "result": result,
                }
                if verdict:
                    # Can't be resumed (too old, folder gone): close the entry instead of retrying.
                    entry.update(
                        active=False, ended_ms=S.now_ms(), ended_reason=verdict
                    )
                elif result == "sent":
                    # The resumed agent runs in this shell now: make it the baseline, so the sweeper
                    # sees a later exit and a later reboot restores the session again.
                    entry["shell_pid"] = pid
                    entry["recorded_ms"] = S.now_ms()
                    entry.pop("ending_since_ms", None)
                    entry.pop("ending_reason", None)
        S.log(f"restore {addr} {e['agent']} {e['session_id']}: {result} ({cmd})")
        if result == "sent":
            time.sleep(S.STAGGER_SECONDS)
    return actions


def cmd_restore(args):
    if S.DISABLED.exists() and not args.dry_run:
        print(
            "Automatic restore is disabled (herdr-sessions enable). Use --dry-run to preview."
        )
    actions = restore_pass(dry_run=args.dry_run, only=args.only)
    if not actions:
        print(
            "Nothing to restore: no terminal was brought back as a shell after its agent was recorded."
        )
    for a in actions:
        print(
            f"{'would type' if args.dry_run else 'typed'} into {a['terminal']}: {a['command'] or 'skipped (' + a['skip'] + ')'}"
        )
    return 0


# ---- bootstrap --------------------------------------------------------------------------


def agent_of(info):
    fg = info.get("foreground") or {}
    argv = fg.get("argv") or []
    text = " ".join(argv)
    base = os.path.basename(argv[0]) if argv else ""
    if "cursor-agent" in text:
        return "cursor"
    if base == "claude" or re.search(r"(^|/)claude(\s|$)", text):
        return "claude"
    if (
        base == "codex"
        or re.search(r"(^|/)codex(\.js)?(\s|$)", text)
        or "/@openai/codex" in text
    ):
        return "codex"
    return None


def tui_session(agent, pid, cwd):
    """The session file a running Claude or Codex TUI has open, through herdr-chat's host (the same
    lookup its "Continue as chat" uses)."""
    cfg = json.loads((S.HOME / ".config/herdr-app/mods/chat/config.json").read_text())
    env = dict(os.environ)
    if cfg.get("node"):
        env["HERDR_CHAT_NODE"] = cfg["node"]
    r = subprocess.run(
        [
            cfg["host"],
            "tui-session",
            "--provider",
            agent,
            "--pid",
            str(pid),
            "--cwd",
            cwd,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:160])
    return json.loads(r.stdout)["session"]


def session_id_of(agent, path):
    stem = Path(path).stem
    if agent == "claude":
        return stem
    m = UUID.search(stem)
    return m.group(1) if m else None


def discover_session(agent, info):
    """(session id, transcript path) of the session an agent process has open now."""
    fg = info["foreground"]
    if agent == "cursor":
        return S.cursor_chat_id(fg["pid"]), None
    path = tui_session(agent, fg["pid"], fg.get("cwd") or info.get("cwd") or "/")
    return session_id_of(agent, path), path


def agent_terminals():
    """(address, hc get, agent) for every live terminal with an agent in front."""
    out = []
    listing = S.hc_json("ls") or []
    for row in listing if isinstance(listing, list) else listing.get("terminals", []):
        info = S.terminal(row["address"])
        if not info or info.get("state") != "live" or not info.get("foreground"):
            continue
        agent = agent_of(info)
        if agent:
            out.append((row["address"], info, agent))
    return out


RECHECK_RECORDED_MS = (
    120_000  # a recorded Codex or Cursor session is looked at again this often
)
RETRY_UNRECORDED_MS = 60_000  # an agent with no record is looked up this often


def discover_pass(rows, seen, force=False, say=None):
    """Record the sessions the hooks missed. Codex and Cursor do not run their start hook when a
    session is resumed (`codex resume`, `cursor-agent resume`), and agents started before the hooks
    existed never ran it. This looks at what each running agent has open. Claude's hook also runs
    on resume, so a Claude session that is recorded is not looked up again."""
    found = 0
    now = S.now_ms()
    inventory = S.read()["entries"]
    for addr, info, agent in rows:
        fg = info["foreground"]
        entry = inventory.get(addr) or {}
        recorded = bool(entry.get("active")) and entry.get("agent") == agent
        if recorded and agent == "claude" and not force:
            continue
        last = seen.get(addr) or {}
        wait = RECHECK_RECORDED_MS if recorded else RETRY_UNRECORDED_MS
        if (
            not force
            and last.get("pid") == fg["pid"]
            and now - last.get("at", 0) < wait
        ):
            continue
        seen[addr] = {"pid": fg["pid"], "at": now}
        try:
            sid, path = discover_session(agent, info)
        except Exception as e:
            if say:
                say(
                    f"{addr}: {agent} is running but its session could not be read ({e})"
                )
            continue
        if not sid:
            if say:
                say(f"{addr}: {agent} is running but no session is open yet")
            continue
        if recorded and entry.get("session_id") == sid:
            if say:
                say(f"{addr}: {agent} {sid} already recorded")
            continue
        S.record(addr, agent, sid, fg.get("cwd") or info.get("cwd"), path, "discovered")
        S.log(f"discovered {addr} {agent} {sid}")
        if say:
            say(f"{addr}: {agent} {sid} recorded ({short(fg.get('cwd'))})")
        found += 1
    return found


def status_pass(rows):
    """Cursor Agent never reports its status, so the sidebar's agents list can't show it. Report
    `idle` for a Cursor with no status yet; its hooks report working and idle from then on."""
    sent = 0
    for addr, info, agent in rows:
        if agent == "cursor" and info.get("status") is None:
            if S.report_status(info, "idle", "cursor"):
                S.log(f"reported idle for cursor in {addr}")
                sent += 1
    return sent


def cmd_bootstrap(args):
    if not S.core_up():
        print("herdr-core is not running.")
        return 1
    found = discover_pass(agent_terminals(), {}, force=True, say=print)
    status_pass(agent_terminals())
    print(f"recorded {found}")
    return 0


# ---- enable / disable / forget ----------------------------------------------------------


def cmd_enable(args):
    S.DISABLED.unlink(missing_ok=True)
    print("Automatic restore is on.")
    return 0


def cmd_disable(args):
    S.STATE_DIR.mkdir(parents=True, exist_ok=True)
    S.DISABLED.write_text(
        "remove this file (or run `herdr-sessions enable`) to turn automatic restore back on\n"
    )
    print("Automatic restore is off. Sessions are still recorded.")
    return 0


def cmd_reopen(args):
    """Make an entry that was closed (by mistake, or by an older version) active again."""
    with S.locked() as data:
        e = data["entries"].get(args.terminal)
        if not e:
            print("no entry for that terminal")
            return 1
        e.update(active=True, ended_ms=None, ended_reason=None)
        for k in ("ending_since_ms", "ending_reason", "restore"):
            e.pop(k, None)
    print(
        f"{args.terminal} is active again; the watcher resumes it if its terminal is a recovered shell"
    )
    return 0


def cmd_forget(args):
    with S.locked() as data:
        gone = data["entries"].pop(args.terminal, None)
    print("dropped" if gone else "no entry for that terminal")
    return 0


# ---- install / uninstall hooks ----------------------------------------------------------


def hook_command(agent, mode):
    return f"{PY} {RECORD} {agent} {mode}"


def ours(entry):
    """A hook entry written by this tool (Claude and Codex nest commands, Cursor does not)."""
    if MARK in str(entry.get("command", "")):
        return True
    return any(
        MARK in str(h.get("command", ""))
        for h in entry.get("hooks", [])
        if isinstance(h, dict)
    )


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def write_json(path, data, indent=2):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=indent) + "\n")
    os.replace(tmp, path)


def backup(paths):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = S.STATE_DIR / "backups" / stamp
    for p in paths:
        if p.exists():
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest / p.name.replace("/", "_"))
    return dest


CLAUDE = S.HOME / ".claude/settings.json"
CODEX_HOOKS = S.HOME / ".codex/hooks.json"
CODEX_CONFIG = S.HOME / ".codex/config.toml"
CURSOR_HOOKS = S.HOME / ".cursor/hooks.json"
INSTALLED = S.STATE_DIR / "installed.json"


def plan_claude(settings, add=True):
    hooks = settings.setdefault("hooks", {})
    changed = False
    for event, matcher, mode in (
        ("SessionStart", "^(startup|resume|clear|compact|fork)$", "start"),
        ("SessionEnd", None, "end"),
    ):
        entries = hooks.setdefault(event, [])
        kept = [e for e in entries if not ours(e)]
        if add:
            entry = {
                "hooks": [
                    {
                        "type": "command",
                        "command": hook_command("claude", mode),
                        "timeout": 10,
                    }
                ]
            }
            if matcher:
                entry = {"matcher": matcher, **entry}
            kept.append(entry)
        if kept != entries:
            changed = True
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    if not hooks:
        settings.pop("hooks", None)
    return changed


def plan_codex_hooks(file, add=True):
    hooks = file.setdefault("hooks", {})
    entries = hooks.setdefault("SessionStart", [])
    kept = [e for e in entries if not ours(e)]
    if add:
        kept.append(
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": hook_command("codex", "start"),
                        "timeout": 10,
                    }
                ]
            }
        )
    changed = kept != entries
    if kept:
        hooks["SessionStart"] = kept
    else:
        hooks.pop("SessionStart", None)
    if not hooks:
        file.pop("hooks", None)
    return changed


CURSOR_EVENTS = {
    "sessionStart": "start",
    "beforeSubmitPrompt": "working",
    "stop": "idle",
}


def plan_cursor_hooks(file, add=True):
    file.setdefault("version", 1)
    hooks = file.setdefault("hooks", {})
    changed = False
    for event, mode in CURSOR_EVENTS.items():
        entries = hooks.setdefault(event, [])
        kept = [e for e in entries if not ours(e)]
        if add:
            kept.append({"command": hook_command("cursor", mode)})
        changed = changed or kept != entries
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    if not hooks:
        file.pop("hooks", None)
    return changed


def toml_features_hooks(text, on):
    """Set `hooks = true` under [features] in a TOML text. Returns (text, added) where added tells
    whether this call created the key (so uninstall removes only what it added)."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, l in enumerate(lines) if l.strip() == "[features]"), None)
    if on:
        if start is None:
            sep = (
                ""
                if not text or text.endswith("\n\n")
                else ("\n" if text.endswith("\n") else "\n\n")
            )
            return text + sep + "[features]\nhooks = true\n", True
        end = next(
            (
                i
                for i in range(start + 1, len(lines))
                if lines[i].lstrip().startswith("[")
            ),
            len(lines),
        )
        for i in range(start + 1, end):
            if re.match(r"\s*hooks\s*=", lines[i]):
                if re.match(r"\s*hooks\s*=\s*true\b", lines[i]):
                    return text, False  # already on
                lines[i] = (
                    "hooks = true\n"  # off: the hook can't run with it, switch it on (not ours to remove)
                )
                return "".join(lines), False
        lines.insert(start + 1, "hooks = true\n")
        return "".join(lines), True
    if start is None:
        return text, False
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")),
        len(lines),
    )
    kept = [
        l
        for i, l in enumerate(lines)
        if not (start < i < end and re.match(r"\s*hooks\s*=\s*true\s*$", l))
    ]
    return "".join(kept), True


def cmd_install(args):
    dry = args.dry_run
    if MARK not in str(RECORD):
        # shortcut: hooks are found again by this folder name, so a differently named clone would
        # leave duplicates behind on every install; upgrade by matching on the script path instead.
        print(
            "Clone the repo into a folder named herdr-session-restore, then run install again."
        )
        return 1
    todo = []
    claude = read_json(CLAUDE, None)
    if claude is None:
        print(f"skip Claude Code: {CLAUDE} not found")
    else:
        before = json.dumps(claude, sort_keys=True)
        plan_claude(claude)
        todo.append(
            (
                "Claude Code",
                CLAUDE,
                claude,
                before != json.dumps(claude, sort_keys=True),
            )
        )
    if CODEX_CONFIG.exists() or CODEX_HOOKS.parent.exists():
        file = read_json(CODEX_HOOKS, {})
        before = json.dumps(file, sort_keys=True)
        plan_codex_hooks(file)
        todo.append(
            (
                "Codex hooks.json",
                CODEX_HOOKS,
                file,
                before != json.dumps(file, sort_keys=True),
            )
        )
    else:
        print("skip Codex: ~/.codex not found")
    if CURSOR_HOOKS.parent.exists():
        file = read_json(CURSOR_HOOKS, {})
        before = json.dumps(file, sort_keys=True)
        plan_cursor_hooks(file)
        todo.append(
            (
                "Cursor hooks.json",
                CURSOR_HOOKS,
                file,
                before != json.dumps(file, sort_keys=True),
            )
        )
    else:
        print("skip Cursor: ~/.cursor not found")
    toml_text = CODEX_CONFIG.read_text() if CODEX_CONFIG.exists() else ""
    new_toml, added = (
        toml_features_hooks(toml_text, True)
        if CODEX_HOOKS.parent.exists()
        else (toml_text, False)
    )
    for name, path, _data, changed in todo:
        print(
            f"{'would change' if dry else 'change'}: {name} ({short(path)})"
            if changed
            else f"unchanged: {name}"
        )
    if new_toml != toml_text:
        print(
            f"{'would add' if dry else 'add'}: [features] hooks = true to {short(CODEX_CONFIG)}"
        )
    if dry:
        return 0
    dest = backup([CLAUDE, CODEX_HOOKS, CODEX_CONFIG, CURSOR_HOOKS])
    for name, path, data, changed in todo:
        if changed:
            write_json(path, data)
    if new_toml != toml_text:
        CODEX_CONFIG.write_text(new_toml)
    state = read_json(INSTALLED, {})
    if added:
        state["codex_features_hooks_added"] = True
    state["installed_ms"] = S.now_ms()
    write_json(INSTALLED, state)
    print(f"backups of the originals: {short(dest)}")
    print(
        "Agents read their hooks when they start: sessions already running are picked up by `herdr-sessions bootstrap`."
    )
    return 0


def cmd_uninstall(args):
    dest = backup([CLAUDE, CODEX_HOOKS, CODEX_CONFIG, CURSOR_HOOKS])
    claude = read_json(CLAUDE, None)
    if claude is not None and plan_claude(claude, add=False):
        write_json(CLAUDE, claude)
        print("removed: Claude Code hooks")
    for path, plan, name in (
        (CODEX_HOOKS, plan_codex_hooks, "Codex hooks"),
        (CURSOR_HOOKS, plan_cursor_hooks, "Cursor hooks"),
    ):
        file = read_json(path, None)
        if file is not None and plan(file, add=False):
            write_json(path, file)
            print(f"removed: {name}")
    state = read_json(INSTALLED, {})
    if state.get("codex_features_hooks_added") and CODEX_CONFIG.exists():
        text, _ = toml_features_hooks(CODEX_CONFIG.read_text(), False)
        CODEX_CONFIG.write_text(text)
        print("removed: [features] hooks = true (this tool had added it)")
    INSTALLED.unlink(missing_ok=True)
    print(f"backups of the files before this change: {short(dest)}")
    return 0


# ---- launchd service --------------------------------------------------------------------


def plist_text():
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array><string>{PY}</string><string>{HERE / "watcher.py"}</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ProcessType</key><string>Background</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>{S.HOME}/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>HOME</key><string>{S.HOME}</string>
  </dict>
  <key>StandardOutPath</key><string>{S.STATE_DIR / "launchd.out"}</string>
  <key>StandardErrorPath</key><string>{S.STATE_DIR / "launchd.err"}</string>
</dict>
</plist>
"""


def launchctl(*args):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def service_loaded():
    return launchctl("print", f"gui/{os.getuid()}/{LABEL}").returncode == 0


def cmd_service(args):
    domain = f"gui/{os.getuid()}"
    if args.action == "install":
        S.STATE_DIR.mkdir(parents=True, exist_ok=True)
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        PLIST.write_text(plist_text())
        launchctl("bootout", f"{domain}/{LABEL}")
        # bootout returns before the old instance is gone, and a bootstrap that runs too early fails
        # with an I/O error and leaves nothing running: wait for the unload, then retry.
        for _ in range(20):
            if not service_loaded():
                break
            time.sleep(0.5)
        r = None
        for attempt in range(4):
            r = launchctl("bootstrap", domain, str(PLIST))
            if r.returncode == 0:
                break
            time.sleep(2)
        if r.returncode != 0:
            print("launchctl bootstrap failed:", r.stderr.strip())
            return 1
        print(f"service installed and started ({short(PLIST)})")
    elif args.action == "uninstall":
        launchctl("bootout", f"{domain}/{LABEL}")
        PLIST.unlink(missing_ok=True)
        print("service stopped and removed")
    else:
        print(
            "service:",
            "running" if service_loaded() else "not loaded",
            f"({short(PLIST)})",
        )
    return 0


# ---- status -----------------------------------------------------------------------------


def cmd_status(args):
    data = S.read()["entries"]
    active = [e for e in data.values() if e.get("active")]
    claude = read_json(CLAUDE, {}) or {}
    has = lambda entries: any(ours(e) for e in (entries or []))
    print(f"core running           : {'yes' if S.core_up() else 'no'}")
    print(
        f"automatic restore      : {'OFF (disabled flag set)' if S.DISABLED.exists() else 'on'}"
    )
    print(f"watcher service        : {'running' if service_loaded() else 'not loaded'}")
    print(
        f"Claude hooks           : {'installed' if has((claude.get('hooks') or {}).get('SessionStart')) else 'missing'}"
    )
    print(
        f"Codex hooks            : {'installed' if has(((read_json(CODEX_HOOKS, {}) or {}).get('hooks') or {}).get('SessionStart')) else 'missing'}"
    )
    print(
        f"Cursor hooks           : {'installed' if has(((read_json(CURSOR_HOOKS, {}) or {}).get('hooks') or {}).get('sessionStart')) else 'missing'}"
    )
    print(
        f"sessions recorded      : {len(active)} active, {len(data) - len(active)} ended"
    )
    print(f"state folder           : {short(S.STATE_DIR)}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="herdr-sessions", description=__doc__.split("\n\n")[0]
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("list")
    s.add_argument("--all", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_list)
    sub.add_parser("commands").set_defaults(fn=cmd_commands)
    sub.add_parser("bootstrap").set_defaults(fn=cmd_bootstrap)
    sub.add_parser("sweep").set_defaults(fn=cmd_sweep)
    s = sub.add_parser("restore")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--only")
    s.set_defaults(fn=cmd_restore)
    sub.add_parser("enable").set_defaults(fn=cmd_enable)
    sub.add_parser("disable").set_defaults(fn=cmd_disable)
    s = sub.add_parser("reopen")
    s.add_argument("terminal")
    s.set_defaults(fn=cmd_reopen)
    s = sub.add_parser("forget")
    s.add_argument("terminal")
    s.set_defaults(fn=cmd_forget)
    s = sub.add_parser("install")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_install)
    sub.add_parser("uninstall").set_defaults(fn=cmd_uninstall)
    s = sub.add_parser("service")
    s.add_argument("action", choices=["install", "uninstall", "status"])
    s.set_defaults(fn=cmd_service)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

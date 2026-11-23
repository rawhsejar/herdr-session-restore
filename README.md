# herdr-session-restore

Resume Claude Code, Codex and Cursor Agent sessions in the [Herdr](https://herdr.dev) desktop app after a reboot or a core restart.

After a loss, herdr-core brings every terminal back as a plain shell in its last folder. The agent that ran there is gone, and nothing says which conversation lived in which terminal. This tool records that link while the agents run, and types each agent's own resume command into the recovered shell for you. The Herdr TUI did this with `resume_agents_on_restore`.

Herdr plans a native version. The maintainer said core will support resume once agents can report how to resume through OSC 7501 ([herdr-core#13](https://github.com/herdrdev/herdr-core/issues/13), [#15](https://github.com/herdrdev/herdr-core/issues/15), [herdr-app#974](https://github.com/herdrdev/herdr-app/issues/974)). Until then this works from outside, with hooks and a small background watcher.

Example output:

```
$ herdr-sessions list
TERMINAL  WHERE  AGENT   SESSION                               FOLDER      STATE    RECORDED
t12       api    claude  11111111-2222-4333-8444-555555555555  ~/code/api  running  2h ago
t14       web    codex   66666666-7777-4888-9999-000000000000  ~/code/web  running  41m ago
t17       web    cursor  aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee  ~/code/web  running  9m ago
```

## What you get

- After a reboot, open Herdr as usual. Within 20 to 40 seconds of the core coming up, each recovered terminal runs `claude --resume <id>`, `codex --no-daemon resume <id>` or `cursor-agent --resume <id>` in its own folder.
- A session you ended on purpose stays ended. Only sessions that were still open when the machine went down come back.
- Codex and Cursor sessions you reopened with `resume` are found too, since those agents do not run their start hook on resume.
- Cursor Agent shows up in Herdr's Idle and Working lists. It does not report its status to the core by itself, so the tool reports it on its behalf.

## Requirements

- macOS. The watcher is a launchd agent, and the code uses `ps` and `lsof`.
- Herdr desktop app 0.1.16 with `hc` 0.1.22 (the versions it was built and tested on).
- `/usr/bin/python3` (3.9 is enough). Standard library only.
- At least one of Claude Code, Codex or Cursor Agent.
- Optional: the [herdr-chat](https://github.com/ChmaraX/herdr-chat) mod (0.3.0 was tested). Its `tui-session` lookup finds the session a running Claude or Codex TUI has open, which `bootstrap` and Codex discovery use. Without it, a Codex session you reopened with `resume` is not recorded, and agents that started before the hooks existed are not picked up. Everything else works without it.

## Install

The folder must be named `herdr-session-restore`. The installer finds its own hooks again by that name.

```sh
git clone https://github.com/rawhsejar/herdr-session-restore ~/.local/share/herdr-session-restore
mkdir -p ~/.local/bin
ln -s ~/.local/share/herdr-session-restore/sessions_cli.py ~/.local/bin/herdr-sessions

herdr-sessions install --dry-run   # shows what it would change
herdr-sessions install             # adds the hooks, backs up the originals first
herdr-sessions service install     # starts the watcher at login
herdr-sessions bootstrap           # records agents that are already running
herdr-sessions status
```

Agents read their hooks when they start, so sessions that are already open are picked up by `bootstrap`, and new ones by the hooks.

Codex asks you to approve a new hook. Choose "Review hooks" the first time it shows its review screen and trust this one. A resumed Codex session stops at that screen until you answer it.

## Commands

```
herdr-sessions list [--all] [--json]   what runs where, with full session ids
herdr-sessions commands                the resume line for each live session
herdr-sessions status                  hooks, service, core and counts
herdr-sessions restore [--dry-run]     type the resume commands now, for recovered shells
herdr-sessions bootstrap               record the agents that are running now
herdr-sessions sweep                   close entries whose agent has exited
herdr-sessions enable | disable        automatic restore on or off (recording continues)
herdr-sessions forget tN               drop one terminal's entry
herdr-sessions reopen tN               make a closed entry active again
herdr-sessions install | uninstall     add or remove the agent hooks
herdr-sessions service install | uninstall | status
```

## How it works

1. A hook in each agent runs `record_session.py` at session start. It reads the session id from the agent's JSON on stdin and the terminal address from `HC_TERMINAL_ID`, and writes both to `~/.local/state/herdr-session-restore/sessions.json`. Outside Herdr it does nothing, prints nothing and always exits 0.
2. Claude's `SessionEnd` hook closes an entry only for a deliberate exit (`prompt_input_exit` or `logout`). A shutdown reports `other`, and that is when the session has to come back.
3. The watcher runs every 20 seconds, and only while herdr-core is already running. It never starts the core, because a core started from launchd would keep launchd's stripped environment for every terminal.
4. The sweeper closes an entry once its agent has exited. A reason has to hold for a grace period first (30 seconds at the prompt, 60 seconds for a gone or finished terminal), because a shutting-down machine kills the agent before its shell and a core that just restarted shows its terminals as finished before it recovers them.
5. Discovery records the sessions the hooks missed, by asking `chat tui-session` for Claude and Codex, and from the open `~/.cursor/chats/<workspace>/<chat id>/store.db` for Cursor.
6. The restorer finds terminals whose shell is not the one the agent started from. `hc get` does not show that directly, so it compares the shell's pid and process start time with what was recorded. It waits for the shell to settle, adds a `cd` if the folder differs, types the resume command, and starts agents 3 seconds apart. Each recovered shell is tried once. Entries older than 30 days or with a missing folder are skipped.

## What it changes

`herdr-sessions install` backs up each file to `~/.local/state/herdr-session-restore/backups/` before editing it, and `uninstall` reverses it.

| File | Change |
| --- | --- |
| `~/.claude/settings.json` | `SessionStart` and `SessionEnd` hooks |
| `~/.codex/hooks.json` | `SessionStart` hook |
| `~/.codex/config.toml` | `hooks = true` under `[features]`, removed again on uninstall only if this tool added it |
| `~/.cursor/hooks.json` | `sessionStart`, `beforeSubmitPrompt` and `stop` hooks |
| `~/Library/LaunchAgents/dev.local.herdr-session-restore.plist` | the watcher, from `service install` |

## Limits

- A resume brings the conversation back. A half-finished tool call or an unsent draft is lost.
- `claude --resume` needs the original folder and an unexpired transcript. Claude keeps them for about 30 days.
- Sessions on other machines (`hc --on`) and herdr-chat chat tabs are not covered.
- Cursor Agent also runs Claude Code's hooks. The Claude hook ignores those calls, otherwise a Cursor session would be recorded as a Claude one.
- Many agents resume at once after a login. They start 3 seconds apart to spread the load.
- The resume commands are fixed in `srlib.py` (`RESUME`). If you start an agent through an alias with extra flags, edit that table.

## Tested

- 51 automated checks run against an isolated state folder and a fake `hc`: `/usr/bin/python3 tests/test_restore.py`. They touch no real terminal, core or inventory.
- Claude, Codex and Cursor were each started in a disposable terminal, recorded by their hook, then killed with `kill -9` on the shell and the agent, which the core treats like a reboot. The watcher resumed each conversation by itself.
- One real reboot on macOS. The Codex session came back by itself. The Claude sessions did not, because their `SessionEnd` hooks reported `other` at shutdown and closed the entries. That led to the deliberate-exit rule and the grace periods above. A second real reboot with the fixed code has not been run yet.

## Uninstall

```sh
herdr-sessions service uninstall
herdr-sessions uninstall
rm ~/.local/bin/herdr-sessions
rm -r ~/.local/share/herdr-session-restore ~/.local/state/herdr-session-restore
```

## License

MIT

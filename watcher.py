#!/usr/bin/python3
"""The loop launchd runs: every 20 seconds, end entries whose agent has exited and resume the
sessions of terminals the core brought back as plain shells.

It only acts while herdr-core is already running, and never starts it. After a reboot the core
starts when you open Herdr; the next pass then finds the recovered shells and resumes the agents.
"""

import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sessions_cli as C
import srlib as S

INTERVAL = 20


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    S.log("watcher started")
    was_up = None
    seen = {}  # when each terminal's agent session was last looked up
    while True:
        try:
            up = S.core_up()
            if up != was_up:
                S.log("core is up" if up else "core is not running")
                was_up = up
            if up:
                C.sweep_pass()
                rows = C.agent_terminals()
                C.discover_pass(rows, seen)
                C.status_pass(rows)
                C.restore_pass()
        except Exception as e:  # a bad pass must not stop the loop
            S.log(f"error: {e!r}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()

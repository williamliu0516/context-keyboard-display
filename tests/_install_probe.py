#!/usr/bin/env python3
"""Run display.install() with every effect stubbed, and report what it called.

    python3 tests/_install_probe.py            # this host
    CKD_PLATFORM=Linux python3 tests/_install_probe.py

Used by test_platform.py, not by hand. It exists because the one thing worth
asserting about the installer -- *which* service manager and *whether* the
hotkey listener -- is the one thing you cannot assert by running it for real:
`launchctl bootout` on this label would stop the daemon actually driving this
machine's panel. So every side effect is replaced with a recorder, and the
routing is what comes out. Nothing here touches launchd, systemd, pip or the
network, and the config path is redirected into a temp dir.
"""

import json
import os
import sys
import tempfile
import types

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_DIR)

import display  # noqa: E402
import service  # noqa: E402

calls = []


def record(name, result=True):
    def stub(*args, **kwargs):
        calls.append([name] + [str(a) for a in args])
        return result
    return stub


# keys.py imports Quartz on the macOS path and installs a real launchd agent;
# a stub module in sys.modules is what makes the Darwin branch safe to run.
keys_stub = types.ModuleType("keys")
keys_stub.install_agent = record("keys.install_agent")
keys_stub.show_permission = record("keys.show_permission")
sys.modules["keys"] = keys_stub

display.ensure_venv = lambda cfg: "/fake/venv/bin/python3"
display.hooks_registered = lambda: True
display.write_plist = record("launchd.write_plist")
display.launchctl = record("launchctl")
service.install_service = record("systemd.install_service")
service.status_commands = lambda log_path=None: ["<status>"]

tmp = tempfile.mkdtemp()
display.CONFIG_PATH = os.path.join(tmp, "config.yaml")
open(display.CONFIG_PATH, "w").close()      # exists, so install() leaves it be
display.LOG_PATH = os.path.join(tmp, "display.log")

display.install({"upstream_path": tmp}, with_agent="--no-agent" not in sys.argv)

json.dump({"platform": service.system_name(), "calls": calls}, sys.stdout)

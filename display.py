#!/usr/bin/env python3
"""Context-aware display daemon for a keyboard's 142x428 image panel.

Chooses between three screens — Claude session detail (working / waiting /
between-turns), the multi-session Sessions switchboard, and Idle — from the
live state of your Claude Code sessions, renders the winner, and POSTs it to
the keyboard. Supersedes claude-code-keyboard-status as the panel's pusher;
that repo stays installed as the owner of the session hooks and is imported
here as a library.

    python3 display.py --install          venv, launchd agent, config
    python3 display.py --daemon           run the loop in the foreground
    python3 display.py --daemon --dry-run render to out/frame.jpg, POST nothing
    python3 display.py --once             one tick (render + push), then exit
    python3 display.py --preview [DIR]    render the approved-mockup dataset
    python3 display.py --live [PATH]      render the real current state once
    python3 display.py --status           dump sessions + chosen screen as JSON
    python3 display.py mode sessions      manual override (auto|claude|sessions|idle)
"""

import json
import os
import sys
import time

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
CLAUDE_DIR = os.path.expanduser("~/.claude")
CONFIG_PATH = os.path.join(CLAUDE_DIR, "context-keyboard-display.yaml")
CONTROL_PATH = os.path.join(CLAUDE_DIR, "context-keyboard-display-control.json")
LOG_PATH = os.path.join(CLAUDE_DIR, "context-keyboard-display.log")
VENV_PATH = os.path.join(REPO_DIR, ".venv")
LAUNCH_LABEL = "com.williamliu.context-keyboard-display"
LAUNCH_PLIST = os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LAUNCH_LABEL)
# The old daemon's agent: exactly one process may push to the panel, so the
# new daemon boots this out on startup and --uninstall restores it.
OLD_LABEL = "com.williamliu.claude-keyboard-status"
OLD_PLIST = os.path.expanduser("~/Library/LaunchAgents/%s.plist" % OLD_LABEL)
OLD_CONFIG_PATH = os.path.join(CLAUDE_DIR, "keyboard-status.json")

DEFAULTS = {
    "url": "http://192.168.0.12/image/upload",
    "width": 142,
    "height": 428,
    "tick_seconds": 5.0,
    "heartbeat_seconds": 300.0,
    "offline_backoff_seconds": 20.0,
    "http_timeout_seconds": 4.0,
    "jpeg_quality": 95,
    # Transcript freshness that counts as "actively working" without a hook.
    "active_seconds": 45.0,
    # Sessions quieter than this stop existing for the display entirely.
    "session_ttl_seconds": 6 * 3600.0,
    # The engagement window: a finished session keeps showing between-turns
    # (rather than dropping to Idle) for this long after its last activity.
    # REVISED_PLAN.md said session_ttl_seconds here, but a 6-hour DONE screen
    # would mean the Idle clock never appears; 30 minutes matches how long
    # "I'm still in this session, just reading" plausibly lasts.
    "engaged_seconds": 1800.0,
    "usage_poll_seconds": 60.0,
    "diff_poll_seconds": 5.0,
    "toast_seconds": 4.0,
    # Where the claude-code-keyboard-status checkout lives (library import).
    "upstream_path": "~/projects/claude-code-keyboard-status",
    # display.aliases in the YAML lands here: {repo-basename: short-name}.
    "aliases": {},
}


def load_config():
    """Defaults, then ~/.claude/context-keyboard-display.yaml.

    PyYAML may be absent (e.g. running --preview from a bare python): the
    config file is then skipped and the keyboard URL is seeded from the old
    repo's JSON config, which points at the same physical keyboard.
    """
    config = dict(DEFAULTS)
    try:
        with open(OLD_CONFIG_PATH) as handle:
            old = json.load(handle)
        if isinstance(old, dict) and isinstance(old.get("url"), str):
            config["url"] = old["url"]
    except (OSError, ValueError):
        pass
    try:
        import yaml
    except ImportError:
        return config
    try:
        with open(CONFIG_PATH) as handle:
            loaded = yaml.safe_load(handle)
    except (OSError, ValueError, yaml.YAMLError):
        return config
    if not isinstance(loaded, dict):
        return config
    display = loaded.pop("display", None)
    if isinstance(display, dict) and isinstance(display.get("aliases"), dict):
        config["aliases"] = {str(k): str(v) for k, v in display["aliases"].items()}
    for key, value in loaded.items():
        if key not in config:
            continue
        if isinstance(config[key], float) and isinstance(value, (int, float)):
            config[key] = float(value)
        elif isinstance(value, type(config[key])):
            config[key] = value
    return config


def import_stack(config):
    """Import keyboard_status (installed or from the sibling checkout), then
    the local modules that depend on it."""
    try:
        import keyboard_status  # noqa: F401
    except ImportError:
        sys.path.insert(0, os.path.expanduser(config["upstream_path"]))
        import keyboard_status  # noqa: F401
    if REPO_DIR not in sys.path:
        sys.path.insert(0, REPO_DIR)
    import collect
    import screens
    return keyboard_status, collect, screens


def log(message):
    sys.stderr.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    sys.stderr.flush()


# ------------------------------------------------------------------- engine


def control_mode():
    try:
        with open(CONTROL_PATH) as handle:
            data = json.load(handle)
        mode = data.get("mode") if isinstance(data, dict) else None
        return mode if mode in ("auto", "claude", "sessions", "idle") else "auto"
    except (OSError, ValueError):
        return "auto"


class Engine:
    """Priority resolution per REVISED_PLAN.md §4.

    Waiting wins instantly; one working session gets its detail screen; two
    or more concurrent anythings get the switchboard; a lone finished
    session holds between-turns for the engagement window; Idle is the
    floor. Done toasts pin the finishing session's between-turns screen for
    a few seconds. There is no separate minimum-screen-lifetime timer: the
    5 s render tick already guarantees every frame outlives the plan's 2 s
    rule.
    """

    def __init__(self, collect_mod):
        self.collect = collect_mod
        self.prev_states = {}
        self.toast = None  # (sid, expires_at)

    def choose(self, sessions, now, cfg):
        for session in sessions:
            if self.prev_states.get(session.sid) == "working" and session.state == "idle":
                self.toast = (session.sid, now + cfg["toast_seconds"])
        self.prev_states = {s.sid: s.state for s in sessions}

        engaged = self.collect.engaged_sessions(sessions, now, cfg)
        waiting = [s for s in engaged if s.state == "waiting"]
        working = [s for s in engaged if s.state == "working"]

        mode = control_mode()
        if mode == "idle":
            return "idle", None
        if mode == "sessions" and engaged:
            return "sessions", engaged
        if mode == "claude" and engaged:
            best = (waiting or working or engaged)[0]
            kind = {"waiting": "claude_waiting", "working": "claude_working"}.get(
                best.state, "claude_between_turns")
            return kind, best

        if self.toast and now < self.toast[1]:
            toasted = next((s for s in sessions if s.sid == self.toast[0]), None)
            if toasted is not None:
                return "claude_between_turns", toasted
        if len(waiting) == 1:
            return "claude_waiting", waiting[0]
        if len(waiting) >= 2:
            return "sessions", engaged
        if len(working) == 1:
            return "claude_working", working[0]
        if len(working) >= 2:
            return "sessions", engaged
        if len(engaged) >= 2:
            return "sessions", engaged
        if len(engaged) == 1:
            return "claude_between_turns", engaged[0]
        return "idle", None


def render_screen(kind, payload, now, cfg, phase, online, collect_mod, screens_mod,
                  allow_poll=True):
    if kind == "claude_working":
        data = collect_mod.working_view(payload, now, cfg, phase)
    elif kind == "claude_waiting":
        data = collect_mod.waiting_view(payload, now, cfg)
    elif kind == "claude_between_turns":
        data = collect_mod.between_view(payload, now, cfg)
    elif kind == "sessions":
        data = collect_mod.sessions_view(payload, now)
    else:
        data = collect_mod.idle_view(now, cfg, online, allow_poll=allow_poll)
    return screens_mod.RENDERERS[kind](data, cfg["aliases"])


# ------------------------------------------------------------------- daemon


def run_daemon(cfg, once=False, dry_run=False):
    """Tick, resolve, render; push when the frame changed or the heartbeat is
    due. Dedup / heartbeat / capped backoff / wake invalidation follow the
    old daemon, whose constants encode real hardware lessons."""
    import hashlib

    ks, collect_mod, screens_mod = import_stack(cfg)

    if not dry_run:
        boot_out_old_agent()

    engine = Engine(collect_mod)
    dry_path = os.path.join(REPO_DIR, "out", "frame.jpg")
    last_digest = None
    last_kind = None
    last_push = 0.0
    last_tick = None
    offline_until = 0.0
    online = True
    phase = 0.0
    failures = 0

    while True:
        started = time.time()
        try:
            if last_tick is not None and started - last_tick > cfg["tick_seconds"] * 3 + 5:
                # The machine slept: the panel may have dropped its image.
                last_digest = None
            last_tick = started

            sessions = collect_mod.collect_sessions(started, cfg)
            kind, payload = engine.choose(sessions, started, cfg)
            phase += 0.55
            image = render_screen(kind, payload, started, cfg, phase,
                                  online, collect_mod, screens_mod,
                                  allow_poll=not once)
            frame = ks.encode(image, cfg)
            digest = hashlib.sha1(frame).digest()

            if kind != last_kind:
                log("screen -> %s" % kind)
                last_kind = kind

            if dry_run:
                if digest != last_digest:
                    os.makedirs(os.path.dirname(dry_path), exist_ok=True)
                    with open(dry_path, "wb") as handle:
                        handle.write(frame)
                    last_digest = digest
            else:
                due = digest != last_digest or started - last_push >= cfg["heartbeat_seconds"]
                if due and started >= offline_until:
                    ok, note = ks.push(frame, cfg)
                    if ok:
                        if not online:
                            log("keyboard back online (%s)" % note)
                        online, failures, offline_until = True, 0, 0.0
                        last_digest, last_push = digest, started
                    else:
                        failures += 1
                        online = False
                        wait = min(60.0, cfg["offline_backoff_seconds"] * min(failures, 3))
                        offline_until = started + wait
                        if failures == 1 or failures % 20 == 0:
                            log("push failed (%s); retrying in %.0fs [%d]" % (note, wait, failures))
        except Exception as error:  # noqa: BLE001 - a daemon that dies is a bug
            log("tick failed: %s: %s" % (type(error).__name__, error))
        if once:
            return 0 if (dry_run or online) else 1
        elapsed = time.time() - started
        time.sleep(max(0.5, cfg["tick_seconds"] - elapsed))


# -------------------------------------------------------------- diagnostics


def preview(cfg, out_dir):
    """Render the approved-mockup dataset — the pixel ground truth from the
    planning repo — so renderer changes can be diffed against it."""
    _, _, screens_mod = import_stack(cfg)
    os.makedirs(out_dir, exist_ok=True)
    for name, _data in screens_mod.MOCKS:
        image = screens_mod.render_mock(name)
        path = os.path.join(out_dir, "%s.jpg" % name)
        image.save(path, quality=95, subsampling=0)
        print("wrote %s" % path)
    return 0


def live(cfg, path):
    """Render the real current state once, to a file. The dry-run smoke test."""
    ks, collect_mod, screens_mod = import_stack(cfg)
    now = time.time()
    engine = Engine(collect_mod)
    sessions = collect_mod.collect_sessions(now, cfg)
    kind, payload = engine.choose(sessions, now, cfg)
    image = render_screen(kind, payload, now, cfg, 1.0, True,
                          collect_mod, screens_mod, allow_poll=False)
    if path.lower().endswith((".jpg", ".jpeg")):
        with open(path, "wb") as handle:
            handle.write(ks.encode(image, cfg))
    else:
        image.save(path)
    print("wrote %s (%s, %dx%d)" % (path, kind, image.width, image.height))
    return 0


def show_status(cfg):
    _, collect_mod, _ = import_stack(cfg)
    now = time.time()
    engine = Engine(collect_mod)
    sessions = collect_mod.collect_sessions(now, cfg)
    kind, _payload = engine.choose(sessions, now, cfg)
    engaged_ids = {s.sid for s in collect_mod.engaged_sessions(sessions, now, cfg)}
    print(json.dumps({
        "screen": kind,
        "mode": control_mode(),
        "sessions": [{
            "sid": s.sid,
            "state": s.state,
            "engaged": s.sid in engaged_ids,
            "project": s.project,
            "cwd": s.cwd,
            "idle_for_seconds": round(now - s.last_activity, 1),
            "turn_started_at": s.hook.get("turn_started_at"),
            "last_turn_seconds": s.hook.get("last_turn_seconds"),
            "todos": len(s.facts.todos) if s.facts and s.facts.todos else 0,
            "last_tool": s.facts.tool_name if s.facts else None,
        } for s in sessions],
    }, indent=2))
    return 0


def set_mode(mode):
    if mode not in ("auto", "claude", "sessions", "idle"):
        sys.stderr.write("mode must be auto|claude|sessions|idle\n")
        return 2
    tmp = "%s.%d.tmp" % (CONTROL_PATH, os.getpid())
    with open(tmp, "w") as handle:
        json.dump({"mode": mode, "set_at": time.time()}, handle)
    os.replace(tmp, CONTROL_PATH)
    print("mode: %s" % mode)
    return 0


# ---------------------------------------------------------------- installer


def launchctl(*args, **kwargs):
    import subprocess

    quiet = kwargs.get("quiet", True)
    try:
        result = subprocess.run(["launchctl"] + list(args), capture_output=True, text=True)
        if result.returncode != 0 and not quiet:
            sys.stderr.write(result.stderr)
        return result.returncode == 0
    except OSError:
        return False


def boot_out_old_agent():
    """Single-pusher rule: exactly one daemon may own the panel."""
    if launchctl("bootout", "gui/%d/%s" % (os.getuid(), OLD_LABEL)):
        log("booted out %s (single-pusher rule)" % OLD_LABEL)


def venv_python():
    return os.path.join(VENV_PATH, "bin", "python3")


def ensure_venv(cfg):
    import subprocess

    python = venv_python()
    if not os.path.exists(python):
        print("creating virtualenv at %s" % VENV_PATH)
        subprocess.run([sys.executable, "-m", "venv", VENV_PATH], check=True)
    probe = subprocess.run([python, "-c", "import PIL, yaml, keyboard_status"],
                           capture_output=True)
    if probe.returncode != 0:
        print("installing dependencies (Pillow, PyYAML, keyboard_status)")
        subprocess.run([python, "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
                       check=False)
        subprocess.run([python, "-m", "pip", "install", "--quiet", "Pillow", "PyYAML"],
                       check=True)
        subprocess.run([python, "-m", "pip", "install", "--quiet", "-e",
                        os.path.expanduser(cfg["upstream_path"])], check=True)
    return python


def write_plist(python):
    import plistlib

    os.makedirs(os.path.dirname(LAUNCH_PLIST), exist_ok=True)
    agent = {
        "Label": LAUNCH_LABEL,
        "ProgramArguments": [python, os.path.join(REPO_DIR, "display.py"), "--daemon"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 15,
        "StandardOutPath": LOG_PATH,
        "StandardErrorPath": LOG_PATH,
        "ProcessType": "Background",
    }
    with open(LAUNCH_PLIST, "wb") as handle:
        plistlib.dump(agent, handle)


def hooks_registered():
    try:
        with open(os.path.join(CLAUDE_DIR, "settings.json")) as handle:
            return "keyboard-status.py" in handle.read()
    except OSError:
        return False


def install(cfg, with_agent=True):
    if not os.path.exists(CONFIG_PATH):
        example = os.path.join(REPO_DIR, "config.example.yaml")
        try:
            with open(example) as src, open(CONFIG_PATH, "w") as dst:
                dst.write(src.read())
            print("wrote %s (edit it: keyboard url, aliases)" % CONFIG_PATH)
        except OSError as error:
            print("could not write %s: %s" % (CONFIG_PATH, error))

    python = ensure_venv(cfg)

    if not hooks_registered():
        print("WARNING: the claude-code-keyboard-status hooks are not registered.")
        print("Run: python3 %s --install" %
              os.path.expanduser("%s/keyboard_status.py" % cfg["upstream_path"]))
        print("(they own the state file this display reads; without them the")
        print(" panel cannot tell a permission prompt from ongoing work)")

    if with_agent:
        write_plist(python)
        domain = "gui/%d" % os.getuid()
        launchctl("bootout", "%s/%s" % (domain, OLD_LABEL))
        launchctl("bootout", "%s/%s" % (domain, LAUNCH_LABEL))
        if not launchctl("bootstrap", domain, LAUNCH_PLIST, quiet=False):
            launchctl("unload", LAUNCH_PLIST)
            launchctl("load", "-w", LAUNCH_PLIST, quiet=False)
        print("loaded launchd agent %s (and booted out %s)" % (LAUNCH_LABEL, OLD_LABEL))
    print("logs: %s" % LOG_PATH)


def uninstall():
    domain = "gui/%d" % os.getuid()
    launchctl("bootout", "%s/%s" % (domain, LAUNCH_LABEL))
    launchctl("unload", LAUNCH_PLIST)
    try:
        os.unlink(LAUNCH_PLIST)
        print("removed %s" % LAUNCH_PLIST)
    except OSError:
        pass
    # Hand the panel back to the old daemon if it is still installed.
    if os.path.exists(OLD_PLIST):
        if launchctl("bootstrap", domain, OLD_PLIST):
            print("restored %s" % OLD_LABEL)
    print("left %s and the venv in place" % CONFIG_PATH)


# -------------------------------------------------------------------- entry

USAGE = __doc__.split("\n\n")[2] + "\n"


def main(argv):
    cfg = load_config()
    args = argv[1:]
    command = args[0] if args else "--help"
    dry_run = "--dry-run" in args

    if command == "--install":
        install(cfg, with_agent="--no-agent" not in args)
        return 0
    if command == "--uninstall":
        uninstall()
        return 0
    if command == "--daemon":
        log("starting: %s every %gs%s" % (cfg["url"], cfg["tick_seconds"],
                                          " (dry run)" if dry_run else ""))
        return run_daemon(cfg, dry_run=dry_run)
    if command == "--once":
        return run_daemon(cfg, once=True, dry_run=dry_run)
    if command == "--preview":
        extra = [a for a in args[1:] if not a.startswith("--")]
        return preview(cfg, extra[0] if extra else os.path.join(REPO_DIR, "out"))
    if command == "--live":
        extra = [a for a in args[1:] if not a.startswith("--")]
        return live(cfg, extra[0] if extra else os.path.join(REPO_DIR, "out", "live.jpg"))
    if command == "--status":
        return show_status(cfg)
    if command == "mode":
        return set_mode(args[1] if len(args) > 1 else "")
    sys.stdout.write(USAGE)
    return 0 if command in ("--help", "-h") else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))

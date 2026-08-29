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
    python3 display.py mode next          advance the override one step round the cycle
                                          (an override lapses back to auto after
                                          override_ttl_seconds, default 60)
    python3 keys.py --permission          the hotkey that calls `mode next` for you

macOS runs the daemon under launchd and installs the keys.py hotkey listener
beside it; Ubuntu/Debian runs it under a systemd --user service and has no
hotkey listener at all. service.py owns that split -- `python3 service.py
--platform` reports what this host resolved to.
"""

import json
import os
import sys
import time

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
# Ahead of `import service`: this module is imported by keys.py and by the
# tests, not only run as a script, so REPO_DIR is not always on the path yet.
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

import service  # noqa: E402

CLAUDE_DIR = os.path.expanduser("~/.claude")
CONFIG_PATH = os.path.join(CLAUDE_DIR, "context-keyboard-display.yaml")
CONTROL_PATH = os.path.join(CLAUDE_DIR, "context-keyboard-display-control.json")
# What the daemon last put on the panel. Written by the daemon, read by the
# hotkey listener; see showing_kind for why that direction exists.
STATUS_PATH = os.path.join(CLAUDE_DIR, "context-keyboard-display-status.json")
LOG_PATH = os.path.join(CLAUDE_DIR, "context-keyboard-display.log")
VENV_PATH = os.path.join(REPO_DIR, ".venv")
LAUNCH_LABEL = "com.williamliu.context-keyboard-display"
LAUNCH_PLIST = os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LAUNCH_LABEL)
# The old daemon's agent: exactly one process may push to the panel, so the
# new daemon boots this out on startup and --uninstall restores it.
OLD_LABEL = "com.williamliu.claude-keyboard-status"
OLD_PLIST = os.path.expanduser("~/Library/LaunchAgents/%s.plist" % OLD_LABEL)
OLD_CONFIG_PATH = os.path.join(CLAUDE_DIR, "keyboard-status.json")

# The screen-selection modes, in the order the hotkey cycles them. `auto` first
# because it is the resting state and the one the daemon falls back to; `idle`
# last because it is the deliberate "park the panel on the clock" end of the
# ladder (detail -> overview -> nothing), which puts it one press from `auto`
# again. claude/sessions degrade to the auto decision when no session is
# engaged, so the cycle never strands the panel on an unreachable screen.
MODES = ("auto", "claude", "sessions", "idle")

# The panel's address has no sensible default: it is whatever DHCP handed your
# keyboard on your network, and a wrong guess fails as a silent no-op (frames
# POSTed into the void, an empty screen, nothing in the log that says why). So
# ship a placeholder that cannot be mistaken for an address and refuse to push
# until it is replaced -- see require_url.
URL_UNSET = "http://PANEL-IP-NOT-SET/image/upload"

DEFAULTS = {
    "url": URL_UNSET,
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
    # "Peek" semantics for the manual override: a mode other than `auto` is
    # forgotten this long after it was set, and the panel falls back to the
    # auto decision on its own. Without it a single hotkey press parks the
    # panel on one screen until you remember to press it back. `auto` itself
    # never expires -- it is the resting state, so there is nothing to expire
    # to. Set it to a very large number for a sticky, pre-TTL override.
    "override_ttl_seconds": 60.0,
    "usage_poll_seconds": 60.0,
    "diff_poll_seconds": 5.0,
    "toast_seconds": 4.0,
    # Where the claude-code-keyboard-status checkout lives (library import).
    "upstream_path": "~/projects/claude-code-keyboard-status",
    # display.aliases in the YAML lands here: {repo-basename: short-name}.
    "aliases": {},
    # keys.* in the YAML lands here (see keys.py: the global-hotkey listener).
    "key_binding": "ctrl+opt+cmd+k",
    # The drill-down key. Only ever acts while the switchboard is the screen on
    # the panel, so it needs no cycle of its own -- see select_next_session.
    "key_select_binding": "ctrl+opt+cmd+j",
    "key_cycle": list(MODES),
    "key_swallow": "auto",
    "key_debounce_seconds": 0.35,
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
    keys = loaded.pop("keys", None)
    if isinstance(keys, dict):
        # Parsed by hand rather than through the type-matched loop below:
        # `swallow` legitimately takes either the string "auto" or a bool, and
        # a mistyped binding must not silently fall back to the default.
        if keys.get("binding") is not None:
            config["key_binding"] = str(keys["binding"])
        if keys.get("select_binding") is not None:
            config["key_select_binding"] = str(keys["select_binding"])
        if isinstance(keys.get("cycle"), list) and keys["cycle"]:
            config["key_cycle"] = [str(m) for m in keys["cycle"]]
        if keys.get("swallow") is not None:
            config["key_swallow"] = keys["swallow"]
        if isinstance(keys.get("debounce_seconds"), (int, float)):
            config["key_debounce_seconds"] = float(keys["debounce_seconds"])
    for key, value in loaded.items():
        if key not in config:
            continue
        if isinstance(config[key], float) and isinstance(value, (int, float)):
            config[key] = float(value)
        elif isinstance(value, type(config[key])):
            config[key] = value
    return config


def upstream_dir(config):
    """Where the keyboard_status library actually is.

    `--install` and `--uninstall` are the two commands that deliberately do
    not re-exec into the venv -- they have to run before it exists -- so they
    run under whatever `python3` the user typed, and that interpreter usually
    has no PyYAML. load_config() then returns pure defaults *silently*, and
    `upstream_path` comes out as the ~/projects/... default no matter what the
    installer wrote into the YAML. On a fresh machine that directory does not
    exist and `pip install -e` on it aborts the install.

    So: if the library is sitting beside this file -- which is exactly what
    the one-command installer arranges, and the only reason those two files
    are copied into INSTALL_DIR -- believe the directory over the config we
    may not have been able to read. A clone checkout has neither file and
    falls through to the configured sibling path, unchanged.
    """
    beside = os.path.join(REPO_DIR, "keyboard_status.py")
    if os.path.exists(beside) and os.path.exists(os.path.join(REPO_DIR, "pyproject.toml")):
        return REPO_DIR
    return os.path.expanduser(config["upstream_path"])


def import_stack(config):
    """Import keyboard_status (installed or from the sibling checkout), then
    the local modules that depend on it."""
    try:
        import keyboard_status  # noqa: F401
    except ImportError:
        sys.path.insert(0, upstream_dir(config))
        import keyboard_status  # noqa: F401
    # keyboard_status hardcodes the macOS system faces and resolves them out of
    # its own globals on every font() call, so off macOS they are repointed
    # here -- one place, before any renderer has asked for a glyph. No-op on
    # macOS, where the faces it names are the right ones.
    service.apply_font_fallback(keyboard_status, warn=log)
    if REPO_DIR not in sys.path:
        sys.path.insert(0, REPO_DIR)
    import collect
    import screens
    return keyboard_status, collect, screens


def log(message):
    sys.stderr.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    sys.stderr.flush()



# The panel sleeps off Wi-Fi to save power. On macOS the first connect after
# its ARP entry expires does not fail fast: it blocks for the whole timeout and
# surfaces as a bare "timed out" (errno None), and only *then* does the kernel
# cache the negative result, so the next connect returns EHOSTDOWN instantly.
# Measured on this machine: attempt 1 burned the full 6 s with errno None,
# attempts 2 and 3 came back in 0.00 s with errno 64. Two consequences, both
# handled by push_frame below -- one knock is never enough to tell a sleeping
# panel from a dead one, and "timed out" on its own is a misleading log line.
PUSH_ATTEMPTS = 2
PUSH_RETRY_PAUSE = 0.4
REACH_PROBE_TIMEOUT = 1.5


def reachability(cfg):
    """One short TCP knock on the panel's port, to name a push failure.

    urllib collapses "nothing answered the ARP" and "the server took the
    connection then stalled" into the same `timed out` string -- which is the
    difference between "the keyboard is asleep" and "the push code is wrong",
    and the reason that distinction cost an afternoon. By the time this runs the
    kernel has the negative entry cached, so it is cheap and it reports errno.
    """
    import socket
    from urllib.parse import urlsplit

    parts = urlsplit(cfg["url"])
    host, port = parts.hostname, parts.port or 80
    if not host:
        return "no host in url %r" % cfg["url"]
    try:
        socket.create_connection((host, port), timeout=REACH_PROBE_TIMEOUT).close()
        return "but %s:%d is open, so the POST itself stalled" % (host, port)
    except OSError as error:
        reason = error.strerror or type(error).__name__
        return "%s:%d unreachable (errno %s: %s)" % (host, port, error.errno, reason)


def require_url(cfg):
    """Stop, loudly, before pushing frames at a placeholder.

    Returns an exit code when the address is unset and None when it is fine, so
    callers can `return code` without a second branch.
    """
    if URL_UNSET not in cfg["url"]:
        return None
    sys.stderr.write(
        "The keyboard's address is not set yet.\n"
        "\n"
        "Edit %s and set `url` to your panel's own address:\n"
        "\n"
        "    url: http://192.168.1.50/image/upload\n"
        "\n"
        "Find it on the keyboard's own display or settings app, or look for a\n"
        "new device in your router's client list. Then restart the daemon:\n"
        "\n"
        "    %s\n"
        % (CONFIG_PATH, service.restart_command(LAUNCH_LABEL)))
    return 2


def push_frame(ks, frame, cfg):
    """POST the frame, knocking twice, and say what actually went wrong.

    ks.push is the upstream pusher and stays untouched: same bytes, same
    headers, HTTP 200 proven against this panel. What it cannot do alone is
    survive the wake -- the first attempt is the one that pays for the cold ARP
    entry, and a panel that is merely asleep answers the second. Retrying here
    rather than waiting out the caller's 20-60 s backoff is the difference
    between a frame that lands this tick and a display a minute stale.
    """
    for attempt in range(1, PUSH_ATTEMPTS + 1):
        ok, note = ks.push(frame, cfg)
        if ok:
            return True, note if attempt == 1 else "%s on attempt %d" % (note, attempt)
        if attempt < PUSH_ATTEMPTS:
            time.sleep(PUSH_RETRY_PAUSE)
    return False, "%s; %s" % (note, reachability(cfg))


# ------------------------------------------------------------------- engine


def control_state(cfg=None):
    """The override in force right now as `(mode, sid)`, honouring its TTL.

    An override is a peek, not a setting: `set_mode` stamps `set_at`, and once
    `override_ttl_seconds` have passed the mode is treated as expired and this
    reports `auto` again — so the panel rebounds to the auto decision without
    anyone having to press their way back. `auto` is exempt (expiring the
    resting state into itself means nothing), as is a file with no usable
    `set_at`: an override with no timestamp is left alone rather than dropped.

    `sid` is the session drilled into inside `sessions` mode, and is None
    everywhere else — a pin recorded against any other mode is stale data from
    a mode change, not a selection. It expires *with* the mode that carries it
    rather than on a clock of its own: drilling into a session is a peek like
    any other, and decaying in two stages would leave the panel overriding
    reality for twice as long, with the first stage ending in a screen change
    nobody asked for.

    `cfg` is optional so the hotkey listener can call this with no arguments;
    the config is then loaded on demand.
    """
    try:
        with open(CONTROL_PATH) as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return "auto", None
        mode = data.get("mode")
        if mode not in MODES:
            return "auto", None
        sid = data.get("sid")
        sid = sid if isinstance(sid, str) and sid and mode == "sessions" else None
        if mode == "auto":
            return mode, None
        set_at = data.get("set_at")
        if not isinstance(set_at, (int, float)):
            return mode, sid
        ttl = (cfg or load_config())["override_ttl_seconds"]
        if time.time() - set_at > ttl:
            return "auto", None
        return mode, sid
    except (OSError, ValueError):
        return "auto", None


def control_mode(cfg=None):
    """The override's mode alone — the question most callers are asking."""
    return control_state(cfg)[0]


def write_status(kind):
    """Publish the screen kind the daemon just chose."""
    tmp = "%s.%d.tmp" % (STATUS_PATH, os.getpid())
    try:
        with open(tmp, "w") as handle:
            json.dump({"kind": kind, "at": time.time()}, handle)
        os.replace(tmp, STATUS_PATH)
    except OSError:
        pass  # a panel that renders but cannot publish is still a working panel


def showing_kind():
    """The screen kind last rendered, or None if that is not knowable.

    The drill-down key has to know whether the switchboard is the screen
    actually on the panel, and that answer lives inside Engine.choose's
    priority ladder — toast state and all — which is the daemon's business and
    nobody else's. Re-deriving it in the listener would be a second copy of the
    ladder, free to drift from the first. So the daemon publishes what it
    chose and the listener reads it. If the daemon is not running the answer is
    stale, which costs nothing: a selection nobody renders is invisible, and
    the TTL clears it.
    """
    try:
        with open(STATUS_PATH) as handle:
            data = json.load(handle)
        kind = data.get("kind") if isinstance(data, dict) else None
        return kind if isinstance(kind, str) else None
    except (OSError, ValueError):
        return None


DETAIL_KINDS = {"waiting": "claude_waiting", "working": "claude_working"}


def detail_kind(session):
    """Which of the three detail screens a session's own state calls for.

    Shared by the `claude` mode's "most important session" pick and the
    drill-down's "the session you chose": both want the screen that fits the
    session, and only differ in how the session was arrived at.
    """
    return DETAIL_KINDS.get(session.state, "claude_between_turns")


class Engine:
    """Priority resolution per REVISED_PLAN.md §4.

    Waiting wins instantly; one working session gets its detail screen; two
    or more concurrent anythings get the switchboard; a lone finished
    session holds between-turns for the engagement window; Idle is the
    floor. Done toasts pin the finishing session's between-turns screen for
    a few seconds. There is no separate minimum-screen-lifetime timer: the
    render tick bounds how fast a frame can be replaced (5 s on the still
    screens, 1 s on the two that animate), and screen *kind* changes are
    state-driven, which is what the plan's 2 s rule was guarding.
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

        mode, pinned = control_state(cfg)
        if mode == "idle":
            return "idle", None
        if mode == "sessions" and engaged:
            if pinned:
                chosen = next((s for s in engaged if s.sid == pinned), None)
                if chosen is not None:
                    return detail_kind(chosen), chosen
                # The pinned session ended while it was on screen. Falling back
                # to the list is the only honest move: promoting some *other*
                # session into the detail screen would show one the user never
                # chose, under a heading that says they did.
            return "sessions", engaged
        if mode == "claude" and engaged:
            best = (waiting or working or engaged)[0]
            return detail_kind(best), best

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

# Two of the six renderings move on their own: WORKING spins the mascot and
# counts a stopwatch, WAITING counts the stuck-for timer. At the 5 s base tick
# both read as a slideshow -- the seconds column jumps by five and the mascot
# teleports. The other screens have nothing to gain: idle's clock and its usage
# meters are minute-resolution, sessions and between-turns hold still.
FAST_TICK_SECONDS = 1.0
FAST_TICK_KINDS = ("claude_working", "claude_waiting")
# Past an hour fmt_elapsed degrades to "1h04", so the row it feeds changes once
# a minute and the fast tick would spend 59 renders out of 60 producing the
# frame that is already on the panel.
FAST_TICK_MAX_ELAPSED = 3600.0
# Angular velocity, radians per second. Held at the old 0.55-per-5 s-tick rate
# so the mascot spins at the same speed it always did; the tick only decides how
# many steps that rotation is cut into.
MASCOT_RADIANS_PER_SECOND = 0.11
# Floor on the "the machine slept" gap. Scaled off the tick alone it lands at
# 8 s when tick is 1 s -- shorter than one failed push (two knocks plus the
# reachability probe, ~10 s), which would invalidate the digest on every
# offline tick and re-push a frame the panel never lost.
WAKE_GAP_FLOOR = 20.0


# A press writes the control file instantly, but the daemon only obeys it on
# its next tick -- up to five seconds of nothing, which reads as a dead key
# rather than a slow one. The fix is not to tick faster for everyone: the sleep
# between ticks is served in slices, and a slice checks exactly one thing --
# whether the control file has been rewritten. That is one stat() per slice,
# with no collect, no render and no push behind it, so an idle panel nobody is
# touching costs what it always did.
CONTROL_POLL_SECONDS = 0.2


def control_signature():
    """Cheap fingerprint of the control file; changes whenever it is rewritten.

    set_mode builds a temp file and os.replace()s it into place, so every write
    lands a new inode -- which is what makes this reliable without depending on
    mtime having a resolution finer than the gap between two presses. A missing
    file is a signature too: deleting it is a change like any other.
    """
    try:
        stat = os.stat(CONTROL_PATH)
    except OSError:
        return None
    return (stat.st_ino, stat.st_mtime_ns, stat.st_size)


def sleep_between_ticks(seconds, signature):
    """Sleep, waking early if someone rewrote the control file. True if early.

    Deliberately blind to the TTL rebound: an override expiring rewrites
    nothing, it only changes what control_mode() computes from the clock, so it
    stays a normal-tick affair exactly as it was before this existed.
    """
    deadline = time.time() + seconds
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            return False
        time.sleep(min(CONTROL_POLL_SECONDS, remaining))
        if control_signature() != signature:
            return True


def tick_for(kind, payload, now, cfg, offline_until, collect_mod):
    """How long to sleep before the next render of `kind`."""
    slow = cfg["tick_seconds"]
    if kind not in FAST_TICK_KINDS:
        return slow
    if now < offline_until:
        # The panel is not answering: animating into a dead socket costs CPU
        # and pushes nothing. A successful push clears offline_until.
        return slow
    if (kind == "claude_working"
            and now - collect_mod.turn_started(payload) >= FAST_TICK_MAX_ELAPSED):
        return slow
    return min(FAST_TICK_SECONDS, slow)


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
    tick = cfg["tick_seconds"]

    while True:
        started = time.time()
        # Snapshot before anything reads the override, so a write that lands
        # while this tick is still rendering is caught by the sleep below
        # instead of being mistaken for the state we just rendered.
        control_sig = control_signature()
        try:
            interval = tick if last_tick is None else started - last_tick
            if last_tick is not None and started - last_tick > max(WAKE_GAP_FLOOR, tick * 3 + 5):
                # The machine slept: the panel may have dropped its image.
                last_digest = None
            last_tick = started

            sessions = collect_mod.collect_sessions(started, cfg)
            kind, payload = engine.choose(sessions, started, cfg)
            # Measured, not assumed: a tick cut short by a press turns the
            # mascot by the time that actually passed rather than by the
            # interval it would have slept for.
            phase += MASCOT_RADIANS_PER_SECOND * interval
            image = render_screen(kind, payload, started, cfg, phase,
                                  online, collect_mod, screens_mod,
                                  allow_poll=not once)
            frame = ks.encode(image, cfg)
            digest = hashlib.sha1(frame).digest()

            if kind != last_kind:
                log("screen -> %s" % kind)
                last_kind = kind
                # Only on change: the listener needs to know which screen is up,
                # not how many times it has been redrawn.
                write_status(kind)

            if dry_run:
                if digest != last_digest:
                    os.makedirs(os.path.dirname(dry_path), exist_ok=True)
                    with open(dry_path, "wb") as handle:
                        handle.write(frame)
                    last_digest = digest
            else:
                due = digest != last_digest or started - last_push >= cfg["heartbeat_seconds"]
                if due and started >= offline_until:
                    ok, note = push_frame(ks, frame, cfg)
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

            tick = tick_for(kind, payload, started, cfg, offline_until, collect_mod)
        except Exception as error:  # noqa: BLE001 - a daemon that dies is a bug
            log("tick failed: %s: %s" % (type(error).__name__, error))
        if once:
            return 0 if (dry_run or online) else 1
        elapsed = time.time() - started
        sleep_between_ticks(max(0.5, tick - elapsed), control_sig)


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
        "mode": control_mode(cfg),
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


def set_mode(mode, sid=None, quiet=False):
    """Write the override. `sid` pins one session inside `sessions` mode.

    Every write re-stamps `set_at`, which is what keeps an active browse alive:
    stepping through sessions is exactly as much of a peek as setting the mode
    was, so each press restarts the same TTL rather than racing it.
    """
    if mode not in MODES:
        sys.stderr.write("mode must be %s\n" % "|".join(MODES + ("next",)))
        return 2
    state = {"mode": mode, "set_at": time.time()}
    if sid and mode == "sessions":
        state["sid"] = sid
    tmp = "%s.%d.tmp" % (CONTROL_PATH, os.getpid())
    with open(tmp, "w") as handle:
        json.dump(state, handle)
    os.replace(tmp, CONTROL_PATH)
    if not quiet:
        print("mode: %s%s" % (mode, " [%s]" % sid[:8] if state.get("sid") else ""))
    return 0


def normalise_cycle(cycle):
    """Drop unknown names, drop duplicates, keep order; fall back to MODES.

    A hand-edited `keys.cycle` is the one place a typo would leave the hotkey
    writing a mode the engine ignores, so the bad entries are dropped here
    rather than at every call site.
    """
    seen = []
    for mode in cycle or ():
        mode = str(mode).strip().lower()
        if mode in MODES and mode not in seen:
            seen.append(mode)
    return tuple(seen) or MODES


def cycle_mode(cycle=MODES, cfg=None):
    """Advance the override one step and return the mode now in force.

    A mode outside the cycle (the file was hand-edited, or the cycle was
    shortened since) lands on the first entry rather than nowhere. The step is
    taken from the *effective* mode, so an override that has already expired
    advances from `auto` — pressing next after a peek lapsed starts the cycle
    over rather than resuming where it left off.

    One exception, and it is what makes this key feel like one control rather
    than two: while a session is drilled into, the first press *pops back to
    the list* instead of advancing. Going straight from a session's detail to
    `idle` would skip the screen the user came from and leave no way back
    except all the way round the cycle.
    """
    cycle = normalise_cycle(cycle)
    current, pinned = control_state(cfg)
    if current == "sessions" and pinned:
        set_mode("sessions", quiet=True)
        return "sessions"
    nxt = cycle[(cycle.index(current) + 1) % len(cycle)] if current in cycle else cycle[0]
    set_mode(nxt, quiet=True)
    return nxt


def select_next_session(cfg=None):
    """Drill into the switchboard, or step to the next session in it.

    Returns the sid now pinned, or None if it did nothing. Doing nothing is the
    common case and the important one: this key is live only while the
    switchboard is the screen on the panel, because a drill-down key that fires
    from any other screen is just a second way to be surprised by a panel you
    were not looking at.

    "Next" follows the order the switchboard draws its rows in, not the order
    sessions were collected in, so it steps down the list the user is reading.
    The pin is a sid rather than a row index for the same reason: rows re-sort
    the moment a session changes state, and an index would then advance to
    whatever slid into that slot.
    """
    cfg = cfg or load_config()
    mode, pinned = control_state(cfg)
    # Already inside the drill-down: the switchboard line is where we are by
    # construction, whatever the daemon happens to be rendering this instant.
    if not (mode == "sessions" and pinned) and showing_kind() != "sessions":
        return None

    _, collect_mod, _ = import_stack(cfg)
    now = time.time()
    engaged = collect_mod.engaged_sessions(
        collect_mod.collect_sessions(now, cfg), now, cfg)
    order = collect_mod.ordered_sessions(engaged)
    if not order:
        return None

    index = next((i for i, s in enumerate(order) if s.sid == pinned), None)
    # No pin, or a pin whose session has since ended: start at the top rather
    # than guessing where in a changed list the user had got to.
    nxt = order[0] if index is None else order[(index + 1) % len(order)]
    set_mode("sessions", sid=nxt.sid, quiet=True)
    return nxt.sid


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
    """Single-pusher rule: exactly one daemon may own the panel.

    macOS only: the old repo's pusher is a launchd agent, so off macOS there
    is nothing that could be holding the panel and nothing to boot out.
    """
    if not service.is_macos():
        return
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
    # pyobjc is the hotkey listener's dependency and macOS-only -- there is no
    # Linux wheel and no reason to want one, since keys.py does not run there.
    wanted = ["Pillow", "PyYAML"] + (["pyobjc-framework-Quartz"]
                                     if service.is_macos() else [])
    modules = "PIL, yaml, keyboard_status" + (", Quartz" if service.is_macos() else "")
    probe = subprocess.run([python, "-c", "import " + modules], capture_output=True)
    if probe.returncode != 0:
        print("installing dependencies (%s, keyboard_status)" % ", ".join(wanted))
        subprocess.run([python, "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
                       check=False)
        subprocess.run([python, "-m", "pip", "install", "--quiet"] + wanted, check=True)
        subprocess.run([python, "-m", "pip", "install", "--quiet", "-e",
                        upstream_dir(cfg)], check=True)
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


def install(cfg, with_agent=True, with_keys=True):
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
              os.path.join(upstream_dir(cfg), "keyboard_status.py"))
        print("(they own the state file this display reads; without them the")
        print(" panel cannot tell a permission prompt from ongoing work)")

    if with_agent and service.is_macos():
        write_plist(python)
        domain = "gui/%d" % os.getuid()
        launchctl("bootout", "%s/%s" % (domain, OLD_LABEL))
        launchctl("bootout", "%s/%s" % (domain, LAUNCH_LABEL))
        if not launchctl("bootstrap", domain, LAUNCH_PLIST, quiet=False):
            launchctl("unload", LAUNCH_PLIST)
            launchctl("load", "-w", LAUNCH_PLIST, quiet=False)
        print("loaded launchd agent %s (and booted out %s)" % (LAUNCH_LABEL, OLD_LABEL))
    elif with_agent and service.is_linux():
        service.install_service(python, LOG_PATH,
                                script=os.path.join(REPO_DIR, "display.py"))
    elif with_agent:
        print("no service manager known for %s; run the daemon yourself:"
              % (service.system_name() or "this platform"))
        print("    %s %s --daemon" % (python, os.path.join(REPO_DIR, "display.py")))
    print("logs: %s" % LOG_PATH)
    if not service.is_macos():
        # macOS says this in the README and in `launchctl print`; the systemd
        # verbs are the ones nobody can guess, so they are printed where the
        # install ends rather than left to be looked up.
        for line in service.status_commands():
            print("      %s" % line)

    # keys.py is a macOS listener and nothing else -- see service.hotkeys_supported.
    # Saying so here, once, is the whole Linux hotkey story: not a failure, not
    # a silent omission, and not a half-listener that never fires.
    if with_keys and not service.hotkeys_supported():
        print("")
        print("global hotkeys: not supported on %s -- skipped."
              % (service.system_name() or "this platform"))
        print("Drive the panel with the same command the hotkey runs:")
        print("    python3 %s mode next" % os.path.join(REPO_DIR, "display.py"))
    elif with_keys:
        # Imported here, not at module scope: keys imports this module, and it
        # is the only part of the install that can fail on a privacy grant.
        import keys

        print("")
        keys.install_agent(python)
        keys.show_permission(cfg, open_pane=False)


def uninstall():
    if not service.is_macos():
        if service.is_linux():
            service.uninstall_service()
        print("left %s and the venv in place" % CONFIG_PATH)
        return
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


def reexec_in_venv():
    """Re-run this script under the venv interpreter when the rendering deps
    are missing from the interpreter the user typed. The README documents
    every command as plain `python3 display.py ...`, and the venv is an
    implementation detail of --install; honour that rather than fail on an
    import. --install itself needs only the stdlib, so it never comes here."""
    try:
        import PIL, yaml  # noqa: F401
        return
    except ImportError:
        pass
    python = venv_python()
    # sys.prefix, not the interpreter path: a stdlib venv's bin/python3 is a
    # symlink to the base interpreter, so comparing paths would always say
    # "already there" and skip the re-exec. sys.prefix also stops a loop when
    # the venv itself is missing deps.
    if not os.path.exists(python) or sys.prefix == VENV_PATH:
        return  # nothing better to offer; let the real ImportError surface
    os.execv(python, [python, os.path.abspath(__file__)] + sys.argv[1:])


def main(argv):
    if (argv[1:] or ["--help"])[0] not in ("--install", "--uninstall", "--help", "-h"):
        reexec_in_venv()
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
    if command in ("--daemon", "--once") and not dry_run:
        code = require_url(cfg)
        if code is not None:
            return code
    if command == "--daemon":
        log("starting: %s every %gs, %gs while working/waiting%s"
            % (cfg["url"], cfg["tick_seconds"],
               min(FAST_TICK_SECONDS, cfg["tick_seconds"]),
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
        target = args[1] if len(args) > 1 else ""
        if target == "next":
            # The same step the hotkey takes, over the same configured cycle,
            # so `mode next` and a keypress can't disagree.
            print("mode: %s" % cycle_mode(cfg["key_cycle"], cfg))
            return 0
        return set_mode(target)
    sys.stdout.write(USAGE)
    return 0 if command in ("--help", "-h") else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))

#!/usr/bin/env python3
"""Platform layer: which service manager owns the daemon, and what it looks like.

macOS keeps what it always had -- a launchd agent written by display.py, and
the Carbon hotkey listener in keys.py. This module exists for the other host
the daemon has to run on: a headless Ubuntu/Debian box, where there is no
launchd, no WindowServer to reserve a hotkey with, and no San Francisco.

    python3 service.py --platform      what this host is and what it supports
    python3 service.py --print-unit    the systemd --user unit, on stdout
    python3 service.py --fonts         the font files the Linux fallback picks

Everything here is stdlib-only and side-effect free at import, so display.py
can import it before the venv exists. The generators (`unit_text`,
`font_choice`) are pure functions of their arguments for exactly one reason:
they are the parts worth testing on a machine that is not the target.
"""

import os
import platform
import sys

REPO_DIR = os.path.dirname(os.path.abspath(__file__))

# The test hook. Real runs never set it; the installer tests set it to drive
# both branches from one machine, which is the only way this file's Linux half
# gets exercised at all before it meets a Linux box.
PLATFORM_ENV = "CKD_PLATFORM"


def system_name():
    """"Darwin" / "Linux" / whatever else, honouring the CKD_PLATFORM hook."""
    return (os.environ.get(PLATFORM_ENV) or platform.system() or "").strip()


def is_macos():
    return system_name() == "Darwin"


def is_linux():
    return system_name() == "Linux"


def hotkeys_supported():
    """Global hotkeys are macOS-only, deliberately -- see keys.py.

    A Linux equivalent means either an X11 grab (dead on a headless box, and
    wrong under Wayland) or reading /dev/input, which is a keylogger asking
    for the input group. Neither is worth shipping for a convenience key, so
    the honest answer is "not here" rather than a half-working listener.
    """
    return is_macos()


# ------------------------------------------------------------------ systemd

UNIT_NAME = "context-keyboard-display.service"
# Debian/Ubuntu honour XDG_CONFIG_HOME for user units; default is ~/.config.
UNIT_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "systemd", "user")
UNIT_PATH = os.path.join(UNIT_DIR, UNIT_NAME)

# `StandardOutput=append:` landed in systemd 240. Ubuntu 20.04 ships 245 and
# Debian 11 ships 247, so every supported target has it -- but an older host
# would take the unit, ignore nothing, and *fail to start*, which is a worse
# failure than losing the log file. Below 240 the unit says nothing about
# streams and the logs live in the journal instead.
APPEND_SINCE = 240


def systemd_version(run=None):
    """The running systemd's major version, or None if it cannot be asked."""
    import subprocess

    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    try:
        result = run(["systemctl", "--version"])
    except OSError:
        return None
    if getattr(result, "returncode", 1) != 0:
        return None
    for token in (result.stdout or "").split():
        if token.isdigit():
            return int(token)
    return None


def _quote(value):
    """One ExecStart argument, quoted the way systemd unquotes it."""
    return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')


def unit_text(python, script, log_path, version=None, working_dir=None):
    """The systemd --user unit for the display daemon, as text.

    Mirrors the launchd agent deliberately, field for field: RunAtLoad ->
    WantedBy=default.target, KeepAlive -> Restart=always, ThrottleInterval 15
    -> RestartSec=15, ProcessType Background -> Nice=10, and the two log paths
    -> append: on the same file, so `tail -f ~/.claude/...log` is one command
    on both platforms rather than two things to remember.
    """
    lines = [
        "[Unit]",
        "Description=Context-aware Claude Code display for the keyboard panel",
        "Documentation=https://github.com/williamliu0516/context-keyboard-display",
        # No network dependency on purpose: the panel is often asleep or
        # unplugged, the daemon retries on its own, and a unit that waits for
        # the network is a unit that does not come back after a Wi-Fi blip.
        "After=default.target",
        "",
        "[Service]",
        "Type=simple",
        "ExecStart=%s %s --daemon" % (_quote(python), _quote(script)),
        "Restart=always",
        "RestartSec=15",
        # Unbuffered, or the log file stays empty for minutes at a time: this
        # daemon writes one short line per event into a pipe, not a tty.
        "Environment=PYTHONUNBUFFERED=1",
        "Nice=10",
    ]
    if working_dir:
        lines.append("WorkingDirectory=%s" % working_dir)
    if version is None or version >= APPEND_SINCE:
        lines.append("StandardOutput=append:%s" % log_path)
        lines.append("StandardError=append:%s" % log_path)
    else:
        lines.append("# systemd %s is older than %d, so append: is unavailable;"
                     % (version, APPEND_SINCE))
        lines.append("# the daemon's output goes to the journal instead:")
        lines.append("#     journalctl --user -u %s -f" % UNIT_NAME)
    lines += [
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def systemctl(*args, **kwargs):
    """`systemctl --user ...`; False when it fails or is not there at all."""
    import subprocess

    quiet = kwargs.get("quiet", True)
    try:
        result = subprocess.run(["systemctl", "--user"] + list(args),
                                capture_output=True, text=True)
    except OSError:
        return False
    if result.returncode != 0 and not quiet:
        sys.stderr.write(result.stderr)
    return result.returncode == 0


def systemd_available():
    """Is there a user systemd to talk to?

    `systemctl --user` exists inside containers and under WSL1 without a user
    bus behind it, where every verb fails with "Failed to connect to bus". So
    ask it something harmless rather than trusting the binary's presence.
    `show` and not `is-system-running`: the latter exits non-zero on a merely
    degraded system, which is a running systemd by any useful definition.
    """
    return systemctl("show", "--property=Version")


def write_unit(python, log_path, script=None, version="probe"):
    """Write the unit file, returning its path."""
    script = script or os.path.join(REPO_DIR, "display.py")
    if version == "probe":
        version = systemd_version()
    os.makedirs(UNIT_DIR, exist_ok=True)
    text = unit_text(python, script, log_path, version=version)
    with open(UNIT_PATH, "w") as handle:
        handle.write(text)
    return UNIT_PATH


def enable_linger():
    """Ask logind to keep this user's units running with nobody logged in.

    The whole point of the headless install: without lingering, a `--user`
    service starts when you ssh in and is killed when you log out, so the
    panel would be driven only while somebody is watching. Non-root callers
    usually need polkit to say yes, which on a headless box means no prompt
    and a refusal -- hence the printed fallback rather than a failure.
    """
    import subprocess

    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    try:
        result = subprocess.run(["loginctl", "enable-linger"] + ([user] if user else []),
                                capture_output=True, text=True)
    except OSError:
        return False
    return result.returncode == 0


def install_service(python, log_path, script=None):
    """Write, enable and start the user unit. Never raises; reports instead.

    Returns True when the daemon is actually running under systemd. A False
    is not an install failure -- the files are on disk and the daemon runs by
    hand -- so the caller prints the fallback and carries on.
    """
    path = write_unit(python, log_path, script=script)
    print("wrote %s" % path)

    if not systemd_available():
        print("NOTE: no user systemd on this host (container, WSL1, or no user bus).")
        print("      The unit is written but not loaded. Run the daemon directly:")
        print("          %s %s --daemon" % (python, script or os.path.join(REPO_DIR, "display.py")))
        return False

    systemctl("daemon-reload")
    if not systemctl("enable", "--now", UNIT_NAME, quiet=False):
        # `enable --now` is one verb doing two jobs; when it fails it is worth
        # knowing which half, so retry them apart before giving up.
        systemctl("enable", UNIT_NAME)
        if not systemctl("restart", UNIT_NAME, quiet=False):
            print("NOTE: could not start %s. Check:" % UNIT_NAME)
            for line in status_commands():
                print("          %s" % line)
            return False
    print("enabled and started %s" % UNIT_NAME)

    if enable_linger():
        print("enabled lingering, so the panel keeps updating with nobody logged in")
    else:
        print("NOTE: could not enable lingering for this user. Without it the")
        print("      daemon stops when your last session ends. Ask an admin for:")
        print("          sudo loginctl enable-linger %s"
              % (os.environ.get("USER") or "$USER"))
    return True


def uninstall_service():
    systemctl("disable", "--now", UNIT_NAME)
    try:
        os.unlink(UNIT_PATH)
        print("removed %s" % UNIT_PATH)
    except OSError:
        pass
    systemctl("daemon-reload")
    systemctl("reset-failed", UNIT_NAME)


def restart_command(launch_label):
    """The one-liner that restarts the daemon on this host."""
    if is_macos():
        return "launchctl kickstart -k gui/%d/%s" % (os.getuid(), launch_label)
    if is_linux():
        return "systemctl --user restart %s" % UNIT_NAME
    return "restart the display daemon"


def status_commands(log_path=None):
    """How to see what the daemon is doing, most useful first."""
    if is_linux():
        out = ["systemctl --user status %s" % UNIT_NAME,
               "journalctl --user -u %s -f" % UNIT_NAME]
    else:
        out = ["launchctl print gui/%d/com.williamliu.context-keyboard-display"
               % os.getuid()]
    if log_path:
        out.append("tail -f %s" % log_path)
    return out


# -------------------------------------------------------------------- fonts
#
# keyboard_status hardcodes the macOS system faces, which is right there and
# useless here. It looks them up through its own module globals at every
# `font()` call, so pointing those at a Linux face is enough -- no fork of the
# renderer, no divergence in layout, and macOS never takes this path.
#
# Variable faces first: the renderer asks for weights 600-800 by setting the
# Weight axis, and a static face silently collapses all of them into one, so
# the pills and headings stop standing out from the body text.

LINUX_TEXT_FONTS = (
    "/usr/share/fonts/truetype/noto/NotoSans[wdth,wght].ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
)
# Rounded has no free equivalent worth chasing; the text face stands in, which
# costs the corner radius on glyphs and nothing else.
LINUX_ROUNDED_FONTS = (
    "/usr/share/fonts/truetype/noto/NotoSans[wdth,wght].ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
) + LINUX_TEXT_FONTS
LINUX_CJK_FONTS = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
)

# apt package names, printed when nothing is found. No sudo is assumed and
# none is run: this is a suggestion in a warning, not a step in the install.
LINUX_FONT_PACKAGES = "fonts-noto-core fonts-noto-cjk fonts-dejavu-core"


def _first_existing(candidates, exists):
    for path in candidates:
        if exists(path):
            return path
    return None


def font_choice(exists=os.path.exists):
    """Which faces the Linux fallback would use, or None when none are installed.

    Pure but for `exists`, which the tests replace -- the interesting cases
    (CJK missing, nothing installed at all) are not reproducible by looking at
    whatever fonts this machine happens to have.
    """
    text = _first_existing(LINUX_TEXT_FONTS, exists)
    rounded = _first_existing(LINUX_ROUNDED_FONTS, exists) or text
    if not text:
        return None
    cjk = _first_existing(LINUX_CJK_FONTS, exists)
    return {
        "FONT_TEXT": text,
        "FONT_ROUNDED": rounded,
        # Falling back to the latin face for CJK is wrong-looking but not
        # broken: the renderer still measures and draws, tofu instead of
        # hanzi. Dropping to Pillow's 11 px bitmap default -- what an
        # unreadable .ttc index would cause -- is broken.
        "FONT_CJK": cjk or text,
        # The macOS indices select PingFang SC out of a five-face collection.
        # Nothing else has that layout, so take face 0 of whatever we found.
        "FONT_CJK_INDEX": 0,
        "FONT_CJK_BOLD_INDEX": 0,
        "has_cjk": bool(cjk),
    }


_FONT_WARNED = False


def apply_font_fallback(ks_module, exists=os.path.exists, warn=None):
    """Point keyboard_status at Linux faces. No-op anywhere but Linux.

    Returns the choice that was applied, or None when this host needs no
    fallback or has no usable face to fall back to. Safe to call repeatedly:
    the daemon runs it on every import_stack, and a missing-font warning that
    repeated once a tick would bury the log it is trying to be seen in.
    """
    global _FONT_WARNED

    if not is_linux():
        return None
    choice = font_choice(exists=exists)
    if not choice:
        if not _FONT_WARNED:
            _FONT_WARNED = True
            warn = warn or (lambda message: sys.stderr.write(message + "\n"))
            warn("no usable system font found; text will render in Pillow's "
                 "bitmap default. Install one, e.g.: apt install %s"
                 % LINUX_FONT_PACKAGES)
        return None
    for key in ("FONT_TEXT", "FONT_ROUNDED", "FONT_CJK",
                "FONT_CJK_INDEX", "FONT_CJK_BOLD_INDEX"):
        setattr(ks_module, key, choice[key])
    return choice


# -------------------------------------------------------------------- entry

USAGE = [p for p in __doc__.split("\n\n") if "python3 service.py" in p][0] + "\n"


def main(argv):
    args = argv[1:]
    command = args[0] if args else "--help"

    if command == "--platform":
        print("system:   %s" % (system_name() or "unknown"))
        print("service:  %s" % ("launchd (~/Library/LaunchAgents)" if is_macos()
                                else "systemd --user (%s)" % UNIT_PATH if is_linux()
                                else "none -- run --daemon yourself"))
        print("hotkeys:  %s" % ("supported (keys.py)" if hotkeys_supported()
                                else "not supported on this platform"))
        if is_linux():
            print("systemd:  %s" % (systemd_version() or "not detected"))
            choice = font_choice()
            print("fonts:    %s" % (choice["FONT_TEXT"] if choice else
                                    "none found (apt install %s)" % LINUX_FONT_PACKAGES))
        return 0
    if command == "--print-unit":
        python = args[1] if len(args) > 1 else os.path.join(REPO_DIR, ".venv", "bin", "python3")
        log_path = os.path.join(os.path.expanduser("~/.claude"),
                                "context-keyboard-display.log")
        sys.stdout.write(unit_text(python, os.path.join(REPO_DIR, "display.py"),
                                   log_path, version=systemd_version()))
        return 0
    if command == "--fonts":
        choice = font_choice()
        if not choice:
            print("no usable font found. apt install %s" % LINUX_FONT_PACKAGES)
            return 1
        for key in ("FONT_ROUNDED", "FONT_TEXT", "FONT_CJK"):
            print("%-14s %s" % (key, choice[key]))
        if not choice["has_cjk"]:
            print("(no CJK face:東京 renders as tofu. apt install fonts-noto-cjk)")
        return 0
    sys.stdout.write(USAGE)
    return 0 if command in ("--help", "-h") else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))

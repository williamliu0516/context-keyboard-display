#!/bin/sh
# Fail fast and legibly on the three things that are wrong from outside the
# container, then get out of the way. Everything here is a mount or an
# environment problem: the daemon itself has good diagnostics, but it cannot
# report on a ~/.claude that was never mounted, because then there is no
# config, no transcripts and no panel address -- it would just log "the
# keyboard's address is not set yet" and look like a config mistake.
set -eu

: "${HOME:?HOME must be set (the image sets /host; ~/.claude mounts under it)}"
claude_dir="$HOME/.claude"

if [ ! -d "$claude_dir" ]; then
	echo "ckd: $claude_dir is not mounted." >&2
	echo "ckd: the live Claude Code state lives there -- config, control," >&2
	echo "ckd: status and the session transcripts this daemon reads. Mount it:" >&2
	echo "ckd:     -v \"\$HOME/.claude:$claude_dir\"" >&2
	exit 78 # EX_CONFIG
fi

# Writable, not just present: the daemon publishes the screen it chose to
# $claude_dir/context-keyboard-display-status.json, which is what the native
# hotkey listener reads to know whether the switchboard is the screen showing.
# A read-only mount would degrade the drill-down key silently.
probe="$claude_dir/.ckd-container-write-probe.$$"
if ! (: >"$probe") 2>/dev/null; then
	echo "ckd: $claude_dir is mounted read-only, or this uid cannot write it." >&2
	echo "ckd: uid=$(id -u) gid=$(id -g); set CKD_UID/CKD_GID to your host user" >&2
	echo "ckd: (id -u / id -g on the Mac) and re-create the container." >&2
	exit 78
fi
rm -f "$probe"

# The heartbeat lives on a container-local tmpfs by default; the health check
# reads it back, so its directory has to exist before the first tick.
if [ -n "${CKD_HEARTBEAT_PATH:-}" ]; then
	mkdir -p "$(dirname "$CKD_HEARTBEAT_PATH")" 2>/dev/null || true
fi

# One banner, then the daemon's own logging. `service.py --platform` is the
# honest answer to "what did this host resolve to" -- inside the container it
# says Linux, systemd-less, no hotkeys, which is exactly right.
echo "ckd: $(python3 -c 'import platform,sys; print("%s %s, python %s" % (platform.system(), platform.machine(), sys.version.split()[0]))')"
echo "ckd: HOME=$HOME  uid=$(id -u):$(id -g)  TZ=${TZ:-unset (UTC: the Idle clock will be wrong)}"
python3 /app/service.py --fonts | sed 's/^/ckd: font /'

# exec, so the daemon is PID 1's child under `init: true` and SIGTERM from
# `docker stop` reaches Python rather than this shell.
exec python3 /app/display.py "$@"

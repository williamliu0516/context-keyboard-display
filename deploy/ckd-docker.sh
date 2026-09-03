#!/bin/bash
# Deploy, verify and migrate the display daemon into Docker on macOS -- and
# back out again. No sudo anywhere: every launchd verb is in the per-user
# `gui/$UID` domain, and every file written is under $HOME.
#
#     deploy/ckd-docker.sh migrate     the whole flow (idempotent, safe to re-run)
#     deploy/ckd-docker.sh status      who owns the panel right now
#     deploy/ckd-docker.sh rollback    hand the panel back to the launchd agent
#     deploy/ckd-docker.sh help        every command
#
# THE ONE RULE this file exists to enforce: exactly one process may push frames
# to the panel. Two pushers means two daemons fighting over a 142x428 screen at
# 1 Hz, which looks like a flickering bug and is nearly impossible to diagnose
# from the panel. So the migration hands over through an interlock rather than
# a hope: the container starts in *standby* (renders, publishes status,
# heartbeats -- pushes nothing), has to pass a health check and land a real
# frame on the real configured panel from inside the container, and only then
# is the native launchd agent booted out and the standby lifted. If any step
# fails, the container is stopped and the native agent is left exactly as it
# was, still pushing.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$REPO_DIR/docker-compose.yml"
SERVICE=display
CONTAINER=ckd-display

CLAUDE_DIR="$HOME/.claude"
CONFIG_PATH="$CLAUDE_DIR/context-keyboard-display.yaml"

NATIVE_LABEL="com.williamliu.context-keyboard-display"

# The three paths whose *existence* decides what migrate and rollback do:
# whether there is a native agent to hand over from, and whether the panel is
# currently spoken for. They are overridable for one reason -- the ordering
# tests in tests/test_docker.py drive CKD_DRY_RUN=1 against a temporary state
# directory, so the plan they read back is the same on a fresh Mac as on one
# that is already migrated. Real runs set none of these; the same test-hook
# convention as service.py's CKD_PLATFORM.
#
# The standby file must stay in step with CKD_STANDBY_PATH in
# docker-compose.yml, which is the container's view of the same file.
STANDBY_PATH="${CKD_STANDBY_FILE:-$CLAUDE_DIR/context-keyboard-display-standby}"
NATIVE_PLIST="${CKD_NATIVE_PLIST:-$HOME/Library/LaunchAgents/$NATIVE_LABEL.plist}"
# Where the native agent's plist is parked while Docker owns the panel. Parked
# rather than deleted: rollback is then a file move, and the thing being
# restored is the plist that was actually working, not a regenerated guess.
PARK_DIR="${CKD_PARK_DIR:-$CLAUDE_DIR/ckd-native-agent-disabled}"
PARKED_PLIST="$PARK_DIR/$NATIVE_LABEL.plist"
# The hotkey listener. Named here only so it is obvious that nothing in this
# file touches it: the global hotkeys are a Carbon reservation against
# WindowServer and stay native, container or no container.
KEYS_LABEL="com.williamliu.context-keyboard-keys"

DOCKER_LABEL="com.williamliu.context-keyboard-display-docker"
DOCKER_PLIST="$HOME/Library/LaunchAgents/$DOCKER_LABEL.plist"
BOOT_SCRIPT="$REPO_DIR/deploy/ckd-docker-boot.sh"
AGENT_LOG="$CLAUDE_DIR/context-keyboard-display-docker.log"

HEALTH_WAIT_SECONDS="${CKD_HEALTH_WAIT:-180}"
HANDOFF_WAIT_SECONDS="${CKD_HANDOFF_WAIT:-60}"
# Fires every 5 minutes while the Mac is awake, and launchd runs a missed
# StartInterval job as soon as the machine wakes -- which is what makes "runs
# whenever the Mac is awake" true without a KeepAlive that would thrash.
AGENT_INTERVAL="${CKD_AGENT_INTERVAL:-300}"

DRY_RUN="${CKD_DRY_RUN:-0}"

# ------------------------------------------------------------------ plumbing

say()  { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# Every state-changing command goes through run(), which is what makes the
# migration testable: CKD_DRY_RUN=1 prints the plan, in order, and changes
# nothing -- see tests/test_docker.py, which asserts that the native agent is
# never booted out before the verification push has run.
run() {
	if [ "$DRY_RUN" = "1" ]; then
		printf 'DRYRUN: %s\n' "$*"
		return 0
	fi
	"$@"
}

dry() { [ "$DRY_RUN" = "1" ]; }

compose() { run docker compose -f "$COMPOSE_FILE" "$@"; }
# Read-only compose/docker queries, so they stay real under --dry-run.
compose_q() { docker compose -f "$COMPOSE_FILE" "$@"; }

launch_domain() { printf 'gui/%s' "$(id -u)"; }

# launchctl says "not found" a lot -- booting out something already gone, or
# printing a label that was never loaded -- and none of that is an error here.
# Not routed through run(), because run() cannot both swallow the output and
# show the plan: under --dry-run this has to print, since the order of these
# verbs relative to the verification push is exactly what the tests assert.
launchctl_soft() {
	if dry; then
		printf 'DRYRUN: launchctl %s\n' "$*"
		return 0
	fi
	launchctl "$@" >/dev/null 2>&1 || true
}

label_loaded() {
	launchctl print "$(launch_domain)/$1" >/dev/null 2>&1
}

native_installed() { [ -f "$NATIVE_PLIST" ]; }
native_parked()    { [ -f "$PARKED_PLIST" ]; }
standby_on()       { [ -f "$STANDBY_PATH" ]; }

# Both of these normalise "no such container" to `absent`: docker inspect
# fails *and* prints an empty line for a missing name, so the fallback alone
# would leave a stray newline in the middle of every status report.
container_state() {
	local out
	out="$(docker inspect --format '{{.State.Status}}' "$CONTAINER" 2>/dev/null | tr -d '\n')"
	printf '%s' "${out:-absent}"
}

container_health() {
	local out
	out="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
		"$CONTAINER" 2>/dev/null | tr -d '\n')"
	printf '%s' "${out:-absent}"
}

heartbeat_json() {
	docker exec "$CONTAINER" sh -c 'cat "$CKD_HEARTBEAT_PATH" 2>/dev/null' 2>/dev/null || true
}

# ----------------------------------------------------------------- preflight

# The address the daemon is actually configured to push to, read from the live
# config the way display.py reads it. Never written to a file in this repo.
configured_url() {
	python3 - "$@" <<-'PY'
		import os, sys
		sys.path.insert(0, os.environ["CKD_REPO_DIR"])
		import display
		print(display.load_config()["url"])
	PY
}

url_host_port() {
	CKD_URL="$1" python3 - <<-'PY'
		import os
		from urllib.parse import urlsplit
		parts = urlsplit(os.environ["CKD_URL"])
		print("%s %d" % (parts.hostname or "", parts.port or 80))
	PY
}

cmd_preflight() {
	local failed=0 url host port probe_image

	step "preflight"

	if ! command -v docker >/dev/null 2>&1; then
		warn "no docker CLI on PATH. Install Docker Desktop for Mac."
		failed=1
	elif ! docker info >/dev/null 2>&1; then
		warn "the Docker daemon is not answering. Start Docker Desktop:  open -ga Docker"
		failed=1
	else
		say "  ok    docker: $(docker version --format '{{.Server.Version}}' 2>/dev/null)"
	fi

	if [ "$failed" = "0" ]; then
		if docker compose version >/dev/null 2>&1; then
			say "  ok    compose: $(docker compose version --short 2>/dev/null)"
		else
			warn "docker compose v2 is required (this is a compose-spec file)."
			failed=1
		fi
	fi

	local upstream
	upstream="$(upstream_path)"
	if [ -f "$upstream/keyboard_status.py" ] && [ -f "$upstream/pyproject.toml" ]; then
		say "  ok    upstream library: $upstream"
	else
		warn "no claude-code-keyboard-status checkout at $upstream"
		warn "      set CKD_UPSTREAM_PATH in $REPO_DIR/.env"
		failed=1
	fi

	if [ -d "$CLAUDE_DIR" ] && [ -w "$CLAUDE_DIR" ]; then
		say "  ok    live state: $CLAUDE_DIR (writable)"
	else
		warn "$CLAUDE_DIR is missing or not writable"
		failed=1
	fi

	if [ -f "$CONFIG_PATH" ]; then
		say "  ok    config: $CONFIG_PATH"
	else
		warn "no $CONFIG_PATH. Run: python3 $REPO_DIR/display.py --install"
		failed=1
	fi

	url="$(CKD_REPO_DIR="$REPO_DIR" configured_url 2>/dev/null || true)"
	case "$url" in
	""|*PANEL-IP-NOT-SET*)
		warn "the panel's address is not set in $CONFIG_PATH"
		failed=1
		;;
	*)
		say "  ok    panel url: $url"
		;;
	esac

	# The one thing that cannot be assumed and is the whole risk of this
	# deployment: Docker Desktop's VM NATs outbound traffic, and the panel is a
	# device on the LAN, not on the host. If a container cannot open a socket
	# to it, the container can never drive the display -- so say so and leave
	# launchd alone rather than migrating into a daemon that pushes into the
	# void. Probed with the image if it exists, else the base python image.
	if [ "$failed" = "0" ]; then
		set -- $(url_host_port "$url")
		host="${1:-}"; port="${2:-80}"
		probe_image="context-keyboard-display:local"
		docker image inspect "$probe_image" >/dev/null 2>&1 || \
			probe_image="python:${CKD_PYTHON_TAG:-3.13-slim-bookworm}"
		say "  ..    probing $host:$port from a container ($probe_image)"
		if docker run --rm -e CKD_HOST="$host" -e CKD_PORT="$port" \
			--entrypoint python3 "$probe_image" -c '
import os, socket, sys
try:
    socket.create_connection((os.environ["CKD_HOST"], int(os.environ["CKD_PORT"])), timeout=4).close()
except OSError as error:
    sys.exit("unreachable: %s" % error)
' >/dev/null 2>&1; then
			say "  ok    LAN reachable from a container: $host:$port"
		else
			warn "a container cannot reach $host:$port."
			warn "      Docker Desktop cannot route to the panel from here, so the"
			warn "      containerised daemon could never drive it. NOT migrating;"
			warn "      the native launchd pusher is left in charge."
			warn "      (Is the panel awake? Try: nc -z -G 2 $host $port from the Mac.)"
			failed=1
		fi
	fi

	[ "$failed" = "0" ] || die "preflight failed; nothing was changed"
	say "  ok    preflight passed"
}

# --------------------------------------------------------------------- .env

upstream_path() {
	local value=""
	if [ -f "$REPO_DIR/.env" ]; then
		value="$(sed -n 's/^CKD_UPSTREAM_PATH=//p' "$REPO_DIR/.env" | tail -1)"
	fi
	value="${CKD_UPSTREAM_PATH:-${value:-$REPO_DIR/../claude-code-keyboard-status}}"
	# Resolved to a real absolute path when it exists, because it ends up in
	# .env and in `docker compose config`, where "..../../foo" is legal but
	# unreadable. Left verbatim when it does not exist, so preflight can name
	# what the user actually typed.
	(cd "$REPO_DIR" && cd "$value" 2>/dev/null && pwd) || printf '%s' "$value"
}

host_timezone() {
	# /etc/localtime is a symlink into the zoneinfo tree on macOS; its tail is
	# the Olson name tzdata wants inside the container.
	local link
	link="$(readlink /etc/localtime 2>/dev/null || true)"
	case "$link" in
	*/zoneinfo/*) printf '%s' "${link##*/zoneinfo/}" ;;
	*) printf '%s' "${TZ:-UTC}" ;;
	esac
}

cmd_env() {
	step "writing $REPO_DIR/.env"
	local tz upstream projects
	tz="$(host_timezone)"
	upstream="$(upstream_path)"
	projects="${CKD_PROJECTS_DIR:-$HOME/projects}"
	[ -d "$projects" ] || { warn "no $projects; the +/- diff counts will be blank"; }

	if dry; then
		printf 'DRYRUN: write .env (CKD_UID=%s CKD_GID=%s TZ=%s)\n' "$(id -u)" "$(id -g)" "$tz"
		return 0
	fi
	cat > "$REPO_DIR/.env" <<-ENV
		# Written by deploy/ckd-docker.sh env -- host-specific, not tracked.
		# The panel's address is NOT here; it stays in
		# ~/.claude/context-keyboard-display.yaml and is read live via the mount.
		CKD_UID=$(id -u)
		CKD_GID=$(id -g)
		TZ=$tz
		CKD_UPSTREAM_PATH=$upstream
		CKD_PROJECTS_DIR=$projects
		CKD_PYTHON_TAG=${CKD_PYTHON_TAG:-3.13-slim-bookworm}
		CKD_INSTALL_CJK_FONT=${CKD_INSTALL_CJK_FONT:-1}
	ENV
	sed 's/^/  /' "$REPO_DIR/.env"
}

# ------------------------------------------------------- build / up / health

cmd_build() {
	step "building the image"
	compose build "$@"
}

# Starting the container is *always* standby-safe: if the native agent is still
# in charge, the standby file goes down first, so there is no window in which
# two processes could push.
cmd_up() {
	if native_installed && label_loaded "$NATIVE_LABEL"; then
		standby_engage "the native launchd agent is still loaded"
	fi
	step "starting the compose service"
	compose up -d "$@"
	compose_q ps || true
}

cmd_down() {
	step "stopping and removing the container"
	compose down "$@"
}

cmd_restart() {
	step "restarting the container"
	compose restart
}

cmd_logs() {
	compose_q logs "$@"
}

cmd_ps() {
	compose_q ps
}

standby_engage() {
	# `! dry &&`: under --dry-run the plan is the output, so the intended touch
	# has to print whether or not the file happens to exist on this machine --
	# tests/test_docker.py reads that ordering.
	if ! dry && standby_on; then
		say "standby already engaged ($STANDBY_PATH)"
		return 0
	fi
	step "engaging standby: the container will render but not push"
	say "reason: ${1:-requested}"
	run touch "$STANDBY_PATH"
}

standby_release() {
	if ! dry && ! standby_on; then
		say "standby already released"
		return 0
	fi
	step "releasing standby: the container takes the panel"
	run rm -f "$STANDBY_PATH"
}

cmd_health() {
	local deadline state health
	deadline=$(( $(date +%s) + HEALTH_WAIT_SECONDS ))
	step "waiting for container health (up to ${HEALTH_WAIT_SECONDS}s)"
	if dry; then
		printf 'DRYRUN: wait for %s to report healthy\n' "$CONTAINER"
		return 0
	fi
	while :; do
		state="$(container_state)"
		health="$(container_health)"
		case "$state" in
		running)
			case "$health" in
			healthy)
				say "  container is running and healthy"
				docker inspect --format '{{range .State.Health.Log}}{{.Output}}{{end}}' "$CONTAINER" \
					2>/dev/null | tail -1 | sed 's/^/  health says: /'
				return 0
				;;
			unhealthy)
				docker inspect --format '{{range .State.Health.Log}}{{.Output}}{{end}}' "$CONTAINER" \
					2>/dev/null | tail -3 | sed 's/^/  /' >&2
				die "container reports unhealthy"
				;;
			esac
			;;
		absent)
			die "container $CONTAINER does not exist (run: $0 up)"
			;;
		exited|dead)
			compose_q logs --tail 30 "$SERVICE" >&2 || true
			die "container is $state"
			;;
		esac
		if [ "$(date +%s)" -ge "$deadline" ]; then
			compose_q logs --tail 30 "$SERVICE" >&2 || true
			die "timed out waiting for health (state=$state health=$health)"
		fi
		sleep 3
	done
}

# ------------------------------------------------------------- verification

# The step the whole migration hangs on: a real `display.py --once` inside the
# running container, rendering the live state and POSTing the JPEG to the
# address in the live config. Not a ping, not a dry run -- the exit code is 0
# only if the panel took the frame.
cmd_push_test() {
	step "verifying a real configured push from inside the container"
	if dry; then
		printf 'DRYRUN: docker exec %s python3 /app/display.py --once\n' "$CONTAINER"
		return 0
	fi
	[ "$(container_state)" = "running" ] || die "container is not running"

	say "--- the container's view of the config ---"
	docker exec "$CONTAINER" python3 -c '
import sys
sys.path.insert(0, "/app")
import display, service
cfg = display.load_config()
print("url:          %s" % cfg["url"])
print("upstream lib: %s" % __import__("keyboard_status").__file__)
print("platform:     %s" % service.system_name())
print("home:         %s" % __import__("os").path.expanduser("~"))
'
	say "--- display.py --once (renders and POSTs) ---"
	if docker exec "$CONTAINER" python3 /app/display.py --once; then
		say "push OK: the panel accepted a frame from the container"
		return 0
	fi
	warn "the container could not push a frame to the configured panel."
	warn "the native launchd pusher has NOT been touched."
	return 1
}

# ------------------------------------------------- the native launchd agent

native_disable() {
	step "disabling the native launchd display agent"
	local domain
	domain="$(launch_domain)"
	launchctl_soft bootout "$domain/$NATIVE_LABEL"
	# `disable` outlives the plist: if anything later re-writes it (a stray
	# `display.py --install`), launchd still will not start it, so the
	# one-pusher rule survives a mistake rather than depending on nobody
	# making one.
	launchctl_soft disable "$domain/$NATIVE_LABEL"
	if native_installed; then
		run mkdir -p "$PARK_DIR"
		run mv "$NATIVE_PLIST" "$PARKED_PLIST"
		say "parked $NATIVE_PLIST -> $PARKED_PLIST"
	else
		say "no plist at $NATIVE_PLIST (already parked or never installed)"
	fi
	if ! dry && label_loaded "$NATIVE_LABEL"; then
		warn "$NATIVE_LABEL is still loaded; two pushers would fight over the panel"
		return 1
	fi
	say "native agent is booted out and disabled; $KEYS_LABEL left untouched"
}

native_restore() {
	step "restoring the native launchd display agent"
	local domain
	domain="$(launch_domain)"
	if native_parked; then
		run mkdir -p "$(dirname "$NATIVE_PLIST")"
		run mv "$PARKED_PLIST" "$NATIVE_PLIST"
		say "restored $NATIVE_PLIST"
	elif native_installed; then
		say "$NATIVE_PLIST is already in place"
	else
		warn "no parked plist and none installed; regenerating with display.py --install"
		run python3 "$REPO_DIR/display.py" --install
	fi
	launchctl_soft enable "$domain/$NATIVE_LABEL"
	launchctl_soft bootout "$domain/$NATIVE_LABEL"
	if ! run launchctl bootstrap "$domain" "$NATIVE_PLIST"; then
		launchctl_soft load -w "$NATIVE_PLIST"
	fi
	if dry; then return 0; fi
	sleep 2
	if label_loaded "$NATIVE_LABEL"; then
		say "native agent is loaded again and owns the panel"
	else
		warn "could not confirm $NATIVE_LABEL is loaded; check:"
		warn "      launchctl print $domain/$NATIVE_LABEL"
		return 1
	fi
}

# ----------------------------------------------- the login / wake LaunchAgent

cmd_agent_plist() {
	CKD_LABEL="$DOCKER_LABEL" CKD_BOOT="$BOOT_SCRIPT" CKD_LOG="$AGENT_LOG" \
	CKD_INTERVAL="$AGENT_INTERVAL" CKD_REPO="$REPO_DIR" python3 - <<-'PY'
		import os, plistlib, sys

		# RunAtLoad covers login; StartInterval covers wake -- launchd runs a
		# StartInterval job it missed while the Mac was asleep as soon as it
		# wakes, which is exactly the "operate whenever the Mac is awake"
		# requirement, and it re-checks every interval in case Docker Desktop
		# was slow, updating, or quit by hand.
		#
		# Deliberately NOT KeepAlive: the boot script is a short idempotent
		# `compose up -d`, and KeepAlive on a script that exits would respawn
		# it forever. The container's own `restart: unless-stopped` is what
		# keeps the daemon alive between these checks.
		agent = {
		    "Label": os.environ["CKD_LABEL"],
		    "ProgramArguments": ["/bin/bash", os.environ["CKD_BOOT"]],
		    "RunAtLoad": True,
		    "StartInterval": int(os.environ["CKD_INTERVAL"]),
		    "StandardOutPath": os.environ["CKD_LOG"],
		    "StandardErrorPath": os.environ["CKD_LOG"],
		    "ProcessType": "Background",
		    # launchd hands agents a minimal PATH that does not include
		    # /usr/local/bin, where Docker Desktop puts the docker CLI. Without
		    # this the agent fails with "docker: command not found" at every
		    # login and the container never comes up.
		    "EnvironmentVariables": {
		        "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
		        "CKD_REPO": os.environ["CKD_REPO"],
		    },
		}
		sys.stdout.buffer.write(plistlib.dumps(agent))
	PY
}

cmd_agent_install() {
	step "installing the login/wake LaunchAgent ($DOCKER_LABEL)"
	local domain
	domain="$(launch_domain)"
	[ -f "$BOOT_SCRIPT" ] || die "missing $BOOT_SCRIPT"
	if dry; then
		printf 'DRYRUN: write %s and bootstrap it into %s\n' "$DOCKER_PLIST" "$domain"
		return 0
	fi
	mkdir -p "$(dirname "$DOCKER_PLIST")"
	cmd_agent_plist > "$DOCKER_PLIST"
	chmod +x "$BOOT_SCRIPT" 2>/dev/null || true
	launchctl_soft enable "$domain/$DOCKER_LABEL"
	launchctl_soft bootout "$domain/$DOCKER_LABEL"
	if ! launchctl bootstrap "$domain" "$DOCKER_PLIST"; then
		launchctl_soft load -w "$DOCKER_PLIST"
	fi
	if label_loaded "$DOCKER_LABEL"; then
		say "loaded $DOCKER_LABEL (RunAtLoad + every ${AGENT_INTERVAL}s while awake)"
		say "log: $AGENT_LOG"
	else
		warn "could not confirm $DOCKER_LABEL is loaded; check:"
		warn "      launchctl print $domain/$DOCKER_LABEL"
	fi
}

cmd_agent_uninstall() {
	step "removing the login/wake LaunchAgent"
	launchctl_soft bootout "$(launch_domain)/$DOCKER_LABEL"
	run rm -f "$DOCKER_PLIST"
	say "removed $DOCKER_PLIST"
}

# ------------------------------------------------------------------- status

cmd_status() {
	local state health url heartbeat owner
	state="$(container_state)"
	health="$(container_health)"

	say "repo:        $REPO_DIR"
	say "compose:     $COMPOSE_FILE"
	say "container:   $CONTAINER  state=$state  health=$health"
	if [ "$state" = "running" ]; then
		heartbeat="$(heartbeat_json)"
		say "heartbeat:   ${heartbeat:-<none yet>}"
		docker inspect --format '{{range .State.Health.Log}}{{.Output}}{{end}}' "$CONTAINER" \
			2>/dev/null | tail -1 | sed 's/^/health:      /'
		say "restart:     $(docker inspect --format '{{.HostConfig.RestartPolicy.Name}}' "$CONTAINER" 2>/dev/null)"
	fi
	say "standby:     $(standby_on && echo "ON  ($STANDBY_PATH) -- container is NOT pushing" || echo "off -- the container pushes when running")"

	if native_installed; then
		say "native agent: installed at $NATIVE_PLIST"
	elif native_parked; then
		say "native agent: parked at $PARKED_PLIST (rollback restores it)"
	else
		say "native agent: not installed"
	fi
	say "             loaded=$(label_loaded "$NATIVE_LABEL" && echo yes || echo no)"
	say "hotkeys:     $KEYS_LABEL loaded=$(label_loaded "$KEYS_LABEL" && echo yes || echo no) (native, by design)"
	say "wake agent:  $DOCKER_LABEL loaded=$(label_loaded "$DOCKER_LABEL" && echo yes || echo no)"

	url="$(CKD_REPO_DIR="$REPO_DIR" configured_url 2>/dev/null || true)"
	say "panel url:   ${url:-<unreadable>}"

	# The one line worth reading: who is actually allowed to push.
	if label_loaded "$NATIVE_LABEL" && [ "$state" = "running" ] && ! standby_on; then
		owner="BOTH (!) -- run '$0 rollback' or '$0 migrate' to resolve"
	elif label_loaded "$NATIVE_LABEL"; then
		owner="the native launchd agent"
	elif [ "$state" = "running" ] && ! standby_on; then
		owner="the container"
	else
		owner="nobody"
	fi
	say ""
	say "pushing to the panel: $owner"
}

# ------------------------------------------------------------------ migrate

cmd_migrate() {
	local handoff_deadline heartbeat

	say "migrating the display daemon into Docker."
	say "the native launchd agent keeps the panel until the container has"
	say "proven itself healthy AND landed a real frame on the real panel."

	cmd_preflight
	cmd_env
	cmd_build

	# (0) The interlock, before anything of ours is running.
	if native_installed || label_loaded "$NATIVE_LABEL"; then
		standby_engage "the native launchd agent still owns the panel"
	else
		say "no native agent to hand over from; skipping standby"
	fi

	# (1) start + health-check
	step "starting the container"
	compose up -d
	cmd_health

	# (2) a real configured push from inside the container
	if ! cmd_push_test; then
		step "aborting: stopping the container, leaving launchd in charge"
		compose stop || true
		standby_release
		die "verification push failed; nothing was migrated"
	fi

	# (3) only now does the native agent lose the panel
	if ! native_disable; then
		step "aborting: could not stop the native agent, so the container stays parked"
		compose stop || true
		die "the native agent is still loaded; refusing to run two pushers"
	fi

	# (4) hand the panel over
	standby_release

	step "confirming the container took the panel"
	if dry; then
		printf 'DRYRUN: wait for a heartbeat with standby=false and a fresh push\n'
	else
		handoff_deadline=$(( $(date +%s) + HANDOFF_WAIT_SECONDS ))
		while :; do
			heartbeat="$(heartbeat_json)"
			case "$heartbeat" in
			*'"standby": false'*|*'"standby":false'*)
				say "  heartbeat: $heartbeat"
				break
				;;
			esac
			if [ "$(date +%s)" -ge "$handoff_deadline" ]; then
				warn "the container has not reported a post-standby tick yet."
				warn "      check: $0 logs --tail 50"
				break
			fi
			sleep 2
		done
	fi

	# (5) survive login, reboot and wake
	cmd_agent_install

	step "done"
	cmd_status
}

cmd_rollback() {
	say "rolling back to the native launchd pusher."

	# Order matters and is the mirror of migrate: stop the container pushing
	# *before* the native agent starts, so the two never overlap.
	standby_engage "rolling back; the native agent is about to take the panel"
	if ! dry; then
		say "waiting one tick for the container to notice standby"
		sleep 3
	fi

	cmd_agent_uninstall
	native_restore || warn "restore incomplete -- see the warnings above"

	step "stopping the container"
	compose down || true

	# The standby file is left in place on purpose: it is the guard that stops
	# a hand-run `docker compose up -d` from pushing behind the native agent's
	# back. `migrate` lifts it as its last step, which is the only path that
	# has verified it is safe to.
	say "left $STANDBY_PATH in place, so a hand-started container cannot push"
	step "done"
	cmd_status
}

# --------------------------------------------------------------------- entry

usage() {
	cat <<-USAGE
		deploy/ckd-docker.sh COMMAND -- the Docker deployment of the display daemon

		  migrate           preflight, build, start, health-check, verify a real
		                    push, then take the panel from the launchd agent and
		                    install the login/wake agent. Idempotent.
		  rollback          hand the panel back to the native launchd agent.
		  status            container, health, standby, launchd, and who is pushing.

		  preflight         checks only, including LAN reach from a container.
		  env               (re)write .env from this machine (uid, tz, paths).
		  build [ARGS]      docker compose build
		  up [ARGS]         start the service (standby-safe)
		  down [ARGS]       stop and remove the container
		  restart           restart the container
		  logs [ARGS]       docker compose logs
		  ps                docker compose ps
		  health            wait for the container to report healthy
		  push-test         a real display.py --once inside the container
		  standby on|off    engage / release the do-not-push interlock by hand
		  agent-install     install the login/wake LaunchAgent
		  agent-uninstall   remove it
		  agent-plist       print the LaunchAgent plist on stdout

		CKD_DRY_RUN=1 prints every state-changing command instead of running it.
		No command here uses sudo.
	USAGE
}

main() {
	local command="${1:-help}"
	shift || true
	case "$command" in
	migrate)         cmd_migrate "$@" ;;
	rollback)        cmd_rollback "$@" ;;
	status)          cmd_status "$@" ;;
	preflight)       cmd_preflight "$@" ;;
	env)             cmd_env "$@" ;;
	build)           cmd_build "$@" ;;
	up)              cmd_up "$@" ;;
	down)            cmd_down "$@" ;;
	restart)         cmd_restart "$@" ;;
	logs)            cmd_logs "$@" ;;
	ps)              cmd_ps "$@" ;;
	health)          cmd_health "$@" ;;
	push-test)       cmd_push_test "$@" ;;
	standby)
		case "${1:-}" in
		on)  standby_engage "requested by hand" ;;
		off) standby_release ;;
		*)   die "usage: $0 standby on|off" ;;
		esac
		;;
	agent-install)   cmd_agent_install "$@" ;;
	agent-uninstall) cmd_agent_uninstall "$@" ;;
	agent-plist)     cmd_agent_plist "$@" ;;
	help|--help|-h)  usage ;;
	*)               usage >&2; exit 2 ;;
	esac
}

main "$@"

#!/bin/bash
# What the login/wake LaunchAgent runs: wait for Docker Desktop, then make sure
# the compose service is up. Idempotent by construction -- `compose up -d` on a
# service that is already running does nothing -- so launchd can call this at
# login, after every wake, and every few minutes in between.
#
# Why this exists at all when the container has `restart: unless-stopped`: that
# policy is honoured by the Docker *daemon*, and on a Mac the daemon is a
# desktop app inside a VM that is not running yet when the user logs in, may be
# quit by hand, and does not come back on its own after an update. This script
# is the piece that starts the engine; the restart policy keeps the daemon alive
# once it has.
#
# No sudo: `open -ga Docker` launches the app as the logged-in user.
set -uo pipefail

REPO_DIR="${CKD_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
COMPOSE_FILE="$REPO_DIR/docker-compose.yml"
NATIVE_PLIST="$HOME/Library/LaunchAgents/com.williamliu.context-keyboard-display.plist"

# Docker Desktop can take a while on a cold login (VM boot + engine start).
WAIT_SECONDS="${CKD_DOCKER_WAIT:-240}"
POLL_SECONDS=5

# launchd's PATH does not include /usr/local/bin, where the docker CLI lives.
# CKD_PATH_PREFIX is the test hook (same idea as service.py's CKD_PLATFORM):
# real runs never set it, and tests/test_docker.py sets it to a directory of
# stub binaries so this script's decisions can be exercised without a Docker
# Desktop and without touching the real container.
export PATH="${CKD_PATH_PREFIX:+$CKD_PATH_PREFIX:}/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

log() { printf '%s ckd-boot: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

# The safety catch: if the native launchd pusher is installed again -- rollback,
# or a hand-run `display.py --install` -- it owns the panel, and starting the
# container behind its back would put two pushers on one 142x428 screen. Do
# nothing and say why.
if [ -f "$NATIVE_PLIST" ]; then
	log "native launchd agent is installed ($NATIVE_PLIST); not starting the container"
	exit 0
fi

if [ ! -f "$COMPOSE_FILE" ]; then
	log "no $COMPOSE_FILE; nothing to start"
	exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
	log "no docker CLI on PATH ($PATH); is Docker Desktop installed?"
	exit 1
fi

if ! docker info >/dev/null 2>&1; then
	log "Docker is not answering; launching Docker Desktop"
	open -ga Docker 2>/dev/null || log "could not 'open -ga Docker' (is it installed?)"
	waited=0
	until docker info >/dev/null 2>&1; do
		if [ "$waited" -ge "$WAIT_SECONDS" ]; then
			log "Docker still not ready after ${WAIT_SECONDS}s; launchd will retry"
			exit 1
		fi
		sleep "$POLL_SECONDS"
		waited=$(( waited + POLL_SECONDS ))
	done
	log "Docker became ready after ${waited}s"
fi

# --no-build on purpose: a login is the wrong moment to discover a five-minute
# image build, and a missing image is a deploy problem to be reported, not
# papered over. `ckd-docker.sh migrate` is what builds.
if docker compose -f "$COMPOSE_FILE" up -d --no-build; then
	state="$(docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' ckd-display 2>/dev/null)"
	log "compose service is up (ckd-display: ${state:-unknown})"
	if [ -f "$HOME/.claude/context-keyboard-display-standby" ]; then
		log "NOTE: standby file exists, so the container renders but does not push"
		log "      (deploy/ckd-docker.sh status explains who owns the panel)"
	fi
	exit 0
fi

log "docker compose up failed; launchd will retry"
exit 1

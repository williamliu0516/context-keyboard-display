# Running the display daemon in Docker on macOS

The panel pusher, containerised, on the same Mac that keeps the global hotkeys
native. It comes up on login, comes back after wake, sleep, a Docker Desktop
restart and a reboot, and starts drawing again as soon as the keyboard
reconnects — the daemon's own offline backoff and heartbeat handle a panel that
was asleep, exactly as they do natively.

```
deploy/ckd-docker.sh migrate     # build, verify, take over from launchd
deploy/ckd-docker.sh status      # who owns the panel right now
deploy/ckd-docker.sh rollback    # hand it back to the native launchd agent
deploy/ckd-docker.sh help        # every command
```

Nothing here uses `sudo`. Every launchd verb runs in the per-user `gui/$UID`
domain and every file written lives under `$HOME`.

## The one rule: exactly one pusher

Two daemons pushing to one 142×428 panel at 1 Hz looks like a flickering
rendering bug and is nearly impossible to diagnose from the panel itself. So
the migration hands the panel over through an interlock rather than a hope.

`CKD_STANDBY_PATH` names a file (`~/.claude/context-keyboard-display-standby`).
While it exists, the containerised daemon collects, renders, publishes the
screen it chose, and heartbeats — but **pushes nothing**. It is read once per
tick, so both directions take effect within a tick and neither needs a restart.

`migrate` therefore goes:

1. **preflight** — Docker up, compose v2, the sibling library present,
   `~/.claude` writable, a real panel address configured, and a container can
   open a TCP connection to that address. If the LAN is not reachable from a
   container, it stops here and says so; the native pusher is left in charge.
2. **build** the image, **engage standby**, then **start** the container.
3. **health-check** it (`docker inspect` → healthy).
4. **verify a real push**: `display.py --once` *inside the running container*,
   which renders the live state and POSTs the JPEG to the configured address.
   Exit code 0 only if the panel took the frame.
5. Only now: **boot out, disable and park** the native launchd display agent.
6. **Release standby** — the container takes the panel within one tick.
7. **Install the login/wake LaunchAgent** and print the final status.

If step 3 or 4 fails, the container is stopped, standby is released, and the
native agent is untouched and still pushing. If the native agent cannot be
stopped, the container stays parked in standby rather than becoming a second
pusher.

`rollback` is the mirror: engage standby, wait a tick, remove the wake agent,
restore and bootstrap the native plist, then `compose down`. It **leaves the
standby file in place** on purpose — that is what stops a hand-run
`docker compose up -d` from pushing behind the native agent's back. `migrate`
is the only thing that lifts it, and only after the verification push.

## What the container can and cannot see

| | |
|---|---|
| `~/.claude` → `/host/.claude` (rw) | The live state: config, the session transcripts, `-control.json` written by the **native** hotkey listener, `-status.json` written back by the daemon. `HOME=/host`, so `os.path.expanduser("~/.claude")` resolves there with no container-specific code. |
| `$CKD_PROJECTS_DIR` → same path (ro) | Mounted at its host path because the transcripts record host cwds and `collect.diff_stat` asks `git` about them by name. Read-only; `GIT_OPTIONAL_LOCKS=0` so git never tries to write an index. |
| `./out` → `/app/out` (rw) | `--preview`, `--live` and `--daemon --dry-run` land in the repo's `out/` like they do natively. |
| the renderer library | **Copied into the image and pip-installed**, from the `upstream` build context. No `~/projects` path exists inside the image, so `import keyboard_status` resolves from site-packages and `upstream_path` in the config is irrelevant there. |
| fonts | The macOS faces (SFNS, PingFang) do not exist on Linux, so `service.apply_font_fallback` repoints the renderer at Noto/DejaVu. The build **fails** if no usable face is installed, and renders the whole mockup dataset as a build step, because font substitution is the one thing a container gets wrong silently. |
| `TZ` | The Idle screen is a clock. `.env` carries the host zone; without it the panel would read UTC. |

Not shared, deliberately: the macOS Keychain. `keyboard_status` reads the usage
meters from `~/.claude/.credentials.json` **or** the Keychain, and only the
file is visible in the container. If the file's token is stale the usage meters
can come up empty where they would have been populated natively; everything
else — sessions, todos, timings, diff counts — comes from the mounted state.

## Health, and what it does not claim

`display.py --health` reads the heartbeat the daemon writes each tick to a
container-local tmpfs and exits 0 if the loop went round recently and is not
raising every tick. It is stdlib-only, so it still answers when the render
dependencies are what broke.

It deliberately says **nothing** about the panel being reachable. The keyboard
sleeps off Wi-Fi for hours by design; a health check that failed on that would
restart the container every time the keyboard slept, causing the outage it was
looking for. `status` reports panel reachability separately (`online` /
`not answering` in the heartbeat line).

Staleness allowance: `max(tick_seconds × 4, 45 s)`, which covers the slowest
real tick — a failed push is two knocks at `http_timeout_seconds` plus the
retry pause and the reachability probe.

## Staying up whenever the Mac is awake

Two mechanisms, because neither is sufficient alone:

- `restart: unless-stopped` on the container — honoured by the Docker daemon, so
  it covers crashes, engine restarts and reboots.
- `com.williamliu.context-keyboard-display-docker`, a user LaunchAgent
  (`ckd-docker.sh agent-install`) that runs `deploy/ckd-docker-boot.sh` at login
  (`RunAtLoad`) and every 300 s while awake (`StartInterval`) — launchd runs a
  `StartInterval` job it missed during sleep as soon as the Mac wakes. The
  script waits for Docker Desktop (launching it with `open -ga Docker` if it is
  not running), then runs an idempotent `compose up -d --no-build`.

  It carries its own `PATH`, because launchd hands agents a minimal one that
  does not include `/usr/local/bin`, where Docker Desktop puts the CLI — the
  classic silent login-agent failure. And it **refuses to start the container if
  the native plist is installed**, so a rollback cannot be undone by a login.

Not `KeepAlive`: the boot script is short and exits, and `KeepAlive` on a script
that exits respawns it forever.

## The hotkeys stay native

`keys.py` reserves `ctrl+opt+cmd+k` / `…+j` through Carbon, against
WindowServer, on the logged-in user's session. That is not something a Linux
container can do, and it is why `keys.py` is not even copied into the image —
"the hotkeys never run in the container" is a property of the build rather than
a promise in a document. `com.williamliu.context-keyboard-keys` keeps running
natively and nothing in `deploy/` touches it.

The cross-boundary handshake is the two JSON files under `~/.claude`: the
listener writes `-control.json`, the containerised daemon polls it every 0.2 s
(polling, not fsevents — which is what makes it work over a bind mount) and
writes back `-status.json` for the drill-down key. So `mode next` and the
switchboard drill-down behave exactly as they did natively.

## Configuration

Host-specific values live in `.env`, which is **not tracked**;
`ckd-docker.sh env` writes it by asking this machine (uid, gid, timezone,
where the sibling checkout and your projects are). See `.env.example`.

The panel's address is **not** in `.env` or anywhere else in this repo. It
stays in `~/.claude/context-keyboard-display.yaml` and is read live through the
mount, so no tracked file learns your LAN — `tests/test_docker.py` asserts
that.

## Troubleshooting

```
deploy/ckd-docker.sh status          # container, health, standby, launchd, owner
deploy/ckd-docker.sh logs -f         # the daemon's own log lines
tail -f ~/.claude/context-keyboard-display-docker.log   # the login/wake agent
docker exec ckd-display python3 /app/display.py --status  # what it decided to show
docker exec ckd-display python3 /app/display.py --health  # the health verdict
deploy/ckd-docker.sh push-test       # a real frame, from inside the container
```

`status` ends with the only line that matters — `pushing to the panel: …` —
and says `BOTH (!)` if it ever finds the native agent loaded while an
un-parked container is running.

**The panel is blank / stale.** Check `status`. If it says standby is ON, the
container is deliberately not pushing: run `migrate` (verifies, then hands
over) or `standby off` if you know the native agent is gone.

**A container cannot reach the panel.** `preflight` probes this. Docker
Desktop NATs outbound traffic through its VM, which normally reaches the LAN
fine; if it does not on your network, keep the native launchd deployment —
`ckd-docker.sh migrate` refuses to migrate in that case rather than leaving you
with a daemon pushing into the void.

**The Idle clock is wrong.** `TZ` in `.env`; re-run `ckd-docker.sh env` and
`restart`.

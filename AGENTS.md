# context-keyboard-display: notes for coding agents

A daemon that picks a screen for a mechanical keyboard's 142x428 image panel (Claude Code sessions, the idle clock,
now playing, print, alerts, due soon), renders it as a JPEG and POSTs it to the panel. `display.py` is the daemon and
installer, `collect.py` reads sessions, `screens.py` draws, `home.py` reads an optional home-panel API, `keys.py` is
the macOS hotkey listener, `service.py` writes launchd/systemd units. It runs natively or in Docker
([docs/DOCKER.md](docs/DOCKER.md)). It uses claude-code-keyboard-status as a library (transcript parsing, fonts, JPEG
encoding, the HTTP push) and that repo's Claude Code hooks for session state. Read [README.md](README.md) first.
Claude Code reads this file through CLAUDE.md; private notes, if any, are in CLAUDE.local.md (not in git).

## Before you start

- `git status`: other agents may be working in this tree. Leave changes that aren't yours alone and keep them out of
  your commits; for a large parallel change use a separate `git worktree`.
- Extend the existing code in place; no second copy, no speculative options.

## Done means

- `scripts/check.sh` passes (all four test suites, and the secret scan is on for this clone). It never touches the
  keyboard, Docker or the network.
- [CHANGELOG.md](CHANGELOG.md) gets one line at the top, in the same commit.
- Replaced code, flags, config keys and stale docs go in the same change; clean up scratch files and test processes.
- No compatibility shims for old behaviour unless asked.

## Rules

- One pusher per panel: only one daemon may push to the keyboard (the old claude-code-keyboard-status daemon must stay
  off where this one runs). Test pushes go through `display.py --once` or the Docker `push-test`, not a second daemon.
- No LAN addresses, hostnames or tokens in tracked files: they live in `.env` and `~/.claude/context-keyboard-display*`
  (`tests/test_docker.py` checks the panel address never appears in a tracked file).
- Commits: subject `<area>: <what now happens>` or the repo's existing `feat(...)` style, a body that says why; no AI
  attribution trailers. A pre-commit hook runs gitleaks (`git config core.hooksPath scripts/hooks`); never
  `--no-verify`.

# Changelog

One line per change, added in the same commit, newest first. The full diff is in git.

## Unreleased

- tests/test_install_sh.sh expects the 10 files install.sh verifies since home.py was added (it still said 9).
- `scripts/check.sh` runs all four test suites; a gitleaks pre-commit hook (`scripts/hooks/pre-commit`).
- AGENTS.md (CLAUDE.md imports it): notes for coding agents.

## 2026-10-05

- fix(home): read homeboard's read-only /api/keyboard; K17 covers load (a164cce)
- feat(screens): v2 panel -- two-line sessions, dwell instead of flapping, home data (70ec27c)
- feat(keys): vim-style hotkeys -- H/L step the screens, J/K walk the switchboard (fee6834)

## 2026-09-03

- feat(fonts): ship the approved Ubuntu face on the Linux/Docker panel (d8cbb6a)
- feat: run the display daemon in Docker on macOS, with a verified handoff (76e6814)

## 2026-09-17

- index.html: say Linux works too (00a94ce)

## 2026-08-28

- feat: add Ubuntu headless service support (db3d7b4)
- index.html: geek-theme visual restyle (030b706)

## 2026-08-27

- install.sh: fetch from GitHub raw only (56f54b0)
- Session identifier marks, one-command installer, and a landing page (cf81911)
- Zero-permission hotkey panel control: mode switching, per-session drill-down, faster ticks (089e909)

## 2026-08-26

- context-keyboard-display v0: Claude/Sessions/Idle screens for the 142x428 keyboard panel (cc46ce2)

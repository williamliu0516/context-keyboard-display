# context-keyboard-display

Context-aware screens for a mechanical keyboard's 142×428 image panel,
driven by the live state of your Claude Code sessions. The daemon decides
*which* screen the panel should show — a single session's detail, the
multi-session switchboard, or the idle clock — renders it, and POSTs the
JPEG to the keyboard.

The three screens (six renderings):

| Screen | When | Shows |
|---|---|---|
| **Claude — working** | exactly one session mid-turn | mascot (spinning = alive), BUSY pill, ⏱ turn elapsed, project, `TODO 3/7` + current item (two lines, from the session's live TodoWrite plan), diff stat `+212 −38` |
| **Claude — waiting** | a session needs permission | waiting mascot + badge, YOU pill, which tool (`Bash?`), ⏱ stuck-for, project, clock |
| **Claude — between turns** | a session finished, still engaged | DONE pill, last turn's duration (`2m 14s`), project, diff stat, clock |
| **Sessions** | ≥2 sessions demand attention at once | `N LIVE` pill + one dot-and-name row per session (amber = waiting, green = working, grey = engaged), waiting-first |
| **Idle** | no engaged session | hero clock (readable across a room), day/date, 5H/7D usage meters ("can I start another session?"), connection dot |

Priority: waiting > done-toast > working detail > sessions > between-turns
> idle, with a single-protagonist rule (one working session gets its detail
screen even when others idle nearby; two of anything concurrent gets the
switchboard).

## Relationship to claude-code-keyboard-status

This repo **supersedes [claude-code-keyboard-status] as the panel's
pusher**, and **depends on it as a library**:

- **Hooks and state file stay owned by the old repo.** Its five
  session hooks (`SessionStart`, `UserPromptSubmit`, `Notification`,
  `Stop`, `SessionEnd`) write `~/.claude/keyboard-status-state.json`; this
  daemon only reads it. Install the old repo's hooks first
  (`keyboard_status.py --install`) if you haven't.
- **All transcript parsing, rendering primitives (fonts, mascot, palette,
  supersampling), JPEG encode and the HTTP push live there** and are
  imported (`pip install -e` from the sibling checkout). The two displays
  can't drift apart visually, and the incremental transcript cache works
  as designed.
- **Single-pusher rule:** on startup (and at `--install`) this daemon
  boots the old launchd agent out so exactly one process owns the panel.
  `--uninstall` restores the old agent. The old repo remains fully usable
  standalone.

Why supersede rather than run alongside: two daemons alternating frames on
one panel is chaos, and everything the old single-status screen showed
either moved here (usage meters → Idle) or was deliberately cut as
redundant with the terminal status bar (model name, branch).

[claude-code-keyboard-status]: https://github.com/williamliu0516/claude-code-keyboard-status

## Install / run

```bash
# prerequisite: the old repo's hooks are registered (once):
#   python3 ~/projects/claude-code-keyboard-status/keyboard_status.py --install

python3 display.py --install     # venv, deps, config, launchd agent
                                 # (boots out the old daemon's agent)
```

Config lands at `~/.claude/context-keyboard-display.yaml`. **The one key to
check before it will reach the real device: `url`** — the keyboard's
`/image/upload` endpoint (seeded with the same address the old repo's
config used). Then set your `display.aliases` map: each repo you work in,
named in ≤7 characters, because that's what a Sessions row holds.

Everyday commands:

```bash
python3 display.py --daemon --dry-run   # render loop -> out/frame.jpg, no POST
python3 display.py --once               # one render + push, exit code = reachability
python3 display.py --status             # every live session + the chosen screen, as JSON
python3 display.py --live out/x.jpg     # render the real current state to a file
python3 display.py --preview            # render the approved-mockup dataset to out/
python3 display.py mode sessions        # manual override: auto|claude|sessions|idle
tail -f ~/.claude/context-keyboard-display.log
```

`--uninstall` removes the launchd agent and hands the panel back to the old
daemon.

## Data sources (all real, no mocks in the loop)

| Fact | Source |
|---|---|
| Session list, working/waiting/idle, cwd | `~/.claude/keyboard-status-state.json` (hooks) merged with transcript mtimes, via `keyboard_status.resolve_state` |
| Turn elapsed / last turn duration | `turn_started_at` / `last_turn_seconds`, recorded by the hooks at `UserPromptSubmit`/`Stop` |
| Which tool wants permission | parsed from the captured `Notification` message |
| Todo count + current item | latest `TodoWrite` `tool_use` in the session transcript (incremental reader) |
| Diff stat | `git diff HEAD --shortstat` in the session cwd, 0.5 s timeout, cached 5 s; whole dirty worktree, honestly |
| 5H/7D usage | the shared `~/.claude/statusline-usage.json` merge machinery (same numbers as the terminal status line) |
| Clock / date | `time.localtime` |

## Decisions made during the build (deltas from the planning docs)

- **Idle shows 5H/7D usage meters, not CPU/RAM** — REVISED_PLAN.md cut
  `psutil` and the approved `idle.jpg` mockup is meters; the earlier
  CPU/RAM idea stayed cut. No `psutil` dependency.
- **Engagement window is `engaged_seconds: 1800` (30 min), not the 6 h
  `session_ttl_seconds`** the plan implied. A six-hour DONE screen would
  mean the idle clock never appears. Waiting sessions ignore the window —
  a permission prompt stays urgent no matter how old.
- **No separate minimum-screen-lifetime timer.** The 5 s render tick
  already guarantees every frame outlives the plan's 2 s rule.
- **Done toast = the between-turns screen pinned for 4 s** when a session
  transitions working → idle, even if another session would otherwise own
  the panel. No error toast: the hooks expose no error state to hang it on.
- **`wrap_words` breaks mid-run when no space is near the cut** — CJK todo
  items carry no spaces and must fill both lines rather than collapse to
  one elided fragment. (Latin text wraps exactly as the approved mockups.)
- **Diff row omitted outside a git repo** (and on a repo with no commits);
  a fabricated `+0 −0` would be a lie. The footer clock keeps its slot on
  the variants that have one.
- **`tool_use` action capture landed upstream but isn't rendered** — the
  FIXES.md trim pass removed the verb/filename rows deliberately. The data
  flows (visible in `--status`) for any future variant.
- Between-turns hides the duration row for sessions that predate the
  hook's turn-bookkeeping (no `last_turn_seconds` yet) instead of showing
  a fake number.

## Rendering verification

`display.py --preview` renders the exact dataset behind the planning
repo's approved mockups. At ship time all seven frames were **byte-identical**
to `context-keyboard-display-planning/preview/output/*.jpg` (the FIXES.md
ground truth). If you touch the renderers, re-run that comparison.

The layout rules those mockups encode — CORE 34 px / FLOOR 25 px type,
~6–8 character budgets, the 40 px dead zone, prefix-elision with alias
maps, stopwatch-vs-clock-vs-duration time forms — are documented in the
planning repo's `REVISED_PLAN.md` and `FIXES.md`.

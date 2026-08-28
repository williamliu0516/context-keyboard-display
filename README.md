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
| **Claude — waiting** | a session needs permission | waiting mascot + badge, YOU pill, ⏱ stuck-for, which tool (`Bash?`), project, clock |
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
python3 display.py mode next            # advance one step round the cycle (same as the hotkey)
tail -f ~/.claude/context-keyboard-display.log
```

`--uninstall` removes the launchd agent and hands the panel back to the old
daemon.

### The override is a peek, not a setting

`mode claude|sessions|idle` pins the panel to one screen — and then lets go.
**An override expires `override_ttl_seconds` (default 60) after it was set**,
at which point the daemon reports and behaves as `auto` again, without you
pressing anything. That's the point of the hotkey: glance at the switchboard,
look away, and the panel goes back to deciding for itself. `auto` itself never
expires — it's the resting state, so there is nothing to expire to.

The timeout is the clock since the mode was *set*, not since the last press:
pressing the hotkey again re-stamps it, so cycling around keeps the override
alive for another full window. To change it (or to get the old sticky
behaviour back), edit `~/.claude/context-keyboard-display.yaml`:

```yaml
override_ttl_seconds: 60.0     # 86400.0 ≈ "until I change it back myself"
```

The daemon reads this at startup, so restart it after editing:
`launchctl kickstart -k gui/$(id -u)/com.williamliu.context-keyboard-display`.

`mode next` reads the mode currently *in force* (so a lapsed override
advances from `auto`, starting the cycle over) and steps it one place through
the same `keys.cycle` list the hotkey uses — `auto -> claude -> sessions ->
idle -> auto` by default.

## Hotkeys: drive the panel from the keyboard

`keys.py` is a small, independent macOS hotkey listener in its own launchd
agent. It writes the manual override — the same
`~/.claude/context-keyboard-display-control.json` that `display.py mode ...`
writes — and the display daemon picks the change up in about 0.3 s.
`display.py --install` installs it automatically; it also has its own
`python3 keys.py --install` / `--uninstall` if you want it independent of the
display daemon.

Two keys, and they are deliberately not peers:

| key | what it does |
| --- | --- |
| **`Ctrl+Opt+Cmd+K`** | one step round `auto -> claude -> sessions -> idle -> auto` — *except* while a session is drilled into, where the first press steps back out to the switchboard |
| **`Ctrl+Opt+Cmd+J`** | drill into the switchboard: show the selected session's own detail screen, and step one row down the list per press, wrapping at the bottom. Does nothing on any other screen |

So the panel has two levels, and `K` reads as "back / next" rather than as a
fixed rotation:

```
sessions (the switchboard)
   │  J  drill in, then J again to walk the rows
   ▼
claude_working / claude_waiting / claude_between_turns   for the chosen session
   │  K  pop back out to the switchboard
   ▼
sessions  ──  K  ──▶  idle  ──  K  ──▶  auto  ──  K  ──▶  claude  ...
```

`J` is inert unless the switchboard is the screen actually on the panel. That
is a real precondition, not a formality: this panel has no input focus and no
cursor, so a key that did something from any screen would be a key that does
something you did not see. The listener finds out by reading
`context-keyboard-display-status.json`, which the daemon writes whenever the
screen kind changes — `Engine.choose`'s priority ladder (toast state and all)
is the daemon's business, and re-deriving it in the listener would be a second
copy free to drift from the first.

**The drill-down is a peek like any other.** Selecting a session does not get
its own clock: it expires with the `override_ttl_seconds` (60 s) rebound
described above, and lands back on `auto` — not on the switchboard. Every `J`
and `K` press re-stamps the timer, so browsing keeps it alive and only walking
away lets it lapse. A two-stage decay would leave the panel overriding reality
for twice as long, and the first stage would end in a screen change nobody
asked for.

**The selection is pinned to a session id, never to a row number.** Rows
re-sort the instant a session changes state (waiting sorts above working),
so a remembered index would quietly advance to whatever slid into that slot.
If the pinned session ends while you are looking at it, the panel falls back
to the switchboard rather than promoting some other session into a detail
screen the heading says you chose.

**Both defaults ask macOS for nothing.** There are two engines, and `--daemon`
picks between them by looking at the binding:

| engine | used when | privacy grant |
| --- | --- | --- |
| **hotkey** — Carbon `RegisterEventHotKey` | the binding carries a real modifier (ctrl/opt/shift/cmd) — the default does | none |
| **tap** — `CGEventTap` | the binding uses `fn`, or is a bare key with no modifiers | Input Monitoring |

The split is about what the process is able to see. `RegisterEventHotKey`
*reserves* one combination with the WindowServer, which from then on
delivers that combination — and nothing else — to this program. Something
that cannot observe any key it didn't reserve isn't surveillance, so macOS
grants it silently: no prompt, no TCC row, nothing for you to find and tick.
A `CGEventTap`, by contrast, is handed every keystroke on the system, which
is exactly why it needs the grant. Carbon knows only those four modifiers
and has no concept of `fn`, so a binding it can't express is the one case
that still falls back to the tap.

Rebind in `~/.claude/context-keyboard-display.yaml`:

```yaml
keys:
  binding: "ctrl+opt+cmd+k"         # any combination of fn/ctrl/opt/shift/cmd + a key
  select_binding: "ctrl+opt+cmd+j"
  swallow: auto                     # tap engine only — see below
```

**Neither may be bound to a bare letter**, and `carbon_modifiers()` refuses to
register one. A reserved combination never reaches the app you are typing
into, so binding `j` alone would stop the letter j working everywhere on the
machine — vim-style `hjkl` navigation is exactly the shape this cannot take.
Only the cycle key falls back to the tap engine; if it does, the drill-down
key is skipped with a line in the log.

`key_swallow` only means something to the tap: a reserved Carbon hotkey is
always consumed, because reserving a combination is what stops it reaching
apps in the first place. On the tap, `auto` passes a bare key through to
whatever has focus (so binding `End` doesn't silently break the End key
everywhere) and swallows anything carrying a real modifier.

**A binding of "fn+right" is really a binding of `End`.** On a Mac keyboard,
holding Fn and pressing the Right arrow never reaches an app as "Fn is down
and Right is down" — macOS's HID layer resolves that chord into a single
`End` keypress before any listener sees it, exactly like Fn+Left becomes
`Home` and Fn+Up/Down become Page Up/Down. There's no lower-level chord to
catch instead. That's also why such a binding lands on the tap engine and
drags the Input Monitoring grant back in: `End` alone has no modifier for
Carbon to reserve.

Checking it:

```bash
python3 keys.py --permission     # which engine runs, and what (if anything) it needs
python3 keys.py --selftest       # register the real hotkey and drive it synthetically
python3 keys.py --watch          # tap engine: keycode+flags for every key you press
                                  # (use this to find a binding's kVK_/flag values)
python3 keys.py --simulate       # tap engine: match/cycle/debounce/autorepeat logic
```

`--selftest` is what to trust before touching a real key. It registers the
*real* hotkey with the *real* WindowServer, then posts a synthetic press
into its own event queue once per mode and checks the cycle advanced,
restoring whatever mode was active before it ran. A clean run proves the
combination was accepted and that every step on this side of the line is
right: the binding's translation into Carbon keycode + modifiers, the
handler, the run loop, and the control-file write.

The one step it *can't* prove is the one no unprivileged process can fake —
the WindowServer deciding to hand us a physical press. Synthesising a real
keystroke needs the Accessibility grant, which this deliberately doesn't
have. So `--selftest` says "registered and wired correctly", never "a key
was pressed". For that last mile, press the key once with `tail -f
~/.claude/context-keyboard-keys.log` running; the same applies if a
remapping tool (Karabiner, etc.) might be intercepting the combination
before macOS gets to it.

If the granted, tap-engine path is what you're on, `--permission` prints
the exact steps: it grants the *interpreter*, not the script — System
Settings -> Privacy & Security -> Input Monitoring -> "+", then Cmd+Shift+G
to paste the path it gives you. Restart the listener after granting:
`launchctl kickstart -k gui/$(id -u)/com.williamliu.context-keyboard-keys`.

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
- **Every Claude variant puts its time row in the first content slot.**
  Waiting used to lead with the tool name and push ⏱ stuck-for below it, so
  the one row all four variants share moved when the screen changed. The
  timer now sits where WORKING's elapsed and BETWEEN-TURNS' duration sit
  (ink top y=196) and the tool name follows.
- **Engagement window is `engaged_seconds: 1800` (30 min), not the 6 h
  `session_ttl_seconds`** the plan implied. A six-hour DONE screen would
  mean the idle clock never appears. Waiting sessions ignore the window —
  a permission prompt stays urgent no matter how old.
- **The tick adapts to the screen: 1 s on Claude working/waiting, the
  configured `tick_seconds` (5 s) everywhere else.** Those two screens are
  the only ones that animate — a spinning mascot and a per-second stopwatch —
  and at 5 s both read as a slideshow. Idle's clock and usage meters are
  minute-resolution, so they gain nothing and would pay 5× the pushes. The
  fast tick also stands down while the panel is in push backoff, and on a
  turn past an hour, where `fmt_elapsed` has degraded to `1h04` and the row
  changes once a minute anyway.
- **A manual mode change doesn't wait for the next tick.** The hotkey and
  `display.py mode ...` both land the same way — `set_mode` os.replace()s the
  control file — so the daemon serves its between-tick sleep in 0.2 s slices
  and each slice checks one thing: has that file been rewritten. A press is
  picked up in ~0.3 s instead of the up-to-5 s a fixed tick would cost
  (measured: mean 3.57 s before, 0.37 s after). Watching the file rather than
  signalling a pid means both entry points are covered by one mechanism, with
  nothing to discover and nothing to re-wire when either daemon restarts. The
  cost is one `stat()` per slice — no collect, no render, no push — so a panel
  nobody is touching ticks exactly as slowly as it always did. The TTL rebound
  is deliberately *not* watched: an override expiring rewrites no file, it only
  changes what `control_mode()` computes from the clock, so it stays a
  normal-tick affair.
- **No separate minimum-screen-lifetime timer.** The tick, not a timer,
  bounds how fast a frame can be replaced: 5 s on the still screens, 1 s on
  working/waiting. The plan's 2 s rule survives where it matters — screen
  *kind* changes are state-driven (a permission prompt should preempt
  instantly) and the done toast pins between-turns for its own 4 s.
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

Six still are. `claude_waiting.jpg` is the one deliberate departure — the
time-row realignment above — so the mockup, not the render, is the stale
side of that pair.

The layout rules those mockups encode — CORE 34 px / FLOOR 25 px type,
~6–8 character budgets, the 40 px dead zone, prefix-elision with alias
maps, stopwatch-vs-clock-vs-duration time forms — are documented in the
planning repo's `REVISED_PLAN.md` and `FIXES.md`.

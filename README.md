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

## Install

```sh
curl -fsSL https://xiaweiliu.com/keyboard-display/install.sh | sh
```

One script, two hosts. On **macOS** it installs a launchd agent for the display
and a second one for the `keys.py` global hotkey listener. On **Ubuntu/Debian**
it installs a `systemd --user` service for the display and no hotkey listener —
see [Ubuntu / Debian, headless](#ubuntu--debian-headless) below, which is the
whole of the difference. Anything else is refused rather than half-supported.

It asks for one thing — your panel's IP address — and configures everything
else. Pass it in instead if you would rather not be asked:

```sh
curl -fsSL https://xiaweiliu.com/keyboard-display/install.sh | PANEL_IP=192.168.1.50 sh
```

The installer downloads this display **and** the `claude-code-keyboard-status`
library it reads session state through into
`~/.claude/context-keyboard-display/`, registers that library's five session
hooks (preserving every other setting and every other tool's hooks in
`settings.json`), builds a private virtualenv, and loads the launchd agents.
Re-running it is how you upgrade; a re-run keeps the address you already
configured unless you pass `PANEL_IP` again.

**There is no default address.** It is whatever DHCP handed your keyboard, and
a wrong guess fails as silence — frames POSTed into the void, a blank panel,
nothing in the log explaining why. So the daemon refuses to push until `url` is
set, and says so in as many words. Find the address on the panel's own display
or settings app, or look for a new device in your router's client list.

| Variable | Effect |
| --- | --- |
| `PANEL_IP` | the panel's address; asked for interactively when unset |
| `CKD_SOURCE` / `CKS_SOURCE` | install from a fork or a `file://` path |
| `INSTALL_DIR` | where the files land (default `~/.claude/context-keyboard-display`) |
| `DRY_RUN=1` | fetch and verify everything, then stop before installing |
| `CKD_PLATFORM` | override the detected OS (`Darwin` / `Linux`); a test hook, honoured by `display.py` and `service.py` too |

### From a clone instead

```sh
# prerequisite: the library's hooks are registered (once):
#   python3 ~/projects/claude-code-keyboard-status/keyboard_status.py --install

python3 display.py --install     # venv, deps, config, and the service:
                                 #   macOS  launchd agent + keys.py hotkeys
                                 #          (boots out the old daemon's agent)
                                 #   Linux  systemd --user service, no hotkeys
```

`python3 service.py --platform` prints which of those this host resolved to,
before you install anything.

Config lands at `~/.claude/context-keyboard-display.yaml`. Set `url` to your
panel, then set your `display.aliases` map: each repo you work in, named in ≤7
characters, because that's what a Sessions row holds.

### Everyday commands

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

## Ubuntu / Debian, headless

The daemon itself was always portable — it reads Claude Code's own files and
POSTs a JPEG. What was macOS-only was everything *around* it: a launchd agent,
a Carbon hotkey listener, and the San Francisco fonts the renderer names by
absolute path. On Linux those become a `systemd --user` service, no hotkey
listener at all, and whichever system face is installed.

### Prerequisites

| Need | Why | Check |
| --- | --- | --- |
| Python **3.9+** | same floor as macOS | `python3 -V` |
| `python3-venv` | the install builds a private venv, and Debian ships `venv` without `ensurepip` | `python3 -c 'import venv, ensurepip'` |
| a system font | the renderer's macOS faces do not exist here | `python3 service.py --fonts` |
| `curl` or `wget` | fetching the install | — |

**`python3-venv` needs a package, and the installer will not install it for
you** — it has no business running `apt` as root on your machine, and on a
managed box it could not anyway. If the check above fails:

```sh
sudo apt install python3-venv        # or python3.12-venv, matching python3 -V
```

No sudo? Any of these works instead, and none of them needs root: ask an
administrator for that one package, or put a self-contained Python first on
`PATH` (`pyenv`, `uv python install`, conda) and re-run. The installer stops
with this exact advice rather than failing halfway through a broken venv.

**Fonts.** `fonts-noto-core` gives the closest match to the macOS look, and
`fonts-noto-cjk` is what keeps a Chinese or Japanese TODO item from rendering
as tofu. `fonts-dejavu-core` is present on most images already and is used as
a fallback. Missing them is not fatal — the daemon warns once and falls back
to Pillow's 11 px bitmap font, which is legible and ugly.

```sh
sudo apt install fonts-noto-core fonts-noto-cjk fonts-dejavu-core
python3 service.py --fonts     # what the daemon will actually load
```

Nothing else about the install needs root. The unit is a **user** unit in
`~/.config/systemd/user`, not `/etc/systemd/system`.

### Install

```sh
curl -fsSL https://xiaweiliu.com/keyboard-display/install.sh | PANEL_IP=192.168.1.50 sh
```

Identical to the macOS command; the platform is detected. On a headless box
pass `PANEL_IP` rather than relying on the prompt, which needs a terminal.
What it does differently here:

- installs `Pillow` and `PyYAML` only — no `pyobjc`, which is macOS-only and
  has no Linux wheel;
- writes `~/.config/systemd/user/context-keyboard-display.service`, then
  `systemctl --user enable --now` it;
- runs `loginctl enable-linger` so the service survives you logging out —
  **the one step that makes "headless" true.** If polkit refuses (common over
  ssh with no seat), the installer says so and prints
  `sudo loginctl enable-linger $USER` for an admin. Without it the panel only
  updates while you are logged in;
- skips the hotkey listener, loudly, and does not fail over it;
- still registers the `claude-code-keyboard-status` session hooks, which are
  plain `settings.json` edits and entirely portable. That library's own
  installer prints "loaded launchd agent" on any platform; on Linux nothing
  was loaded and this installer says so, and deletes the inert plist it wrote.

### Running it

```sh
systemctl --user status context-keyboard-display.service     # is it up
journalctl --user -u context-keyboard-display.service -f     # unit-level: starts, exits, restarts
tail -f ~/.claude/context-keyboard-display.log               # the daemon's own lines
systemctl --user restart context-keyboard-display.service    # after editing the config
systemctl --user stop context-keyboard-display.service       # pause without uninstalling
```

Both log destinations work because the unit sets
`StandardOutput=append:~/.claude/context-keyboard-display.log`, so the same
`tail -f` from the macOS docs is the same command here. `append:` needs
systemd ≥ 240 (Ubuntu 20.04 ships 245); on anything older the generated unit
omits the redirect and the journal is the only log. `python3 service.py
--print-unit` shows exactly what will be written, before writing it.

Everything under [Everyday commands](#everyday-commands) works unchanged —
`--status`, `--once`, `--live`, `--preview`, `mode ...`. Uninstall:

```sh
python3 ~/.claude/context-keyboard-display/display.py --uninstall
```

which disables and stops the unit, removes it, and reloads systemd. It leaves
your config and the venv alone, exactly as the macOS `--uninstall` does.

### No global hotkeys, deliberately

`Ctrl+Opt+Cmd+K` and `Ctrl+Opt+Cmd+J` do not exist on Linux and are not
planned. `keys.py` is built on Carbon's `RegisterEventHotKey` and
`CGEventTap`; the Linux equivalents are an X11 grab, which is meaningless on a
headless box and wrong under Wayland, or reading `/dev/input`, which is a
keylogger that wants your user in the `input` group. Neither is a fair trade
for a convenience key.

So `keys.py` is installed but inert here: every command prints why and does
nothing. `--install` and `--uninstall` exit **0**, which is what keeps a Linux
install from failing on a listener it was never going to have; everything else
exits **3**.

The panel is still fully drivable — the hotkey never did anything you cannot
type:

```sh
python3 ~/.claude/context-keyboard-display/display.py mode next       # the K key
python3 ~/.claude/context-keyboard-display/display.py mode sessions   # jump straight there
```

Both take effect in ~0.3 s, same as a keypress: the daemon watches the control
file rather than the clock. Bind them to whatever your terminal multiplexer,
desktop, or keyboard firmware already uses for shortcuts — that layer knows
about your input devices and this daemon does not need to.

### What is *not* claimed here

This platform work was written and tested on macOS, driving the Linux branch
through `CKD_PLATFORM=Linux`: the installer's dry runs, the generated unit,
the routing between launchd and systemd, the font substitution, and a full
sandboxed `install.sh` run. `tests/test_platform.py` and
`tests/test_install_sh.sh` are that suite. **No part of it has been run
against real Ubuntu hardware or a real panel on Linux** — `systemctl enable`,
`loginctl enable-linger`, and the actual on-panel appearance of Noto in place
of San Francisco are all unverified.

## Which session am I looking at

The three detail screens end in a coloured dot and six characters. That mark is
derived from the session's `session_id`, and
[claude-status-bar](https://github.com/williamliu0516/claude-status-bar) prints
the identical mark in the terminal that session is running in. Glance at the
panel, glance at the status line, and the pair either matches or it does not.

This matters because the panel's other identifying field — the project name —
does not identify anything when several sessions are open in one repository,
which is the normal case. `cwd` is the same, the branch is the same, the model
is the same. The session id is the only thing that differs, and on its own it
is a 36-character UUID nobody can eyeball.

The two projects share no code. This specification is the entire contract, and
it is reproduced verbatim in both READMEs:

```
SESSION IDENTIFIER SPEC v1

  tag   = session_id[:6], lowercased          (a UUID, so these are hex)
  slot  = sha1(session_id utf-8).digest()[0] % 8
  xterm = PALETTE[slot]
  rgb   = the xterm-256 colour cube entry for that index:
            i = xterm - 16;  r = i // 36;  g = (i // 6) % 6;  b = i % 6
            rgb = (CUBE[r], CUBE[g], CUBE[b])

  PALETTE = (45, 46, 49, 69, 201, 202, 211, 228)
  CUBE    = (0, 95, 135, 175, 215, 255)

  Terminal renders ESC[38;5;{xterm}m ● ; a full-colour display fills a dot
  with rgb. Both therefore show one colour, not two similar ones.
```

Integer arithmetic only, deliberately — no float, no locale, no font or
terminal metrics — so two independent implementations cannot drift.

**Why eight slots and not sixteen.** `screens.py` already records that WARN and
CLAY "are too close in hue to tell apart" at a 12 px dot; that pair measures
ΔE 37.3 in CIE-Lab, which makes it this panel's own empirical floor for
"distinguishable". A sixteen-slot palette selected from the colour cube gets
its two nearest members down to ΔE 30.5 — below that floor. Eight slots hold
ΔE 61.5, 1.65× the floor, and stay ΔE 34.1 clear of every semantic colour.
Eight also divides 256, so `digest[0] % 8` is exactly uniform where `% 10` or
`% 12` would over-weight the low slots. The identifier dot is drawn at radius 7
rather than the switchboard's 6, since it carries eight colours where those
rows carry three.

**Colours do repeat.** With eight slots, three concurrent sessions collide
about 34% of the time. That is the deliberate trade: a repeat is *visibly
identical*, which reads as "check the tag", where a sixteen-slot near-miss
would read as "these are different" when they are not. The tag is the
authority; the colour is the fast path to it.

**A session with no id shows no row.** `ident_view` returns None and the
screens omit it, rather than drawing a placeholder — a tag that matches nothing
in any terminal is noise on a panel this small.

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

## Security notes

**Frames carry your work, in the clear.** The working screen renders the current
TODO item's own text, and the daemon POSTs the JPEG over plain HTTP because that
is the protocol the panel speaks. Anyone on the same network can read those
frames off the wire, and can POST their own image to the panel. Fine on a home
network; think twice on café Wi-Fi or a shared office VLAN.

**The hotkeys need no privacy grant — unless you rebind them.** The defaults use
Carbon's `RegisterEventHotKey`, which *reserves* those two combinations with the
WindowServer and can observe nothing else, so macOS grants them silently. A
binding Carbon cannot express — `fn`, or a bare key with no modifier — falls back
to a `CGEventTap`, which is handed every keystroke on the system and so needs
Input Monitoring. Before granting that, note that **TCC records the interpreter,
not the script**: Input Monitoring on `python3` lets *any* Python script that
interpreter later runs read your keystrokes, not just this one. `keys.py
--permission` prints which binary that would be. Keep a binding with a real
modifier and the question never arises.

**`keys.py --watch` prints every keycode you press.** It exists to answer "what
does my keyboard actually send for that combination" while you pick a binding,
it needs the same Input Monitoring grant, and **the daemon never runs it** —
`--daemon` logs the bound combination by name and never any other key.

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

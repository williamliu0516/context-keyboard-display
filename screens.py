"""The three screens (Claude x4 variants, Sessions, Idle) for the 142x428 panel.

Rendering primitives — palette, fonts, mixed-script text, the mascot, the
supersampled vector layer — are imported from keyboard_status (the
claude-code-keyboard-status repo) so this display and that one can never
drift apart visually. Everything in this file is layout: the y-coordinates,
sizes and character budgets are the ones REVISED_PLAN.md defined and
FIXES.md refined against rendered previews; the approved mockups in the
planning repo's preview/output/ are the ground truth these functions match.

Every renderer takes a plain-dict view model (see collect.py for the live
builders, MOCKS below for the approved-mockup data) and returns a PIL Image
of exactly 142x428. No renderer reads the network, the clock, or any file.
"""

from PIL import Image, ImageDraw

from keyboard_status import (
    BG, INK, DIM, FAINT, RULE, CLAY, GOOD, WARN, BAD, SLEEP, SS,
    font, write, measure, cap_box, capsule, disc, draw_mascot, usage_color,
)

# ------------------------------------------------------------------- layout
# Constants shared with keyboard_status.py, plus the two REVISED_PLAN.md/
# FIXES.md additions: IDLE_CLOCK_SIZE (the hero clock, 52 px cap = 47 arcmin,
# derived from the documented arcmin math) and the tightened pill header
# (FLOOR_SIZE pill, pad 5, 6 px under the mascot — FIXES.md trim pass).

WIDTH, HEIGHT = 142, 428
PAD = 10
INNER = WIDTH - 2 * PAD

CORE_SIZE = 34               # 25 px cap = 22 arcmin at 600 mm
FLOOR_SIZE = 25              # 18 px cap = 16 arcmin; nothing readable below
STATE_SIZE = 28              # 20 px cap
CLOCK_SIZE = 28
IDLE_CLOCK_SIZE = 72         # 52 px cap = 47 arcmin, across-the-room

MASCOT_TOP = 44              # rows 0-40 are physically covered
MASCOT_SIZE = 80
MASCOT_REACH = 0.63

PILL_PAD_TIGHT = 5
BAR_H = 20
GAP_MASCOT_TIGHT = 6
GAP_SECTION = 18
GAP_BAR = 6
GAP_FOOTER = 14

STATE_STYLE = {
    "working": ("BUSY", CLAY),
    "waiting": ("YOU", WARN),
    "done": ("DONE", SLEEP),
}

# Dot colours per REVISED_PLAN §3.4. GOOD rather than CLAY for working: at
# 12 px diameter, WARN yellow and CLAY orange are too close in hue to tell
# apart at a glance; yellow/green/grey is unambiguous.
SESSION_DOT = {"waiting": WARN, "working": GOOD, "engaged": DIM}


# ------------------------------------------------------------------ elision
# FIXES.md Problem 2: middle-ellipsis is gone everywhere. Kebab/slug names
# carry their meaning up front, so prefix truncation reads as a name where
# two middle fragments read as noise.


def elide_end(draw, text, fonts, max_width):
    """Prefix truncation, pulled back to the nearest -/_/space boundary when
    one sits within 3 chars of the raw cut ("docs-si…" → "docs…")."""
    if measure(draw, text, fonts) <= max_width:
        return text
    keep = len(text) - 1
    while keep > 1:
        if measure(draw, text[:keep] + "…", fonts) <= max_width:
            break
        keep -= 1
    else:
        return "…"
    for boundary in "- _":
        idx = text[:keep].rfind(boundary)
        if keep - 3 <= idx and idx >= 3:
            keep = idx
            break
    return text[:keep].rstrip("-_ .") + "…"


def elide_file(draw, text, fonts, max_width):
    """Filename elision: keep the extension, prefix-truncate the stem
    ("renderer.py" → "rende…py" — the ellipsis swallows the extension's dot,
    which would otherwise render as a noisy four-dot run at 18 px cap)."""
    if measure(draw, text, fonts) <= max_width:
        return text
    stem, dot, ext = text.rpartition(".")
    if not dot or not stem or len(ext) > 4:
        return elide_end(draw, text, fonts, max_width)
    keep = len(stem)
    while keep > 1:
        candidate = stem[:keep] + "…" + ext
        if measure(draw, candidate, fonts) <= max_width:
            return candidate
        keep -= 1
    return elide_end(draw, text, fonts, max_width)


def wrap_words(draw, text, fonts, budget_first, budget_rest, max_lines):
    """Greedy wrap into at most max_lines; the last line prefix-elides.

    Breaks at spaces when one sits near the cut, else mid-run — CJK todo
    items carry no spaces at all and must still fill both lines rather than
    collapse to one elided fragment.
    """
    if max_lines <= 1:
        return [elide_end(draw, text, fonts, budget_first)]
    lines = []
    remaining = text.strip()
    while remaining and len(lines) < max_lines - 1:
        budget = budget_first if not lines else budget_rest
        if measure(draw, remaining, fonts) <= budget:
            lines.append(remaining)
            return lines
        cut = len(remaining)
        while cut > 1 and measure(draw, remaining[:cut], fonts) > budget:
            cut -= 1
        space = remaining.rfind(" ", 0, cut + 1)
        if space >= max(1, cut - 8):
            head, remaining = remaining[:space], remaining[space + 1:]
        else:
            head, remaining = remaining[:cut], remaining[cut:]
        lines.append(head.rstrip())
        remaining = remaining.lstrip()
    if remaining:
        budget = budget_first if not lines else budget_rest
        lines.append(elide_end(draw, remaining, fonts, budget))
    return lines


def project_label(draw, name, aliases, fonts, max_width):
    """Alias map first (the user names each repo once), prefix elision after.

    No automatic truncation can disambiguate worldengine-api vs
    worldengine-web in the ~7 characters a row holds; the alias map can.
    """
    if not isinstance(name, str) or not name:
        name = "--"
    return elide_end(draw, aliases.get(name, name), fonts, max_width)


# ------------------------------------------------------------------- canvas


class Canvas:
    """One 142x428 frame: supersampled vector layer + native glyph layer."""

    def __init__(self):
        self.base = Image.new("RGB", (WIDTH, HEIGHT), BG)
        self.shapes = Image.new("RGBA", (WIDTH * SS, HEIGHT * SS), (0, 0, 0, 0))
        self.glyphs = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        self.vector = ImageDraw.Draw(self.shapes)
        self.draw = ImageDraw.Draw(self.glyphs)

    def flatten(self):
        flattened = self.shapes.resize((WIDTH, HEIGHT), Image.LANCZOS)
        self.base.paste(flattened, (0, 0), flattened)
        self.base.paste(self.glyphs, (0, 0), self.glyphs)
        return self.base


def put(canvas, x, y_ink, text, fonts, fill, align="left", budget=INNER,
        elider=None):
    """Place text with its ink (cap) top exactly at y_ink; over-budget text
    is elided (prefix truncation by default) so no row ever overruns.
    Returns the ink bottom."""
    top, cap = cap_box(canvas.draw, fonts)
    if measure(canvas.draw, text, fonts) > budget:
        text = (elider or elide_end)(canvas.draw, text, fonts, budget)
    write(canvas.draw, x, y_ink - top, text, fonts, fill, align=align)
    return y_ink + cap


def mascot(canvas, state, phase=0.0, size=MASCOT_SIZE):
    cx = WIDTH / 2
    radius = size * MASCOT_REACH
    cy = MASCOT_TOP + radius
    draw_mascot(canvas.vector, cx * SS, cy * SS, size * SS, state, phase)
    if state == "idle":
        write(canvas.draw, cx + size * 0.30, cy - size * 0.42, "z",
              font(max(9, int(size * 0.17)), 700), (118, 128, 142))
        write(canvas.draw, cx + size * 0.46, cy - size * 0.62, "z",
              font(max(11, int(size * 0.24)), 700), (132, 142, 156))
    return cy + radius


def pill(canvas, word, tint, y, size=STATE_SIZE, pad=7):
    """State/count badge exactly as keyboard_status.render draws it."""
    cx = WIDTH / 2
    state_font = font(size, 800)
    state_top, state_cap = cap_box(canvas.draw, state_font, "H")
    pill_h = state_cap + 2 * pad
    pill_w = min(INNER, measure(canvas.draw, word, state_font) + 2 * pad + 6)
    canvas.vector.rounded_rectangle(
        [(cx - pill_w / 2) * SS, y * SS, (cx + pill_w / 2) * SS, (y + pill_h) * SS],
        radius=pill_h / 2 * SS, fill=tint + (40,), outline=tint + (170,), width=SS,
    )
    write(canvas.draw, cx, y + pad - state_top, word, state_font, tint, align="center")
    return y + pill_h


def header(canvas, mascot_state, word, tint, phase=0.0):
    """Mascot + tightened state pill (FIXES.md trim pass), shared by every
    Claude variant. Returns the ink-top y where content starts (197)."""
    bottom = mascot(canvas, mascot_state, phase=phase)
    pill_bottom = pill(canvas, word, tint, bottom + GAP_MASCOT_TIGHT,
                       size=FLOOR_SIZE, pad=PILL_PAD_TIGHT)
    return pill_bottom + GAP_SECTION


def rule(canvas, y):
    canvas.vector.rectangle(
        [PAD * SS, y * SS, (WIDTH - PAD) * SS, y * SS + SS], fill=RULE + (255,))


def stopwatch(canvas, cx, cy, r, tint):
    """Stopwatch glyph: marks a number as *time elapsed on the thing you're
    looking at*, distinct from the wall clock (DIM, below the footer rule,
    never gets the glyph) and finished durations ("2m 14s" units)."""
    v = canvas.vector
    stroke = max(SS, round(r * 0.30 * SS))
    v.ellipse([(cx - r) * SS, (cy - r) * SS, (cx + r) * SS, (cy + r) * SS],
              outline=tint + (255,), width=stroke)
    v.rounded_rectangle(
        [(cx - r * 0.24) * SS, (cy - r * 1.55) * SS,
         (cx + r * 0.24) * SS, (cy - r * 0.85) * SS],
        radius=r * 0.20 * SS, fill=tint + (255,))
    capsule(v, (cx * SS, cy * SS),
            ((cx + r * 0.42) * SS, (cy - r * 0.42) * SS),
            r * 0.14 * SS, r * 0.10 * SS, tint + (255,))


def elapsed_row(canvas, y_ink, text, tint):
    """Stopwatch glyph + m:ss at CORE_SIZE. Returns the row's ink bottom."""
    fonts = font(CORE_SIZE, 800)
    top, cap = cap_box(canvas.draw, fonts)
    r = 9
    glyph_w = 2 * r + 7
    stopwatch(canvas, PAD + r, y_ink + cap / 2 + 1, r, tint)
    return put(canvas, PAD + glyph_w, y_ink, text, fonts, tint,
               budget=INNER - glyph_w)


def todo_block(canvas, y_ink, count, item, item_lines=2):
    """TODO label/count row plus the chevron-anchored current item (FIXES.md:
    CLAY chevron ties the item to the CLAY count, item text at FLOOR+INK,
    two-line word wrap). item=None renders the counter row alone."""
    label_fonts = font(FLOOR_SIZE, 700)
    count_fonts = font(STATE_SIZE, 700)
    put(canvas, PAD, y_ink + 2, "TODO", label_fonts, DIM)
    bottom = put(canvas, WIDTH - PAD, y_ink, count, count_fonts, CLAY,
                 align="right")
    if not item or item_lines <= 0:
        return bottom
    item_fonts = font(FLOOR_SIZE, 700)
    top, cap = cap_box(canvas.draw, item_fonts)
    chevron_w = measure(canvas.draw, "▸", item_fonts)
    text_x = PAD + chevron_w + 6
    lines = wrap_words(canvas.draw, item, item_fonts, WIDTH - PAD - text_x,
                       INNER, item_lines)
    for index, line in enumerate(lines):
        line_ink = bottom + GAP_BAR
        if index == 0:
            write(canvas.draw, PAD, line_ink - top, "▸", item_fonts, CLAY)
        line_x = text_x if index == 0 else PAD
        put(canvas, line_x, line_ink, line, item_fonts, INK,
            budget=WIDTH - PAD - line_x)
        bottom = line_ink + cap
    return bottom


def footer_clock(canvas, rule_y, text):
    rule(canvas, rule_y)
    clock_font = font(CLOCK_SIZE, 600, rounded=False)
    write(canvas.draw, WIDTH / 2, rule_y + 11 - cap_box(canvas.draw, clock_font)[0],
          text, clock_font, DIM, align="center")


def diff_row(canvas, y, adds, dels):
    """Footer diff stat: additions left GOOD, deletions right BAD, FLOOR."""
    fonts = font(FLOOR_SIZE, 700)
    put(canvas, PAD, y, adds, fonts, GOOD)
    return put(canvas, WIDTH - PAD, y, dels, fonts, BAD, align="right")


# ------------------------------------------------------------------ screens
#
# View-model shapes (all strings pre-formatted by collect.py):
#   working: {phase, elapsed, project, todo: {count, item|None}|None,
#             diff: (adds, dels)|None, clock}
#   waiting: {tool, stuck, project, clock}
#   between: {duration|None, project, diff: (adds, dels)|None, clock}
#   sessions: {entries: [(state, project), ...], clock}
#   idle: {hh, mm, weekday, date, meters: [(label, pct|None), ...], online}


def claude_working(data, aliases):
    """WORKING variant, post trim pass: elapsed (CORE + stopwatch), project
    (FLOOR DIM — "which session is grinding"), TODO block, rule, diff. The
    footer clock returns only on the sparser no-item variants."""
    c = Canvas()
    word, tint = STATE_STYLE["working"]
    y = header(c, "working", word, tint, phase=data.get("phase", 0.0))
    y = elapsed_row(c, y, data["elapsed"], INK) + GAP_SECTION
    label = project_label(c.draw, data.get("project"), aliases,
                          font(FLOOR_SIZE, 700), INNER)
    y = put(c, PAD, y, label, font(FLOOR_SIZE, 700), DIM) + GAP_SECTION
    todo = data.get("todo")
    diff = data.get("diff")
    if todo:
        y = todo_block(c, y, todo["count"], todo.get("item"))
        bottom = y
        if diff:
            rule_y = y + GAP_FOOTER
            rule(c, rule_y)
            bottom = diff_row(c, rule_y + 12, *diff)
        if not todo.get("item"):
            # Counter-only fallback: cutting the item line buys the clock back.
            footer_clock(c, bottom + GAP_FOOTER, data["clock"])
    else:
        bottom = y - GAP_SECTION
        if diff:
            rule(c, y)
            bottom = diff_row(c, y + 12, *diff)
        footer_clock(c, bottom + GAP_FOOTER, data["clock"])
    return c.flatten()


def claude_waiting(data, aliases):
    """WAITING variant — the interrupt signal. Deliberately sparse: stuck-for
    timer in WARN, tool name as a question ("Bash?"), project, clock.

    The timer takes the first content slot, not the tool name, so the time row
    lands on the same baseline as WORKING's elapsed and BETWEEN-TURNS' duration.
    Flipping between screens then reads as one field changing rather than the
    whole layout shifting — and the eye already knows where to look for "how
    long has this been sitting there", which is the urgent number here.
    """
    c = Canvas()
    word, tint = STATE_STYLE["waiting"]
    y = header(c, "waiting", word, tint)
    y = elapsed_row(c, y, data["stuck"], WARN) + GAP_SECTION
    y = put(c, PAD, y, data["tool"], font(CORE_SIZE, 800), INK) + GAP_SECTION
    label = project_label(c.draw, data.get("project"), aliases,
                          font(FLOOR_SIZE, 700), INNER)
    y = put(c, PAD, y, label, font(FLOOR_SIZE, 700), DIM)
    footer_clock(c, y + GAP_FOOTER, data["clock"])
    return c.flatten()


def claude_between_turns(data, aliases):
    """BETWEEN-TURNS (engagement window): DONE pill, last turn's duration in
    bare units (no stopwatch — the glyph means a *running* timer), project,
    diff, clock."""
    c = Canvas()
    word, tint = STATE_STYLE["done"]
    y = header(c, "idle", word, tint)
    if data.get("duration"):
        y = put(c, PAD, y, data["duration"], font(CORE_SIZE, 800), INK) + GAP_SECTION
    label = project_label(c.draw, data.get("project"), aliases,
                          font(FLOOR_SIZE, 700), INNER)
    y = put(c, PAD, y, label, font(FLOOR_SIZE, 700), DIM) + GAP_SECTION
    diff = data.get("diff")
    bottom = y - GAP_SECTION
    if diff:
        rule(c, y)
        bottom = diff_row(c, y + 12, *diff)
    footer_clock(c, bottom + GAP_FOOTER, data["clock"])
    return c.flatten()


def sessions(data, aliases):
    """The multi-session switchboard. No mascot: count pill, then one
    dot + name row per session (30 px pitch, waiting-first), overflow line
    past six, clock. The dot carries the state so the name gets the full
    ~7-character budget."""
    c = Canvas()
    entries = data["entries"]
    pill(c, "{} LIVE".format(len(entries)), CLAY, 44)

    row_fonts = font(FLOOR_SIZE, 700)
    dot_r = 6
    dot_cx = PAD + dot_r
    text_x = dot_cx + dot_r + 6
    name_budget = WIDTH - PAD - text_x

    for index, (state, name) in enumerate(entries[:6]):
        y_ink = 104 + 30 * index
        top, cap = cap_box(c.draw, row_fonts)
        disc(c.vector, dot_cx * SS, (y_ink + cap / 2) * SS, dot_r * SS,
             SESSION_DOT.get(state, DIM) + (255,))
        label = project_label(c.draw, name, aliases, row_fonts, name_budget)
        tint = DIM if state == "engaged" else INK
        write(c.draw, text_x, y_ink - top, label, row_fonts, tint)

    if len(entries) > 6:
        put(c, PAD, 302, "+{} MORE".format(len(entries) - 6),
            font(FLOOR_SIZE, 700), DIM)

    footer_clock(c, 334, data["clock"])
    return c.flatten()


def idle(data, aliases=None):
    """The default screen: hero clock (stacked HH/MM), day/date, 5H/7D usage
    meters ("do I have budget to start another session"), connection dot."""
    c = Canvas()
    cx = WIDTH / 2
    hero = font(IDLE_CLOCK_SIZE, 800)
    put(c, cx, 60, data["hh"], hero, INK, align="center")
    put(c, cx, 122, data["mm"], hero, INK, align="center")
    put(c, cx, 192, data["weekday"], font(STATE_SIZE, 700), DIM, align="center")
    put(c, cx, 218, data["date"], font(STATE_SIZE, 700), INK, align="center")

    meter_font = font(FLOOR_SIZE, 700)
    for (label, pct), label_y, bar_y in zip(data["meters"], (256, 318), (280, 342)):
        if pct is None:
            colour, reading, filled = FAINT, "--", 0.0
        else:
            pct = max(0.0, min(100.0, float(pct)))
            colour, reading = usage_color(pct), "{:.0f}%".format(pct)
            filled = INNER * pct / 100.0
        put(c, PAD, label_y, label, meter_font, DIM)
        put(c, WIDTH - PAD, label_y, reading, meter_font, colour, align="right")
        c.vector.rounded_rectangle(
            [PAD * SS, bar_y * SS, (WIDTH - PAD) * SS, (bar_y + BAR_H) * SS],
            radius=BAR_H / 2 * SS, fill=FAINT + (255,))
        if filled >= 1:
            c.vector.rounded_rectangle(
                [PAD * SS, bar_y * SS, (PAD + max(BAR_H, filled)) * SS,
                 (bar_y + BAR_H) * SS],
                radius=BAR_H / 2 * SS, fill=colour + (255,))

    if data.get("online", True):
        disc(c.vector, cx * SS, 395 * SS, 3 * SS, FAINT + (255,))
    return c.flatten()


RENDERERS = {
    "claude_working": claude_working,
    "claude_waiting": claude_waiting,
    "claude_between_turns": claude_between_turns,
    "sessions": sessions,
    "idle": idle,
}


# --------------------------------------------------------------------- mocks
# The exact data behind the approved planning mockups (preview/output/*.jpg).
# `display.py --preview` renders these so any change to the renderers can be
# diffed pixel-for-pixel against the approved ground truth.

MOCK_ALIASES = {
    "psi0-detector": "detect",
    "psi0-planner": "plan",
    "worldengine-api": "we-api",
    "worldengine-web": "we-web",
}

MOCK_SESSIONS_6 = [
    ("waiting", "psi0-detector"),
    ("waiting", "robot-g1"),
    ("working", "worldengine-api"),
    ("working", "worldengine-web"),
    ("engaged", "psi0-planner"),
    ("engaged", "keyboard-display"),
]

MOCKS = [
    ("claude_working", {
        "phase": 0.85, "elapsed": "4:32", "project": "psi0-detector",
        "todo": {"count": "3/7", "item": "wire up previews"},
        "diff": ("+212", "−38"), "clock": "12:27"}),
    ("claude_working_no_todos", {
        "phase": 0.85, "elapsed": "4:32", "project": "psi0-detector",
        "todo": None, "diff": ("+212", "−38"), "clock": "12:27"}),
    ("claude_waiting", {
        "tool": "Bash?", "stuck": "2:41", "project": "psi0-detector",
        "clock": "12:27"}),
    ("claude_between_turns", {
        "duration": "2m 14s", "project": "psi0-detector",
        "diff": ("+212", "−38"), "clock": "12:27"}),
    ("sessions", {"entries": MOCK_SESSIONS_6, "clock": "12:27"}),
    ("sessions_overflow", {
        "entries": MOCK_SESSIONS_6 + [("engaged", "infra-tools"),
                                      ("engaged", "docs-site")],
        "clock": "12:27"}),
    ("idle", {"hh": "12", "mm": "27", "weekday": "THU", "date": "AUG 27",
              "meters": [("5H", 42), ("7D", 61)], "online": True}),
]


def render_mock(name):
    for mock_name, data in MOCKS:
        if mock_name == name:
            kind = "sessions" if name.startswith("sessions") else \
                   "claude_working" if name.startswith("claude_working") else name
            return RENDERERS[kind](data, MOCK_ALIASES)
    raise KeyError(name)

"""Every screen for the 142x428 panel: Claude (working / waiting / between
turns), Sessions, the Idle family (clock, now playing, print), and the home
takeovers (print failed, alert, due soon).

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

import math

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
    text_x = PAD + CHEVRON_H + 7
    lines = wrap_words(canvas.draw, item, item_fonts, WIDTH - PAD - text_x,
                       INNER, item_lines)
    for index, line in enumerate(lines):
        line_ink = bottom + GAP_BAR
        if index == 0:
            chevron(canvas, PAD + 1, line_ink + cap / 2, CLAY)
        line_x = text_x if index == 0 else PAD
        put(canvas, line_x, line_ink, line, item_fonts, INK,
            budget=WIDTH - PAD - line_x)
        bottom = line_ink + cap
    return bottom


# U+25B8 is in neither Ubuntu nor Noto CJK, so on the Linux/Docker panel a typed
# chevron rendered as a tofu box. Drawn on the vector layer it is the same
# shape under every font.
CHEVRON_H = 13


def chevron(canvas, x, y_mid, tint, h=CHEVRON_H):
    canvas.vector.polygon([(x * SS, (y_mid - h / 2) * SS),
                           ((x + h * 0.8) * SS, y_mid * SS),
                           (x * SS, (y_mid + h / 2) * SS)], fill=tint + (255,))


def footer_clock(canvas, rule_y, text):
    """Rule + centred clock. Returns the clock's ink bottom, so a row placed
    after it (the identifier) can sit a known gap below rather than guessing."""
    rule(canvas, rule_y)
    clock_font = font(CLOCK_SIZE, 600, rounded=False)
    top, cap = cap_box(canvas.draw, clock_font)
    write(canvas.draw, WIDTH / 2, rule_y + 11 - top,
          text, clock_font, DIM, align="center")
    return rule_y + 11 + cap


def diff_row(canvas, y, adds, dels):
    """Footer diff stat: additions left GOOD, deletions right BAD, FLOOR."""
    fonts = font(FLOOR_SIZE, 700)
    put(canvas, PAD, y, adds, fonts, GOOD)
    return put(canvas, WIDTH - PAD, y, dels, fonts, BAD, align="right")


# The identifier dot is drawn a shade larger than the switchboard's state dots.
# Those carry three well-separated colours; this one carries eight, and the
# extra radius is what keeps the palette's nearest pair apart at a glance. See
# collect.py's SESSION IDENTIFIER SPEC v1 for how the colour is derived.
IDENT_DOT_R = 7


def ident_row(canvas, y_ink, ident):
    """Colour dot + six-character tag: the same pair the terminal's status bar
    prints for this session. Returns the row's ink bottom, or `y_ink` unchanged
    when the session has no id to identify."""
    if not ident:
        return y_ink
    fonts = font(FLOOR_SIZE, 700)
    cap = cap_box(canvas.draw, fonts)[1]
    cx = PAD + IDENT_DOT_R
    disc(canvas.vector, cx * SS, (y_ink + cap / 2) * SS, IDENT_DOT_R * SS,
         tuple(ident["rgb"]) + (255,))
    text_x = cx + IDENT_DOT_R + 7
    return put(canvas, text_x, y_ink, ident["tag"], fonts, DIM,
               budget=WIDTH - PAD - text_x)


def bar(canvas, x0, y, x1, h, frac, colour):
    """Rounded track with a rounded fill, like the idle meters but any height."""
    v = canvas.vector
    v.rounded_rectangle([x0 * SS, y * SS, x1 * SS, (y + h) * SS],
                        radius=h / 2 * SS, fill=FAINT + (255,))
    filled = (x1 - x0) * max(0.0, min(1.0, frac))
    if filled >= 1:
        v.rounded_rectangle([x0 * SS, y * SS, (x0 + max(h, filled)) * SS,
                             (y + h) * SS], radius=h / 2 * SS, fill=colour + (255,))


# Strip icons: 16 px, drawn, so they survive any font.

def icon_printer(canvas, x, y, s, tint):
    v, w = canvas.vector, max(SS, round(s * 0.12 * SS))
    v.rounded_rectangle([x * SS, y * SS, (x + s) * SS, (y + s) * SS],
                        radius=s * 0.18 * SS, outline=tint + (255,), width=w)
    cx = x + s / 2
    v.polygon([((cx - s * 0.18) * SS, (y + s * 0.22) * SS),
               ((cx + s * 0.18) * SS, (y + s * 0.22) * SS),
               (cx * SS, (y + s * 0.50) * SS)], fill=tint + (255,))
    v.rectangle([(x + s * 0.22) * SS, (y + s * 0.68) * SS,
                 (x + s * 0.78) * SS, (y + s * 0.80) * SS], fill=tint + (255,))


def icon_calendar(canvas, x, y, s, tint):
    v, w = canvas.vector, max(SS, round(s * 0.12 * SS))
    v.rounded_rectangle([x * SS, (y + s * 0.12) * SS, (x + s) * SS, (y + s) * SS],
                        radius=s * 0.18 * SS, outline=tint + (255,), width=w)
    v.rectangle([x * SS, (y + s * 0.12) * SS, (x + s) * SS, (y + s * 0.38) * SS],
                fill=tint + (255,))
    for fx in (0.28, 0.72):
        v.rectangle([(x + s * fx - s * 0.06) * SS, y * SS,
                     (x + s * fx + s * 0.06) * SS, (y + s * 0.22) * SS],
                    fill=tint + (255,))


def icon_note(canvas, x, y, s, tint):
    v = canvas.vector
    r = s * 0.22
    disc(v, (x + r + s * 0.08) * SS, (y + s - r) * SS, r * SS, tint + (255,))
    stem_x = x + s * 0.08 + 2 * r - s * 0.06
    v.rectangle([stem_x * SS, (y + s * 0.08) * SS, (stem_x + s * 0.12) * SS,
                 (y + s - r) * SS], fill=tint + (255,))
    v.polygon([(stem_x * SS, (y + s * 0.08) * SS),
               ((x + s * 0.95) * SS, (y + s * 0.28) * SS),
               ((x + s * 0.95) * SS, (y + s * 0.46) * SS),
               (stem_x * SS, (y + s * 0.30) * SS)], fill=tint + (255,))


def icon_alert(canvas, x, y, s, tint):
    v = canvas.vector
    pts = [((x + s / 2) * SS, y * SS), ((x + s) * SS, (y + s * 0.92) * SS),
           (x * SS, (y + s * 0.92) * SS)]
    v.polygon(pts, fill=tint + (255,))
    v.rectangle([(x + s * 0.44) * SS, (y + s * 0.30) * SS,
                 (x + s * 0.56) * SS, (y + s * 0.62) * SS], fill=BG + (255,))
    v.rectangle([(x + s * 0.44) * SS, (y + s * 0.70) * SS,
                 (x + s * 0.56) * SS, (y + s * 0.80) * SS], fill=BG + (255,))


STRIP_ICONS = {"print": icon_printer, "due": icon_calendar, "music": icon_note,
               "alert": icon_alert}


def strip(canvas, rule_y, item, clock=None):
    """The home strip: the clock row, promoted to the one household thing worth
    a glance right now (home.strip_item picks it). Falls back to the plain clock
    when there is none, or draws nothing when `clock` is None too. Returns the
    ink bottom.

    Two lines, because 142 px at the floor size holds about seven characters: an
    icon and the headline, then either a progress bar or a dim second line, each
    with an optional right-aligned token (63%, 2d)."""
    if not item:
        return footer_clock(canvas, rule_y, clock) if clock else rule_y
    rule(canvas, rule_y)
    y = rule_y + 12
    kind = item["kind"]
    tint = item.get("tint", DIM)
    STRIP_ICONS[kind](canvas, PAD, y + 1, 16, tint)
    text_x = PAD + 16 + 5
    f = font(FLOOR_SIZE, 700)
    put(canvas, text_x, y, item["head"], f, item.get("head_tint", INK),
        budget=WIDTH - PAD - text_x)
    y += 18 + line_gap(item["head"]) + 2
    right = item.get("right")
    right_w = measure(canvas.draw, right, f) if right else 0
    if right:
        put(canvas, WIDTH - PAD, y, right, f, item.get("right_tint", tint),
            align="right")
    if item.get("frac") is not None:
        bar(canvas, PAD, y + 5, WIDTH - PAD - right_w - 8, 8, item["frac"], tint)
        return y + 18
    if item.get("sub"):
        budget = INNER - (right_w + 8 if right else 0)
        sub = item["sub"]
        # "CS572" beside "17h" is 4 px too wide; "572" says the same to its owner
        if item.get("sub_alt") and measure(canvas.draw, sub, f) > budget:
            sub = item["sub_alt"]
        return put(canvas, PAD, y, sub, f, item.get("sub_tint", DIM), budget=budget)
    return y + 18


def ring(canvas, cx, cy, r, width, frac, colour):
    """Progress ring with round caps, starting at twelve o'clock."""
    v = canvas.vector
    box = [(cx - r) * SS, (cy - r) * SS, (cx + r) * SS, (cy + r) * SS]
    v.ellipse(box, outline=FAINT + (255,), width=round(width * SS))
    frac = max(0.0, min(1.0, frac))
    if frac <= 0:
        return
    v.arc(box, -90, -90 + 360 * frac, fill=colour + (255,), width=round(width * SS))
    for angle in (-90, -90 + 360 * frac):
        a = math.radians(angle)
        disc(v, (cx + (r - width / 2) * math.cos(a)) * SS,
             (cy + (r - width / 2) * math.sin(a)) * SS, width / 2 * SS, colour + (255,))


def badge_header(canvas, draw_icon, word, tint):
    """An icon where the mascot sits and a pill under it: the same skeleton as
    WAITING, so a takeover reads as 'something needs you' before it is read."""
    cx, cy, r = WIDTH / 2, MASCOT_TOP + 50, 46
    draw_icon(canvas, cx, cy, r)
    bottom = pill(canvas, word, tint, cy + r + GAP_MASCOT_TIGHT + 2,
                  size=FLOOR_SIZE, pad=PILL_PAD_TIGHT)
    return bottom + GAP_SECTION


def glyph_ring(frac, colour, centre, centre_tint):
    def draw(canvas, cx, cy, r):
        ring(canvas, cx, cy, r, 10, frac, colour)
        put(canvas, cx, cy - 12, centre, font(CORE_SIZE, 800), centre_tint, align="center")
    return draw


def glyph_alert(canvas, cx, cy, r):
    v = canvas.vector
    pts = [(cx * SS, (cy - r) * SS), ((cx + r * 1.05) * SS, (cy + r * 0.8) * SS),
           ((cx - r * 1.05) * SS, (cy + r * 0.8) * SS)]
    v.polygon(pts, fill=BAD + (52,))
    v.line(pts + [pts[0]], fill=BAD + (230,), width=3 * SS, joint="curve")
    v.rounded_rectangle([(cx - 4) * SS, (cy - r * 0.42) * SS, (cx + 4) * SS,
                         (cy + r * 0.28) * SS], radius=4 * SS, fill=BAD + (255,))
    disc(v, cx * SS, (cy + r * 0.53) * SS, 5 * SS, BAD + (255,))


def glyph_calendar(month, day, tint):
    def draw(canvas, cx, cy, r):
        v = canvas.vector
        x0, x1, y0, y1 = cx - r, cx + r, cy - r * 0.9, cy + r
        v.rounded_rectangle([x0 * SS, y0 * SS, x1 * SS, y1 * SS], radius=12 * SS,
                            fill=FAINT + (255,))
        v.rounded_rectangle([x0 * SS, y0 * SS, x1 * SS, (y0 + 26) * SS],
                            radius=12 * SS, fill=tint + (255,))
        v.rectangle([x0 * SS, (y0 + 14) * SS, x1 * SS, (y0 + 26) * SS],
                    fill=tint + (255,))
        put(canvas, cx, y0 + 6, month, font(18, 800), BG, align="center")
        put(canvas, cx, y0 + 37, day, font(46, 800), INK, align="center")
    return draw


def big_with_unit(canvas, y, number, unit, tint):
    """"1h12 left": the number at CORE, its unit dim and smaller beside it --
    or the number alone when a wide one leaves no room for the unit."""
    f = font(CORE_SIZE, 800)
    put(canvas, PAD, y, number, f, tint)
    x = PAD + measure(canvas.draw, number, f) + 7
    unit_font = font(20, 700)
    if measure(canvas.draw, unit, unit_font) <= WIDTH - PAD - x:
        put(canvas, x, y + 10, unit, unit_font, DIM)
    return y + cap_box(canvas.draw, f)[1]


def line_gap(text):
    """Gap under a wrapped line. Han glyphs stand taller than the Latin cap the
    rows are spaced by, so a CJK line needs a few more pixels of air or its
    ink touches the line below."""
    return GAP_BAR + (10 if any("\u2e80" <= ch <= "\u9fff" or "\uf900" <= ch <= "\uffef"
                               for ch in text) else 0)


# ------------------------------------------------------------------ screens
#
# View-model shapes (all strings pre-formatted by collect.py):
#   working: {phase, elapsed, project, todo: {count, item|None}|None,
#             diff: (adds, dels)|None, clock}
#   waiting: {tool, stuck, project, clock}
#   between: {duration|None, project, diff: (adds, dels)|None, clock}
#   sessions: {rows: [{state, name, detail: (left, right)}], idle, clock}
#   idle: {hh, mm, weekday, date, meters: [(label, pct|None), ...], online}
# plus, from home.py: `strip` on sessions / waiting / between / idle, and the
# whole view model of now_playing / print / print_failed / alert / due_soon.


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
            bottom = footer_clock(c, bottom + GAP_FOOTER, data["clock"])
    else:
        bottom = y - GAP_SECTION
        if diff:
            rule(c, y)
            bottom = diff_row(c, y + 12, *diff)
        bottom = footer_clock(c, bottom + GAP_FOOTER, data["clock"])
    ident_row(c, bottom + GAP_FOOTER, data.get("ident"))
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
    bottom = strip(c, y + GAP_FOOTER, data.get("strip"), data["clock"])
    ident_row(c, bottom + GAP_FOOTER, data.get("ident"))
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
    bottom = strip(c, bottom + GAP_FOOTER, data.get("strip"), data["clock"])
    ident_row(c, bottom + GAP_FOOTER, data.get("ident"))
    return c.flatten()


# Dot colours per REVISED_PLAN §3.4. GOOD rather than CLAY for working: at
# 12 px diameter, WARN yellow and CLAY orange are too close in hue to tell
# apart at a glance; yellow/green/grey is unambiguous.
SESSION_ROW_DOT = {"waiting": WARN, "working": GOOD, "done": SLEEP}
SESSION_ROW_TINTS = {"waiting": (WARN, WARN), "working": (DIM, CLAY), "done": (DIM, DIM)}
SESSION_ROWS_MAX = 4


def sessions(data, aliases):
    """The switchboard: a count pill, then two lines per session that matters
    (name; what it is doing), waiting first. Past four rows the last slot becomes
    "+N more"; sessions that are merely engaged fold into "+N idle". The clock
    row is the home strip.

    The second line starts at the left margin, not under the name: "Bash? 0:41"
    needs the full 122 px at the floor size in the panel's Ubuntu face."""
    c = Canvas()
    rows = data["rows"]
    pill(c, "{} LIVE".format(len(rows)), CLAY, 44)

    f = font(FLOOR_SIZE, 700)
    top, cap = cap_box(c.draw, f)
    dot_r = 6
    dot_cx = PAD + dot_r
    text_x = dot_cx + dot_r + 6
    y = 98
    shown = rows if len(rows) <= SESSION_ROWS_MAX else rows[:SESSION_ROWS_MAX - 1]
    for row in shown:
        state = row["state"]
        disc(c.vector, dot_cx * SS, (y + cap / 2) * SS, dot_r * SS,
             SESSION_ROW_DOT.get(state, DIM) + (255,))
        label = project_label(c.draw, row["name"], aliases, f, WIDTH - PAD - text_x)
        write(c.draw, text_x, y - top, label, f, INK)
        detail_y = y + cap + 8
        left, right = row["detail"]
        left_tint, right_tint = SESSION_ROW_TINTS.get(state, (DIM, DIM))
        right_w = measure(c.draw, right, f) + 4 if right else 0
        put(c, PAD, detail_y, left, f, left_tint, budget=INNER - right_w)
        if right:
            put(c, WIDTH - PAD, detail_y, right, f, right_tint, align="right")
        y = detail_y + cap + 16
    more = len(rows) - len(shown)
    tail = ("+{} more".format(more) if more else
            "+{} idle".format(data["idle"]) if data.get("idle") else None)
    if tail:
        put(c, PAD, y - 2, tail, f, DIM)
    strip(c, 352, data.get("strip"), data["clock"])
    return c.flatten()


def idle(data, aliases=None):
    """The default screen: hero clock (stacked HH/MM), day/date, 5H/7D usage
    meters ("do I have budget to start another session"), then the home strip.
    The meters are slim bars so the strip fits; with no strip the old
    connection dot comes back in its place."""
    c = Canvas()
    cx = WIDTH / 2
    hero = font(IDLE_CLOCK_SIZE, 800)
    put(c, cx, 60, data["hh"], hero, INK, align="center")
    put(c, cx, 122, data["mm"], hero, INK, align="center")
    put(c, cx, 192, data["weekday"], font(STATE_SIZE, 700), DIM, align="center")
    put(c, cx, 218, data["date"], font(STATE_SIZE, 700), INK, align="center")

    meter_font = font(FLOOR_SIZE, 700)
    y = 256
    for label, pct in data["meters"]:
        if pct is None:
            colour, reading, frac = FAINT, "--", 0.0
        else:
            pct = max(0.0, min(100.0, float(pct)))
            colour, reading, frac = usage_color(pct), "{:.0f}%".format(pct), pct / 100.0
        put(c, PAD, y, label, meter_font, DIM)
        put(c, WIDTH - PAD, y, reading, meter_font, colour, align="right")
        bar(c, PAD, y + 25, WIDTH - PAD, 8, frac, colour)
        y += 25 + 8 + 14

    if data.get("strip"):
        strip(c, y + 2, data["strip"])
    elif data.get("online", True):
        disc(c.vector, cx * SS, 395 * SS, 3 * SS, FAINT + (255,))
    return c.flatten()


def cover_image(art, size):
    """Centre-crop the cover to a square of `size` px, or None."""
    if art is None:
        return None
    w, h = art.size
    side = min(w, h)
    box = ((w - side) // 2, (h - side) // 2, (w - side) // 2 + side, (h - side) // 2 + side)
    return art.convert("RGB").crop(box).resize((size, size), Image.LANCZOS)


def now_playing(data, aliases=None):
    """Idle while music plays: cover, title (two lines), artist, progress,
    the speaker and its volume, clock."""
    c = Canvas()
    art = 122
    top = 46
    cover = cover_image(data.get("art"), art)
    mask = Image.new("L", (art * SS, art * SS), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, art * SS - 1, art * SS - 1],
                                           radius=10 * SS, fill=255)
    mask = mask.resize((art, art), Image.LANCZOS)
    if cover is not None:
        c.base.paste(cover, (PAD, top), mask)
    else:
        c.vector.rounded_rectangle([PAD * SS, top * SS, (PAD + art) * SS, (top + art) * SS],
                                   radius=10 * SS, fill=FAINT + (255,))
        icon_note(c, WIDTH / 2 - 22, top + art / 2 - 22, 44, DIM)
    f = font(FLOOR_SIZE, 700)
    y = top + art + 16
    for line in wrap_words(c.draw, data["title"], f, INNER, INNER, 2):
        y = put(c, PAD, y, line, f, INK) + line_gap(line)
    if data.get("artist"):
        y = put(c, PAD, y - GAP_BAR + 10, data["artist"], f, DIM) + GAP_SECTION
    else:
        y += GAP_SECTION - GAP_BAR
    if data.get("frac") is not None:
        bar(c, PAD, y, WIDTH - PAD, 6, data["frac"], INK)
        y += 6 + 10
        small = font(20, 700)
        put(c, PAD, y, data.get("pos") or "", small, DIM)
        y = put(c, WIDTH - PAD, y, data.get("rest") or "", small, DIM, align="right")
        y += GAP_SECTION
    vol = data.get("volume")
    vol_w = measure(c.draw, vol, f) + 8 if vol else 0
    put(c, PAD, y, data.get("source") or "", f, INK, budget=INNER - vol_w)
    if vol:
        put(c, WIDTH - PAD, y, vol, f, DIM, align="right")
    y += 18
    footer_clock(c, y + GAP_FOOTER, data["clock"])
    return c.flatten()


def print_progress(data, aliases=None):
    """A print running (or paused): ring, time left, job, filament, ETA, clock."""
    c = Canvas()
    paused = data.get("paused")
    tint = WARN if paused else CLAY
    y = badge_header(c, glyph_ring(data["pct"] / 100.0, tint, "{}%".format(data["pct"]), INK),
                     "PAUSE" if paused else "PRINT", tint)
    if data.get("left"):
        y = big_with_unit(c, y, data["left"], "left", INK) + GAP_SECTION
    f = font(FLOOR_SIZE, 700)
    y = put(c, PAD, y, data.get("file") or "--", f, DIM) + GAP_SECTION
    if data.get("filament"):
        rgb = data.get("filament_rgb") or DIM
        disc(c.vector, (PAD + 7) * SS, (y + 9) * SS, 7 * SS, tuple(rgb) + (255,))
        y = put(c, PAD + 21, y, data["filament"], f, INK, budget=WIDTH - PAD - (PAD + 21))
    else:
        y -= GAP_SECTION
    rule(c, y + GAP_FOOTER)
    y = y + GAP_FOOTER + 12
    if data.get("eta"):
        put(c, PAD, y, "ETA", f, DIM)
        y = put(c, WIDTH - PAD, y, data["eta"], f, INK, align="right")
        footer_clock(c, y + GAP_FOOTER, data["clock"])
    else:
        clock_font = font(CLOCK_SIZE, 600, rounded=False)
        top, _ = cap_box(c.draw, clock_font)
        write(c.draw, WIDTH / 2, y - 1 - top, data["clock"], clock_font, DIM, align="center")
    return c.flatten()


def print_failed(data, aliases=None):
    """A print that stopped: red ring at the percentage it died at, how long it
    has sat there, the printer's own (Chinese) HMS text, the job, clock."""
    c = Canvas()
    y = badge_header(c, glyph_ring(data["pct"] / 100.0, BAD, "{}%".format(data["pct"]), BAD),
                     "FAIL", BAD)
    if data.get("stuck"):
        y = elapsed_row(c, y, data["stuck"], BAD) + GAP_SECTION
    f = font(FLOOR_SIZE, 700)
    for line in wrap_words(c.draw, data.get("error") or "--", f, INNER, INNER, 2):
        y = put(c, PAD, y, line, f, INK) + line_gap(line)
    y += GAP_SECTION - GAP_BAR
    y = put(c, PAD, y, data.get("file") or "--", f, DIM)
    footer_clock(c, y + GAP_FOOTER, data["clock"])
    return c.flatten()


def alert(data, aliases=None):
    """A homeboard health check gone bad: how long, what (two lines), detail."""
    c = Canvas()
    y = badge_header(c, glyph_alert, "ALERT", BAD)
    if data.get("since"):
        y = elapsed_row(c, y, data["since"], BAD) + GAP_SECTION
    f = font(FLOOR_SIZE, 700)
    for line in wrap_words(c.draw, data["what"], f, INNER, INNER, 2):
        y = put(c, PAD, y, line, f, INK) + line_gap(line)
    if data.get("detail"):
        y = put(c, PAD, y + 2, data["detail"], f, BAD)
    else:
        y -= GAP_BAR
    footer_clock(c, y + GAP_FOOTER, data["clock"])
    return c.flatten()


def due_soon(data, aliases=None):
    """A deadline inside the takeover window: calendar tile, countdown, title,
    course, when."""
    c = Canvas()
    tint = BAD if data.get("urgent") else WARN
    y = badge_header(c, glyph_calendar(data["month"], data["day"], tint), "DUE", tint)
    y = big_with_unit(c, y, data["left"], "left", tint) + GAP_SECTION
    f = font(FLOOR_SIZE, 700)
    y = put(c, PAD, y, data["title"], f, INK) + line_gap(data["title"])
    if data.get("course"):
        y = put(c, PAD, y, data["course"], f, DIM) + GAP_SECTION
    else:
        y += GAP_SECTION - GAP_BAR
    y = put(c, PAD, y, data.get("when") or "", f, DIM)
    footer_clock(c, y + GAP_FOOTER, data["clock"])
    return c.flatten()


RENDERERS = {
    "claude_working": claude_working,
    "claude_waiting": claude_waiting,
    "claude_between_turns": claude_between_turns,
    "sessions": sessions,
    "idle": idle,
    "now_playing": now_playing,
    "print": print_progress,
    "print_failed": print_failed,
    "alert": alert,
    "due_soon": due_soon,
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

MOCK_ROWS = [
    {"state": "waiting", "name": "psi0-detector", "detail": ("Bash?", "0:41")},
    {"state": "working", "name": "worldengine-api", "detail": ("4:32", "3/7")},
    {"state": "working", "name": "worldengine-web", "detail": ("0:21", "")},
    {"state": "done", "name": "psi0-planner", "detail": ("done", "2m")},
]

MOCK_STRIPS = {
    "print": {"kind": "print", "head": "1h12", "right": "63%", "frac": 0.63,
              "tint": CLAY, "right_tint": CLAY},
    "due": {"kind": "due", "head": "HW6", "sub": "CS570", "right": "2d",
            "tint": DIM, "right_tint": INK},
    "music": {"kind": "music", "head": "Blue in Green", "sub": "Bill Evans",
              "tint": CLAY},
}

# Three different session ids, so the previews show three different slots of
# the identifier palette rather than one colour repeated.
MOCK_IDENTS = {
    "a3f92c": {"tag": "a3f92c", "rgb": (255, 95, 0)},
    "7b14de": {"tag": "7b14de", "rgb": (0, 215, 255)},
    "c081fa": {"tag": "c081fa", "rgb": (255, 0, 255)},
}

MOCKS = [
    ("claude_working", {
        "phase": 0.85, "elapsed": "4:32", "project": "psi0-detector",
        "todo": {"count": "3/7", "item": "wire up previews"},
        "diff": ("+212", "−38"), "clock": "12:27",
        "ident": MOCK_IDENTS["a3f92c"]}),
    ("claude_working_no_todos", {
        "phase": 0.85, "elapsed": "4:32", "project": "psi0-detector",
        "todo": None, "diff": ("+212", "−38"), "clock": "12:27",
        "ident": MOCK_IDENTS["a3f92c"]}),
    ("claude_waiting", {
        "tool": "Bash?", "stuck": "2:41", "project": "psi0-detector",
        "clock": "12:27", "ident": MOCK_IDENTS["7b14de"], "strip": MOCK_STRIPS["due"]}),
    ("claude_between_turns", {
        "duration": "2m 14s", "project": "psi0-detector",
        "diff": ("+212", "−38"), "clock": "12:27",
        "ident": MOCK_IDENTS["c081fa"], "strip": MOCK_STRIPS["music"]}),
    ("sessions", {"rows": MOCK_ROWS, "idle": 3, "clock": "12:27",
                  "strip": MOCK_STRIPS["print"]}),
    ("sessions_overflow", {
        "rows": MOCK_ROWS + [{"state": "working", "name": "infra-tools",
                              "detail": ("7:02", "5/5")}],
        "idle": 2, "clock": "12:27", "strip": None}),
    ("idle", {"hh": "12", "mm": "27", "weekday": "THU", "date": "AUG 27",
              "meters": [("5H", 42), ("7D", 61)], "online": True,
              "strip": MOCK_STRIPS["due"]}),
    ("now_playing", {"title": "Blue in Green", "artist": "Bill Evans", "art": None,
                     "frac": 0.41, "pos": "2:14", "rest": "−3:13",
                     "source": "LSX II", "volume": "38", "clock": "12:27"}),
    ("print", {"pct": 63, "left": "1h12", "file": "hook_v2", "filament": "PLA",
               "filament_rgb": (226, 226, 220), "eta": "13:39", "clock": "12:27"}),
    ("print_failed", {"pct": 41, "stuck": "4:12", "error": "喷嘴堵头",
                      "file": "hook_v2", "clock": "12:27"}),
    ("alert", {"since": "12:40", "what": "T7 SSD 未挂载", "detail": "/mnt/t7",
               "clock": "12:27"}),
    ("due_soon", {"month": "OCT", "day": "16", "left": "18h", "title": "HW6",
                  "course": "CS570", "when": "Fri 23:59", "clock": "05:59"}),
]

MOCK_KINDS = {"claude_working_no_todos": "claude_working",
              "sessions_overflow": "sessions"}


def render_mock(name):
    for mock_name, data in MOCKS:
        if mock_name == name:
            return RENDERERS[MOCK_KINDS.get(name, name)](data, MOCK_ALIASES)
    raise KeyError(name)

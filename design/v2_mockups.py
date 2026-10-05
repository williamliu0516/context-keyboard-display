#!/usr/bin/env python3
"""v2 design mockups: every screen the panel would show after the redesign.

Not wired to anything -- each renderer takes a hand-written view model and
returns a 142x428 PIL image, built from the same primitives as screens.py so
the pictures are what the panel would actually show. Run it inside the
container (that is where the real panel's Ubuntu face lives):

    docker exec ckd-display python3 \\
        /Users/hermes/projects/context-keyboard-display/design/v2_mockups.py /tmp/v2

What changes, in one line each:
  sessions  two-line rows (name / what it is doing), idle sessions folded
            into one "+N idle" line, home strip instead of the clock
  strip     the clock row becomes the single most important household item:
            a running print, a deadline inside 3 days, or what is playing
  takeovers print, print failed, Hermes approval, system alert, due soon --
            full screens on the same ladder as WAITING
  playing   idle while music plays: cover art, title, artist, source
"""

import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if os.path.isdir("/app"):
    sys.path.insert(0, "/app")                       # the image's own copy
else:
    sys.path.insert(0, REPO)
    sys.path.insert(0, os.path.expanduser("~/projects/claude-code-keyboard-status"))

import keyboard_status as ks  # noqa: E402
import service  # noqa: E402

service.apply_font_fallback(ks)

from PIL import Image, ImageDraw  # noqa: E402

import screens as S  # noqa: E402
from keyboard_status import (  # noqa: E402
    BG, INK, DIM, FAINT, RULE, CLAY, GOOD, WARN, BAD, SLEEP, SS,
    font, write, measure, cap_box, disc,
)
from screens import (  # noqa: E402
    Canvas, put, pill, header, rule, elapsed_row, footer_clock, diff_row,
    ident_row, project_label, elide_file, wrap_words,
    WIDTH, HEIGHT, PAD, INNER, CORE_SIZE, FLOOR_SIZE, STATE_SIZE,
    GAP_SECTION, GAP_FOOTER, GAP_BAR, GAP_MASCOT_TIGHT, PILL_PAD_TIGHT,
    MASCOT_TOP,
)

F_FLOOR = lambda: font(FLOOR_SIZE, 700)  # noqa: E731
F_CORE = lambda: font(CORE_SIZE, 800)    # noqa: E731
CAP_FLOOR = 18


# ------------------------------------------------------------------- pieces


def bar(c, x0, y, x1, h, frac, colour):
    """Rounded track + fill, on the supersampled layer."""
    v = c.vector
    v.rounded_rectangle([x0 * SS, y * SS, x1 * SS, (y + h) * SS],
                        radius=h / 2 * SS, fill=FAINT + (255,))
    filled = (x1 - x0) * max(0.0, min(1.0, frac))
    if filled >= 1:
        v.rounded_rectangle([x0 * SS, y * SS, (x0 + max(h, filled)) * SS,
                             (y + h) * SS], radius=h / 2 * SS, fill=colour + (255,))


def icon_printer(c, x, y, s, tint):
    """A gantry: frame, nozzle, bed. Reads as 'printer' at 16 px."""
    v, w = c.vector, max(SS, round(s * 0.12 * SS))
    v.rounded_rectangle([x * SS, y * SS, (x + s) * SS, (y + s) * SS],
                        radius=s * 0.18 * SS, outline=tint + (255,), width=w)
    cx = x + s / 2
    v.polygon([((cx - s * 0.18) * SS, (y + s * 0.22) * SS),
               ((cx + s * 0.18) * SS, (y + s * 0.22) * SS),
               (cx * SS, (y + s * 0.50) * SS)], fill=tint + (255,))
    v.rectangle([(x + s * 0.22) * SS, (y + s * 0.68) * SS,
                 (x + s * 0.78) * SS, (y + s * 0.80) * SS], fill=tint + (255,))


def icon_calendar(c, x, y, s, tint):
    v, w = c.vector, max(SS, round(s * 0.12 * SS))
    v.rounded_rectangle([x * SS, (y + s * 0.12) * SS, (x + s) * SS, (y + s) * SS],
                        radius=s * 0.18 * SS, outline=tint + (255,), width=w)
    v.rectangle([x * SS, (y + s * 0.12) * SS, (x + s) * SS, (y + s * 0.38) * SS],
                fill=tint + (255,))
    for fx in (0.28, 0.72):
        v.rectangle([(x + s * fx - s * 0.06) * SS, y * SS,
                     (x + s * fx + s * 0.06) * SS, (y + s * 0.22) * SS],
                    fill=tint + (255,))


def icon_note(c, x, y, s, tint):
    v = c.vector
    r = s * 0.22
    disc(v, (x + r + s * 0.08) * SS, (y + s - r) * SS, r * SS, tint + (255,))
    stem_x = x + s * 0.08 + 2 * r - s * 0.06
    v.rectangle([stem_x * SS, (y + s * 0.08) * SS, (stem_x + s * 0.12) * SS,
                 (y + s - r) * SS], fill=tint + (255,))
    v.polygon([(stem_x * SS, (y + s * 0.08) * SS),
               ((x + s * 0.95) * SS, (y + s * 0.28) * SS),
               ((x + s * 0.95) * SS, (y + s * 0.46) * SS),
               (stem_x * SS, (y + s * 0.30) * SS)], fill=tint + (255,))


ICONS = {"print": icon_printer, "due": icon_calendar, "music": icon_note}


def strip(c, rule_y, item, clock=None):
    """The home strip: the clock row, promoted to the one household thing
    worth a glance right now. Falls back to the plain clock when nothing is.
    Returns the ink bottom."""
    if not item:
        return footer_clock(c, rule_y, clock)
    rule(c, rule_y)
    y = rule_y + 12
    kind = item["kind"]
    tint = item.get("tint", DIM)
    ICONS[kind](c, PAD, y + 1, 16, tint)
    text_x = PAD + 16 + 7
    put(c, text_x, y, item["head"], F_FLOOR(), INK, budget=WIDTH - PAD - text_x)
    y += CAP_FLOOR + 8
    f = F_FLOOR()
    right = item.get("right")
    right_w = measure(c.draw, right, f) if right else 0
    if right:
        put(c, WIDTH - PAD, y, right, f, item.get("right_tint", tint), align="right")
    if kind == "print":
        bar(c, PAD, y + 5, WIDTH - PAD - right_w - 8, 8, item["frac"], CLAY)
        return y + CAP_FLOOR
    if item.get("sub"):
        return put(c, PAD, y, item["sub"], f, DIM,
                   budget=INNER - (right_w + 8 if right else 0))
    return y + CAP_FLOOR


def title_pill(c, word, tint, y):
    return pill(c, word, tint, y, size=FLOOR_SIZE, pad=PILL_PAD_TIGHT)


# --------------------------------------------------------- reworked screens


def sessions_v2(d, aliases=None):
    """Two lines per session that matters, idle ones folded away."""
    aliases = aliases or {}
    c = Canvas()
    rows = d["rows"]
    pill(c, "{} LIVE".format(len(rows)), CLAY, 44)

    f = F_FLOOR()
    top, cap = cap_box(c.draw, f)
    dot_r = 6
    dot_cx = PAD + dot_r
    text_x = dot_cx + dot_r + 6
    y = 98
    shown = rows if len(rows) <= 4 else rows[:3]
    for row in shown:
        state = row["state"]
        dot = {"waiting": WARN, "working": GOOD, "done": SLEEP}[state]
        disc(c.vector, dot_cx * SS, (y + cap / 2) * SS, dot_r * SS, dot + (255,))
        name = project_label(c.draw, row["name"], aliases, f, WIDTH - PAD - text_x)
        write(c.draw, text_x, y - top, name, f, INK)
        dy = y + cap + 8
        left, right = row["detail"]
        lt, rt = {"waiting": (WARN, WARN), "working": (DIM, CLAY),
                  "done": (DIM, DIM)}[state]
        put(c, PAD, dy, left, f, lt)
        if right:
            put(c, WIDTH - PAD, dy, right, f, rt, align="right")
        y = dy + cap + 16
    extra = len(rows) - len(shown)
    tail = ("+{} more".format(extra) if extra else
            "+{} idle".format(d["idle"]) if d.get("idle") else None)
    if tail:
        put(c, PAD, y - 2, tail, f, DIM)
    strip(c, 352, d.get("strip"), d.get("clock"))
    return c.flatten()


def waiting_v2(d, aliases=None):
    c = Canvas()
    word, tint = S.STATE_STYLE["waiting"]
    y = header(c, "waiting", word, tint)
    y = elapsed_row(c, y, d["stuck"], WARN) + GAP_SECTION
    y = put(c, PAD, y, d["tool"], F_CORE(), INK) + GAP_SECTION
    y = put(c, PAD, y, d["project"], F_FLOOR(), DIM)
    bottom = strip(c, y + GAP_FOOTER, d.get("strip"), d.get("clock"))
    ident_row(c, bottom + GAP_FOOTER, d.get("ident"))
    return c.flatten()


def between_v2(d, aliases=None):
    c = Canvas()
    word, tint = S.STATE_STYLE["done"]
    y = header(c, "idle", word, tint)
    y = put(c, PAD, y, d["duration"], F_CORE(), INK) + GAP_SECTION
    y = put(c, PAD, y, d["project"], F_FLOOR(), DIM) + GAP_SECTION
    rule(c, y)
    bottom = diff_row(c, y + 12, *d["diff"])
    bottom = strip(c, bottom + GAP_FOOTER, d.get("strip"), d.get("clock"))
    ident_row(c, bottom + GAP_FOOTER, d.get("ident"))
    return c.flatten()


def idle_v2(d, aliases=None):
    """Hero clock kept; the meters slim down to make room for the strip."""
    c = Canvas()
    cx = WIDTH / 2
    hero = font(S.IDLE_CLOCK_SIZE, 800)
    put(c, cx, 60, d["hh"], hero, INK, align="center")
    put(c, cx, 122, d["mm"], hero, INK, align="center")
    put(c, cx, 192, d["weekday"], font(STATE_SIZE, 700), DIM, align="center")
    put(c, cx, 218, d["date"], font(STATE_SIZE, 700), INK, align="center")
    y = 256
    for label, pct in d["meters"]:
        colour = ks.usage_color(pct)
        put(c, PAD, y, label, F_FLOOR(), DIM)
        put(c, WIDTH - PAD, y, "{:.0f}%".format(pct), F_FLOOR(), colour,
            align="right")
        bar(c, PAD, y + CAP_FLOOR + 7, WIDTH - PAD, 8, pct / 100.0, colour)
        y += CAP_FLOOR + 7 + 8 + 14
    strip(c, y + 2, d.get("strip"), d.get("clock"))
    return c.flatten()


def chevron(c, x, y_mid, h, tint):
    """The TODO marker, drawn: U+25B8 exists in neither Ubuntu nor Noto CJK,
    so on the Linux/Docker panel the typed one renders as a tofu box."""
    c.vector.polygon([(x * SS, (y_mid - h / 2) * SS),
                      ((x + h * 0.8) * SS, y_mid * SS),
                      (x * SS, (y_mid + h / 2) * SS)], fill=tint + (255,))


def working_v2(d, aliases=None):
    c = Canvas()
    word, tint = S.STATE_STYLE["working"]
    y = header(c, "working", word, tint, phase=d.get("phase", 0.0))
    y = elapsed_row(c, y, d["elapsed"], INK) + GAP_SECTION
    y = put(c, PAD, y, d["project"], F_FLOOR(), DIM) + GAP_SECTION
    f = F_FLOOR()
    put(c, PAD, y + 2, "TODO", f, DIM)
    y = put(c, WIDTH - PAD, y, d["todo"], font(STATE_SIZE, 700), CLAY, align="right")
    text_x = PAD + 18
    lines = wrap_words(c.draw, d["item"], f, WIDTH - PAD - text_x, INNER, 2)
    for i, line in enumerate(lines):
        y_ink = y + GAP_BAR
        if i == 0:
            chevron(c, PAD + 1, y_ink + CAP_FLOOR / 2, 13, CLAY)
        y = put(c, text_x if i == 0 else PAD, y_ink, line, f, INK,
                budget=WIDTH - PAD - (text_x if i == 0 else PAD))
    rule_y = y + GAP_FOOTER
    rule(c, rule_y)
    bottom = diff_row(c, rule_y + 12, *d["diff"])
    ident_row(c, bottom + GAP_FOOTER, d.get("ident"))
    return c.flatten()


# --------------------------------------------------------------- new screens


def ring(c, cx, cy, r, width, frac, colour):
    v = c.vector
    box = [(cx - r) * SS, (cy - r) * SS, (cx + r) * SS, (cy + r) * SS]
    v.ellipse(box, outline=FAINT + (255,), width=round(width * SS))
    if frac > 0:
        v.arc(box, -90, -90 + 360 * frac, fill=colour + (255,),
              width=round(width * SS))
        # round caps
        for ang in (-90, -90 + 360 * frac):
            a = math.radians(ang)
            mx = cx + (r - width / 2) * math.cos(a)
            my = cy + (r - width / 2) * math.sin(a)
            disc(v, mx * SS, my * SS, width / 2 * SS, colour + (255,))


def ring_header(c, frac, colour, centre, word, tint):
    cx, cy, r = WIDTH / 2, MASCOT_TOP + 50, 48
    ring(c, cx, cy, r, 10, frac, colour)
    put(c, cx, cy - 12, centre, F_CORE(), INK if colour != BAD else BAD,
        align="center")
    return title_pill(c, word, tint, cy + r + GAP_MASCOT_TIGHT) + GAP_SECTION


def print_progress(d, aliases=None):
    c = Canvas()
    y = ring_header(c, d["pct"] / 100.0, CLAY, "{}%".format(d["pct"]),
                    "PRINT", CLAY)
    f = F_CORE()
    w = measure(c.draw, d["left"], f)
    put(c, PAD, y, d["left"], f, INK)
    put(c, PAD + w + 8, y + 7, "left", F_FLOOR(), DIM)
    y += 25 + GAP_SECTION
    y = put(c, PAD, y, d["file"], F_FLOOR(), DIM) + GAP_SECTION
    disc(c.vector, (PAD + 7) * SS, (y + 9) * SS, 7 * SS, d["filament_rgb"] + (255,))
    y = put(c, PAD + 21, y, d["filament"], F_FLOOR(), INK,
            budget=WIDTH - PAD - (PAD + 21))
    rule(c, y + GAP_FOOTER)
    y = y + GAP_FOOTER + 12
    put(c, PAD, y, "ETA", F_FLOOR(), DIM)
    y = put(c, WIDTH - PAD, y, d["eta"], F_FLOOR(), INK, align="right")
    footer_clock(c, y + GAP_FOOTER, d["clock"])
    return c.flatten()


def print_failed(d, aliases=None):
    c = Canvas()
    y = ring_header(c, d["pct"] / 100.0, BAD, "{}%".format(d["pct"]),
                    "FAIL", BAD)
    y = elapsed_row(c, y, d["stuck"], BAD) + GAP_SECTION
    error = d["error"].rstrip("。.")   # HMS texts end in a full stop that wraps alone
    for line in wrap_words(c.draw, error, F_FLOOR(), INNER, INNER, 2):
        y = put(c, PAD, y, line, F_FLOOR(), INK) + GAP_BAR
    y += GAP_SECTION - GAP_BAR
    y = put(c, PAD, y, d["where"], F_FLOOR(), DIM)
    footer_clock(c, y + GAP_FOOTER, d["clock"])
    return c.flatten()


def badge_header(c, draw_icon, word, tint):
    """Icon in the mascot's slot, pill under it: same skeleton as WAITING, so a
    takeover reads as 'something needs you' before it is read at all."""
    cx, cy, r = WIDTH / 2, MASCOT_TOP + 50, 44
    draw_icon(c, cx, cy, r)
    return title_pill(c, word, tint, cy + r + GAP_MASCOT_TIGHT + 4) + GAP_SECTION


def glyph_hermes(c, cx, cy, r):
    v = c.vector
    disc(v, cx * SS, cy * SS, r * SS, WARN + (46,))
    v.ellipse([(cx - r) * SS, (cy - r) * SS, (cx + r) * SS, (cy + r) * SS],
              outline=WARN + (220,), width=3 * SS)
    put(c, cx, cy - 25, "H", font(64, 800), WARN, align="center")


def glyph_alert(c, cx, cy, r):
    v = c.vector
    pts = [(cx * SS, (cy - r) * SS), ((cx + r * 1.05) * SS, (cy + r * 0.8) * SS),
           ((cx - r * 1.05) * SS, (cy + r * 0.8) * SS)]
    v.polygon(pts, fill=BAD + (52,), outline=BAD + (230,))
    v.line(pts + [pts[0]], fill=BAD + (230,), width=3 * SS, joint="curve")
    v.rounded_rectangle([(cx - 4) * SS, (cy - r * 0.42) * SS, (cx + 4) * SS,
                         (cy + r * 0.28) * SS], radius=4 * SS, fill=BAD + (255,))
    disc(v, cx * SS, (cy + r * 0.53) * SS, 5 * SS, BAD + (255,))


def glyph_calendar(month, day, tint):
    def draw(c, cx, cy, r):
        v = c.vector
        x0, x1, y0, y1 = cx - r, cx + r, cy - r * 0.9, cy + r
        v.rounded_rectangle([x0 * SS, y0 * SS, x1 * SS, y1 * SS], radius=12 * SS,
                            fill=FAINT + (255,))
        v.rounded_rectangle([x0 * SS, y0 * SS, x1 * SS, (y0 + 26) * SS],
                            radius=12 * SS, fill=tint + (255,))
        v.rectangle([x0 * SS, (y0 + 14) * SS, x1 * SS, (y0 + 26) * SS],
                    fill=tint + (255,))
        put(c, cx, y0 + 6, month, font(18, 800), BG, align="center")
        put(c, cx, y0 + 37, day, font(46, 800), INK, align="center")
    return draw


def hermes_approval(d, aliases=None):
    c = Canvas()
    y = badge_header(c, glyph_hermes, "HERMES", WARN)
    y = elapsed_row(c, y, d["stuck"], WARN) + GAP_SECTION
    y = put(c, PAD, y, d["tool"], F_CORE(), INK) + GAP_SECTION
    for line in wrap_words(c.draw, d["command"], F_FLOOR(), INNER, INNER, 2):
        y = put(c, PAD, y, line, F_FLOOR(), DIM) + GAP_BAR
    footer_clock(c, y - GAP_BAR + GAP_FOOTER, d["clock"])
    return c.flatten()


def system_alert(d, aliases=None):
    c = Canvas()
    y = badge_header(c, glyph_alert, "ALERT", BAD)
    y = elapsed_row(c, y, d["since"], BAD) + GAP_SECTION
    y = put(c, PAD, y, d["what"], F_CORE(), INK) + GAP_BAR + 2
    y = put(c, PAD, y, d["state"], F_FLOOR(), BAD) + GAP_SECTION
    y = put(c, PAD, y, d["host"], F_FLOOR(), DIM)
    footer_clock(c, y + GAP_FOOTER, d["clock"])
    return c.flatten()


def due_soon(d, aliases=None):
    c = Canvas()
    y = badge_header(c, glyph_calendar(d["month"], d["day"], WARN), "DUE", WARN)
    f = F_CORE()
    w = measure(c.draw, d["left"], f)
    put(c, PAD, y, d["left"], f, WARN)
    put(c, PAD + w + 8, y + 7, "left", F_FLOOR(), DIM)
    y += 25 + GAP_SECTION
    y = put(c, PAD, y, d["title"], F_FLOOR(), INK) + GAP_BAR
    y = put(c, PAD, y, d["course"], F_FLOOR(), DIM) + GAP_SECTION
    y = put(c, PAD, y, d["when"], F_FLOOR(), DIM)
    footer_clock(c, y + GAP_FOOTER, d["clock"])
    return c.flatten()


def placeholder_cover(size):
    """Abstract stand-in for real cover art (homeboard serves the real one)."""
    img = Image.new("RGB", (size * 4, size * 4))
    px = ImageDraw.Draw(img)
    for i in range(size * 4):
        t = i / (size * 4)
        px.line([(0, i), (size * 4, i)],
                fill=(int(18 + 30 * t), int(46 + 70 * t), int(92 + 60 * t)))
    px.ellipse([size * 1.3, size * 0.5, size * 3.7, size * 2.9],
               fill=(214, 120, 74))
    px.rectangle([0, size * 2.9, size * 4, size * 4], fill=(16, 22, 40))
    return img.resize((size, size), Image.LANCZOS)


def now_playing(d, aliases=None):
    c = Canvas()
    art = 122
    cover = placeholder_cover(art)
    mask = Image.new("L", (art * SS, art * SS), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, art * SS - 1, art * SS - 1],
                                           radius=10 * SS, fill=255)
    c.base.paste(cover, (PAD, 46), mask.resize((art, art), Image.LANCZOS))
    y = 46 + art + 16
    for line in wrap_words(c.draw, d["title"], F_FLOOR(), INNER, INNER, 2):
        y = put(c, PAD, y, line, F_FLOOR(), INK) + GAP_BAR
    y = put(c, PAD, y - GAP_BAR + 10, d["artist"], F_FLOOR(), DIM) + GAP_SECTION
    bar(c, PAD, y, WIDTH - PAD, 6, d["frac"], INK)
    y += 6 + 10
    small = font(20, 700)
    put(c, PAD, y, d["pos"], small, DIM)
    y = put(c, WIDTH - PAD, y, d["rest"], small, DIM, align="right") + GAP_SECTION
    put(c, PAD, y, d["source"], F_FLOOR(), INK)
    y = put(c, WIDTH - PAD, y, d["volume"], F_FLOOR(), DIM, align="right")
    footer_clock(c, y + GAP_FOOTER, d["clock"])
    return c.flatten()


# ---------------------------------------------------------------------- data

CLOCK = "03:48"
PRINT_STRIP = {"kind": "print", "head": "1h12", "right": "63%", "frac": 0.63,
               "tint": CLAY, "right_tint": CLAY}
DUE_STRIP = {"kind": "due", "head": "Exam 2", "sub": "Nov 6", "right": "32d",
             "tint": DIM, "right_tint": INK}
MUSIC_STRIP = {"kind": "music", "head": "Blue in Green", "tint": CLAY,
               "sub": "Bill Evans"}
IDENT = {"tag": "a3f92c", "rgb": (255, 95, 0)}

LIVE_ENTRIES = [("waiting", "homeboard"), ("working", "display"),
                ("working", "memory"), ("engaged", ".dotfiles"),
                ("engaged", "memory"), ("engaged", "hermes"),
                ("engaged", "claude-status-bar")]

SHEET = [
    # (file, group, title, note, renderer, data)
    ("00_sessions_now", "before", "Sessions · 现在",
     "同一组会话的现状：7 行只有名字，一半是灰色闲置",
     S.sessions, {"entries": LIVE_ENTRIES, "clock": CLOCK}),
    ("00b_working_now", "before", "Working · 现在",
     "真机现状：TODO 前的 ▸ 两套字体都没有，显示成方块",
     S.claude_working, {"phase": 0.85, "elapsed": "4:32", "project": "display",
                        "todo": {"count": "3/7", "item": "render v2 mockups"},
                        "diff": ("+212", "−38"), "clock": CLOCK, "ident": IDENT}),
    ("01_sessions_v2", "reworked", "Sessions · 新",
     "两行一组：名字 / 在干什么。闲置折成一行，底栏换成打印进度",
     sessions_v2, {"rows": [
         {"state": "waiting", "name": "homeboard", "detail": ("Bash?", "0:41")},
         {"state": "working", "name": "display", "detail": ("4:32", "3/7")},
         {"state": "working", "name": "memory", "detail": ("0:21", "")},
         {"state": "done", "name": "dotfiles", "detail": ("done", "2m")},
     ], "idle": 3, "strip": PRINT_STRIP, "clock": CLOCK}),
    ("02_sessions_v2_busy", "reworked", "Sessions · 新（会话多）",
     "超过 4 个活跃会话时显示 3 个 + 计数；没有家里的事时底栏退回时钟",
     sessions_v2, {"rows": [
         {"state": "waiting", "name": "homeboard", "detail": ("Edit?", "1:05")},
         {"state": "waiting", "name": "hermes", "detail": ("Bash?", "0:12")},
         {"state": "working", "name": "display", "detail": ("4:32", "3/7")},
         {"state": "working", "name": "memory", "detail": ("0:21", "")},
         {"state": "working", "name": "dotfiles", "detail": ("7:02", "5/5")},
     ], "idle": 2, "strip": None, "clock": CLOCK}),
    ("03_working", "reworked", "Working · 几乎不变",
     "Claude 在干活时只显示它，不加底栏；唯一改动是修好 TODO 前的方块",
     working_v2, {"phase": 0.85, "elapsed": "4:32", "project": "display",
                  "todo": "3/7", "item": "render v2 mockups",
                  "diff": ("+212", "−38"), "ident": IDENT}),
    ("04_waiting_v2", "reworked", "Waiting · 新",
     "等你批准：时钟一行换成最近的截止日",
     waiting_v2, {"stuck": "0:41", "tool": "Bash?", "project": "homeboard",
                  "strip": DUE_STRIP, "clock": CLOCK,
                  "ident": {"tag": "7b14de", "rgb": (0, 215, 255)}}),
    ("05_between_v2", "reworked", "Done · 新",
     "回合结束：底栏换成正在播放",
     between_v2, {"duration": "2m 14s", "project": "display",
                  "diff": ("+212", "−38"), "strip": MUSIC_STRIP, "clock": CLOCK,
                  "ident": {"tag": "c081fa", "rgb": (255, 0, 255)}}),
    ("06_idle_v2", "reworked", "Idle · 新",
     "大时钟保留，用量条变细，腾出底栏",
     idle_v2, {"hh": "03", "mm": "48", "weekday": "MON", "date": "OCT 5",
               "meters": [("5H", 42), ("7D", 61)], "strip": DUE_STRIP,
               "clock": CLOCK}),
    ("07_now_playing", "new", "Now playing",
     "放音乐时的待机屏：封面（占位图）、曲名、音源和音量",
     now_playing, {"title": "Blue in Green", "artist": "Bill Evans",
                   "frac": 0.41, "pos": "2:14", "rest": "−3:13",
                   "source": "LSX II", "volume": "38", "clock": CLOCK}),
    ("08_print", "new", "Print",
     "打印时整屏：进度环、剩余时间、任务名、耗材颜色、预计完成",
     print_progress, {"pct": 63, "left": "1h12", "file": "hook_v2",
                      "filament": "PLA", "filament_rgb": (226, 226, 220),
                      "eta": "05:00", "clock": CLOCK}),
    ("09_print_failed", "new", "Print failed",
     "打印出错：HMS 中文报错原文、停了多久",
     print_failed, {"pct": 41, "stuck": "4:12", "error": "喷嘴堵头。",
                    "where": "layer 128", "clock": CLOCK}),
    ("10_hermes", "new", "Hermes approval",
     "Hermes 等你审批，和 Claude 的 YOU 屏同一套结构",
     hermes_approval, {"stuck": "3:10", "tool": "shell?",
                       "command": "rm -rf build/", "clock": CLOCK}),
    ("11_alert", "new", "System alert",
     "homeboard 健康检查变红（例：T7 掉盘）",
     system_alert, {"since": "12:40", "what": "T7 disk", "state": "dropped",
                    "host": "pi-nas", "clock": CLOCK}),
    ("12_due", "new", "Due soon",
     "截止前 48 小时接管：倒计时、标题、截止时间",
     due_soon, {"month": "OCT", "day": "5", "left": "18h", "title": "HW5", "course": "CS570",
                "when": "Mon 23:59", "clock": "05:59"}),
]


def main(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    frames = []
    for name, group, title, note, render, data in SHEET:
        img = render(data, {})
        assert img.size == (WIDTH, HEIGHT), (name, img.size)
        img.save(os.path.join(out_dir, name + ".png"))
        frames.append((name, img))
        print("wrote", name)
    # contact sheet, 2x, with the covered top 40 rows tinted so the dead zone
    # is visible in review
    scale, gap = 2, 24
    sheet = Image.new("RGB", (len(frames) * (WIDTH * scale + gap) + gap,
                              HEIGHT * scale + 2 * gap), (12, 11, 10))
    for i, (_, img) in enumerate(frames):
        big = img.resize((WIDTH * scale, HEIGHT * scale), Image.LANCZOS)
        sheet.paste(big, (gap + i * (WIDTH * scale + gap), gap))
    sheet.save(os.path.join(out_dir, "contact_sheet.png"))
    print("wrote contact_sheet")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO, "out", "v2"))

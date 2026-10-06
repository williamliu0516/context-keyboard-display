"""The household half of the panel: a print, the next deadline, what is
playing, and anything homeboard's health checks call bad.

homeboard (the iPad home panel's backend) already gathers all of it; this
module reads its `/api/keyboard` snapshot (the slice of `/api/state` this
panel uses) and turns it into the small view
models the strip and the home screens draw. Two layers, kept apart on purpose:

  views(state, now, cfg, ...)   pure: one snapshot in, the four views out.
                                Everything the tests exercise lives here.
  Home                          a background thread polling homeboard, plus
                                the cover art for the track that is playing.
                                The tick loop never waits on the network: it
                                reads whatever the last good poll left.

Off (no `home_url`), unreachable, or unauthorised, every view is None and the
panel looks exactly as it did before: clocks where the strip would be, no home
screens.
"""

import datetime as _dt
import io
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request

# ---------------------------------------------------------------- formatting


def fmt_left(seconds):
    """Time remaining, in the fewest characters: 3d, 18h, 45m."""
    seconds = max(0, int(seconds))
    if seconds >= 2 * 86400:
        return "{}d".format(seconds // 86400)
    if seconds >= 3600:
        return "{}h".format(seconds // 3600)
    return "{}m".format(max(1, seconds // 60))


def fmt_minutes(minutes):
    """Print time left: 1h12 past an hour, else 45m."""
    minutes = max(0, int(minutes))
    if minutes >= 60:
        return "{}h{:02d}".format(minutes // 60, minutes % 60)
    return "{}m".format(minutes)


def fmt_clock(epoch):
    return time.strftime("%H:%M", time.localtime(epoch))


def fmt_elapsed(seconds):
    seconds = max(0, int(seconds))
    minutes, secs = divmod(seconds, 60)
    if minutes >= 60:
        return "{}h{:02d}".format(minutes // 60, minutes % 60)
    return "{}:{:02d}".format(minutes, secs)


def hex_rgb(text):
    text = (text or "").lstrip("#")
    if len(text) < 6:
        return None
    try:
        return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


def _num(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


# ---------------------------------------------------------------------- print

PRINT_RUNNING = {"RUNNING", "PREPARE", "SLICING", "INIT"}


def print_view(state, now):
    """The X1C, from homeboard's bambu source. None when nothing is worth
    showing: idle, finished, cancelled, or a failure homeboard has already
    marked stale (acknowledged, or older than its two-hour window)."""
    b = state.get("bambu") if isinstance(state, dict) else None
    if not isinstance(b, dict) or not b.get("connected"):
        return None
    job = b.get("state") or ""
    pct = int(_num(b.get("progress")) or 0)
    file = re.sub(r"\.(3mf|gcode)$", "", b.get("file") or "", flags=re.I) or None
    if job == "FAILED":
        if b.get("cancelled") or b.get("done_stale"):
            return None
        hms = [h for h in (b.get("hms") or []) if isinstance(h, dict) and h.get("text")]
        error = (hms[0]["text"] if hms else b.get("error_text")) or "打印失败"
        done_at = _num(b.get("done_at"))
        return {"phase": "failed", "pct": pct, "file": file,
                "error": error.rstrip("。. "),
                "stuck_s": now - done_at if done_at else None,
                "key": "fail:{}".format(b.get("job") or done_at or file)}
    if not (b.get("active") or job in PRINT_RUNNING or job == "PAUSE"):
        return None
    tray = None
    active = b.get("active_tray")
    for unit in b.get("ams") or []:
        for t in (unit.get("trays") or []) if isinstance(unit, dict) else []:
            if isinstance(active, int) and unit.get("id", 0) * 4 + t.get("slot", -1) == active:
                tray = t
    remaining = _num(b.get("remaining_min"))
    eta = _num(b.get("eta"))
    return {"phase": "paused" if job == "PAUSE" else "printing", "pct": pct, "file": file,
            "left": fmt_minutes(remaining) if remaining else None,
            "eta": fmt_clock(eta) if eta else None,
            "filament": (tray.get("type") or None) if tray else None,
            "filament_rgb": hex_rgb(tray.get("color")) if tray else None}


# ------------------------------------------------------------------ deadlines

DUE_KINDS = ("hw", "exam", "project")


def _short_course(course):
    """CSCI-570 -> CS570."""
    m = re.search(r"(\d{3})", course or "")
    return "CS" + m.group(1) if m else (course or "")


def _strip_course(title):
    """"CS570 HW6" -> "HW6"; the course gets its own line."""
    return re.sub(r"^\s*CS(CI)?[- ]?\d{3}\s*", "", title or "").strip() or (title or "")


def _parse_local(text):
    try:
        return time.mktime(_dt.datetime.strptime(text[:16], "%Y-%m-%dT%H:%M").timetuple())
    except (TypeError, ValueError):
        return None


def due_view(state, now):
    """The next homework, exam or project deadline that is still open.

    Skipped: anything past, anything submitted, and homework that is only a
    projection (not posted yet) -- a countdown to a guessed date is noise."""
    course = state.get("course") if isinstance(state, dict) else None
    events = course.get("events") if isinstance(course, dict) else None
    best = None
    for ev in events or []:
        if not isinstance(ev, dict) or ev.get("kind") not in DUE_KINDS or ev.get("past"):
            continue
        status = ev.get("status") or ""
        if status == "submitted" or (ev.get("projected") and status == "not_posted"):
            continue
        due = _parse_local(ev.get("due"))
        if due is None or due <= now:
            continue
        if best is None or due < best[0]:
            best = (due, ev)
    if best is None:
        return None
    due, ev = best
    local = time.localtime(due)
    return {"title": _strip_course(ev.get("title")), "course": _short_course(ev.get("course")),
            "kind": ev.get("kind"), "due": due, "left_s": due - now,
            "left": fmt_left(due - now),
            "when": time.strftime("%a %H:%M", local),
            "month": time.strftime("%b", local).upper(), "day": str(local.tm_mday),
            "key": "due:{}:{}".format(ev.get("title"), ev.get("due"))}


# ---------------------------------------------------------------------- music


def _track(player, now, via, source):
    pos, at = _num(player.get("position_ms")), _num(player.get("position_at"))
    elapsed = pos / 1000 if pos is not None else None
    if elapsed is not None and player.get("state") == "playing" and at:
        elapsed += max(0.0, now - at)
    dur = _num(player.get("duration_ms"))
    return {"playing": player.get("state") == "playing", "title": player["title"],
            "artist": player.get("artist") or None, "via": via, "source": source,
            "elapsed": elapsed, "duration": dur / 1000 if dur else None}


def music_view(state, now):
    """What is audible now, mirroring homeboard's own pick: the KEF (its
    streamer, or the Mac when the speaker is on USB), else the K17. Only a
    track that is actually playing counts; paused music is not news."""
    if not isinstance(state, dict):
        return None
    kef, k17, mac = (state.get(k) or {} for k in ("kef", "k17", "mac"))
    found = None
    if kef.get("connected") and kef.get("on"):
        if kef.get("source") == "usb" and mac.get("connected") and (mac.get("now_playing") or {}).get("title"):
            n = mac["now_playing"]
            elapsed, ts = _num(n.get("elapsed")), _num(n.get("timestamp"))
            if elapsed is not None and n.get("playing") and ts:
                elapsed += max(0.0, now - ts)
            found = {"playing": bool(n.get("playing")), "title": n["title"],
                     "artist": n.get("artist") or None, "via": "mac", "source": "LSX II",
                     "elapsed": elapsed, "duration": _num(n.get("duration")),
                     "art_key": ("mac", n.get("artwork_id")) if n.get("has_artwork") else None}
        else:
            p = kef.get("player") or {}
            if p.get("title") and kef.get("source") == "wifi":
                found = _track(p, now, "kef", "LSX II")
                found["art_key"] = ("kef", p["art"]) if p.get("art") else None
        if found:
            found["volume"] = None if kef.get("mute") else kef.get("volume")
    if not (found and found["playing"]):
        p = k17.get("player") or {}
        if k17.get("connected") and p.get("title") and p.get("state") == "playing":
            found = _track(p, now, "k17", "K17")
            found["art_key"] = ("k17", p["art"]) if p.get("art") else None
            found["volume"] = k17.get("volume")
    if not (found and found["playing"]):
        return None
    dur, el = found.get("duration"), found.get("elapsed")
    if dur and el is not None:
        found["frac"] = max(0.0, min(1.0, el / dur))
        found["pos"] = fmt_elapsed(el)
        found["rest"] = "−" + fmt_elapsed(max(0.0, dur - el))
    found["key"] = "music:{}:{}".format(found["title"], found.get("artist"))
    return found


# --------------------------------------------------------------------- health


def alert_view(state, first_seen=None, now=None):
    """The first health check homeboard calls bad. Warnings stay off the panel:
    "unpushed commits" is a fact, not an interruption."""
    health = state.get("health") if isinstance(state, dict) else None
    if not isinstance(health, dict):
        return None
    for a in health.get("alerts") or []:
        if isinstance(a, dict) and not a.get("ok") and a.get("level") == "bad":
            key = "alert:{}".format(a.get("id") or a.get("label"))
            since = (first_seen or {}).get(key)
            return {"what": a.get("label") or a.get("id") or "--",
                    "detail": a.get("detail") or None,
                    "since_s": (now - since) if since and now else None,
                    "key": key}
    return None


# ---------------------------------------------------------------- composition


def views(state, now, first_seen=None):
    """All four views from one snapshot. Missing state means all None."""
    if not isinstance(state, dict):
        return {"print": None, "due": None, "music": None, "alert": None}
    return {"print": print_view(state, now), "due": due_view(state, now),
            "music": music_view(state, now),
            "alert": alert_view(state, first_seen, now)}


def strip_item(v, cfg):
    """The one household thing the clock row should show, or None for the clock.

    alert > failed print > running print > a deadline within due_strip_days >
    music. One item, never a rotation: a strip that changes on its own is the
    flicker this design exists to remove."""
    from keyboard_status import BAD, CLAY, DIM, INK, WARN
    if not v:
        return None
    if v.get("alert"):
        a = v["alert"]
        return {"kind": "alert", "head": a["what"], "sub": a.get("detail"),
                "tint": BAD, "sub_tint": BAD}
    p = v.get("print")
    if p and p["phase"] == "failed":
        return {"kind": "print", "head": p["error"], "sub": "FAIL", "sub_tint": BAD,
                "right": "{}%".format(p["pct"]), "tint": BAD, "right_tint": DIM}
    if p:
        paused = p["phase"] == "paused"
        tint = WARN if paused else CLAY
        return {"kind": "print", "head": "PAUSE" if paused else (p.get("left") or "PRINT"),
                "head_tint": WARN if paused else INK, "right": "{}%".format(p["pct"]),
                "frac": p["pct"] / 100.0, "tint": tint, "right_tint": tint}
    d = v.get("due")
    if d and d["left_s"] <= cfg["due_strip_days"] * 86400:
        tint = BAD if d["left_s"] <= 6 * 3600 else WARN if d["left_s"] <= 86400 else DIM
        return {"kind": "due", "head": d["title"], "sub": d["course"],
                "sub_alt": re.sub(r"\D", "", d["course"] or "") or None, "right": d["left"],
                "tint": tint, "right_tint": INK if tint == DIM else tint}
    m = v.get("music")
    if m:
        return {"kind": "music", "head": m["title"], "sub": m.get("artist"), "tint": CLAY}
    return None


def takeover_keys(v, cfg):
    """Every announceable event in this snapshot, as (key, kind, view).

    The engine announces a key the first time it appears (a full screen for
    takeover_seconds) and only then: the strip carries it afterwards. A
    deadline produces one key per threshold it has crossed (48 h, 24 h, ...),
    so it is announced again as it gets closer."""
    out = []
    if not v:
        return out
    if v.get("alert"):
        out.append((v["alert"]["key"], "alert", v["alert"]))
    p = v.get("print")
    if p and p["phase"] == "failed":
        out.append((p["key"], "print_failed", p))
    d = v.get("due")
    if d:
        for hours in cfg["due_takeover_hours"]:
            if d["left_s"] <= float(hours) * 3600:
                out.append(("{}:{}h".format(d["key"], hours), "due_soon", d))
    return out


# ---------------------------------------------------------------------- poller


class Home:
    """Polls homeboard in a thread; the daemon asks it for views.

    `log` is the daemon's logger. Failures are logged on the first miss and on
    recovery, never once per poll."""

    def __init__(self, cfg, log=None):
        self.url = (cfg.get("home_url") or "").rstrip("/")
        self.poll = max(2.0, float(cfg.get("home_poll_seconds") or 10.0))
        self.token_file = os.path.expanduser(cfg.get("home_token_file") or "")
        self.timeout = float(cfg.get("http_timeout_seconds") or 4.0)
        self.log = log or (lambda message: None)
        self.lock = threading.Lock()
        self.state = None
        self.fetched_at = 0.0
        self.first_seen = {}
        self.art_key = None
        self.art = None
        self.failing = False

    @property
    def enabled(self):
        return bool(self.url)

    def start(self):
        if self.enabled:
            threading.Thread(target=self._run, name="home", daemon=True).start()
        return self

    def _token(self):
        if not self.token_file:
            return None
        try:
            with open(self.token_file) as handle:
                return handle.read().strip() or None
        except OSError:
            return None

    def _get(self, path):
        request = urllib.request.Request(self.url + path)
        token = self._token()
        if token:
            request.add_header("Authorization", "Bearer " + token)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return response.read()

    def _art_path(self, key):
        kind, ref = key
        if kind == "kef":
            return "/api/music/kefart?u=" + urllib.parse.quote(ref, safe="")
        if kind == "k17":
            # homeboard publishes the K17 cover as its own proxy path already
            if str(ref).startswith("/api/music/k17art/"):
                return str(ref)
            return "/api/music/k17art/" + urllib.parse.quote(str(ref), safe="")
        return "/api/music/artwork"

    def _fetch_art(self, key):
        from PIL import Image
        try:
            image = Image.open(io.BytesIO(self._get(self._art_path(key))))
            image.load()
            return image.convert("RGB")
        except Exception:  # noqa: BLE001 - no cover is a placeholder, not an error
            return None

    def poll_once(self):
        """One fetch of /api/keyboard (and of the cover, when the track changed).
        True on success. The thread calls this; so do --live and --status."""
        try:
            data = json.loads(self._get("/api/keyboard"))
            state = data.get("state") if isinstance(data, dict) else None
            if not isinstance(state, dict):
                raise ValueError("no state in the reply")
            now = time.time()
            music = music_view(state, now)
            key = tuple(music["art_key"]) if music and music.get("art_key") else None
            art = self.art if key == self.art_key else (self._fetch_art(key) if key else None)
            with self.lock:
                self.state, self.fetched_at = state, now
                self.art_key, self.art = key, art
            if self.failing:
                self.log("homeboard reachable again")
                self.failing = False
            return True
        except Exception as error:  # noqa: BLE001 - the panel must outlive homeboard
            if not self.failing:
                self.log("homeboard poll failed (%s: %s); strip and home screens off "
                         "until it answers" % (type(error).__name__, error))
                self.failing = True
            return False

    def _run(self):
        while True:
            self.poll_once()
            time.sleep(self.poll)

    def fresh(self, now):
        """A snapshot no older than three missed polls."""
        with self.lock:
            return self.state is not None and now - self.fetched_at <= 3 * self.poll + 30

    def views(self, now):
        """The four views from the last good poll, or all None once it is stale:
        a frozen "1h12 left" is worse than no strip."""
        if not self.fresh(now):
            return views(None, now)
        with self.lock:
            state, art = self.state, self.art
        # first-seen times for alerts, so the alert screen can say how long
        alert = alert_view(state)
        if alert and alert["key"] not in self.first_seen:
            self.first_seen[alert["key"]] = now
        out = views(state, now, self.first_seen)
        if out["music"] is not None:
            out["music"]["art"] = art
        return out


# ------------------------------------------------------------- screen models


def screen_data(kind, view, now):
    """The renderer's view model for a home screen, from one of the views."""
    clock = fmt_clock(now)
    if kind == "now_playing":
        volume = view.get("volume")
        return {"title": view["title"], "artist": view.get("artist"), "art": view.get("art"),
                "frac": view.get("frac"), "pos": view.get("pos"), "rest": view.get("rest"),
                "source": view.get("source"),
                "volume": str(int(volume)) if isinstance(volume, (int, float)) else None,
                "clock": clock}
    if kind == "print":
        return {"pct": view["pct"], "left": view.get("left"), "file": view.get("file"),
                "filament": view.get("filament"), "filament_rgb": view.get("filament_rgb"),
                "eta": view.get("eta"), "paused": view["phase"] == "paused", "clock": clock}
    if kind == "print_failed":
        stuck = view.get("stuck_s")
        return {"pct": view["pct"], "stuck": fmt_elapsed(stuck) if stuck is not None else None,
                "error": view["error"], "file": view.get("file"), "clock": clock}
    if kind == "alert":
        since = view.get("since_s")
        return {"since": fmt_elapsed(since) if since is not None else None,
                "what": view["what"], "detail": view.get("detail"), "clock": clock}
    if kind == "due_soon":
        return {"month": view["month"], "day": view["day"], "left": view["left"],
                "title": view["title"], "course": view.get("course"), "when": view["when"],
                "urgent": view["left_s"] <= 6 * 3600, "clock": clock}
    raise KeyError(kind)

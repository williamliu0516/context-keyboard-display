"""Live data: session enumeration, per-session state, and view-model builders.

All Claude facts come through the keyboard_status library (hooks state file +
incremental transcript readers) — the transcript parsing lives there and only
there. What this module adds is the multi-session view that library's own
collect() discards: one SessionInfo per live session, plus the formatting
that turns raw facts into the strings the screens draw.
"""

import os
import re
import subprocess
import time

import keyboard_status as ks


class SessionInfo:
    """One live session, as the engine and the screens see it."""

    def __init__(self, sid, state, last_activity, cwd, project, hook, facts):
        self.sid = sid
        self.state = state          # working | waiting | idle
        self.last_activity = last_activity
        self.cwd = cwd
        self.project = project
        self.hook = hook            # the state-file entry (may be {})
        self.facts = facts          # Transcript reader (may be None)


def collect_sessions(now, cfg):
    """Every live session, newest first — the enumeration collect() throws away.

    A session is known from its hook entry (state file), its transcript, or
    both; each is scored by whichever of its two clocks ran last, and
    anything quieter than session_ttl_seconds is dropped.
    """
    store = ks.read_json(ks.STATE_PATH).get("sessions")
    hooks = store if isinstance(store, dict) else {}
    transcripts = {sid: (mtime, path) for mtime, path, sid in ks.newest_transcripts(limit=12)}

    out = []
    for sid in set(hooks) | set(transcripts):
        hook = hooks.get(sid)
        hook = hook if isinstance(hook, dict) else {}
        tr = transcripts.get(sid)
        hook_at = hook.get("at", 0) if isinstance(hook.get("at"), (int, float)) else 0
        activity = max(hook_at, tr[0] if tr else 0)
        if not activity or now - activity > cfg["session_ttl_seconds"]:
            continue

        path = tr[1] if tr else hook.get("transcript")
        facts = None
        if isinstance(path, str) and os.path.isfile(path):
            facts = ks.transcript_facts(path)

        state = ks.resolve_state(hook, tr, now, cfg)
        cwd = hook.get("cwd") or (facts.cwd if facts else None)
        project = None
        if isinstance(cwd, str) and cwd.strip("/"):
            project = os.path.basename(cwd.rstrip("/"))
        out.append(SessionInfo(sid, state, activity, cwd, project, hook, facts))

    out.sort(key=lambda s: s.last_activity, reverse=True)
    return out


def engaged_sessions(sessions, now, cfg):
    """Sessions that currently own a share of the display: anything working
    or waiting, plus anything active within the engagement window. This is
    what keeps the panel from dropping to Idle between turns."""
    return [s for s in sessions
            if s.state in ("working", "waiting")
            or now - s.last_activity <= cfg["engaged_seconds"]]


# --------------------------------------------------------------- diff stat

_DIFF_CACHE = {}


def diff_stat(cwd, now, poll_seconds=5.0):
    """(insertions, deletions) for the dirty worktree at cwd, or None outside
    a repository. Cached per cwd, 0.5 s subprocess timeout — a slow or absent
    git must never stall a frame. Measures the whole worktree against HEAD
    (staged + unstaged), which is honest about "how big has this change
    gotten", not just Claude's own edits."""
    if not cwd or not isinstance(cwd, str):
        return None
    hit = _DIFF_CACHE.get(cwd)
    if hit and now - hit[0] < poll_seconds:
        return hit[1]
    result = None
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, "diff", "HEAD", "--shortstat"],
            capture_output=True, text=True, timeout=0.5)
        if proc.returncode == 0:
            adds = re.search(r"(\d+) insertion", proc.stdout)
            dels = re.search(r"(\d+) deletion", proc.stdout)
            result = (int(adds.group(1)) if adds else 0,
                      int(dels.group(1)) if dels else 0)
    except (OSError, subprocess.SubprocessError):
        pass
    if len(_DIFF_CACHE) > 32:
        _DIFF_CACHE.clear()
    _DIFF_CACHE[cwd] = (now, result)
    return result


# --------------------------------------------------------------- formatting


def fmt_count(n):
    """212 → "212"; 4+ digits render as "9.9k" — FLOOR budget is 8 chars for
    the whole split-aligned diff pair, so fields shrink rather than the type."""
    if n < 1000:
        return str(n)
    return "{:.1f}k".format(min(n, 9949) / 1000.0)


def fmt_diff(pair):
    if pair is None:
        return None
    adds, dels = pair
    return ("+" + fmt_count(adds), "−" + fmt_count(dels))


def fmt_elapsed(seconds):
    """Running-timer format for the stopwatch rows: m:ss, or "1h04" past an
    hour (the row budget holds 5 characters next to the glyph)."""
    seconds = max(0, int(seconds))
    minutes, secs = divmod(seconds, 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return "{}h{:02d}".format(hours, minutes)
    return "{}:{:02d}".format(minutes, secs)


def fmt_duration(seconds):
    """Finished-span format ("2m 14s"): units, no glyph — FIXES.md's third
    time form, distinct from running timers and the wall clock."""
    seconds = max(0, int(seconds))
    minutes, secs = divmod(seconds, 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return "{}h {}m".format(hours, minutes)
    if minutes:
        return "{}m {:02d}s".format(minutes, secs)
    return "{}s".format(secs)


def clock_text(now):
    return time.strftime("%H:%M", time.localtime(now))


# ------------------------------------------------------------- view models


def todo_view(facts):
    """{"count": "3/7", "item": "wire up previews"} from the latest TodoWrite,
    or None when the session has no plan. The count is the 1-based position
    of the item being worked; when everything is done it reads n/n and the
    item line is dropped (the counter-only layout takes over)."""
    todos = facts.todos if facts else None
    if not todos:
        return None
    total = len(todos)
    current = None
    for todo in todos:
        if todo.get("status") == "in_progress":
            current = todo
            break
    if current is None:
        for todo in todos:
            if todo.get("status") == "pending":
                current = todo
                break
    completed = sum(1 for t in todos if t.get("status") == "completed")
    position = min(completed + 1, total) if current is not None else total
    item = None
    if current is not None:
        item = current.get("content") or current.get("activeForm")
        item = item.strip() if isinstance(item, str) else None
    return {"count": "{}/{}".format(position, total), "item": item}


def turn_started(session):
    """When the turn on screen began — the hook's own timestamp, else the
    state-file write, else the transcript's last activity. Shared with the
    daemon, which needs the same number to decide how fast to tick."""
    hook = session.hook
    started = hook.get("turn_started_at")
    if not isinstance(started, (int, float)):
        started = hook.get("at") if isinstance(hook.get("at"), (int, float)) else session.last_activity
    return started


def working_view(session, now, cfg, phase):
    return {
        "phase": phase,
        "elapsed": fmt_elapsed(now - turn_started(session)),
        "project": session.project,
        "todo": todo_view(session.facts),
        "diff": fmt_diff(diff_stat(session.cwd, now, cfg["diff_poll_seconds"])),
        "clock": clock_text(now),
    }


_TOOL_PATTERN = re.compile(r"permission to use (\S+)")


def waiting_view(session, now, cfg):
    hook = session.hook
    message = hook.get("message") if isinstance(hook.get("message"), str) else ""
    match = _TOOL_PATTERN.search(message)
    tool = match.group(1).strip(".,:;") if match else "Tool"
    since = hook.get("at") if isinstance(hook.get("at"), (int, float)) else session.last_activity
    return {
        "tool": tool + "?",
        "stuck": fmt_elapsed(now - since),
        "project": session.project,
        "clock": clock_text(now),
    }


def between_view(session, now, cfg):
    last_turn = session.hook.get("last_turn_seconds")
    return {
        "duration": fmt_duration(last_turn) if isinstance(last_turn, (int, float)) else None,
        "project": session.project,
        "diff": fmt_diff(diff_stat(session.cwd, now, cfg["diff_poll_seconds"])),
        "clock": clock_text(now),
    }


_SESSION_ORDER = {"waiting": 0, "working": 1}


def ordered_sessions(engaged):
    """The switchboard's row order: waiting first, then working, then merely
    engaged, each group by recency (engaged_sessions already delivers recency
    order, and sorted() is stable, so the groups keep it).

    Shared with the drill-down key so that "the next session" means the next
    row down the list on the panel, rather than the next entry in some other
    ordering the user cannot see.
    """
    return sorted(engaged, key=lambda s: _SESSION_ORDER.get(s.state, 2))


def sessions_view(engaged, now):
    """Rows sort waiting-first, then working, then engaged-idle, each group
    by recency (engaged_sessions already delivers recency order)."""
    entries = ordered_sessions(engaged)
    return {
        "entries": [(s.state if s.state in ("waiting", "working") else "engaged",
                     s.project or "--") for s in entries],
        "clock": clock_text(now),
    }


def idle_view(now, cfg, online, allow_poll=True):
    usage = ks.current_usage(now, cfg, allow_poll=allow_poll)
    meters = []
    for label, name in (("5H", "five_hour"), ("7D", "seven_day")):
        data = usage.get(name)
        meters.append((label, data["used_percentage"] if data else None))
    local = time.localtime(now)
    return {
        "hh": time.strftime("%H", local),
        "mm": time.strftime("%M", local),
        "weekday": time.strftime("%a", local).upper(),
        "date": "{} {}".format(time.strftime("%b", local).upper(), local.tm_mday),
        "meters": meters,
        "online": online,
    }

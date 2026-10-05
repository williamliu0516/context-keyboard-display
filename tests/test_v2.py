#!/usr/bin/env python3
"""The v2 screens: home views, the strip, takeovers, the ladder and dwell.

    .venv/bin/python3 tests/test_v2.py

Needs Pillow and keyboard_status (the venv has both), no panel and no
homeboard: every homeboard snapshot here is synthetic, and the engine runs
against a temporary control file so a test can never move the real panel.
"""

import os
import sys
import tempfile
import time
import unittest

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_DIR)

import display  # noqa: E402

CFG = dict(display.DEFAULTS)
ks, collect, screens = display.import_stack(CFG)
import home  # noqa: E402

NOW = time.mktime((2026, 10, 5, 12, 0, 0, 0, 0, -1))


def local(days=0, hours=0, minutes=0):
    """An ISO local time the way homeboard writes course dues."""
    return time.strftime("%Y-%m-%dT%H:%M",
                         time.localtime(NOW + days * 86400 + hours * 3600 + minutes * 60))


def state(**parts):
    base = {"bambu": {"connected": True, "state": "FINISH", "active": False,
                      "done_stale": True},
            "course": {"events": []},
            "health": {"level": "ok", "alerts": []},
            "kef": {"connected": True, "on": False},
            "k17": {"connected": False},
            "mac": {"connected": True, "now_playing": None}}
    base.update(parts)
    return base


PRINTING = {"connected": True, "active": True, "state": "RUNNING", "progress": 63,
            "remaining_min": 72, "eta": NOW + 72 * 60, "file": "hook_v2.3mf",
            "active_tray": 1,
            "ams": [{"id": 0, "trays": [{"slot": 0, "color": "#FFFFFF", "type": "PLA"},
                                        {"slot": 1, "color": "#E4BDD0", "type": "PETG"}]}]}
FAILED = {"connected": True, "active": False, "state": "FAILED", "progress": 41,
          "done_at": NOW - 252, "done_stale": False, "cancelled": False,
          "file": "hook_v2", "job": "j1",
          "hms": [{"code": "0300_4006", "text": "喷嘴堵头。"}]}
PLAYING = {"connected": True, "on": True, "source": "wifi", "volume": 34, "mute": False,
           "player": {"state": "playing", "title": "听夜雨", "artist": "礼越",
                      "art": "http://192.168.50.68/file/x", "duration_ms": 200000,
                      "position_ms": 100000, "position_at": NOW}}
BAD = {"level": "bad", "alerts": [
    {"id": "git_ahead", "label": "未推送的提交", "ok": False, "level": "warn", "detail": "+1"},
    {"id": "t7", "label": "T7 SSD 未挂载", "ok": False, "level": "bad", "detail": "/mnt/t7"}]}


def hw(days, hours=0, **extra):
    event = {"course": "CSCI-570", "kind": "hw", "title": "CS570 HW6", "status": "",
             "projected": False, "due": local(days, hours), "past": False}
    event.update(extra)
    return event


class Views(unittest.TestCase):

    def test_print_running(self):
        v = home.print_view(state(bambu=PRINTING), NOW)
        self.assertEqual(v["phase"], "printing")
        self.assertEqual((v["pct"], v["left"], v["file"]), (63, "1h12", "hook_v2"))
        self.assertEqual((v["filament"], v["filament_rgb"]), ("PETG", (0xE4, 0xBD, 0xD0)))

    def test_print_failed_and_stale(self):
        v = home.print_view(state(bambu=FAILED), NOW)
        self.assertEqual((v["phase"], v["error"], v["pct"]), ("failed", "喷嘴堵头", 41))
        self.assertAlmostEqual(v["stuck_s"], 252)
        self.assertIsNone(home.print_view(state(bambu=dict(FAILED, done_stale=True)), NOW))
        self.assertIsNone(home.print_view(state(bambu=dict(FAILED, cancelled=True)), NOW))
        self.assertIsNone(home.print_view(state(), NOW))

    def test_due_skips_closed_and_projected(self):
        events = [hw(1, title="CS570 HW5", status="submitted"),
                  hw(2, title="CS570 HW6", projected=True, status="not_posted"),
                  hw(-1, title="CS570 HW4"),
                  hw(4, title="CS570 HW7")]
        v = home.due_view(state(course={"events": events}), NOW)
        self.assertEqual((v["title"], v["course"], v["left"]), ("HW7", "CS570", "4d"))

    def test_due_formats(self):
        self.assertEqual(home.fmt_left(18 * 3600 + 59), "18h")
        self.assertEqual(home.fmt_left(45 * 60), "45m")
        self.assertEqual(home.fmt_left(3 * 86400), "3d")

    def test_music_playing_only(self):
        v = home.music_view(state(kef=PLAYING), NOW + 20)
        self.assertEqual((v["title"], v["source"], v["volume"]), ("听夜雨", "LSX II", 34))
        self.assertAlmostEqual(v["frac"], 0.6)
        self.assertEqual(v["art_key"], ("kef", "http://192.168.50.68/file/x"))
        paused = dict(PLAYING, player=dict(PLAYING["player"], state="paused"))
        self.assertIsNone(home.music_view(state(kef=paused), NOW))

    def test_alert_ignores_warnings(self):
        self.assertIsNone(home.alert_view(state(health={"alerts": BAD["alerts"][:1]})))
        v = home.alert_view(state(health=BAD), {"alert:t7": NOW - 60}, NOW)
        self.assertEqual((v["what"], v["since_s"]), ("T7 SSD 未挂载", 60))


class Strip(unittest.TestCase):

    def item(self, **parts):
        return home.strip_item(home.views(state(**parts), NOW), CFG)

    def test_priority(self):
        everything = dict(bambu=PRINTING, kef=PLAYING, health=BAD,
                          course={"events": [hw(1)]})
        self.assertEqual(self.item(**everything)["kind"], "alert")
        everything.pop("health")
        self.assertEqual(self.item(**everything)["kind"], "print")
        everything.pop("bambu")
        self.assertEqual(self.item(**everything)["kind"], "due")
        everything.pop("course")
        self.assertEqual(self.item(**everything)["kind"], "music")
        self.assertIsNone(self.item())

    def test_due_only_inside_window(self):
        self.assertIsNone(self.item(course={"events": [hw(5)]}))
        self.assertEqual(self.item(course={"events": [hw(2)]})["right"], "2d")

    def test_failed_print_beats_running(self):
        self.assertEqual(self.item(bambu=FAILED)["sub"], "FAIL")


class FakeHome:
    def __init__(self, st):
        self.state = st

    def fresh(self, now):
        return self.state is not None

    def views(self, now):
        return home.views(self.state, now)


def session(sid, state_, ago=0.0, project=None):
    return collect.SessionInfo(sid, state_, NOW - ago, None, project or sid, {}, None)


class EngineCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = display.CONTROL_PATH
        display.CONTROL_PATH = os.path.join(self.tmp.name, "control.json")

    def tearDown(self):
        display.CONTROL_PATH = self.saved
        self.tmp.cleanup()

    def kind(self, engine, sessions, at=NOW):
        return engine.choose(sessions, at, CFG)[0]


class Ladder(EngineCase):

    def test_lone_working_gets_detail(self):
        e = display.Engine(collect)
        self.assertEqual(self.kind(e, [session("a", "working"), session("b", "idle", 900)]),
                         "claude_working")

    def test_working_plus_fresh_done_is_switchboard(self):
        e = display.Engine(collect)
        self.assertEqual(self.kind(e, [session("a", "working"), session("b", "idle", 30)]),
                         "sessions")

    def test_stale_engaged_falls_to_idle_family(self):
        e = display.Engine(collect, FakeHome(state(kef=PLAYING)))
        quiet = [session("a", "idle", 600), session("b", "idle", 900)]
        self.assertEqual(self.kind(e, quiet), "now_playing")
        e = display.Engine(collect, FakeHome(state(bambu=PRINTING, kef=PLAYING)))
        self.assertEqual(self.kind(e, quiet), "print")
        self.assertEqual(self.kind(display.Engine(collect), quiet), "idle")

    def test_waiting_beats_everything(self):
        e = display.Engine(collect, FakeHome(state(health=BAD)))
        self.assertEqual(self.kind(e, [session("a", "waiting"), session("b", "working")]),
                         "claude_waiting")


class Dwell(EngineCase):

    def test_true_screens_do_not_trade_places_quickly(self):
        e = display.Engine(collect)
        a, b = session("a", "working"), session("b", "idle", 30)
        self.assertEqual(self.kind(e, [a]), "claude_working")
        # b finishing makes the switchboard the better screen, but A's detail
        # is still true: it stays until dwell_seconds have passed.
        self.assertEqual(self.kind(e, [a, b], NOW + 5), "claude_working")
        self.assertEqual(self.kind(e, [a, b], NOW + 16), "sessions")

    def test_untrue_screen_goes_at_once(self):
        e = display.Engine(collect)
        self.assertEqual(self.kind(e, [session("a", "working")]), "claude_working")
        self.assertEqual(self.kind(e, [session("a", "idle", 0)], NOW + 2),
                         "claude_between_turns")

    def test_urgent_goes_at_once(self):
        e = display.Engine(collect)
        self.assertEqual(self.kind(e, [session("a", "working")]), "claude_working")
        self.assertEqual(self.kind(e, [session("a", "waiting")], NOW + 1), "claude_waiting")


class Takeovers(EngineCase):

    def test_boot_state_is_not_announced(self):
        e = display.Engine(collect, FakeHome(state(health=BAD)))
        self.assertEqual(self.kind(e, []), "idle")

    def test_new_alert_takes_over_then_strip(self):
        fake = FakeHome(state())
        e = display.Engine(collect, fake)
        self.assertEqual(self.kind(e, []), "idle")
        fake.state = state(health=BAD)
        self.assertEqual(self.kind(e, [], NOW + 10), "alert")
        self.assertEqual(self.kind(e, [], NOW + 69), "alert")
        self.assertEqual(self.kind(e, [], NOW + 71), "idle")
        self.assertEqual(home.strip_item(e.views, CFG)["kind"], "alert")

    def test_resolved_alert_ends_takeover(self):
        fake = FakeHome(state())
        e = display.Engine(collect, fake)
        self.kind(e, [])
        fake.state = state(health=BAD)
        self.assertEqual(self.kind(e, [], NOW + 10), "alert")
        fake.state = state()
        self.assertEqual(self.kind(e, [], NOW + 20), "idle")

    def test_deadline_announced_at_each_threshold(self):
        fake = FakeHome(state(course={"events": [hw(2, 1)]}))       # 49 h out
        e = display.Engine(collect, fake)
        self.kind(e, [])
        self.assertEqual(self.kind(e, [], NOW + 2 * 3600), "due_soon")   # crossed 48 h
        self.assertEqual(self.kind(e, [], NOW + 2 * 3600 + 120), "idle")
        self.assertEqual(self.kind(e, [], NOW + 26 * 3600), "due_soon")  # crossed 24 h

    def test_stale_home_does_not_reannounce(self):
        fake = FakeHome(state(health=BAD))
        e = display.Engine(collect, fake)
        self.kind(e, [])
        fake.state = None                       # homeboard down
        self.kind(e, [], NOW + 30)
        fake.state = state(health=BAD)          # back, same alert
        self.assertEqual(self.kind(e, [], NOW + 60), "idle")


class Rows(unittest.TestCase):

    def test_rows_and_fold(self):
        engaged = [session("w", "waiting", 41, "homeboard"),
                   session("k", "working", 0, "display"),
                   session("d", "idle", 120, "dotfiles"),
                   session("o", "idle", 900, "memory")]
        engaged[0].hook = {"message": "Claude needs your permission to use Bash", "at": NOW - 41}
        engaged[1].hook = {"turn_started_at": NOW - 272}
        view = collect.sessions_view(engaged, NOW, CFG)
        self.assertEqual([r["state"] for r in view["rows"]], ["waiting", "working", "done"])
        self.assertEqual(view["rows"][0]["detail"], ("Bash?", "0:41"))
        self.assertEqual(view["rows"][1]["detail"], ("4:32", ""))
        self.assertEqual(view["rows"][2]["detail"], ("done", "2m"))
        self.assertEqual(view["idle"], 1)


class Render(EngineCase):

    def test_every_mock_renders(self):
        for name, _ in screens.MOCKS:
            self.assertEqual(screens.render_mock(name).size, (142, 428), name)

    def test_every_kind_renders_from_live_views(self):
        st = state(bambu=PRINTING, kef=PLAYING, health=BAD, course={"events": [hw(0, 18)]})
        v = home.views(st, NOW, {"alert:t7": NOW - 60})
        failed = home.views(state(bambu=FAILED), NOW)["print"]
        engaged = [session("a", "working"), session("b", "idle", 20)]
        cases = [("sessions", engaged), ("claude_working", engaged[0]),
                 ("claude_between_turns", engaged[1]), ("idle", None),
                 ("now_playing", v["music"]), ("print", v["print"]),
                 ("print_failed", failed), ("alert", v["alert"]), ("due_soon", v["due"])]
        for kind, payload in cases:
            image = display.render_screen(kind, payload, NOW, CFG, 0.0, True, collect,
                                          screens, allow_poll=False, views=v)
            self.assertEqual(image.size, (142, 428), kind)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Platform selection, systemd unit generation, and the Linux font fallback.

    python3 tests/test_platform.py

Stdlib unittest, no Pillow, no venv, no keyboard: everything here is about the
*decisions* the installer makes, and those are all pure functions or thin
wrappers over them. That is the point -- the Linux branch has to be provable
from a Mac, so `CKD_PLATFORM` drives it and `exists` is injected wherever the
answer would otherwise depend on which fonts this machine happens to have.

What this deliberately does NOT claim: that the unit starts, that systemd
accepts it, or that a panel updates. Those need an Ubuntu box.
"""

import json
import os
import subprocess
import sys
import unittest

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_DIR)

import service  # noqa: E402


class Platform(unittest.TestCase):
    """system_name() is the single switch every other decision hangs off."""

    def setUp(self):
        self.saved = os.environ.get(service.PLATFORM_ENV)

    def tearDown(self):
        if self.saved is None:
            os.environ.pop(service.PLATFORM_ENV, None)
        else:
            os.environ[service.PLATFORM_ENV] = self.saved

    def as_platform(self, name):
        os.environ[service.PLATFORM_ENV] = name

    def test_darwin_selects_launchd_and_hotkeys(self):
        self.as_platform("Darwin")
        self.assertTrue(service.is_macos())
        self.assertFalse(service.is_linux())
        self.assertTrue(service.hotkeys_supported())
        self.assertIn("launchctl kickstart", service.restart_command("com.x.y"))

    def test_linux_selects_systemd_and_no_hotkeys(self):
        self.as_platform("Linux")
        self.assertTrue(service.is_linux())
        self.assertFalse(service.is_macos())
        self.assertFalse(service.hotkeys_supported())
        self.assertEqual(service.restart_command("com.x.y"),
                         "systemctl --user restart context-keyboard-display.service")

    def test_unknown_platform_claims_nothing(self):
        self.as_platform("FreeBSD")
        self.assertFalse(service.is_macos())
        self.assertFalse(service.is_linux())
        self.assertFalse(service.hotkeys_supported())

    def test_status_commands_differ_per_platform(self):
        self.as_platform("Linux")
        linux = service.status_commands("/tmp/x.log")
        self.assertTrue(any("systemctl --user status" in c for c in linux))
        self.assertTrue(any("journalctl --user" in c for c in linux))
        self.assertIn("tail -f /tmp/x.log", linux)
        self.as_platform("Darwin")
        self.assertTrue(any("launchctl print" in c for c in service.status_commands()))


class Unit(unittest.TestCase):
    """The generated systemd --user unit."""

    PY = "/home/u/.claude/context-keyboard-display/.venv/bin/python3"
    SCRIPT = "/home/u/.claude/context-keyboard-display/display.py"
    LOG = "/home/u/.claude/context-keyboard-display.log"

    def unit(self, **kwargs):
        return service.unit_text(self.PY, self.SCRIPT, self.LOG, **kwargs)

    def sections(self, text):
        return [line for line in text.splitlines() if line.startswith("[")]

    def test_has_the_three_sections_in_order(self):
        self.assertEqual(self.sections(self.unit()),
                         ["[Unit]", "[Service]", "[Install]"])

    def test_execstart_runs_the_venv_python_as_a_daemon(self):
        line = [l for l in self.unit().splitlines() if l.startswith("ExecStart=")][0]
        self.assertEqual(
            line, 'ExecStart="%s" "%s" --daemon' % (self.PY, self.SCRIPT))

    def test_restarts_and_starts_at_login_like_the_launchd_agent(self):
        text = self.unit()
        self.assertIn("Restart=always", text)          # KeepAlive
        self.assertIn("RestartSec=15", text)           # ThrottleInterval 15
        self.assertIn("WantedBy=default.target", text)  # RunAtLoad
        self.assertIn("Environment=PYTHONUNBUFFERED=1", text)

    def test_modern_systemd_appends_to_the_documented_log_file(self):
        text = self.unit(version=245)
        self.assertIn("StandardOutput=append:%s" % self.LOG, text)
        self.assertIn("StandardError=append:%s" % self.LOG, text)

    def test_old_systemd_omits_append_rather_than_failing_to_start(self):
        """append: is systemd >= 240; emitting it on 239 breaks the unit."""
        text = self.unit(version=239)
        directives = [l for l in text.splitlines() if not l.startswith("#")]
        self.assertFalse([l for l in directives if l.startswith("Standard")],
                         "no stream directive may survive on old systemd")
        self.assertFalse([l for l in directives if "append:" in l])
        self.assertIn("journalctl --user", text)   # said in a comment instead

    def test_unknown_version_assumes_modern(self):
        self.assertIn("append:", self.unit(version=None))

    def test_paths_with_spaces_stay_one_argument(self):
        text = service.unit_text("/opt/my python/bin/python3",
                                 "/opt/my python/display.py", self.LOG)
        self.assertIn('ExecStart="/opt/my python/bin/python3" '
                      '"/opt/my python/display.py" --daemon', text)

    def test_no_launchd_vocabulary_leaks_into_the_unit(self):
        text = self.unit()
        for word in ("launchd", "launchctl", "plist", "KeepAlive", "RunAtLoad"):
            self.assertNotIn(word, text)

    def test_unit_path_is_a_user_unit(self):
        """~/.config/systemd/user, not /etc: no root anywhere in this install."""
        self.assertTrue(service.UNIT_PATH.endswith(
            os.path.join("systemd", "user", service.UNIT_NAME)), service.UNIT_PATH)
        self.assertFalse(service.UNIT_PATH.startswith("/etc"))


class SystemdVersion(unittest.TestCase):
    """Parsing `systemctl --version` without needing systemctl."""

    class Result(object):
        def __init__(self, returncode, stdout):
            self.returncode, self.stdout = returncode, stdout

    def test_parses_ubuntu_output(self):
        out = "systemd 245 (245.4-4ubuntu3.22)\n+PAM +AUDIT ...\n"
        self.assertEqual(
            service.systemd_version(run=lambda a: self.Result(0, out)), 245)

    def test_absent_systemctl_is_none_not_a_crash(self):
        def boom(argv):
            raise OSError("no systemctl")
        self.assertIsNone(service.systemd_version(run=boom))

    def test_failed_systemctl_is_none(self):
        self.assertIsNone(
            service.systemd_version(run=lambda a: self.Result(1, "")))


class Fonts(unittest.TestCase):
    """The Linux face substitution, with the filesystem injected."""

    def only(self, *present):
        present = set(present)
        return lambda path: path in present

    def test_prefers_the_variable_noto_face(self):
        choice = service.font_choice(exists=self.only(
            "/usr/share/fonts/truetype/noto/NotoSans[wdth,wght].ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
        self.assertIn("NotoSans[wdth,wght]", choice["FONT_TEXT"])
        self.assertIn("NotoSans[wdth,wght]", choice["FONT_ROUNDED"])

    def test_falls_back_to_dejavu(self):
        choice = service.font_choice(exists=self.only(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
        self.assertTrue(choice["FONT_TEXT"].endswith("DejaVuSans.ttf"))

    def test_cjk_face_is_a_collection_face_zero(self):
        """The macOS indices name PingFang inside a five-face .ttc; nothing on
        Linux has that layout, and a bad index drops the whole render to
        Pillow's 11 px bitmap default."""
        choice = service.font_choice(exists=self.only(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"))
        self.assertTrue(choice["FONT_CJK"].endswith("NotoSansCJK-Regular.ttc"))
        self.assertEqual(choice["FONT_CJK_INDEX"], 0)
        self.assertEqual(choice["FONT_CJK_BOLD_INDEX"], 0)
        self.assertTrue(choice["has_cjk"])

    def test_missing_cjk_borrows_the_latin_face_rather_than_breaking(self):
        choice = service.font_choice(exists=self.only(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
        self.assertEqual(choice["FONT_CJK"], choice["FONT_TEXT"])
        self.assertFalse(choice["has_cjk"])

    def test_no_fonts_at_all_is_none(self):
        self.assertIsNone(service.font_choice(exists=lambda path: False))

    def test_apply_is_a_no_op_on_macos(self):
        class Fake(object):
            FONT_TEXT = "/System/Library/Fonts/SFNS.ttf"
        os.environ[service.PLATFORM_ENV] = "Darwin"
        try:
            fake = Fake()
            self.assertIsNone(service.apply_font_fallback(fake))
            self.assertEqual(fake.FONT_TEXT, "/System/Library/Fonts/SFNS.ttf")
        finally:
            os.environ.pop(service.PLATFORM_ENV, None)

    def test_apply_repoints_every_font_global_on_linux(self):
        class Fake(object):
            FONT_ROUNDED = "/System/Library/Fonts/SFNSRounded.ttf"
            FONT_TEXT = "/System/Library/Fonts/SFNS.ttf"
            FONT_CJK = "/System/Library/Fonts/PingFang.ttc"
            FONT_CJK_INDEX = 2
            FONT_CJK_BOLD_INDEX = 5
        os.environ[service.PLATFORM_ENV] = "Linux"
        try:
            fake = Fake()
            applied = service.apply_font_fallback(fake, exists=self.only(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
            self.assertIsNotNone(applied)
            for name in ("FONT_ROUNDED", "FONT_TEXT", "FONT_CJK"):
                self.assertNotIn("/System/", getattr(fake, name))
            self.assertEqual(fake.FONT_CJK_INDEX, 0)
            self.assertEqual(fake.FONT_CJK_BOLD_INDEX, 0)
        finally:
            os.environ.pop(service.PLATFORM_ENV, None)

    def test_warns_once_when_nothing_is_installed(self):
        os.environ[service.PLATFORM_ENV] = "Linux"
        service._FONT_WARNED = False
        try:
            said = []
            for _ in range(3):
                service.apply_font_fallback(object(), exists=lambda p: False,
                                            warn=said.append)
            self.assertEqual(len(said), 1)
            self.assertIn("fonts-noto", said[0])
        finally:
            service._FONT_WARNED = False
            os.environ.pop(service.PLATFORM_ENV, None)


def run(argv, platform=None, expect=None):
    env = dict(os.environ)
    env.pop("CKD_PLATFORM", None)
    if platform:
        env["CKD_PLATFORM"] = platform
    proc = subprocess.run(argv, cwd=REPO_DIR, env=env,
                          capture_output=True, text=True)
    if expect is not None and proc.returncode != expect:
        raise AssertionError("%s exited %d (wanted %d)\n%s%s"
                             % (argv, proc.returncode, expect,
                                proc.stdout, proc.stderr))
    return proc


class CLI(unittest.TestCase):
    """The scripts as a Linux user would actually invoke them."""

    def test_service_reports_the_linux_plan(self):
        out = run([sys.executable, "service.py", "--platform"],
                  platform="Linux", expect=0).stdout
        self.assertIn("systemd --user", out)
        self.assertIn("not supported", out)

    def test_service_reports_the_macos_plan(self):
        out = run([sys.executable, "service.py", "--platform"],
                  platform="Darwin", expect=0).stdout
        self.assertIn("launchd", out)
        self.assertIn("supported (keys.py)", out)

    def test_print_unit_emits_a_parseable_unit(self):
        out = run([sys.executable, "service.py", "--print-unit"],
                  platform="Linux", expect=0).stdout
        self.assertIn("[Service]", out)
        self.assertIn("--daemon", out)
        self.assertIn("WantedBy=default.target", out)
        # Nothing may be left unsubstituted.
        self.assertNotIn("%s", out)

    def test_keys_install_is_a_successful_no_op_on_linux(self):
        """The whole point of requirement 4: a Linux install must not fail on
        the hotkey listener it is never going to have."""
        proc = run([sys.executable, "keys.py", "--install"],
                   platform="Linux", expect=0)
        self.assertIn("macOS-only", proc.stdout)
        self.assertIn("display.py mode next", proc.stdout)

    def test_keys_uninstall_is_a_successful_no_op_on_linux(self):
        run([sys.executable, "keys.py", "--uninstall"], platform="Linux", expect=0)

    def test_keys_daemon_refuses_on_linux(self):
        proc = run([sys.executable, "keys.py", "--daemon"], platform="Linux",
                   expect=3)
        self.assertIn("macOS-only", proc.stdout)

    def test_keys_help_works_everywhere(self):
        out = run([sys.executable, "keys.py", "--help"], platform="Linux",
                  expect=0).stdout
        self.assertIn("python3 keys.py", out)

    def test_display_help_still_lists_its_commands(self):
        out = run([sys.executable, "display.py", "--help"], expect=0).stdout
        self.assertIn("python3 display.py --install", out)
        self.assertIn("python3 display.py --daemon", out)

    def test_display_writes_no_launchd_plist_path_on_linux(self):
        """A smoke test that the module imports and picks the Linux branch."""
        out = run([sys.executable, "-c",
                   "import display, service; "
                   "print(service.is_linux(), service.restart_command(display.LAUNCH_LABEL))"],
                  platform="Linux", expect=0).stdout
        self.assertIn("True systemctl --user restart", out)


def _renderable():
    try:
        import PIL  # noqa: F401
        import keyboard_status  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(_renderable(),
                     "needs Pillow + keyboard_status: run under .venv/bin/python3")
class FontSubstitutionRenders(unittest.TestCase):
    """The substitution has to survive contact with the renderer, not just
    with assertEqual: keyboard_status.font() reads those names out of its own
    module globals, and a face it cannot open silently degrades the whole
    panel to Pillow's 11 px bitmap default. Standing in macOS faces for the
    Linux ones proves the mechanism on a machine with no /usr/share/fonts."""

    STAND_INS = ("/System/Library/Fonts/SFNS.ttf",
                 "/System/Library/Fonts/PingFang.ttc")

    def setUp(self):
        if not all(os.path.exists(p) for p in self.STAND_INS):
            self.skipTest("no stand-in faces on this host")
        import keyboard_status
        self.ks = keyboard_status
        self.saved = {k: getattr(keyboard_status, k) for k in
                      ("FONT_TEXT", "FONT_ROUNDED", "FONT_CJK",
                       "FONT_CJK_INDEX", "FONT_CJK_BOLD_INDEX")}
        self.saved_lists = (service.LINUX_TEXT_FONTS, service.LINUX_ROUNDED_FONTS,
                            service.LINUX_CJK_FONTS)
        service.LINUX_TEXT_FONTS = (self.STAND_INS[0],)
        service.LINUX_ROUNDED_FONTS = (self.STAND_INS[0],)
        service.LINUX_CJK_FONTS = (self.STAND_INS[1],)
        os.environ[service.PLATFORM_ENV] = "Linux"

    def tearDown(self):
        os.environ.pop(service.PLATFORM_ENV, None)
        (service.LINUX_TEXT_FONTS, service.LINUX_ROUNDED_FONTS,
         service.LINUX_CJK_FONTS) = self.saved_lists
        for key, value in self.saved.items():
            setattr(self.ks, key, value)
        self.ks._FONT_CACHE.clear()

    def test_substituted_faces_are_real_faces_not_the_bitmap_default(self):
        from PIL import Image, ImageDraw

        self.ks._FONT_CACHE.clear()
        applied = service.apply_font_fallback(self.ks)
        self.assertIsNotNone(applied)
        self.assertEqual(self.ks.FONT_CJK_INDEX, 0)

        draw = ImageDraw.Draw(Image.new("RGB", (142, 428)))
        small = self.ks.measure(draw, "BUSY", self.ks.font(12, 800))
        large = self.ks.measure(draw, "BUSY", self.ks.font(34, 800))
        # load_default ignores size; a real face does not.
        self.assertGreater(large, small * 2,
                           "size is being ignored -- this is the bitmap default")

    def test_cjk_runs_still_measure_through_the_substituted_collection(self):
        from PIL import Image, ImageDraw

        self.ks._FONT_CACHE.clear()
        service.apply_font_fallback(self.ks)
        draw = ImageDraw.Draw(Image.new("RGB", (142, 428)))
        self.assertGreater(
            self.ks.measure(draw, "\u6771\u4eac", self.ks.font(34, 700)), 20)


class InstallRouting(unittest.TestCase):
    """Which service manager display.install() reaches for, and whether it
    installs the hotkey listener. Effects stubbed -- see tests/_install_probe.py."""

    def probe(self, platform, *args):
        proc = run([sys.executable, os.path.join("tests", "_install_probe.py")] + list(args),
                   platform=platform, expect=0)
        data = json.loads(proc.stdout[proc.stdout.index("{"):])
        return [c[0] for c in data["calls"]]

    def test_macos_still_installs_the_launchd_agent_and_the_hotkeys(self):
        """Requirements 1 and 5: nothing about the default path moved."""
        names = self.probe("Darwin")
        self.assertIn("launchd.write_plist", names)
        self.assertIn("launchctl", names)
        self.assertIn("keys.install_agent", names)
        self.assertNotIn("systemd.install_service", names)

    def test_linux_installs_the_systemd_service_and_no_hotkeys(self):
        names = self.probe("Linux")
        self.assertIn("systemd.install_service", names)
        self.assertNotIn("launchd.write_plist", names)
        self.assertNotIn("launchctl", names)
        self.assertNotIn("keys.install_agent", names)

    def test_no_agent_installs_neither_service(self):
        for platform in ("Darwin", "Linux"):
            names = self.probe(platform, "--no-agent")
            self.assertNotIn("launchd.write_plist", names, platform)
            self.assertNotIn("systemd.install_service", names, platform)

    def test_unknown_platform_installs_no_service_but_does_not_crash(self):
        names = self.probe("FreeBSD")
        self.assertNotIn("launchd.write_plist", names)
        self.assertNotIn("systemd.install_service", names)
        self.assertNotIn("keys.install_agent", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)

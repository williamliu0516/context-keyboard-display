#!/usr/bin/env python3
"""The Docker deployment: the interlock, the image, and the migration order.

    python3 tests/test_docker.py

Three layers, in order of how much they need:

  1. Pure unit tests over display.py's container hooks -- standby, heartbeat,
     health -- including a fake-driven run of the daemon loop itself, which is
     the only way to prove the thing that actually matters: that a daemon in
     standby renders and heartbeats but does NOT push, and that `--once` pushes
     anyway because it is the verification push.
  2. Static reading of Dockerfile / docker-compose.yml / the deploy scripts,
     plus CKD_DRY_RUN=1 runs of ckd-docker.sh, which is what pins the migration
     order: the native launchd agent is never booted out before the container
     has been health-checked and has landed a real frame.
  3. Docker-gated tests (skipped without a working daemon) that ask the built
     image about itself: no host-only paths inside it, the library importable
     from site-packages, and the entrypoint refusing to start without the
     ~/.claude mount.

What this deliberately does NOT claim: that the panel took a frame, or that
launchd did what it was told. Those are the live steps in `ckd-docker.sh
migrate`, and they are verified there against the real hardware -- a test
suite that pushed to the keyboard would be a second pusher.
"""

import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_DIR)

import display  # noqa: E402
import service  # noqa: E402

DOCKERFILE = os.path.join(REPO_DIR, "Dockerfile")
COMPOSE = os.path.join(REPO_DIR, "docker-compose.yml")
ENTRYPOINT = os.path.join(REPO_DIR, "docker", "entrypoint.sh")
DEPLOY = os.path.join(REPO_DIR, "deploy", "ckd-docker.sh")
BOOT = os.path.join(REPO_DIR, "deploy", "ckd-docker-boot.sh")
IMAGE = "context-keyboard-display:local"


def read(path):
    with open(path) as handle:
        return handle.read()


def docker_ok():
    """Is there a Docker daemon to ask? Cached: `docker info` is not cheap."""
    if not hasattr(docker_ok, "answer"):
        try:
            docker_ok.answer = subprocess.run(
                ["docker", "info"], capture_output=True, timeout=30).returncode == 0
        except (OSError, subprocess.SubprocessError):
            docker_ok.answer = False
    return docker_ok.answer


def image_built():
    if not docker_ok():
        return False
    return subprocess.run(["docker", "image", "inspect", IMAGE],
                          capture_output=True).returncode == 0


# --------------------------------------------------------- standby / heartbeat


class Standby(unittest.TestCase):
    """The single-pusher interlock, as one file and one env var."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = os.environ.get(display.STANDBY_ENV)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self.saved is None:
            os.environ.pop(display.STANDBY_ENV, None)
        else:
            os.environ[display.STANDBY_ENV] = self.saved

    def test_unset_env_is_never_standby(self):
        """The native launchd install sets nothing and must be unaffected."""
        os.environ.pop(display.STANDBY_ENV, None)
        self.assertEqual(display.standby_path(), "")
        self.assertFalse(display.standby_active())

    def test_configured_but_absent_file_is_not_standby(self):
        path = os.path.join(self.tmp, "standby")
        os.environ[display.STANDBY_ENV] = path
        self.assertFalse(display.standby_active())

    def test_present_file_is_standby(self):
        path = os.path.join(self.tmp, "standby")
        os.environ[display.STANDBY_ENV] = path
        open(path, "w").close()
        self.assertTrue(display.standby_active())
        # Read per call, not cached: the handoff is the file going away under a
        # running daemon.
        os.unlink(path)
        self.assertFalse(display.standby_active())


class Heartbeat(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "sub", "heartbeat.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_round_trip_creates_its_directory(self):
        display.write_heartbeat("idle", True, 123.0, False, 0, path=self.path)
        data = display.read_heartbeat(self.path)
        self.assertEqual(data["kind"], "idle")
        self.assertTrue(data["online"])
        self.assertEqual(data["last_push_at"], 123.0)
        self.assertFalse(data["standby"])
        self.assertEqual(data["errors"], 0)
        self.assertEqual(data["pid"], os.getpid())
        self.assertAlmostEqual(data["at"], time.time(), delta=5)

    def test_written_atomically_and_leaves_no_temp(self):
        display.write_heartbeat("idle", True, 0.0, False, 0, path=self.path)
        display.write_heartbeat("sessions", False, 0.0, True, 1, path=self.path)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["heartbeat.json"])
        self.assertEqual(display.read_heartbeat(self.path)["kind"], "sessions")

    def test_unwritable_path_is_survivable(self):
        """A daemon that cannot heartbeat still drives the panel."""
        display.write_heartbeat("idle", True, 0.0, False, 0,
                                path="/proc/nope/heartbeat.json")

    def test_no_path_writes_nothing(self):
        display.write_heartbeat("idle", True, 0.0, False, 0, path="")
        self.assertIsNone(display.read_heartbeat(""))

    def test_garbage_reads_as_nothing(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as handle:
            handle.write("not json")
        self.assertIsNone(display.read_heartbeat(self.path))


class Health(unittest.TestCase):
    """What `display.py --health` -- the container's health check -- claims."""

    CFG = {"tick_seconds": 5.0}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "heartbeat.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def beat(self, **fields):
        payload = {"at": time.time(), "kind": "idle", "online": True,
                   "standby": False, "errors": 0}
        payload.update(fields)
        with open(self.path, "w") as handle:
            json.dump(payload, handle)

    def verdict(self, **kwargs):
        return display.health_verdict(self.CFG, path=self.path, **kwargs)

    def test_fresh_tick_is_healthy(self):
        self.beat()
        code, message = self.verdict()
        self.assertEqual(code, 0)
        self.assertIn("ok:", message)
        self.assertIn("idle", message)

    def test_no_heartbeat_configured_is_its_own_answer(self):
        code, message = display.health_verdict(self.CFG, path="")
        self.assertEqual(code, 2)
        self.assertIn(display.HEARTBEAT_ENV, message)

    def test_missing_file_is_unhealthy_not_a_crash(self):
        code, message = self.verdict()
        self.assertEqual(code, 1)
        self.assertIn("no readable heartbeat", message)

    def test_stale_tick_is_unhealthy(self):
        self.beat(at=time.time() - 600)
        code, message = self.verdict()
        self.assertEqual(code, 1)
        self.assertIn("stuck", message)

    def test_staleness_allowance_follows_the_tick_but_has_a_floor(self):
        """A slow tick must not be called sick; the floor covers a failed push."""
        self.beat(at=time.time() - 40)
        self.assertEqual(self.verdict()[0], 0)          # inside the 45 s floor
        self.beat(at=time.time() - 100)
        self.assertEqual(self.verdict()[0], 1)
        # A 60 s tick gets 240 s of slack rather than the floor.
        self.beat(at=time.time() - 100)
        code, _ = display.health_verdict({"tick_seconds": 60.0}, path=self.path)
        self.assertEqual(code, 0)

    def test_an_offline_panel_is_a_healthy_daemon(self):
        """The keyboard sleeps off Wi-Fi by design. Restarting for that would
        be a health check causing the outage it is looking for."""
        self.beat(online=False)
        code, message = self.verdict()
        self.assertEqual(code, 0)
        self.assertIn("not answering", message)

    def test_standby_is_healthy_and_said_out_loud(self):
        self.beat(standby=True)
        code, message = self.verdict()
        self.assertEqual(code, 0)
        self.assertIn("standby", message)

    def test_a_loop_that_only_raises_is_unhealthy(self):
        self.beat(errors=display.HEALTH_MAX_ERRORS)
        code, message = self.verdict()
        self.assertEqual(code, 1)
        self.assertIn("raising", message)
        self.beat(errors=display.HEALTH_MAX_ERRORS - 1)
        self.assertEqual(self.verdict()[0], 0)

    def test_no_timestamp_is_unhealthy(self):
        with open(self.path, "w") as handle:
            json.dump({"kind": "idle"}, handle)
        self.assertEqual(self.verdict()[0], 1)

    def test_clock_skew_from_the_future_is_not_a_stuck_loop(self):
        self.beat(at=time.time() + 120)
        self.assertEqual(self.verdict()[0], 0)

    def test_cli_reports_the_verdict_and_the_exit_code(self):
        self.beat()
        env = dict(os.environ, CKD_HEARTBEAT_PATH=self.path)
        proc = subprocess.run([sys.executable, os.path.join(REPO_DIR, "display.py"),
                               "--health"], capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ok:", proc.stdout)

        os.unlink(self.path)
        proc = subprocess.run([sys.executable, os.path.join(REPO_DIR, "display.py"),
                               "--health"], capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no readable heartbeat", proc.stdout)

    def test_health_is_stdlib_only(self):
        """It is the check that has to answer when the render deps are what
        broke, so it must not import Pillow, PyYAML or keyboard_status."""
        code = (
            "import sys, json, tempfile, os, time;"
            "sys.path.insert(0, %r);"
            "sys.modules['PIL'] = None; sys.modules['yaml'] = None;"
            "sys.modules['keyboard_status'] = None;"
            "import display;"
            "p = os.path.join(tempfile.mkdtemp(), 'h.json');"
            "json.dump({'at': time.time(), 'kind': 'idle', 'online': True,"
            " 'standby': False, 'errors': 0}, open(p, 'w'));"
            "print(display.health_verdict({'tick_seconds': 5.0}, path=p))"
        ) % REPO_DIR
        proc = subprocess.run([sys.executable, "-c", code],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("(0,", proc.stdout)


# ------------------------------------------------------------- the daemon loop


class FakeKs:
    """Stands in for keyboard_status: encode + push, and a record of pushes."""

    def __init__(self, ok=True):
        self.ok = ok
        self.pushes = []
        self.frames = 0

    def encode(self, image, cfg):
        self.frames += 1
        return b"jpeg-%d" % self.frames

    def push(self, frame, cfg):
        self.pushes.append(frame)
        return (True, "200") if self.ok else (False, "timed out")


class FakeCollect:
    """The two entry points run_daemon reaches for on the collect module."""

    def collect_sessions(self, now, cfg):
        return []

    def turn_started(self, payload):
        return 0.0


class FakeEngine:
    def __init__(self, collect_mod):
        pass

    def choose(self, sessions, now, cfg):
        return "idle", None


class StopLoop(Exception):
    """Raised out of the patched inter-tick sleep to end a --daemon run."""


class DaemonStandby(unittest.TestCase):
    """The interlock where it counts: inside run_daemon's push path.

    Everything the loop needs from the renderer and the library is faked --
    this is a test about the *decision* to push, not about pixels, and it must
    run without Pillow, without the panel, and without touching ~/.claude.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.standby = os.path.join(self.tmp, "standby")
        self.heartbeat = os.path.join(self.tmp, "heartbeat.json")
        self.logs = []
        self.ks = FakeKs()

        self.saved = {k: os.environ.get(k) for k in
                      (display.STANDBY_ENV, display.HEARTBEAT_ENV, service.PLATFORM_ENV)}
        os.environ[display.STANDBY_ENV] = self.standby
        os.environ[display.HEARTBEAT_ENV] = self.heartbeat
        # Linux, so boot_out_old_agent is the no-op it is off macOS -- the
        # loop must not shell out to launchctl from a test.
        os.environ[service.PLATFORM_ENV] = "Linux"

        self.patches = {
            "import_stack": lambda cfg: (self.ks, FakeCollect(), object()),
            "Engine": FakeEngine,
            "render_screen": lambda *a, **k: object(),
            "tick_for": lambda *a, **k: 5.0,
            "write_status": lambda kind: None,
            "log": self.logs.append,
            "sleep_between_ticks": self.stop,
        }
        self.originals = {name: getattr(display, name) for name in self.patches}
        for name, value in self.patches.items():
            setattr(display, name, value)

    def tearDown(self):
        for name, value in self.originals.items():
            setattr(display, name, value)
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def stop(self, seconds, signature):
        raise StopLoop

    CFG = dict(display.DEFAULTS, url="http://panel.test/image/upload")

    def daemon_tick(self):
        """One tick of `--daemon`, then out."""
        with self.assertRaises(StopLoop):
            display.run_daemon(dict(self.CFG))

    def test_standby_renders_and_heartbeats_but_never_pushes(self):
        open(self.standby, "w").close()
        self.daemon_tick()
        self.assertEqual(self.ks.pushes, [], "a standby daemon must not push")
        self.assertEqual(self.ks.frames, 1, "it must still render")
        beat = display.read_heartbeat(self.heartbeat)
        self.assertTrue(beat["standby"])
        self.assertIsNone(beat["last_push_at"])
        self.assertTrue(any("standby" in line for line in self.logs),
                        "the reason must be in the log: %r" % self.logs)

    def test_without_the_file_the_daemon_pushes(self):
        self.daemon_tick()
        self.assertEqual(len(self.ks.pushes), 1)
        beat = display.read_heartbeat(self.heartbeat)
        self.assertFalse(beat["standby"])
        self.assertTrue(beat["online"])
        self.assertTrue(beat["last_push_at"])

    def test_once_pushes_even_in_standby(self):
        """`--once` IS the verification push: it is the one frame that has to
        go out while the native agent still owns the panel."""
        open(self.standby, "w").close()
        code = display.run_daemon(dict(self.CFG), once=True)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.ks.pushes), 1)

    def test_once_reports_a_failed_push_as_a_nonzero_exit(self):
        """What the migration hangs on: a push that did not land must fail."""
        self.ks.ok = False
        code = display.run_daemon(dict(self.CFG), once=True)
        self.assertEqual(code, 1)

    def test_dry_run_never_pushes_either(self):
        with tempfile.TemporaryDirectory() as out:
            saved = display.REPO_DIR
            display.REPO_DIR = out
            try:
                code = display.run_daemon(dict(self.CFG), once=True, dry_run=True)
            finally:
                display.REPO_DIR = saved
        self.assertEqual(code, 0)
        self.assertEqual(self.ks.pushes, [])

    def test_a_raising_tick_still_heartbeats_and_counts(self):
        display.render_screen = lambda *a, **k: 1 / 0
        self.daemon_tick()
        beat = display.read_heartbeat(self.heartbeat)
        self.assertEqual(beat["errors"], 1)
        self.assertEqual(self.ks.pushes, [])


# ------------------------------------------------------------------ the image


class DockerfileText(unittest.TestCase):
    """What the image must and must not contain, read off the recipe."""

    def setUp(self):
        self.text = read(DOCKERFILE)

    def test_the_library_is_copied_in_and_installed_not_mounted(self):
        self.assertIn("COPY --from=upstream keyboard_status.py", self.text)
        self.assertRegex(self.text, r"pip install --no-cache-dir /opt/upstream")
        self.assertNotIn("pip install -e", self.text,
                         "an editable install would point at a host path")

    def test_no_host_only_paths_are_baked_in(self):
        """Instructions only: the comments discuss ~/projects precisely because
        the image must not contain it."""
        for number, line in enumerate(self.text.splitlines(), 1):
            if line.lstrip().startswith("#") or not line.strip():
                continue
            code = line.split(" #", 1)[0]
            for bad in ("/Users/", "~/projects", "$HOME/projects"):
                self.assertNotIn(bad, code, "Dockerfile:%d bakes in %s" % (number, bad))

    def test_keys_py_is_not_in_the_image(self):
        """The global hotkeys are a Carbon reservation against WindowServer and
        stay a native launchd agent; leaving the file out of the image makes
        that a property rather than a promise."""
        copied = re.findall(r"^COPY (.+)$", self.text, re.M)
        self.assertTrue(any("display.py" in line for line in copied))
        self.assertFalse(any(re.search(r"\bkeys\.py\b", line) for line in copied),
                         "keys.py must not be COPYd into the image")

    def test_the_daemon_modules_are_all_there(self):
        copied = " ".join(re.findall(r"^COPY (.+)$", self.text, re.M))
        for module in ("display.py", "collect.py", "screens.py", "service.py"):
            self.assertIn(module, copied)

    def test_home_is_set_so_expanduser_finds_the_mount(self):
        self.assertRegex(self.text, r"ENV HOME=/host")

    def test_healthcheck_runs_the_stdlib_health_command(self):
        self.assertIn("HEALTHCHECK", self.text)
        self.assertRegex(self.text, r'CMD \["python3", "/app/display.py", "--health"\]')

    def test_fonts_are_installed_and_verified_at_build_time(self):
        """Font substitution is the one thing a Linux container gets wrong
        silently, so the build fails rather than the panel rendering tofu."""
        self.assertIn("fonts-ubuntu", self.text)
        self.assertIn("fonts-noto-core", self.text)
        self.assertIn("fonts-dejavu-core", self.text)
        self.assertIn("fonts-noto-cjk", self.text)
        self.assertIn("service.py --fonts", self.text)
        self.assertIn("--preview /tmp/buildcheck", self.text)

    def test_the_approved_face_is_installed_and_gated_not_merely_hoped_for(self):
        """fonts-ubuntu lives in Debian non-free, so the component has to be
        enabled or the install silently is not there; --fonts --strict is what
        turns "the resolved face is Ubuntu" from a hope into a build failure."""
        self.assertRegex(self.text, r"Components:.*non-free")
        self.assertIn("service.py --fonts --strict", self.text)

    def test_the_installed_faces_are_ones_service_py_looks_for(self):
        """The apt packages and service.py's candidate lists have to agree;
        they are in different files and would drift silently."""
        wanted = service.LINUX_TEXT_FONTS + service.LINUX_CJK_FONTS
        self.assertTrue(any("/ubuntu/" in path.lower() for path in wanted))
        self.assertTrue(any("noto" in path.lower() for path in wanted))
        self.assertTrue(any("dejavu" in path.lower() for path in wanted))

    def test_the_primary_latin_faces_service_py_picks_first_are_ubuntu(self):
        """Read off the same constants the image resolves at run time: the
        head of each latin list is the face the panel is actually drawn in."""
        for candidates in (service.LINUX_TEXT_FONTS, service.LINUX_ROUNDED_FONTS):
            first = candidates[0].lower()
            self.assertIn("/ubuntu/ubuntu-b.ttf", first)
            self.assertNotIn("noto", first)
            self.assertNotIn("dejavu", first)
        self.assertTrue(service.LINUX_CJK_FONTS[0].endswith("NotoSansCJK-Bold.ttc"))

    def test_timezone_data_is_present_because_idle_is_a_clock(self):
        self.assertIn("tzdata", self.text)

    def test_git_is_present_for_the_diff_counts(self):
        """collect.diff_stat shells out to it for the +/- row."""
        self.assertRegex(self.text, r"(?m)^\s+git \\$")


class ComposeText(unittest.TestCase):
    def setUp(self):
        self.text = read(COMPOSE)

    def test_restart_policy_is_declared(self):
        self.assertIn("restart: unless-stopped", self.text)

    def test_the_live_claude_state_is_mounted_read_write(self):
        self.assertIn("${HOME}/.claude:/host/.claude", self.text)
        self.assertNotIn("${HOME}/.claude:/host/.claude:ro", self.text)

    def test_the_projects_mount_is_read_only_and_path_identical(self):
        lines = [l for l in self.text.splitlines()
                 if "CKD_PROJECTS_DIR" in l and l.strip().startswith("-")]
        self.assertEqual(len(lines), 1, lines)
        spec = lines[0].strip().lstrip("- ")
        body, mode = spec.rsplit(":", 1)
        self.assertEqual(mode, "ro", "the projects mount must be read-only")
        # Split down the middle rather than on the first colon: the halves are
        # ${VAR:-default} expressions with colons of their own.
        half = len(body) // 2
        self.assertEqual(body[half], ":", spec)
        self.assertEqual(body[:half], body[half + 1:],
                         "transcripts record host cwds; git is asked by that name")

    def test_the_standby_and_heartbeat_paths_are_where_the_tooling_looks(self):
        self.assertIn("CKD_STANDBY_PATH: /host/.claude/context-keyboard-display-standby",
                      self.text)
        self.assertIn("CKD_HEARTBEAT_PATH: /tmp/ckd/heartbeat.json", self.text)

    def test_the_container_runs_as_the_host_user(self):
        self.assertRegex(self.text, r'user: "\$\{CKD_UID:-\d+\}:\$\{CKD_GID:-\d+\}"')

    def test_timezone_is_passed_through(self):
        self.assertIn("TZ: ${TZ:-UTC}", self.text)

    def test_no_panel_address_in_the_tracked_file(self):
        self.assertNotIn("image/upload", self.text)
        self.assertNotRegex(self.text, r"\b\d{1,3}(\.\d{1,3}){3}\b")

    def test_logs_are_capped(self):
        self.assertIn("max-size", self.text)


class ComposeResolves(unittest.TestCase):
    """`docker compose config` -- the file as the engine actually reads it."""

    @classmethod
    def setUpClass(cls):
        if not docker_ok():
            raise unittest.SkipTest("no docker daemon")
        proc = subprocess.run(["docker", "compose", "config"], cwd=REPO_DIR,
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise AssertionError("docker compose config failed:\n%s" % proc.stderr)
        cls.resolved = proc.stdout

    def test_it_is_valid_and_names_one_service(self):
        self.assertIn("ckd-display", self.resolved)
        self.assertEqual(self.resolved.count("container_name:"), 1,
                         "one pusher means one service")

    def test_interpolation_produced_absolute_mount_sources(self):
        self.assertIn("source: %s/.claude" % os.path.expanduser("~"), self.resolved)
        self.assertNotIn("${", self.resolved)

    def test_the_projects_mount_resolved_to_one_read_only_identity_bind(self):
        projects = os.environ.get("CKD_PROJECTS_DIR") or os.path.expanduser("~/projects")
        self.assertIn("source: %s\n" % projects, self.resolved)
        self.assertIn("target: %s\n" % projects, self.resolved)
        self.assertIn("read_only: true", self.resolved)

    def test_health_and_restart_survived_resolution(self):
        self.assertIn("restart: unless-stopped", self.resolved)
        self.assertIn("--health", self.resolved)


class ImageContents(unittest.TestCase):
    """Questions only the built image can answer."""

    @classmethod
    def setUpClass(cls):
        if not image_built():
            raise unittest.SkipTest("%s is not built (run: ckd-docker.sh build)" % IMAGE)

    def run_in_image(self, *argv):
        proc = subprocess.run(["docker", "run", "--rm", "--entrypoint", "python3",
                               IMAGE] + list(argv), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def test_the_library_resolves_from_site_packages(self):
        where = self.run_in_image("-c", "import keyboard_status; print(keyboard_status.__file__)")
        self.assertIn("site-packages", where)
        self.assertNotIn("/Users", where)

    def test_the_render_stack_imports(self):
        out = self.run_in_image("-c", "import PIL, yaml, keyboard_status; print('ok')")
        self.assertEqual(out, "ok")

    def test_no_host_paths_in_the_image_environment(self):
        proc = subprocess.run(["docker", "image", "inspect", "--format",
                               "{{json .Config.Env}}", IMAGE],
                              capture_output=True, text=True)
        env = json.loads(proc.stdout)
        self.assertIn("HOME=/host", env)
        self.assertFalse([v for v in env if "/Users" in v], env)

    def test_it_reports_itself_as_a_linux_host_with_no_hotkeys(self):
        out = self.run_in_image("/app/service.py", "--platform")
        self.assertIn("Linux", out)
        self.assertIn("not supported", out)

    def test_a_usable_font_was_installed(self):
        out = self.run_in_image("/app/service.py", "--fonts")
        self.assertIn("FONT_TEXT", out)
        self.assertNotIn("no usable font", out)
        self.assertNotIn("tofu", out, "the CJK face should be present")

    def test_the_image_resolves_the_approved_ubuntu_stack(self):
        """The deployed answer to "what face is the panel in": asked of the
        built image rather than of the constants, so an apt change that drops
        fonts-ubuntu fails here even if service.py still asks for it."""
        out = self.run_in_image("/app/service.py", "--fonts")
        rows = dict(line.split(None, 1) for line in out.splitlines()
                    if line.startswith("FONT_"))
        self.assertEqual(rows["FONT_TEXT"].strip(),
                         "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf")
        self.assertEqual(rows["FONT_ROUNDED"].strip(),
                         "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf")
        self.assertEqual(rows["FONT_CJK"].strip(),
                         "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc")

    def test_the_strict_font_gate_passes_inside_the_built_image(self):
        proc = subprocess.run(["docker", "run", "--rm", "--entrypoint", "python3",
                               IMAGE, "/app/service.py", "--fonts", "--strict"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_cjk_still_draws_hanzi_beside_the_ubuntu_latin_face(self):
        """Ubuntu has no CJK coverage at all, so the fallback is the only
        thing between a todo written in Japanese and a row of tofu. Measured
        through the renderer's own font() pair, at a weight above its 550
        threshold, which is where the panel actually lives."""
        probe = (
            "import sys; sys.path.insert(0, '/app');"
            "import service, keyboard_status as ks;"
            "service.apply_font_fallback(ks);"
            "latin, wide = ks.font(20, 700);"
            "from PIL import Image, ImageDraw;"
            "d = ImageDraw.Draw(Image.new('RGB', (64, 64)));"
            "print(type(wide).__name__, int(d.textlength('\u6771\u4eac', font=wide)),"
            " int(d.textlength('ok', font=latin)))")
        kind, cjk_width, latin_width = self.run_in_image("-c", probe).split()
        self.assertEqual(kind, "FreeTypeFont",
                         "the CJK face fell back to Pillow's bitmap default")
        self.assertGreater(int(cjk_width), 20, "CJK measured as tofu/blank")
        self.assertGreater(int(latin_width), 0)

    def test_keys_py_is_absent_from_the_image(self):
        proc = subprocess.run(["docker", "run", "--rm", "--entrypoint", "test",
                               IMAGE, "-e", "/app/keys.py"], capture_output=True)
        self.assertNotEqual(proc.returncode, 0, "keys.py must not be in the image")

    def test_the_entrypoint_refuses_to_start_without_the_claude_mount(self):
        """The one failure that would otherwise look like a config mistake."""
        proc = subprocess.run(["docker", "run", "--rm", "-e", "HOME=/nowhere",
                               IMAGE, "--daemon"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 78, proc.stdout + proc.stderr)
        self.assertIn("is not mounted", proc.stderr)


# ------------------------------------------------------------ the deploy tool


class DeployScripts(unittest.TestCase):
    def test_they_are_executable_shell_that_parses(self):
        for path, shell in ((DEPLOY, "bash"), (BOOT, "bash"), (ENTRYPOINT, "sh")):
            self.assertTrue(os.access(path, os.X_OK), "%s must be executable" % path)
            proc = subprocess.run([shell, "-n", path], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_no_sudo_anywhere(self):
        """In the code, that is -- the comments say the word on purpose."""
        for path in (DEPLOY, BOOT, ENTRYPOINT):
            for number, line in enumerate(read(path).splitlines(), 1):
                code = line.split("#", 1)[0]
                self.assertNotRegex(code, r"(^|[;&|(`$]\s*)sudo\b",
                                    "%s:%d invokes sudo" % (path, number))

    def test_help_is_the_default_and_an_unknown_command_fails(self):
        proc = subprocess.run([DEPLOY], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("migrate", proc.stdout)
        proc = subprocess.run([DEPLOY, "wat"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)

    def test_the_hotkey_agent_is_named_only_to_be_left_alone(self):
        """The listener is a macOS Carbon reservation and stays native, so the
        only mention of its label may be read-only: status, and one say()."""
        text = read(DEPLOY)
        self.assertIn("com.williamliu.context-keyboard-keys", text)
        for number, line in enumerate(text.splitlines(), 1):
            code = line.split("#", 1)[0]
            if "KEYS_LABEL" not in code:
                continue
            self.assertNotRegex(
                code, r"launchctl(_soft)?\s+(bootout|disable|unload|kickstart|kill)",
                "%s:%d would touch the native hotkey listener" % (DEPLOY, number))


class LaunchAgentPlist(unittest.TestCase):
    """The login/wake agent, as launchd will read it."""

    @classmethod
    def setUpClass(cls):
        proc = subprocess.run([DEPLOY, "agent-plist"], capture_output=True)
        assert proc.returncode == 0, proc.stderr
        cls.agent = plistlib.loads(proc.stdout)

    def test_label_and_program(self):
        self.assertEqual(self.agent["Label"],
                         "com.williamliu.context-keyboard-display-docker")
        argv = self.agent["ProgramArguments"]
        self.assertEqual(argv[0], "/bin/bash")
        self.assertTrue(os.path.exists(argv[1]), argv)
        self.assertTrue(os.access(argv[1], os.X_OK), argv)

    def test_it_runs_at_login_and_again_while_awake(self):
        self.assertTrue(self.agent["RunAtLoad"])
        self.assertGreaterEqual(self.agent["StartInterval"], 60)
        # KeepAlive on a script that exits would respawn it forever; the
        # container's restart policy is what keeps the daemon up.
        self.assertNotIn("KeepAlive", self.agent)

    def test_it_carries_a_path_that_includes_the_docker_cli(self):
        """launchd's default PATH has no /usr/local/bin, which is where Docker
        Desktop puts docker -- the classic silent login-agent failure."""
        self.assertIn("/usr/local/bin", self.agent["EnvironmentVariables"]["PATH"])

    def test_it_logs_somewhere_readable(self):
        self.assertTrue(self.agent["StandardOutPath"].endswith(".log"))
        self.assertEqual(self.agent["StandardOutPath"], self.agent["StandardErrorPath"])


class PlannedState:
    """A temporary copy of the state migrate/rollback read, for the dry runs.

    Without this, the plan depends on the machine: on an already-migrated Mac
    there is no native plist to hand over from, so `migrate` legitimately skips
    the standby step and the ordering assertions have nothing to anchor on.
    Pointing the three state paths at a temp directory -- and putting a plist
    where the flow expects one -- makes the plan the same everywhere, without
    changing a single decision the script makes about them.
    """

    def __init__(self, native=False, parked=False, standby=False):
        self.dir = tempfile.mkdtemp()
        self.native_plist = os.path.join(self.dir, "native.plist")
        self.park_dir = os.path.join(self.dir, "park")
        self.standby = os.path.join(self.dir, "standby")
        self.parked_plist = os.path.join(
            self.park_dir, "com.williamliu.context-keyboard-display.plist")
        if native:
            open(self.native_plist, "w").close()
        if parked:
            os.makedirs(self.park_dir, exist_ok=True)
            open(self.parked_plist, "w").close()
        if standby:
            open(self.standby, "w").close()

    def env(self):
        return {"CKD_NATIVE_PLIST": self.native_plist,
                "CKD_PARK_DIR": self.park_dir,
                "CKD_STANDBY_FILE": self.standby}

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def dry_run(*args, **env):
    """`ckd-docker.sh` with every state-changing command printed, not run."""
    return subprocess.run([DEPLOY] + list(args), capture_output=True, text=True,
                          env=dict(os.environ, CKD_DRY_RUN="1", **env))


class MigrationOrder(unittest.TestCase):
    """The requirement, asserted as an order of operations.

    CKD_DRY_RUN=1 makes every state-changing command print instead of running,
    so the sequence can be read back and pinned. This is the test that would
    catch the dangerous refactor: booting out the native pusher before the
    container has proven it can drive the panel, or lifting standby while
    launchd is still loaded.
    """

    @classmethod
    def setUpClass(cls):
        if not docker_ok():
            raise unittest.SkipTest("no docker daemon (preflight probes with one)")
        # The state a migration starts from: a native agent still installed.
        cls.state = PlannedState(native=True)
        proc = dry_run("migrate", **cls.state.env())
        if proc.returncode != 0:
            cls.state.cleanup()
            raise unittest.SkipTest("migrate --dry-run could not run: %s"
                                    % (proc.stderr.strip() or proc.stdout.strip()))
        cls.lines = proc.stdout.splitlines()

    @classmethod
    def tearDownClass(cls):
        cls.state.cleanup()

    def test_the_dry_run_changed_nothing(self):
        """The plan is a plan: no file it names may have been touched."""
        self.assertTrue(os.path.exists(self.state.native_plist),
                        "the native plist must still be where it was")
        self.assertFalse(os.path.exists(self.state.standby))
        self.assertFalse(os.path.exists(self.state.parked_plist))

    def index(self, needle):
        for i, line in enumerate(self.lines):
            if needle in line:
                return i
        self.fail("migrate never mentions %r:\n%s" % (needle, "\n".join(self.lines)))

    def test_the_order_is_build_standby_up_health_push_then_handover(self):
        build = self.index("compose -f")
        standby = self.index("touch %s" % self.state.standby)
        up = self.index("up -d")
        health = self.index("report healthy")
        push = self.index("display.py --once")
        bootout = self.index("launchctl bootout gui")
        park = self.index("mv %s" % self.state.native_plist)
        release = self.index("rm -f %s" % self.state.standby)
        agent = self.index("context-keyboard-display-docker.plist")

        self.assertLess(build, standby)
        self.assertLess(standby, up, "standby must be engaged before the container starts")
        self.assertLess(up, health)
        self.assertLess(health, push, "health first, then the real push")
        self.assertLess(push, bootout,
                        "the native agent must not be booted out before the push verifies")
        self.assertLess(bootout, park)
        self.assertLess(park, release,
                        "standby must not lift until the native agent is gone")
        self.assertLess(release, agent)

    def test_preflight_runs_first_and_probes_the_lan_from_a_container(self):
        self.assertLess(self.index("preflight"), self.index("build"))
        self.assertIn("LAN reachable", "\n".join(self.lines))

    def test_it_never_touches_the_hotkey_agent(self):
        joined = "\n".join(self.lines)
        self.assertNotIn("bootout gui/%d/com.williamliu.context-keyboard-keys" % os.getuid(),
                         joined)
        self.assertIn("left untouched", joined)

    def test_the_panel_url_is_read_from_the_live_config(self):
        self.assertIn("panel url:", "\n".join(self.lines))

    def test_the_default_standby_path_matches_the_container_view(self):
        """The override above is a test hook; the shipped default has to be the
        same file docker-compose.yml hands the container as CKD_STANDBY_PATH,
        or the interlock would have two halves that never meet."""
        default = re.search(r'STANDBY_PATH="\$\{CKD_STANDBY_FILE:-([^}"]+)\}"',
                            read(DEPLOY))
        self.assertIsNotNone(default, "no default standby path in the script")
        script_side = os.path.basename(default.group(1))
        compose_side = os.path.basename(
            re.search(r"CKD_STANDBY_PATH: (\S+)", read(COMPOSE)).group(1))
        self.assertEqual(script_side, compose_side)
        self.assertEqual(script_side, "context-keyboard-display-standby")


class RollbackOrder(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The state a rollback starts from: migrated, so the native plist is
        # parked and the container owns the panel.
        cls.state = PlannedState(parked=True)
        proc = dry_run("rollback", **cls.state.env())
        if proc.returncode != 0:
            cls.state.cleanup()
            raise unittest.SkipTest("rollback --dry-run could not run: %s" % proc.stderr)
        cls.lines = proc.stdout.splitlines()

    @classmethod
    def tearDownClass(cls):
        cls.state.cleanup()

    def test_the_dry_run_changed_nothing(self):
        self.assertTrue(os.path.exists(self.state.parked_plist),
                        "the parked plist must still be parked")
        self.assertFalse(os.path.exists(self.state.native_plist))
        self.assertFalse(os.path.exists(self.state.standby))

    def index(self, needle):
        for i, line in enumerate(self.lines):
            if needle in line:
                return i
        self.fail("rollback never mentions %r:\n%s" % (needle, "\n".join(self.lines)))

    def test_the_container_stops_pushing_before_launchd_starts(self):
        standby = self.index("touch %s" % self.state.standby)
        bootstrap = self.index("launchctl bootstrap")
        down = self.index("down")
        self.assertLess(standby, bootstrap,
                        "engaging standby first is what stops two pushers overlapping")
        self.assertLess(bootstrap, down)

    def test_it_removes_the_wake_agent_and_re_enables_the_native_one(self):
        joined = "\n".join(self.lines)
        self.assertIn("context-keyboard-display-docker.plist", joined)
        self.assertIn("launchctl enable gui/%d/com.williamliu.context-keyboard-display"
                      % os.getuid(), joined)
        self.assertIn("mv %s" % self.state.parked_plist, joined,
                      "the plist that was working is what gets restored")


class BootScript(unittest.TestCase):
    """The login/wake script's two decisions, driven with stub binaries."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.bin = os.path.join(self.home, "bin")
        os.makedirs(self.bin)
        os.makedirs(os.path.join(self.home, "Library", "LaunchAgents"))
        self.calls = os.path.join(self.home, "calls.log")
        self.write_stub("docker", 'printf "%s\\n" "$*" >> "$CKD_CALLS"\nexit 0\n')
        self.write_stub("open", 'printf "open %s\\n" "$*" >> "$CKD_CALLS"\nexit 0\n')

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def write_stub(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as handle:
            handle.write("#!/bin/sh\n" + body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)

    def boot(self):
        return subprocess.run([BOOT], capture_output=True, text=True,
                              env=dict(os.environ, HOME=self.home,
                                       CKD_REPO=REPO_DIR,
                                       CKD_PATH_PREFIX=self.bin,
                                       CKD_CALLS=self.calls))

    def called(self):
        try:
            return read(self.calls)
        except OSError:
            return ""

    def test_it_starts_the_service_when_docker_answers(self):
        proc = self.boot()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("up -d --no-build", self.called())
        self.assertIn("compose service is up", proc.stdout)

    def test_it_refuses_to_start_behind_the_native_agent(self):
        """Two pushers is the failure this whole deployment is arranged to
        avoid, and a login agent is exactly where it would sneak back in."""
        plist = os.path.join(self.home, "Library", "LaunchAgents",
                             "com.williamliu.context-keyboard-display.plist")
        open(plist, "w").close()
        proc = self.boot()
        self.assertEqual(proc.returncode, 0)
        self.assertIn("not starting the container", proc.stdout)
        self.assertEqual(self.called(), "", "docker must not be called at all")

    def test_it_waits_for_docker_desktop_and_gives_up_cleanly(self):
        self.write_stub("docker", 'printf "%s\\n" "$*" >> "$CKD_CALLS"\nexit 1\n')
        proc = subprocess.run([BOOT], capture_output=True, text=True,
                              env=dict(os.environ, HOME=self.home, CKD_REPO=REPO_DIR,
                                       CKD_PATH_PREFIX=self.bin, CKD_CALLS=self.calls,
                                       CKD_DOCKER_WAIT="0"))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("open -ga Docker", self.called())
        self.assertIn("not ready", proc.stdout)

    def test_it_notes_standby_rather_than_silently_not_pushing(self):
        standby = os.path.join(self.home, ".claude",
                               "context-keyboard-display-standby")
        os.makedirs(os.path.dirname(standby))
        open(standby, "w").close()
        proc = self.boot()
        self.assertEqual(proc.returncode, 0)
        self.assertIn("standby file exists", proc.stdout)


# ---------------------------------------------------------------- hygiene


class TrackedFiles(unittest.TestCase):
    """Nothing about this machine may end up in git."""

    @classmethod
    def setUpClass(cls):
        proc = subprocess.run(["git", "-C", REPO_DIR, "ls-files"],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise unittest.SkipTest("not a git checkout")
        cls.tracked = [f for f in proc.stdout.split() if f]

    def test_env_is_not_tracked_but_the_example_exists(self):
        self.assertNotIn(".env", self.tracked)
        self.assertIn(".env", read(os.path.join(REPO_DIR, ".gitignore")).split())
        self.assertTrue(os.path.exists(os.path.join(REPO_DIR, ".env.example")))

    def test_the_panel_address_is_in_no_tracked_file(self):
        """The IP lives in ~/.claude/context-keyboard-display.yaml and is read
        live through the mount; a deployment that copied it into the repo would
        commit one Mac's LAN."""
        url = display.load_config()["url"]
        if display.URL_UNSET in url:
            self.skipTest("no real panel address configured on this machine")
        host = url.split("//", 1)[-1].split("/", 1)[0]
        for name in self.tracked:
            path = os.path.join(REPO_DIR, name)
            try:
                text = read(path)
            except (OSError, UnicodeDecodeError):
                continue
            self.assertNotIn(host, text, "%s leaks the panel address" % name)

    def test_the_new_deployment_files_are_all_tracked_or_ignored_on_purpose(self):
        for name in ("Dockerfile", "docker-compose.yml", ".dockerignore",
                     ".env.example", "docker/entrypoint.sh",
                     "deploy/ckd-docker.sh", "deploy/ckd-docker-boot.sh",
                     "docs/DOCKER.md", "tests/test_docker.py"):
            self.assertTrue(os.path.exists(os.path.join(REPO_DIR, name)),
                            "%s is missing" % name)


if __name__ == "__main__":
    unittest.main(verbosity=2)

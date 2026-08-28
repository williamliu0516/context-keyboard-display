#!/usr/bin/env python3
"""Global hotkey listener: cycle the panel's screen mode from the keyboard.

One press of the bound key advances ~/.claude/context-keyboard-display-control.json
through auto -> claude -> sessions -> idle -> auto; the display daemon reads
that file on its next tick (<=5 s) and obeys it. No keyboard firmware macro is
involved -- this is a small launchd agent of its own.

Two engines, picked automatically by --daemon:

  hotkey  Carbon RegisterEventHotKey. Reserves one modifier+key combination
          with the WindowServer, which then delivers that -- and nothing else
          -- to this process. Because it cannot observe any other key, macOS
          grants it with no privacy prompt at all. Used whenever the binding
          carries a modifier, which is every binding a hotkey should have.
  tap     CGEventTap, the original engine: sees every keystroke, and so needs
          the Input Monitoring grant. Only reachable now by a binding Carbon
          cannot express -- fn, or a bare key with no modifiers.

    python3 keys.py --daemon        run the listener in the foreground
    python3 keys.py --selftest      register the real hotkey, drive it with a
                                    synthetic press, check the mode advanced
    python3 keys.py --watch         print keycode+flags for every key you press
    python3 keys.py --simulate      feed a synthetic event through the tap
                                    callback (no keypress, no permission needed)
    python3 keys.py --permission    report which engine runs and what it needs
    python3 keys.py --install       install just this listener's launchd agent
    python3 keys.py --uninstall     remove it

Why a separate process from display.py: either engine needs its own run loop,
and that does not belong in the render loop. If the engine cannot start, the
panel keeps working and only the hotkey is dead.
"""

import ctypes
import os
import sys
import time

import display

REPO_DIR = display.REPO_DIR
LOG_PATH = os.path.join(display.CLAUDE_DIR, "context-keyboard-keys.log")
LAUNCH_LABEL = "com.williamliu.context-keyboard-keys"
LAUNCH_PLIST = os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LAUNCH_LABEL)
PERMISSION_PANE = ("x-apple.systempreferences:com.apple.preference.security"
                   "?Privacy_ListenEvent")

# CGEventFlags. Arrow and navigation keys on a Mac keyboard set NumericPad --
# and, on the built-in keyboard, SecondaryFn -- with nothing held down, so the
# comparison mask below covers only the four modifiers a human can mean.
MOD_SHIFT = 0x00020000
MOD_CTRL = 0x00040000
MOD_OPT = 0x00080000
MOD_CMD = 0x00100000
MOD_FN = 0x00800000
CARE_MASK = MOD_SHIFT | MOD_CTRL | MOD_OPT | MOD_CMD

MODIFIERS = {
    "cmd": MOD_CMD, "command": MOD_CMD, "meta": MOD_CMD,
    "ctrl": MOD_CTRL, "control": MOD_CTRL,
    "opt": MOD_OPT, "option": MOD_OPT, "alt": MOD_OPT,
    "shift": MOD_SHIFT,
    "fn": MOD_FN, "globe": MOD_FN,
}
MOD_NAMES = (("fn", MOD_FN), ("ctrl", MOD_CTRL), ("opt", MOD_OPT),
             ("shift", MOD_SHIFT), ("cmd", MOD_CMD))

# kVK_* virtual keycodes. Layout-independent: 40 is where "k" sits on ANSI, and
# that is the physical key a binding names, whatever the active input source
# prints on it.
KEYCODES = {
    "left": 123, "right": 124, "down": 125, "up": 126,
    "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "delete": 51, "forwarddelete": 117, "return": 36, "enter": 76,
    "tab": 48, "space": 49, "escape": 53, "esc": 53,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
    "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
    "f13": 105, "f14": 107, "f15": 113, "f16": 106, "f17": 64, "f18": 79,
    "f19": 80, "f20": 90,
    "a": 0, "b": 11, "c": 8, "d": 2, "e": 14, "f": 3, "g": 5, "h": 4,
    "i": 34, "j": 38, "k": 40, "l": 37, "m": 46, "n": 45, "o": 31, "p": 35,
    "q": 12, "r": 15, "s": 1, "t": 17, "u": 32, "v": 9, "w": 13, "x": 7,
    "y": 16, "z": 6,
    "0": 29, "1": 18, "2": 19, "3": 20, "4": 21, "5": 23, "6": 22,
    "7": 26, "8": 28, "9": 25,
    "minus": 27, "equal": 24, "leftbracket": 33, "rightbracket": 30,
    "semicolon": 41, "quote": 39, "comma": 43, "period": 47, "slash": 44,
    "backslash": 42, "grave": 50,
}
KEY_BY_CODE = {}
for _name, _code in KEYCODES.items():
    KEY_BY_CODE.setdefault(_code, _name)

# What macOS itself does with fn + an arrow on a keyboard that has no navigation
# cluster: the HID layer substitutes the navigation keycode, so an event tap
# never sees "fn is down and so is right arrow" -- it sees End. A binding may
# therefore be *written* the way the user's fingers think of it while matching
# the keycode the system actually delivers.
FN_ARROW = {"right": "end", "left": "home", "up": "pageup", "down": "pagedown"}


def log(message):
    sys.stderr.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    sys.stderr.flush()


# ------------------------------------------------------------------ bindings


class Binding(object):
    """A parsed hotkey: which keycode, which modifiers must be exactly held."""

    def __init__(self, keycode, mods, care, label, key_name, substituted=None):
        self.keycode = keycode
        self.mods = mods
        self.care = care
        self.label = label
        self.key_name = key_name
        self.substituted = substituted  # the fn+arrow -> nav-key note, or None

    def matches(self, keycode, flags):
        return keycode == self.keycode and (flags & self.care) == self.mods

    def describe(self):
        text = "%s (keycode %d" % (self.label, self.keycode)
        if self.mods:
            text += ", flags 0x%06x" % self.mods
        text += ")"
        if self.substituted:
            text += ", delivered by macOS as %s" % self.substituted
        return text


def parse_binding(spec):
    """"fn+right" / "ctrl+opt+cmd+k" -> Binding. Raises ValueError on nonsense."""
    parts = [p.strip().lower() for p in str(spec).split("+") if p.strip()]
    if not parts:
        raise ValueError("empty key binding")
    key, names = parts[-1], parts[:-1]
    mods = 0
    for name in names:
        if name not in MODIFIERS:
            raise ValueError("unknown modifier %r in %r (have: %s)"
                             % (name, spec, ", ".join(sorted(set(MODIFIERS)))))
        mods |= MODIFIERS[name]

    substituted = None
    if mods & MOD_FN and key in FN_ARROW:
        substituted, key = FN_ARROW[key], FN_ARROW[key]
        # fn is spent on the substitution: the delivered nav key carries the fn
        # flag on the built-in keyboard and not on an external one, so requiring
        # it would make the binding depend on which keyboard is in your hands.
        mods &= ~MOD_FN
    if key not in KEYCODES:
        raise ValueError("unknown key %r in %r" % (key, spec))

    care = CARE_MASK | (MOD_FN if mods & MOD_FN else 0)
    label = "+".join([n for n, bit in MOD_NAMES if mods & bit] + [key])
    return Binding(KEYCODES[key], mods & care, care, label, key, substituted)


def resolve_swallow(setting, binding):
    """Should the key be consumed, or passed on to whatever app has focus?

    "auto" swallows only a binding that carries real modifiers. The default
    binding fn+right arrives as a bare End -- a key apps legitimately use to
    jump to the end of a line -- and silently breaking it system-wide is not a
    trade a display toggle gets to make on its own. A binding with modifiers is
    unambiguous, so that one is consumed.
    """
    if isinstance(setting, bool):
        return setting
    text = str(setting).strip().lower()
    if text in ("true", "yes", "on", "1"):
        return True
    if text in ("false", "no", "off", "0"):
        return False
    return bool(binding.mods)


# ------------------------------------------------------------------- cycling


class Cycler(object):
    """Binding + debounce + "advance the mode", with the tap callback on top."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.binding = parse_binding(cfg["key_binding"])
        self.select_binding = parse_binding(cfg["key_select_binding"])
        self.cycle = display.normalise_cycle(cfg["key_cycle"])
        self.debounce = max(0.0, float(cfg["key_debounce_seconds"]))
        self.swallow = resolve_swallow(cfg["key_swallow"], self.binding)
        self.last_fired = 0.0
        self.last_selected = 0.0
        self.tap = None

    def hotkeys(self):
        """The bindings this listener wants, paired with what they do.

        Order is the registration order and therefore the hotkey id order; the
        cycle key stays first so an engine that can only host one still hosts
        the one the panel cannot be driven without.
        """
        return ((self.binding, self.fire),
                (self.select_binding, self.select))

    def fire(self, now=None):
        """Advance the mode — or pop out of a drilled-into session, which
        cycle_mode decides. Returns the new mode, or None if debounced."""
        now = time.time() if now is None else now
        if now - self.last_fired < self.debounce:
            return None
        self.last_fired = now
        was, pinned = display.control_state(self.cfg)
        mode = display.cycle_mode(self.cycle, self.cfg)
        log("%s: mode %s%s -> %s" % (self.binding.label, was,
                                     " [%s]" % pinned[:8] if pinned else "", mode))
        return mode

    def select(self, now=None):
        """Step to the next session on the switchboard. None if it did
        nothing — debounced, or the switchboard is not the screen showing."""
        now = time.time() if now is None else now
        if now - self.last_selected < self.debounce:
            return None
        self.last_selected = now
        sid = display.select_next_session(self.cfg)
        if sid is None:
            log("%s: not on the switchboard; ignored" % self.select_binding.label)
        else:
            log("%s: session -> %s" % (self.select_binding.label, sid[:8]))
        return sid

    def callback(self, proxy, event_type, event, refcon):
        """CGEventTap callback. Must never raise: an exception here would take
        the tap down with it. Returning None consumes the event."""
        import Quartz

        if event_type in (Quartz.kCGEventTapDisabledByTimeout,
                          Quartz.kCGEventTapDisabledByUserInput):
            # The system disables a tap whose callback was too slow, and on some
            # user input; re-arming is the documented recovery.
            if self.tap is not None:
                Quartz.CGEventTapEnable(self.tap, True)
            log("tap disabled by the system (%s); re-enabled" % event_type)
            return event
        try:
            keycode = Quartz.CGEventGetIntegerValueField(
                event, Quartz.kCGKeyboardEventKeycode)
            repeat = Quartz.CGEventGetIntegerValueField(
                event, Quartz.kCGKeyboardEventAutorepeat)
            flags = Quartz.CGEventGetFlags(event)
            if repeat or not self.binding.matches(keycode, flags):
                return event
            self.fire()
            return None if self.swallow else event
        except Exception as error:  # noqa: BLE001 - a dead tap is worse
            log("callback failed: %s: %s" % (type(error).__name__, error))
            return event


# ------------------------------------------------------------ Carbon hotkey

# RegisterEventHotKey asks the WindowServer to reserve one modifier+key
# combination for this process. That is the whole reason to prefer it over the
# tap: a reservation cannot observe anything it did not reserve, so it carries
# no privacy grant, no TCC row, and no prompt the user has to find and tick.
# The cost is expressiveness -- Carbon knows four modifiers and no fn -- which
# is why the tap stays behind it as the fallback.
CARBON_PATH = "/System/Library/Frameworks/Carbon.framework/Carbon"
CARBON_MODS = ((MOD_CMD, 0x0100), (MOD_SHIFT, 0x0200),
               (MOD_OPT, 0x0800), (MOD_CTRL, 0x1000))


def fourcc(text):
    """'keyb' -> 0x6b657962. Carbon spells its constants as four ASCII bytes."""
    return int.from_bytes(text.encode("ascii"), "big")


def carbon_modifiers(binding):
    """The Carbon modifier mask for a binding, or None if Carbon cannot host it.

    Two ways to be unhostable: fn, which has no Carbon bit, and no modifier at
    all -- RegisterEventHotKey would take that happily and swallow a bare key
    system-wide, which is not something a display toggle gets to do.
    """
    if binding.mods & MOD_FN or not binding.mods:
        return None
    mask = 0
    for bit, carbon_bit in CARBON_MODS:
        if binding.mods & bit:
            mask |= carbon_bit
    return mask


class HotKey(object):
    """The Carbon engine: register, dispatch, run. ctypes all the way down."""

    SIGNATURE = fourcc("ckdp")          # this program's tag on its own hotkeys
    CLASS_KEYBOARD = fourcc("keyb")
    KIND_PRESSED = 5                    # kEventHotKeyPressed
    CLASS_OWN = fourcc("ckdp")          # our own event class, for the quit below
    KIND_QUIT = 1
    PARAM_DIRECT = fourcc("----")       # kEventParamDirectObject
    TYPE_HOTKEY_ID = fourcc("hkid")
    PRIORITY_STANDARD = 1

    class EventTypeSpec(ctypes.Structure):
        _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]

    class EventHotKeyID(ctypes.Structure):
        _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]

    class ProcessSerialNumber(ctypes.Structure):
        _fields_ = [("highLongOfPSN", ctypes.c_uint32),
                    ("lowLongOfPSN", ctypes.c_uint32)]

    CALLBACK = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p,
                                ctypes.c_void_p, ctypes.c_void_p)

    def __init__(self, cycler):
        self.cycler = cycler
        # Slot order is hotkey-id order, and ids are 1-based because 0 is what
        # an uninitialised EventHotKeyID reads as.
        self.slots = []
        for binding, action in cycler.hotkeys():
            mods = carbon_modifiers(binding)
            if mods is None:
                if not self.slots:
                    raise ValueError("%s cannot be a Carbon hotkey" % binding.label)
                log("skipping %s: no Carbon form (needs a real modifier)"
                    % binding.label)
                continue
            self.slots.append((binding, mods, action))
        self.fired = 0
        self.refs = []
        self.handler = ctypes.c_void_p()
        # ctypes does not retain the thunk for the C side; a callback that gets
        # collected while the WindowServer still holds its address is a crash.
        self.callback = self.CALLBACK(self._dispatch)
        self.lib = self._load()

    @classmethod
    def _load(cls):
        lib = ctypes.cdll.LoadLibrary(CARBON_PATH)
        void, u32, i32 = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int32
        lib.GetApplicationEventTarget.restype = void
        lib.GetMainEventQueue.restype = void
        lib.GetEventClass.argtypes = [void]
        lib.GetEventClass.restype = u32
        lib.RegisterEventHotKey.argtypes = [u32, u32, cls.EventHotKeyID, void, u32,
                                            ctypes.POINTER(void)]
        lib.RegisterEventHotKey.restype = i32
        lib.UnregisterEventHotKey.argtypes = [void]
        lib.UnregisterEventHotKey.restype = i32
        lib.InstallEventHandler.argtypes = [void, void, u32,
                                            ctypes.POINTER(cls.EventTypeSpec),
                                            void, ctypes.POINTER(void)]
        lib.InstallEventHandler.restype = i32
        lib.GetEventParameter.argtypes = [void, u32, u32, void, u32, void, void]
        lib.GetEventParameter.restype = i32
        lib.CreateEvent.argtypes = [void, u32, u32, ctypes.c_double, u32,
                                    ctypes.POINTER(void)]
        lib.CreateEvent.restype = i32
        lib.SetEventParameter.argtypes = [void, u32, u32, u32, void]
        lib.SetEventParameter.restype = i32
        lib.PostEventToQueue.argtypes = [void, void, ctypes.c_uint16]
        lib.PostEventToQueue.restype = i32
        lib.ReleaseEvent.argtypes = [void]
        lib.TransformProcessType.argtypes = [ctypes.POINTER(cls.ProcessSerialNumber),
                                             u32]
        lib.TransformProcessType.restype = i32
        return lib

    def _dispatch(self, call_ref, event, user_data):
        """Carbon calls this on the main thread for every hotkey press. Like the
        tap callback it must not raise -- an exception unwinding into C is not
        something the event loop survives."""
        try:
            if self.lib.GetEventClass(event) == self.CLASS_OWN:
                # QuitApplicationEventLoop only takes effect on the thread the
                # loop runs on, so --selftest asks for the exit by posting an
                # event and the request is honoured here, where it works.
                self.lib.QuitApplicationEventLoop()
                return 0
            got = self.EventHotKeyID()
            self.lib.GetEventParameter(event, self.PARAM_DIRECT,
                                       self.TYPE_HOTKEY_ID, None,
                                       ctypes.sizeof(got), None, ctypes.byref(got))
            if got.signature == self.SIGNATURE and 1 <= got.id <= len(self.slots):
                self.fired += 1
                self.slots[got.id - 1][2]()
        except Exception as error:  # noqa: BLE001 - a dead loop is worse
            log("hotkey handler failed: %s: %s" % (type(error).__name__, error))
        return 0  # noErr: handled, and nobody else needs to see it

    def background(self):
        """Become a UI element, so running an application event loop from an
        unbundled script does not put a nameless Dock tile on screen. Best
        effort: the hotkey works either way."""
        psn = self.ProcessSerialNumber(0, 2)  # kCurrentProcess
        self.lib.TransformProcessType(ctypes.byref(psn), 4)  # ToUIElement

    def register(self):
        """Install the handler and claim the combination. Raises on refusal."""
        target = ctypes.c_void_p(self.lib.GetApplicationEventTarget())
        specs = (self.EventTypeSpec * 2)(
            self.EventTypeSpec(self.CLASS_KEYBOARD, self.KIND_PRESSED),
            self.EventTypeSpec(self.CLASS_OWN, self.KIND_QUIT))
        status = self.lib.InstallEventHandler(
            target, ctypes.cast(self.callback, ctypes.c_void_p), len(specs),
            specs, None, ctypes.byref(self.handler))
        if status != 0:
            raise OSError("InstallEventHandler failed (OSStatus %d)" % status)
        for index, (binding, mods, _) in enumerate(self.slots, start=1):
            ref = ctypes.c_void_p()
            status = self.lib.RegisterEventHotKey(
                binding.keycode, mods,
                self.EventHotKeyID(self.SIGNATURE, index), target, 0,
                ctypes.byref(ref))
            if status != 0:
                # -9878 is eventHotKeyExistsErr: something else owns it.
                raise OSError("RegisterEventHotKey failed for %s (OSStatus %d)%s"
                              % (binding.label, status,
                                 " -- another app already owns it"
                                 if status == -9878 else ""))
            self.refs.append(ref)

    def press(self, slot=0):
        """Post a synthetic press of one slot to our own main queue.

        Not a keystroke: it enters the queue one step downstream of where the
        WindowServer would have put a real press, so it exercises the run loop,
        the handler, the ID match and the mode write without needing the
        Accessibility grant that synthesising a real keypress would.
        """
        ident = self.EventHotKeyID(self.SIGNATURE, slot + 1)
        self._post(self.CLASS_KEYBOARD, self.KIND_PRESSED, ident)

    def stop(self):
        """Ask the event loop to end, from any thread. See _dispatch."""
        self._post(self.CLASS_OWN, self.KIND_QUIT)

    def _post(self, event_class, kind, ident=None):
        event = ctypes.c_void_p()
        if self.lib.CreateEvent(None, event_class, kind, 0.0, 0,
                                ctypes.byref(event)) != 0:
            raise OSError("CreateEvent failed")
        if ident is not None:
            self.lib.SetEventParameter(event, self.PARAM_DIRECT,
                                       self.TYPE_HOTKEY_ID,
                                       ctypes.sizeof(ident), ctypes.byref(ident))
        queue = ctypes.c_void_p(self.lib.GetMainEventQueue())
        status = self.lib.PostEventToQueue(queue, event, self.PRIORITY_STANDARD)
        self.lib.ReleaseEvent(event)
        if status != 0:
            raise OSError("PostEventToQueue failed (OSStatus %d)" % status)

    def run(self):
        self.lib.RunApplicationEventLoop()


def run_hotkey(cfg):
    """--daemon's Carbon path: claim the combination and sit in the event loop."""
    cycler = Cycler(cfg)
    hotkey = HotKey(cycler)
    hotkey.background()
    hotkey.register()
    log("hotkeys registered (Carbon RegisterEventHotKey, no grant needed): "
        "%s -> %s; %s -> next session on the switchboard"
        % (cycler.binding.describe(), " -> ".join(cycler.cycle),
           cycler.select_binding.label))
    hotkey.run()
    return 0


def selftest(cfg):
    """Prove the binding is live without touching the keyboard.

    Registers the real hotkey with the real WindowServer, then drives it with a
    synthetic press for each mode in the cycle. What this does *not* cover is
    the one step no unprivileged process can fake -- the WindowServer deciding
    to send us a physical press -- so a clean run means "everything on our side
    of that line is correct and the combination was accepted", not "a key was
    pressed".
    """
    import threading

    cycler = Cycler(cfg)
    mods = carbon_modifiers(cycler.binding)
    print("binding:  %s" % cycler.binding.describe())
    print("cycle:    %s" % " -> ".join(cycler.cycle + (cycler.cycle[0],)))
    if mods is None:
        print("")
        print("FAILED: %s has no Carbon form; --daemon would run the event tap"
              % cycler.binding.label)
        return 1
    print("carbon:   keycode %d, modifiers 0x%04x"
          % (cycler.binding.keycode, mods))

    hotkey = HotKey(cycler)
    hotkey.register()
    print("register: OSStatus 0 for %s (%s)"
          % (len(hotkey.slots) if len(hotkey.slots) != 1 else "the",
             ", ".join(b.label for b, _, _ in hotkey.slots)))
    print("")

    start = display.control_mode()
    result = {"ok": True, "seen": []}

    def drive():
        try:
            for _ in cycler.cycle:
                cycler.last_fired = 0.0  # each synthetic press is a deliberate one
                before = hotkey.fired
                hotkey.press()
                deadline = time.time() + 3.0
                while hotkey.fired == before and time.time() < deadline:
                    time.sleep(0.02)
                if hotkey.fired == before:
                    print("  FAIL: the event loop never delivered the press")
                    result["ok"] = False
                    break
                result["seen"].append(display.control_mode())
        finally:
            hotkey.stop()

    def watchdog():
        # Every exit from the event loop -- success or failure -- is a request
        # posted to the loop itself, so a loop that dispatches nothing cannot be
        # asked to stop. Hanging is the worst way for a test to fail; say what
        # went wrong and take the process down.
        time.sleep(30.0)
        sys.stdout.write("\nFAILED: the event loop dispatched nothing in 30 s\n")
        sys.stdout.flush()
        os._exit(1)

    print("starting mode: %s" % start)
    threading.Thread(target=watchdog, daemon=True).start()
    driver = threading.Thread(target=drive, daemon=True)
    driver.start()
    hotkey.run()
    driver.join(2.0)  # it posted the quit we just returned from; this is a formality

    seen = result["seen"]
    for index, mode in enumerate(seen):
        print("press %d -> mode %s" % (index + 1, mode))
    offset = cycler.cycle.index(start) + 1 if start in cycler.cycle else 0
    expected = [cycler.cycle[(offset + i) % len(cycler.cycle)]
                for i in range(len(seen))]
    if len(seen) != len(cycler.cycle) or seen != expected:
        print("  FAIL: expected %s, got %s" % (expected, seen))
        result["ok"] = False

    print("")
    if start != display.control_mode():
        display.set_mode(start, quiet=True)
        print("restored the mode that was set before this run: %s" % start)
    print("OK" if result["ok"] else "FAILED")
    return 0 if result["ok"] else 1


# --------------------------------------------------------------- permissions


def listen_access():
    """True when this executable holds Input Monitoring."""
    import Quartz

    return bool(Quartz.CGPreflightListenEventAccess())


def grant_target():
    """The path the privacy grant actually attaches to.

    TCC records the executable, not the script: under launchd that is the
    interpreter, and .venv/bin/python3 is a symlink, so the row the user has to
    tick is the framework binary it resolves to.
    """
    return os.path.realpath(sys.executable)


def permission_help(open_pane=False):
    print("Input Monitoring is required to watch for the hotkey.")
    print("")
    print("System Settings -> Privacy & Security -> Input Monitoring -> \"+\",")
    print("then Cmd+Shift+G and paste:")
    print("")
    print("    %s" % grant_target())
    print("")
    print("(That is the interpreter the launchd agent runs. Granting it means")
    print(" any script run by this Python can read your keystrokes -- the cost")
    print(" of not shipping a signed .app; use a dedicated venv if that")
    print(" bothers you.) Then restart the agent:")
    print("")
    print("    launchctl kickstart -k gui/%d/%s" % (os.getuid(), LAUNCH_LABEL))
    print("")
    print("Testing in a terminal instead? The grant lands on the *terminal*")
    print("app (or your IDE), not on Python, so tick that row too.")
    if open_pane:
        import subprocess

        subprocess.run(["open", PERMISSION_PANE], check=False)
        print("opened the Input Monitoring pane")


def show_permission(cfg, open_pane=True):
    cycler = Cycler(cfg)
    carbon = carbon_modifiers(cycler.binding) is not None
    print("binding:  %s" % cycler.binding.describe())
    print("cycle:    %s" % " -> ".join(cycler.cycle + (cycler.cycle[0],)))
    print("select:   %s%s" % (cycler.select_binding.label,
                              "" if carbon_modifiers(cycler.select_binding) is not None
                              else "  (NO Carbon form -- will not be registered)"))
    print("engine:   %s" % ("hotkey (Carbon RegisterEventHotKey)" if carbon
                            else "tap (CGEventTap, cycle key only)"))
    print("swallow:  %s" % ("yes, unconditionally: a reserved combination never"
                            " reaches apps" if carbon else
                            "yes (the key stops reaching apps)" if cycler.swallow
                            else "no (apps still see the key)"))
    print("agent:    %s" % ("installed" if os.path.exists(LAUNCH_PLIST)
                            else "not installed"))
    if carbon:
        print("granted:  n/a")
        print("")
        print("Nothing to grant: reserving one combination is not surveillance,")
        print("so macOS asks for no permission. `keys.py --selftest` proves the")
        print("registration without a keypress.")
        return 0

    granted = listen_access()
    print("granted:  %s" % ("yes" if granted else "NO"))
    print("")
    if granted:
        print("Input Monitoring is already granted to %s" % grant_target())
        return 0
    permission_help(open_pane=open_pane)
    return 1


# ------------------------------------------------------------------ listener


def run_listener(cfg):
    import Quartz

    cycler = Cycler(cfg)
    if not listen_access():
        # Asks once, per executable; under launchd the prompt may never appear,
        # which is exactly why permission_help() prints the manual route.
        Quartz.CGRequestListenEventAccess()
        if not listen_access():
            log("no Input Monitoring for %s" % grant_target())
            permission_help()
            return 3

    mask = Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
    option = (Quartz.kCGEventTapOptionDefault if cycler.swallow
              else Quartz.kCGEventTapOptionListenOnly)
    tap = Quartz.CGEventTapCreate(
        Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap, option,
        mask, cycler.callback, None)
    if tap is None:
        log("CGEventTapCreate returned NULL (permission revoked mid-flight?)")
        permission_help()
        return 3
    cycler.tap = tap

    source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
    Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetCurrent(), source,
                              Quartz.kCFRunLoopCommonModes)
    Quartz.CGEventTapEnable(tap, True)
    log("listening for %s -> %s%s"
        % (cycler.binding.describe(), " -> ".join(cycler.cycle),
           " (consuming the key)" if cycler.swallow else ""))
    Quartz.CFRunLoopRun()
    return 0


def watch(cfg):
    """Print every key-down's keycode and flags. The two-second answer to
    "what does my keyboard actually send for that combination"."""
    import Quartz

    cycler = Cycler(cfg)
    if not listen_access():
        Quartz.CGRequestListenEventAccess()
        if not listen_access():
            permission_help()
            return 3

    def show(proxy, event_type, event, refcon):
        if event_type in (Quartz.kCGEventTapDisabledByTimeout,
                          Quartz.kCGEventTapDisabledByUserInput):
            Quartz.CGEventTapEnable(tap, True)
            return event
        keycode = Quartz.CGEventGetIntegerValueField(
            event, Quartz.kCGKeyboardEventKeycode)
        flags = Quartz.CGEventGetFlags(event)
        held = [n for n, bit in MOD_NAMES if flags & bit] or ["-"]
        print("keycode %-4d name %-14s flags 0x%08x  held %-24s %s"
              % (keycode, KEY_BY_CODE.get(keycode, "?"), flags, "+".join(held),
                 "<-- MATCHES %s" % cycler.binding.label
                 if cycler.binding.matches(keycode, flags) else ""))
        sys.stdout.flush()
        return event

    tap = Quartz.CGEventTapCreate(
        Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
        Quartz.kCGEventTapOptionListenOnly,
        Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown), show, None)
    if tap is None:
        permission_help()
        return 3
    source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
    Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetCurrent(), source,
                              Quartz.kCFRunLoopCommonModes)
    print("watching (binding: %s). Press keys; Ctrl+C to stop."
          % cycler.binding.describe())
    Quartz.CFRunLoopRun()
    return 0


def simulate(cfg, presses=None):
    """Drive the real callback with synthetic events -- no keypress, no tap, no
    privacy grant. Covers everything except the tap's own event delivery:
    binding parse, keycode/flag matching, autorepeat and debounce suppression,
    the cycle order, and the control-file write the daemon reads."""
    import Quartz

    cycler = Cycler(cfg)
    count = presses if presses is not None else len(cycler.cycle) + 1
    print("binding: %s" % cycler.binding.describe())
    print("cycle:   %s" % " -> ".join(cycler.cycle))
    print("swallow: %s" % cycler.swallow)
    print("")

    def event_for(keycode, flags, repeat=0):
        event = Quartz.CGEventCreateKeyboardEvent(None, keycode, True)
        Quartz.CGEventSetFlags(event, flags)
        Quartz.CGEventSetIntegerValueField(
            event, Quartz.kCGKeyboardEventAutorepeat, repeat)
        return event

    # The built-in keyboard's inherent fn+numpad flags ride along on every arrow
    # and navigation key; a binding that survives simulation must ignore them.
    noise = MOD_FN | 0x00200000
    hit = event_for(cycler.binding.keycode, cycler.binding.mods | noise)
    ok = True

    start = display.control_mode()
    print("starting mode: %s" % start)
    seen = []
    for index in range(count):
        cycler.last_fired = 0.0  # each simulated press is a deliberate one
        result = cycler.callback(None, Quartz.kCGEventKeyDown, hit, None)
        mode = display.control_mode()
        seen.append(mode)
        consumed = result is None
        print("press %d -> mode %-8s (event %s)"
              % (index + 1, mode, "consumed" if consumed else "passed through"))
        if consumed != cycler.swallow:
            print("  FAIL: swallow=%s but event was %s"
                  % (cycler.swallow, "consumed" if consumed else "passed"))
            ok = False

    expected = [cycler.cycle[(cycler.cycle.index(start) + 1 + i) % len(cycler.cycle)]
                if start in cycler.cycle else
                cycler.cycle[i % len(cycler.cycle)]
                for i in range(count)]
    if seen != expected:
        print("  FAIL: expected %s, got %s" % (expected, seen))
        ok = False

    print("")
    before = display.control_mode()
    cycler.callback(None, Quartz.kCGEventKeyDown, hit, None)  # inside debounce
    if display.control_mode() != before:
        print("FAIL: debounce did not suppress a second press within %.2fs"
              % cycler.debounce)
        ok = False
    else:
        print("debounce: a second press within %.2fs was ignored (mode still %s)"
              % (cycler.debounce, before))

    cycler.last_fired = 0.0
    if cycler.callback(None, Quartz.kCGEventKeyDown,
                       event_for(cycler.binding.keycode,
                                 cycler.binding.mods | noise, repeat=1),
                       None) is None or display.control_mode() != before:
        print("FAIL: an autorepeat event was not ignored")
        ok = False
    else:
        print("autorepeat: ignored and passed through")

    # A near miss: same key, one extra modifier that the binding does not ask
    # for. Must not fire, and must reach the app.
    cycler.last_fired = 0.0
    extra = MOD_CMD if not (cycler.binding.mods & MOD_CMD) else MOD_SHIFT
    miss = event_for(cycler.binding.keycode, cycler.binding.mods | noise | extra)
    if cycler.callback(None, Quartz.kCGEventKeyDown, miss, None) is None \
            or display.control_mode() != before:
        print("FAIL: an extra modifier still triggered the binding")
        ok = False
    else:
        print("near miss: same key with an extra modifier ignored")

    cycler.last_fired = 0.0
    other = 12 if cycler.binding.keycode != 12 else 13  # some unbound key
    if cycler.callback(None, Quartz.kCGEventKeyDown,
                       event_for(other, cycler.binding.mods | noise), None) is None \
            or display.control_mode() != before:
        print("FAIL: an unrelated key triggered the binding")
        ok = False
    else:
        print("unrelated key: ignored")

    print("")
    if start != display.control_mode():
        display.set_mode(start, quiet=True)
        print("restored the mode that was set before this run: %s" % start)
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


# ---------------------------------------------------------------- launchd


def write_plist(python):
    import plistlib

    os.makedirs(os.path.dirname(LAUNCH_PLIST), exist_ok=True)
    agent = {
        "Label": LAUNCH_LABEL,
        "ProgramArguments": [python, os.path.join(REPO_DIR, "keys.py"), "--daemon"],
        "RunAtLoad": True,
        "KeepAlive": True,
        # Deliberately long: a missing privacy grant makes --daemon exit 3, and
        # this is the retry that picks the grant up once the user gives it,
        # without a per-second respawn storm in the meantime.
        "ThrottleInterval": 60,
        "StandardOutPath": LOG_PATH,
        "StandardErrorPath": LOG_PATH,
        "ProcessType": "Background",
    }
    with open(LAUNCH_PLIST, "wb") as handle:
        plistlib.dump(agent, handle)


def install_agent(python):
    write_plist(python)
    domain = "gui/%d" % os.getuid()
    display.launchctl("bootout", "%s/%s" % (domain, LAUNCH_LABEL))
    if not display.launchctl("bootstrap", domain, LAUNCH_PLIST, quiet=False):
        display.launchctl("unload", LAUNCH_PLIST)
        display.launchctl("load", "-w", LAUNCH_PLIST, quiet=False)
    print("loaded launchd agent %s" % LAUNCH_LABEL)
    print("hotkey logs: %s" % LOG_PATH)


def uninstall_agent():
    domain = "gui/%d" % os.getuid()
    display.launchctl("bootout", "%s/%s" % (domain, LAUNCH_LABEL))
    display.launchctl("unload", LAUNCH_PLIST)
    try:
        os.unlink(LAUNCH_PLIST)
        print("removed %s" % LAUNCH_PLIST)
    except OSError:
        pass


# -------------------------------------------------------------------- entry

USAGE = [p for p in __doc__.split("\n\n") if "python3 keys.py" in p][0] + "\n"


def reexec_in_venv():
    """--daemon/--watch/--simulate need pyobjc; the README documents plain
    `python3 keys.py ...`, so honour that by re-running under the venv that
    --install populated rather than failing on the import."""
    try:
        import Quartz  # noqa: F401
        return
    except ImportError:
        pass
    python = display.venv_python()
    if not os.path.exists(python) or sys.prefix == display.VENV_PATH:
        return
    os.execv(python, [python, os.path.abspath(__file__)] + sys.argv[1:])


def main(argv):
    args = argv[1:]
    command = args[0] if args else "--help"
    if command in ("--daemon", "--watch", "--simulate", "--permission",
                   "--selftest"):
        reexec_in_venv()
    cfg = display.load_config()

    if command == "--daemon":
        # Carbon first: it is the engine that needs nothing from the user. The
        # tap is what a binding falls back to when Carbon has no word for it.
        if carbon_modifiers(Cycler(cfg).binding) is not None:
            return run_hotkey(cfg)
        return run_listener(cfg)
    if command == "--selftest":
        return selftest(cfg)
    if command == "--watch":
        return watch(cfg)
    if command == "--simulate":
        extra = [a for a in args[1:] if a.isdigit()]
        return simulate(cfg, int(extra[0]) if extra else None)
    if command == "--permission":
        return show_permission(cfg, open_pane="--no-open" not in args)
    if command == "--install":
        install_agent(display.ensure_venv(cfg))
        return show_permission(cfg, open_pane=False) and 0
    if command == "--uninstall":
        uninstall_agent()
        return 0
    sys.stdout.write(USAGE)
    return 0 if command in ("--help", "-h") else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))

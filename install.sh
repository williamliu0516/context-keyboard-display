#!/bin/sh
# One-command install for the context-aware keyboard panel display.
#
#   curl -fsSL https://xiaweiliu.com/keyboard-display/install.sh | sh
#
# Or, skipping the prompt:
#
#   curl -fsSL https://xiaweiliu.com/keyboard-display/install.sh | PANEL_IP=192.168.1.50 sh
#
# Downloads this display and the claude-code-keyboard-status library it reads
# session state through into ~/.claude/context-keyboard-display/, writes a config
# pointing at your panel, registers the five session hooks in
# ~/.claude/settings.json (preserving every other setting and every other tool's
# hooks), and loads the launchd agents. Safe to re-run; that is also how you
# upgrade, and a re-run keeps the address you already configured.
#
#   PANEL_IP           the keyboard's address. Asked for interactively if unset.
#   CKD_SOURCE         where to fetch the display from. Default: this repo on
#                      raw.githubusercontent.com. Set it to a fork or a file://
#                      path; whitespace-separated for several, tried in order.
#   CKS_SOURCE         same, for the claude-code-keyboard-status library
#   DRY_RUN=1          fetch and verify everything, then stop before installing
#   INSTALL_DIR        where the files land (default ~/.claude/context-keyboard-display)
set -eu

CKD_BASES="${CKD_SOURCE:-}"
if [ -z "$CKD_BASES" ]; then
	CKD_BASES="https://raw.githubusercontent.com/williamliu0516/context-keyboard-display/main"
fi

CKS_BASES="${CKS_SOURCE:-}"
if [ -z "$CKS_BASES" ]; then
	CKS_BASES="https://raw.githubusercontent.com/williamliu0516/claude-code-keyboard-status/main"
fi

INSTALL_DIR="${INSTALL_DIR:-$HOME/.claude/context-keyboard-display}"
CONFIG_PATH="$HOME/.claude/context-keyboard-display.yaml"
UPSTREAM_CONFIG="$HOME/.claude/keyboard-status.json"

fail() {
	printf 'install: %s\n' "$*" >&2
	exit 1
}

note() { printf 'install: %s\n' "$*"; }

command -v python3 >/dev/null 2>&1 || fail "python3 is required. Install it and re-run."

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' ||
	fail "python3 3.9 or newer is required (found $(python3 -V 2>&1))."

python3 -c 'import venv' 2>/dev/null ||
	fail "python3 is missing the venv module. On Debian/Ubuntu: apt install python3-venv."

case "$(uname -s)" in
Darwin) ;;
*) fail "this display drives a macOS launchd agent and Claude Code's macOS hooks." ;;
esac

# A source can fail -- with CKD_SOURCE / CKS_SOURCE naming several, we fall
# through to the next -- so their diagnostics are suppressed. Only the final
# "nothing worked" message is shown.
if command -v curl >/dev/null 2>&1; then
	fetch() { curl -fsSL "$1" -o "$2" 2>/dev/null; }
elif command -v wget >/dev/null 2>&1; then
	fetch() { wget -qO "$2" "$1" 2>/dev/null; }
else
	fail "need curl or wget to download the display."
fi

tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/ckd-install.XXXXXX")" || fail "cannot create a temporary directory."
trap 'rm -rf "$tmp_dir"' EXIT INT TERM

# get <bases> <filename> <marker>
#
# The marker is the reason this is not just `curl -o`. A captive portal or a
# redirect to a login page answers 200 with HTML, which curl -f cannot catch;
# installing that would leave a login page on disk named display.py. Every file
# has to prove it is itself before it counts as downloaded.
get() {
	bases="$1"
	name="$2"
	marker="$3"
	for base in $bases; do
		fetch "$base/$name" "$tmp_dir/$name" || continue
		if grep -q "$marker" "$tmp_dir/$name" 2>/dev/null; then
			return 0
		fi
	done
	fail "could not download $name from any of:
$bases
If a repository is private, make it public or set CKD_SOURCE / CKS_SOURCE."
}

note "downloading the display..."
get "$CKD_BASES" display.py          'Context-aware display daemon'
get "$CKD_BASES" collect.py          'def collect_sessions'
get "$CKD_BASES" screens.py          'def claude_working'
get "$CKD_BASES" keys.py             'RegisterEventHotKey'
get "$CKD_BASES" config.example.yaml 'context-keyboard-display configuration'
get "$CKD_BASES" requirements.txt    'Pillow'

note "downloading the claude-code-keyboard-status library..."
get "$CKS_BASES" keyboard_status.py 'keyboard-status-state.json'
get "$CKS_BASES" pyproject.toml     'py-modules'

# ---------------------------------------------------------------- the address
#
# Asked for *after* the downloads, so a network or mirror failure does not waste
# the one question this installer gets to ask.
panel="${PANEL_IP:-}"
if [ -z "$panel" ]; then
	# stdin is the script itself under `curl | sh`, so a plain `read` would
	# consume the rest of this file (or return nothing at all). /dev/tty is the
	# controlling terminal regardless of what stdin was redirected to. Probe it
	# by actually opening it: it can exist and still not be usable, which is
	# exactly the case in CI and in a container.
	if { : </dev/tty; } 2>/dev/null; then
		printf '\n' >/dev/tty
		printf "What is your keyboard panel's IP address?\n" >/dev/tty
		printf '  (find it on the panel itself, or in your router'"'"'s client list)\n' >/dev/tty
		printf '  address: ' >/dev/tty
		IFS= read -r panel </dev/tty || panel=""
		printf '\n' >/dev/tty
	fi
fi

# Never guess. A wrong address fails as silence -- frames POSTed into the void --
# which is the single worst outcome this installer could produce.
[ -n "$panel" ] || fail "no panel address given.
Re-run with the address in the environment:

    curl -fsSL <this-url> | PANEL_IP=192.168.1.50 sh

(the interactive prompt needs a terminal, which this run did not have.)"

case "$panel" in
*" "*|*"	"*) fail "the address may not contain spaces: '$panel'" ;;
esac

# Accept a bare address, a hostname, or a full URL, and normalise to the endpoint
# the panel actually serves.
case "$panel" in
http://*|https://*) url="$panel" ;;
*) url="http://$panel/image/upload" ;;
esac
case "$url" in
*/image/upload) ;;
*/) url="${url}image/upload" ;;
*) url="$url/image/upload" ;;
esac

if [ "${DRY_RUN:-0}" != "0" ]; then
	note "DRY_RUN: verified $(ls "$tmp_dir" | wc -l | tr -d ' ') files, resolved url to $url"
	note "DRY_RUN: would install into $INSTALL_DIR"
	exit 0
fi

# ------------------------------------------------------------------- install
mkdir -p "$INSTALL_DIR" || fail "cannot create $INSTALL_DIR"
for f in display.py collect.py screens.py keys.py config.example.yaml \
	requirements.txt keyboard_status.py pyproject.toml; do
	cp "$tmp_dir/$f" "$INSTALL_DIR/$f" || fail "cannot write $INSTALL_DIR/$f"
done
note "installed 8 files into $INSTALL_DIR"

# The config is written before either --install runs, so no daemon ever starts
# against the placeholder address. A re-run keeps an address you already set
# unless PANEL_IP was passed explicitly, which is what makes upgrades safe.
CKD_URL="$url" CKD_CONFIG="$CONFIG_PATH" CKD_EXAMPLE="$INSTALL_DIR/config.example.yaml" \
	CKD_UPSTREAM_CONFIG="$UPSTREAM_CONFIG" CKD_DIR="$INSTALL_DIR" \
	CKD_FORCED="${PANEL_IP:+1}" python3 - <<'PYEOF'
import json, os, re

url = os.environ["CKD_URL"]
config = os.environ["CKD_CONFIG"]
example = os.environ["CKD_EXAMPLE"]
forced = bool(os.environ.get("CKD_FORCED"))
install_dir = os.environ["CKD_DIR"]

if not os.path.exists(config):
    with open(example) as src:
        text = src.read()
    action = "wrote"
else:
    with open(config) as src:
        text = src.read()
    current = re.search(r"^url:\s*(\S+)", text, re.M)
    if current and "PANEL-IP-NOT-SET" not in current.group(1) and not forced:
        print("install: keeping the address already in %s (%s)"
              % (config, current.group(1)))
        raise SystemExit(0)
    action = "updated"

text, n = re.subn(r"^url:.*$", "url: " + url, text, count=1, flags=re.M)
if not n:
    text = "url: %s\n%s" % (url, text)
# The library lives beside the display now, not in a sibling checkout.
text, n = re.subn(r"^upstream_path:.*$", "upstream_path: " + install_dir,
                  text, count=1, flags=re.M)
if not n:
    text += "\nupstream_path: %s\n" % install_dir
with open(config, "w") as dst:
    dst.write(text)
print("install: %s %s (url: %s)" % (action, config, url))

# The library keeps its own config; give it the same address so its own --once
# and --status work, even though the display boots out its pusher.
upstream = os.environ["CKD_UPSTREAM_CONFIG"]
try:
    with open(upstream) as handle:
        data = json.load(handle)
    data = data if isinstance(data, dict) else {}
except (OSError, ValueError):
    data = {}
if forced or "PANEL-IP-NOT-SET" in str(data.get("url", "")) or not data.get("url"):
    data["url"] = url
    tmp = upstream + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(data, handle, indent=2)
    os.replace(tmp, upstream)
PYEOF

# The library owns the session hooks and the state file this display reads, so
# it is installed first -- the display warns if the hooks are missing.
note "installing the session hooks..."
python3 "$INSTALL_DIR/keyboard_status.py" --install

note "installing the display..."
python3 "$INSTALL_DIR/display.py" --install

# ------------------------------------------------------- connectivity check
#
# A warning, never a failure: installing before the keyboard is plugged in is a
# perfectly normal order to do this in, and the daemon retries on its own.
host="$(printf '%s' "$url" | sed -e 's|^https\{0,1\}://||' -e 's|[:/].*$||')"
printf '\n'
if ping -c 1 -t 2 "$host" >/dev/null 2>&1; then
	note "panel at $host answers ping."
elif curl -fsS -m 3 -o /dev/null "$url" 2>/dev/null; then
	note "panel at $host answers HTTP."
else
	printf 'install: WARNING - no answer from %s yet.\n' "$host" >&2
	printf 'install: that is fine if the keyboard is not plugged in or awake.\n' >&2
	printf 'install: the daemon retries by itself; check with\n' >&2
	printf 'install:     tail -f ~/.claude/context-keyboard-display.log\n' >&2
	printf 'install: if the address is wrong, edit url in %s\n' "$CONFIG_PATH" >&2
fi

printf '\n'
note "done. The panel updates within about five seconds of a session changing."
note "logs:   tail -f ~/.claude/context-keyboard-display.log"
note "hotkey: Ctrl+Opt+Cmd+K cycles screens, Ctrl+Opt+Cmd+J walks the session list"

#!/bin/sh
# install.sh's platform selection, exercised on whatever host you are on.
#
#     sh tests/test_install_sh.sh
#
# DRY_RUN stops the installer after it has downloaded, verified and decided --
# which is exactly the part that had to learn about a second platform. The
# downloads come from this checkout over file://, so this needs no network and
# tests the working tree rather than what is on GitHub.
#
# What this cannot do: install anything, start systemd, or touch a keyboard.
# It proves the installer *chooses* correctly, and that the choice for Linux
# is reachable at all -- which before this change it was not, because the
# script rejected every non-Darwin host in its ninth line.
set -eu

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(dirname "$here")"
# The sibling library is fetched from wherever it is checked out; without it
# the download stage cannot complete and the platform tests are skipped.
sibling="${CKS_LOCAL:-$HOME/projects/claude-code-keyboard-status}"

pass=0
fail=0

check() {
	label="$1"
	haystack="$2"
	needle="$3"
	case "$haystack" in
	*"$needle"*)
		printf '  ok    %s\n' "$label"
		pass=$((pass + 1))
		;;
	*)
		printf '  FAIL  %s\n        wanted: %s\n' "$label" "$needle"
		fail=$((fail + 1))
		;;
	esac
}

absent() {
	label="$1"
	haystack="$2"
	needle="$3"
	case "$haystack" in
	*"$needle"*)
		printf '  FAIL  %s\n        must not say: %s\n' "$label" "$needle"
		fail=$((fail + 1))
		;;
	*)
		printf '  ok    %s\n' "$label"
		pass=$((pass + 1))
		;;
	esac
}

# ------------------------------------------------------------------ syntax

printf 'install.sh syntax\n'
if sh -n "$repo/install.sh"; then
	printf '  ok    parses under /bin/sh\n'
	pass=$((pass + 1))
else
	printf '  FAIL  does not parse\n'
	fail=$((fail + 1))
fi

if [ ! -f "$sibling/keyboard_status.py" ]; then
	printf '\nSKIP: no claude-code-keyboard-status checkout at %s\n' "$sibling"
	printf '      (set CKS_LOCAL to point at one to run the dry runs)\n'
	printf '\n%d passed, %d failed\n' "$pass" "$fail"
	[ "$fail" -eq 0 ] || exit 1
	exit 0
fi

dry() {
	CKD_PLATFORM="$1" \
		CKD_SOURCE="file://$repo" \
		CKS_SOURCE="file://$sibling" \
		PANEL_IP=192.168.1.50 \
		DRY_RUN=1 \
		INSTALL_DIR="$(mktemp -d)" \
		sh "$repo/install.sh" 2>&1
}

# -------------------------------------------------------------------- Linux

printf '\nDRY_RUN as Linux\n'
linux_out="$(dry Linux)"
check   "accepts a Linux host at all"          "$linux_out" "DRY_RUN: platform Linux"
check   "picks the systemd --user service"     "$linux_out" "systemd --user service"
check   "reports hotkeys as unavailable"       "$linux_out" "global hotkeys: no"
check   "resolves the panel url"               "$linux_out" "http://192.168.1.50/image/upload"
check   "downloads service.py too"             "$linux_out" "verified 9 files"
absent  "no macOS-only rejection"              "$linux_out" "this display drives a macOS"
absent  "no launchd on the Linux path"         "$linux_out" "launchd"

# ------------------------------------------------------------------- Darwin

printf '\nDRY_RUN as Darwin\n'
darwin_out="$(dry Darwin)"
check   "still installs a launchd agent"       "$darwin_out" "DRY_RUN: platform Darwin"
check   "picks launchd"                        "$darwin_out" "launchd agent"
check   "keeps the hotkey listener"            "$darwin_out" "global hotkeys: yes"
check   "downloads the same file set"          "$darwin_out" "verified 9 files"

# ------------------------------------------------------------ anything else

printf '\nDRY_RUN as an unsupported host\n'
if other_out="$(dry SunOS 2>&1)"; then
	printf '  FAIL  SunOS should have been rejected\n'
	fail=$((fail + 1))
else
	check "rejects an unknown platform" "$other_out" "unsupported platform 'SunOS'"
fi

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ] || exit 1

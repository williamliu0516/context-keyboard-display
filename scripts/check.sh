#!/bin/bash
# The one check to run before saying a change is done or committing it: every test suite, then whether this clone
# runs the secret scan before commits. Needs the .venv (Pillow) for test_v2: python3 -m venv .venv &&
# .venv/bin/pip install -r requirements.txt. Docker, the keyboard and the network are never touched.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
failed=0
step() { printf '→ %s\n' "$1"; }
bad() { printf '✗ %s\n' "$1" >&2; failed=1; }

step "tests/test_v2.py";       .venv/bin/python3 tests/test_v2.py >/dev/null 2>&1 || bad "tests/test_v2.py (run it to see why)"
step "tests/test_docker.py";   python3 tests/test_docker.py >/dev/null 2>&1 || bad "tests/test_docker.py"
step "tests/test_platform.py"; python3 tests/test_platform.py >/dev/null 2>&1 || bad "tests/test_platform.py"
step "tests/test_install_sh.sh"; sh tests/test_install_sh.sh >/dev/null 2>&1 || bad "tests/test_install_sh.sh"
step "hooks"
[ "$(git config core.hooksPath || true)" = "scripts/hooks" ] || bad "the secret scan is off in this clone: git config core.hooksPath scripts/hooks"
command -v gitleaks >/dev/null || bad "gitleaks is not installed (brew install gitleaks)"

if [ "$failed" = 0 ]; then echo "✓ all checks passed"; else echo "✗ some checks failed" >&2; fi
exit "$failed"

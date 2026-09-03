# The display daemon as a Linux container, for running the pusher under Docker
# Desktop on the same Mac that runs the native hotkey listener.
#
# Two build contexts, because this repo is half of the stack: the default
# context is this checkout, and `upstream` is the claude-code-keyboard-status
# checkout that owns the renderer and the session hooks. docker-compose.yml
# wires the second one up (`additional_contexts`); by hand it is
#
#     docker build --build-context upstream=../claude-code-keyboard-status -t ckd .
#
# The library is *copied in and pip-installed*, never bind-mounted: an image
# that depends on ~/projects/... existing at run time is an image that only
# works on the machine it was built on, and `import keyboard_status` resolving
# from site-packages is what makes display.py's upstream_path irrelevant here.
ARG PYTHON_TAG=3.13-slim-bookworm
FROM python:${PYTHON_TAG}

# 1 = install the CJK face (~215 MB). The renderer draws whatever a todo says,
# and Claude Code sessions do contain CJK, so the default is to render it
# rather than a row of tofu. Set to 0 for a smaller image on a Latin-only Mac.
ARG INSTALL_CJK_FONT=1

# fonts-*: service.apply_font_fallback repoints the renderer at these, since
#   the faces keyboard_status names (SFNS, PingFang) are macOS-only.
#   fonts-ubuntu is the approved panel face and the reason for the non-free
#   component below -- Ubuntu is UFL-licensed, which is free to use and
#   redistribute but not DFSG-free, so Debian ships it outside main. The
#   Noto/DejaVu pair stays as service.py's degradation tail, not as a face this
#   image ever renders in: the --fonts --strict check below fails the build if
#   the resolved latin faces are not the Ubuntu ones.
# git: collect.diff_stat shells out to it for the +/- counts on the working
#   screen (see the read-only projects mount in docker-compose.yml).
# tzdata: the Idle screen is a clock. Without it TZ= is ignored and the panel
#   shows UTC, which is a wrong display rather than a missing one.
#
# The extra .sources file is additive and repeats bookworm/main on purpose:
# deb822 sources are per-file, so enabling non-free means restating the suite,
# and the duplicate-target warning apt prints for it is cosmetic.
RUN set -eux; \
    printf '%s\n' \
        'Types: deb' \
        'URIs: http://deb.debian.org/debian' \
        'Suites: bookworm' \
        'Components: main contrib non-free' \
        'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
        > /etc/apt/sources.list.d/ckd-nonfree.sources; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        fonts-ubuntu \
        fonts-noto-core \
        fonts-dejavu-core \
        git \
        tzdata \
        ca-certificates; \
    if [ "$INSTALL_CJK_FONT" = "1" ]; then \
        apt-get install -y --no-install-recommends fonts-noto-cjk; \
    fi; \
    rm -rf /var/lib/apt/lists/*

# The renderer library, from the sibling checkout, as a real installed package.
COPY --from=upstream keyboard_status.py pyproject.toml /opt/upstream/
COPY requirements.txt /tmp/requirements.txt
RUN set -eux; \
    pip install --no-cache-dir -r /tmp/requirements.txt; \
    pip install --no-cache-dir /opt/upstream; \
    rm -f /tmp/requirements.txt

WORKDIR /app
# keys.py is deliberately absent: the global hotkeys are a Carbon reservation
# against WindowServer and stay a native launchd agent on the host. Leaving the
# file out makes "hotkeys never run in the container" a property of the image
# rather than a promise in a README.
COPY display.py collect.py screens.py service.py config.example.yaml /app/
COPY docker/entrypoint.sh /usr/local/bin/ckd-entrypoint
RUN chmod +x /usr/local/bin/ckd-entrypoint

# Build-time proof that this image can actually draw, which is the one thing a
# font-substituted container gets wrong silently: --fonts --strict fails the
# build unless the resolved faces are the approved Ubuntu latin pair *and* a
# real CJK face (silently degrading to Noto is the failure this catches), and
# --preview renders the whole approved-mockup dataset through Pillow, the
# installed keyboard_status and those faces.
#
# The CJK half of --strict is conditional on the build arg that installs it:
# INSTALL_CJK_FONT=0 is a deliberate smaller-image choice, not a regression, so
# it downgrades the gate to the latin faces rather than failing the build.
RUN set -eux; \
    if [ "$INSTALL_CJK_FONT" = "1" ]; then \
        python3 /app/service.py --fonts --strict; \
    else \
        python3 /app/service.py --fonts; \
        python3 -c "import sys; sys.path.insert(0, '/app'); import service; \
sys.exit(0 if service.font_choice()['is_ubuntu'] else 'latin faces are not Ubuntu')"; \
    fi; \
    python3 /app/display.py --preview /tmp/buildcheck >/dev/null; \
    test -s /tmp/buildcheck/idle.jpg; \
    rm -rf /tmp/buildcheck

# HOME is where ~/.claude resolves: display.py, collect.py and keyboard_status
# all reach the live config, control, status and transcripts through
# os.path.expanduser("~/.claude"), so the mount goes at $HOME/.claude and no
# code has to learn a container-only path.
ENV HOME=/host \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CKD_HEARTBEAT_PATH=/tmp/ckd/heartbeat.json

# Same check compose declares, so a bare `docker run` of this image is
# introspectable too. --health is stdlib-only and never pushes: it reports that
# the tick loop went round recently, not that the keyboard answered.
HEALTHCHECK --interval=20s --timeout=10s --start-period=45s --retries=3 \
    CMD ["python3", "/app/display.py", "--health"]

ENTRYPOINT ["/usr/local/bin/ckd-entrypoint"]
CMD ["--daemon"]

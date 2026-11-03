#!/usr/bin/env bash
# install.sh — install (or update) the Roster API + live dashboard inside a Debian LXC
#
# First install, as root inside the container:
#   curl -fsSL https://raw.githubusercontent.com/Wm-Mason-Cyber/we-do-talk-about-bruno/main/deploy/install.sh | bash
#
# Update to the latest commit on GitHub (same command, or):
#   bash /opt/bruno-api/src/deploy/install.sh
#
# Safe to re-run: pulls the latest code, keeps the existing teacher key,
# and restarts the service. Override the source with REPO_URL= / BRANCH=.

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Wm-Mason-Cyber/we-do-talk-about-bruno.git}"
BRANCH="${BRANCH:-main}"
APP=/opt/bruno-api
SRC="$APP/src"
ENV_FILE=/etc/bruno-api.env
LOG_DIR=/var/log/bruno-api

# ── Stage 1: get the code, then hand off to the installer inside it ──────────
fetch() {
    [[ $EUID -eq 0 ]] || { echo "Run as root."; exit 1; }
    export DEBIAN_FRONTEND=noninteractive

    if ! command -v git &>/dev/null; then
        echo "[0/6] Installing git..."
        apt-get update -qq
        apt-get install -y -qq git ca-certificates >/dev/null
    fi

    install -d -m 755 "$APP"
    if [[ -d "$SRC/.git" ]]; then
        echo "[0/6] Updating $SRC from $BRANCH..."
        git -C "$SRC" fetch -q origin "$BRANCH"
        git -C "$SRC" reset -q --hard "origin/$BRANCH"
    else
        echo "[0/6] Cloning $REPO_URL..."
        git clone -q --branch "$BRANCH" "$REPO_URL" "$SRC"
    fi
    echo "      at $(git -C "$SRC" log -1 --format='%h %s')"

    # Run the installer from the checkout we just pulled, so updates to this
    # script take effect on the same run
    exec bash "$SRC/deploy/install.sh" --from-checkout
}

# ── Stage 2: everything else, from the checkout ──────────────────────────────
install_app() {
    local deploy="$SRC/deploy"
    export DEBIAN_FRONTEND=noninteractive

    echo "[1/6] Installing packages..."
    apt-get update -qq
    apt-get install -y -qq python3 python3-venv nginx avahi-daemon curl >/dev/null

    # avahi-daemon's process limit trips over unprivileged LXC; drop it so
    # <hostname>.local resolves for student laptops on the same network
    sed -i 's/^rlimit-nproc=/#rlimit-nproc=/' /etc/avahi/avahi-daemon.conf
    systemctl enable avahi-daemon >/dev/null 2>&1 || true
    systemctl restart avahi-daemon || echo "      (avahi failed to start — .local names won't work; use the IP)"

    echo "[2/6] Python environment..."
    id bruno-api &>/dev/null || useradd --system --home-dir "$APP" --shell /usr/sbin/nologin bruno-api
    [[ -x "$APP/venv/bin/python" ]] || python3 -m venv "$APP/venv"
    "$APP/venv/bin/pip" install -q --upgrade pip
    "$APP/venv/bin/pip" install -q -r "$SRC/requirements.txt"

    echo "[3/6] Teacher key..."
    if [[ ! -f "$ENV_FILE" ]]; then
        echo "TEACHER_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(9))')" > "$ENV_FILE"
    fi
    chmod 600 "$ENV_FILE"

    echo "[4/6] Logs + logrotate..."
    install -d -m 755 "$LOG_DIR"
    for f in access.log error.log; do
        [[ -f "$LOG_DIR/$f" ]] || install -m 644 -o www-data -g adm /dev/null "$LOG_DIR/$f"
    done
    install -m 644 "$deploy/logrotate-bruno-api" /etc/logrotate.d/bruno-api

    echo "[5/6] nginx..."
    install -m 644 "$deploy/nginx-bruno-api.conf" /etc/nginx/sites-available/bruno-api
    ln -sf /etc/nginx/sites-available/bruno-api /etc/nginx/sites-enabled/bruno-api
    rm -f /etc/nginx/sites-enabled/default
    nginx -t -q
    systemctl enable nginx >/dev/null 2>&1
    systemctl reload-or-restart nginx

    echo "[6/6] systemd services..."
    install -m 644 "$deploy/bruno-api.service" "$deploy/bruno-api-nightly.service" \
        "$deploy/bruno-api-nightly.timer" /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable bruno-api.service bruno-api-nightly.timer >/dev/null 2>&1
    systemctl restart bruno-api.service
    systemctl start bruno-api-nightly.timer

    summary
}

summary() {
    local ok=0
    for _ in $(seq 1 20); do
        if curl -sf http://127.0.0.1:5000/students >/dev/null; then ok=1; break; fi
        sleep 0.5
    done

    local ip host key
    ip="$(hostname -I | awk '{print $1}')"
    host="$(hostname)"
    key="$(sed -n 's/^TEACHER_KEY=//p' "$ENV_FILE")"

    echo ""
    if [[ "$ok" -eq 1 ]]; then
        echo "  Roster API is up  ($(git -C "$SRC" log -1 --format='%h %s'))"
    else
        echo "  WARNING: API did not answer on :5000. Check: journalctl -u bruno-api -n 50"
    fi
    cat <<SUMMARY

  Student base URL:   http://${host}.local:5000      (or http://${ip}:5000)
  Projector page:     http://${host}.local/dashboard
  API docs:           http://${host}.local:5000/docs
  Raw access log:     http://${host}.local:5000/logs/access

  Teacher key:        ${key}
                      (Dashboard → "Teacher" button. Stored in ${ENV_FILE})

  Roster resets itself at 05:00 every day.
  Update later with:  bash ${SRC}/deploy/install.sh

SUMMARY
}

main() {
    if [[ "${1:-}" == "--from-checkout" ]]; then
        install_app
    else
        fetch
    fi
}

# Everything is inside functions so `curl | bash` never runs a half-downloaded script
main "$@"

#!/usr/bin/env bash
# install-service.sh — run the Roster API at boot with systemd (after ./setup.sh).
#
#   ./install-service.sh              # serve on port 5000
#   PORT=5001 ./install-service.sh    # some other port
#   ./install-service.sh --remove     # stop it and remove it from boot
#
# Safe to re-run (e.g. after `git pull`, to restart on the new code).

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

DIR="$(pwd)"
PORT="${PORT:-5000}"
RUN_AS="${SUDO_USER:-$(id -un)}"
UNIT=/etc/systemd/system/bruno-api.service
SUDO=""
[[ $EUID -eq 0 ]] || SUDO="sudo"

if [[ "${1:-}" == "--remove" ]]; then
    $SUDO systemctl disable --now bruno-api.service 2>/dev/null || true
    $SUDO rm -f "$UNIT"
    $SUDO systemctl daemon-reload
    echo "Removed bruno-api.service."
    exit 0
fi

[[ -x .venv/bin/python ]] || { echo "Run ./setup.sh first."; exit 1; }

# Don't fight another server (like the old Pi's student-api.service) for the port
if ! systemctl is-active -q bruno-api.service 2>/dev/null && ss -ltnH "sport = :$PORT" | grep -q .; then
    echo "Something is already listening on port $PORT:"
    $SUDO ss -ltnpH "sport = :$PORT" || true
    echo ""
    echo "Stop it first (the old Raspberry Pi server: sudo systemctl disable --now student-api),"
    echo "or pick another port:  PORT=5001 ./install-service.sh"
    exit 1
fi

$SUDO tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=Class Roster API (Bruno lab) + live dashboard
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_AS
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port $PORT
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

$SUDO systemctl daemon-reload
$SUDO systemctl enable bruno-api.service >/dev/null 2>&1
$SUDO systemctl restart bruno-api.service

ok=0
for _ in $(seq 1 30); do
    if .venv/bin/python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:$PORT/students', timeout=1)" 2>/dev/null; then
        ok=1; break
    fi
    sleep 0.5
done

if [[ "$ok" -ne 1 ]]; then
    echo "The service didn't answer on port $PORT. Last log lines:"
    $SUDO journalctl -u bruno-api.service -n 20 --no-pager || true
    exit 1
fi

cat <<EOF

  Running, and will start at every boot.

  Students:    http://$(hostname).local:$PORT/students     (or http://$(hostname -I | awk '{print $1}'):$PORT)
  Projector:   http://$(hostname).local:$PORT/dashboard
  Teacher key: $(cat "$DIR/.teacher_key" 2>/dev/null || echo "(set by \$TEACHER_KEY)")

  Logs:        journalctl -u bruno-api -f
  Update:      git pull && ./setup.sh && ./install-service.sh

EOF

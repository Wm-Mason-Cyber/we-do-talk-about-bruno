#!/usr/bin/env bash
# setup.sh — install what the Roster API needs, into .venv/ next to this file.
#
# Works on Raspberry Pi OS, Debian, and Ubuntu (a Pi, an LXC, a VM, or a laptop).
# Safe to re-run.
#
#   ./setup.sh
#
# Then start the server with:
#   .venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 5000

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

SUDO=""
[[ $EUID -eq 0 ]] || SUDO="sudo"
apt_install() {
    $SUDO apt-get update -qq
    $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@"
}

# ── 1. mDNS, so students can use http://<hostname>.local ─────────────────────
if [[ ! -x /usr/sbin/avahi-daemon ]]; then
    echo "[setup] Installing avahi-daemon (for <hostname>.local)..."
    apt_install avahi-daemon
fi
# avahi's process limit trips over unprivileged LXC containers
if systemd-detect-virt -cq 2>/dev/null && grep -q '^rlimit-nproc=' /etc/avahi/avahi-daemon.conf 2>/dev/null; then
    $SUDO sed -i 's/^rlimit-nproc=/#rlimit-nproc=/' /etc/avahi/avahi-daemon.conf
    $SUDO systemctl restart avahi-daemon || true
fi

# ── 2. Python packages ────────────────────────────────────────────────────────
if [[ ! -x .venv/bin/python ]]; then
    echo "[setup] Creating .venv..."
    if ! python3 -m venv .venv 2>/dev/null; then
        rm -rf .venv
        apt_install python3-venv
        python3 -m venv .venv
    fi
fi

echo "[setup] Installing Python packages..."
if ! .venv/bin/pip install -q -r requirements.txt; then
    # Usually 32-bit Raspberry Pi OS, where some wheels don't exist.
    # Debian's own packages are older, but the app is tested against them too.
    echo "[setup] pip failed — falling back to the system's python3-fastapi + uvicorn packages..."
    apt_install python3-fastapi uvicorn
    rm -rf .venv
    python3 -m venv --system-site-packages .venv
fi

.venv/bin/python -c 'import fastapi, uvicorn; print(f"[setup] OK: fastapi {fastapi.__version__}, uvicorn {uvicorn.__version__}")'

cat <<EOF

  Start the server:
    .venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 5000

  Then open http://$(hostname).local:5000/dashboard

  Keep it running across reboots:
    ./install-service.sh

EOF

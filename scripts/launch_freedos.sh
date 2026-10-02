#!/usr/bin/env bash
set -euo pipefail

# Project SENTINEL: FreeDOS QEMU VM Launcher
# Usage:
#   ./scripts/launch_freedos.sh         # Normal mode (persistent state, vnc :0)
#   ./scripts/launch_freedos.sh --test  # Test mode (-snapshot, -display none)

FREEDOS_DIR="${FREEDOS_DIR:-/home/sivakuhan/Projects/FreeDos}"
FREEDOS_IMG="${FREEDOS_IMG:-$FREEDOS_DIR/freedos.qcow2}"
QMP_PORT="${QMP_PORT:-4444}"
VNC_DISPLAY="${VNC_DISPLAY:-:0}"

if [ ! -f "$FREEDOS_IMG" ]; then
    echo "ERROR: FreeDOS image not found at $FREEDOS_IMG" >&2
    exit 1
fi

# Ensure no other QEMU instance is running against this image or holding the ports
if pgrep -f "freedos.qcow2" > /dev/null; then
    echo "ERROR: A QEMU instance running freedos.qcow2 is already active!" >&2
    echo "Never run two QEMU instances on the same image." >&2
    exit 1
fi

if ss -tulpn | grep -q ":$QMP_PORT\b"; then
    echo "ERROR: Port $QMP_PORT (QMP) is already in use." >&2
    exit 1
fi

TEST_MODE=false
EXTRA_ARGS=()

for arg in "$@"; do
    case "$arg" in
        --test)
            TEST_MODE=true
            ;;
        *)
            echo "Unknown argument: $arg" >&2
            echo "Usage: $0 [--test]" >&2
            exit 1
            ;;
    esac
done

if [ "$TEST_MODE" = true ]; then
    echo "[FreeDOS Launcher] Starting in TEST mode (-snapshot, -display none)..."
    EXTRA_ARGS+=("-snapshot" "-display" "none")
else
    echo "[FreeDOS Launcher] Starting in NORMAL mode (persistent)..."
fi

echo "[FreeDOS Launcher] Image: $FREEDOS_IMG"
echo "[FreeDOS Launcher] QMP: 127.0.0.1:$QMP_PORT"
echo "[FreeDOS Launcher] VNC: 127.0.0.1$VNC_DISPLAY (TCP port 5900)"

exec qemu-system-x86_64 \
    -m 512M \
    -drive file="$FREEDOS_IMG",format=qcow2 \
    -qmp tcp:127.0.0.1:"$QMP_PORT",server=on,wait=off \
    -vnc 127.0.0.1"$VNC_DISPLAY" \
    "${EXTRA_ARGS[@]}"

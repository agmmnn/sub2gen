#!/bin/sh
set -eu

DISPLAY_VALUE="${DISPLAY:-:99}"
XVFB_SCREEN_VALUE="${XVFB_SCREEN:-1440x900x24}"
export DISPLAY="${DISPLAY_VALUE}"
XVFB_PID=""
FLUXBOX_PID=""
X11VNC_PID=""
NOVNC_PID=""

cleanup() {
    # Best-effort shutdown for background GUI processes.
    [ -n "${NOVNC_PID}" ] && kill "${NOVNC_PID}" 2>/dev/null || true
    [ -n "${X11VNC_PID}" ] && kill "${X11VNC_PID}" 2>/dev/null || true
    [ -n "${FLUXBOX_PID}" ] && kill "${FLUXBOX_PID}" 2>/dev/null || true
    [ -n "${XVFB_PID}" ] && kill "${XVFB_PID}" 2>/dev/null || true
}

trap 'cleanup' EXIT INT TERM

resolve_browser_path() {
python - <<'PY'
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    print(p.chromium.executable_path or "")
PY
}

if [ -z "${BROWSER_EXECUTABLE_PATH:-}" ] || [ ! -x "${BROWSER_EXECUTABLE_PATH:-}" ]; then
    detected_browser_path="$(resolve_browser_path 2>/dev/null | tr -d '\r' | tail -n 1)"
    if [ -n "${detected_browser_path}" ] && [ -x "${detected_browser_path}" ]; then
        export BROWSER_EXECUTABLE_PATH="${detected_browser_path}"
    fi
fi

if [ "${ALLOW_DOCKER_HEADED_CAPTCHA:-true}" = "true" ] || [ "${ALLOW_DOCKER_HEADED_CAPTCHA:-1}" = "1" ]; then
    if ! command -v Xvfb >/dev/null 2>&1; then
        echo "[entrypoint] Xvfb not found in image" >&2
        exit 1
    fi
    if ! command -v fluxbox >/dev/null 2>&1; then
        echo "[entrypoint] fluxbox not found in image" >&2
        exit 1
    fi

    display_suffix="$(printf '%s' "${DISPLAY}" | sed 's/^://; s/\..*$//')"
    socket_path="/tmp/.X11-unix/X${display_suffix}"

    mkdir -p /tmp/.X11-unix
    rm -f "/tmp/.X${display_suffix}-lock"

    echo "[entrypoint] starting Xvfb on DISPLAY=${DISPLAY} (${XVFB_SCREEN_VALUE})"
    Xvfb "${DISPLAY}" -screen 0 "${XVFB_SCREEN_VALUE}" -ac +extension RANDR >/tmp/xvfb.log 2>&1 &
    XVFB_PID="$!"

    waited=0
    while [ ! -S "${socket_path}" ] && [ "${waited}" -lt 100 ]; do
        if ! kill -0 "${XVFB_PID}" 2>/dev/null; then
            echo "[entrypoint] Xvfb exited during startup; check /tmp/xvfb.log" >&2
            exit 1
        fi
        sleep 0.1
        waited=$((waited + 1))
    done

    if [ ! -S "${socket_path}" ]; then
        echo "[entrypoint] failed to start Xvfb, socket not ready: ${socket_path}" >&2
        exit 1
    fi

    echo "[entrypoint] starting Fluxbox on DISPLAY=${DISPLAY}"
    fluxbox >/tmp/fluxbox.log 2>&1 &
    FLUXBOX_PID="$!"

    # Optional VNC server so you can view :99 with RealVNC/TigerVNC (localhost:5900 by default).
    # Set ENABLE_HEADED_VNC=0 to disable. Set VNC_PASSWORD for auth; otherwise -nopw (dev only).
    case "${ENABLE_HEADED_VNC:-1}" in
        0|false|FALSE|no|NO|off|OFF)
            echo "[entrypoint] headed VNC disabled (ENABLE_HEADED_VNC=${ENABLE_HEADED_VNC:-})"
            ;;
        *)
            VNC_PORT_VALUE="${VNC_PORT:-5900}"
            if [ -n "${VNC_PASSWORD:-}" ]; then
                rm -f /tmp/x11vnc.pass
                if x11vnc -storepasswd "${VNC_PASSWORD}" /tmp/x11vnc.pass 2>/dev/null; then
                    VNC_AUTH_OPTS="-rfbauth /tmp/x11vnc.pass"
                else
                    echo "[entrypoint] VNC_PASSWORD set but -storepasswd failed; falling back to -nopw" >&2
                    VNC_AUTH_OPTS="-nopw"
                fi
            else
                VNC_AUTH_OPTS="-nopw"
                echo "[entrypoint] VNC: no VNC_PASSWORD set (passwordless). Do not expose port 5900 to the internet." >&2
            fi
            echo "[entrypoint] starting x11vnc on 0.0.0.0:${VNC_PORT_VALUE} for DISPLAY=${DISPLAY}"
            x11vnc -display "${DISPLAY}" -forever -shared -noxdamage \
                ${VNC_AUTH_OPTS} \
                -listen 0.0.0.0 -rfbport "${VNC_PORT_VALUE}" \
                >/tmp/x11vnc.log 2>&1 &
            X11VNC_PID="$!"

            case "${ENABLE_HEADED_NOVNC:-1}" in
                0|false|FALSE|no|NO|off|OFF)
                    echo "[entrypoint] headed noVNC disabled (ENABLE_HEADED_NOVNC=${ENABLE_HEADED_NOVNC:-})"
                    ;;
                *)
                    if ! command -v websockify >/dev/null 2>&1; then
                        echo "[entrypoint] websockify not found; noVNC unavailable" >&2
                    elif [ ! -d /usr/share/novnc ]; then
                        echo "[entrypoint] /usr/share/novnc not found; noVNC unavailable" >&2
                    else
                        NOVNC_PORT_VALUE="${NOVNC_PORT:-6080}"
                        echo "[entrypoint] starting noVNC on 0.0.0.0:${NOVNC_PORT_VALUE} -> localhost:${VNC_PORT_VALUE}"
                        websockify --web=/usr/share/novnc "0.0.0.0:${NOVNC_PORT_VALUE}" "localhost:${VNC_PORT_VALUE}" \
                            >/tmp/novnc.log 2>&1 &
                        NOVNC_PID="$!"
                    fi
                    ;;
            esac
            ;;
    esac
fi

echo "[entrypoint] starting sub2gen (headed browser mode)"
if [ -n "${BROWSER_EXECUTABLE_PATH:-}" ] && [ -x "${BROWSER_EXECUTABLE_PATH}" ]; then
    echo "[entrypoint] browser executable: ${BROWSER_EXECUTABLE_PATH}"
    "${BROWSER_EXECUTABLE_PATH}" --version || true
else
    echo "[entrypoint] warning: no valid browser executable found for personal/browser captcha" >&2
fi

exec sub2gen

#!/bin/sh
set -eu
umask 077
# Normal collection has no X server, VNC server or graphical viewer.
if [ "${READER_HEADLESS:-1}" = "1" ]; then
    exec python /reader/reader.py
fi
Xvfb :99 -screen 0 1280x800x24 -nolisten tcp &
XVFB_PID=$!
trap 'kill "$XVFB_PID" ${VNC_PID:-} ${WEB_PID:-} ${READER_PID:-} 2>/dev/null || true' EXIT INT TERM
sleep 2
# Viewer is published only on server loopback and reached over an SSH tunnel.
# Disable it after login: docker compose exec reader touch /state/viewer.disabled
if [ ! -f /state/viewer.disabled ]; then
    x11vnc -display :99 -localhost -forever -shared -nopw -rfbport 5900 >/dev/null 2>&1 &
    VNC_PID=$!
    websockify --web=/usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &
    WEB_PID=$!
fi
python /reader/reader.py &
READER_PID=$!
while kill -0 "$READER_PID" 2>/dev/null; do
    if [ -f /state/viewer.disabled ]; then
        kill ${VNC_PID:-} ${WEB_PID:-} 2>/dev/null || true
    fi
    sleep 3
done
wait "$READER_PID"

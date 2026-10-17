#!/data/data/com.termux/files/usr/bin/bash
# Start the RaheeCraft panel + Cloudflare tunnel together on Android/Termux.
#
# Usage:
#   chmod +x start_panel.sh
#   ./start_panel.sh        # start both, detach
#   ./start_panel.sh stop   # stop both
#   ./start_panel.sh status # show state
#
# The backend listens on 127.0.0.1:8080. The Cloudflare connector reaches it
# through the existing tunnel (token in ~/.cloudflare-tunnel-token). On Termux
# the connector runs under proot so the Go binary can see resolv.conf.

set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_LOG="$APP_DIR/../backend.log"
TUNNEL_LOG="$APP_DIR/../tunnel.log"
PID_DIR="$APP_DIR/../run"
mkdir -p "$PID_DIR"

start_backend() {
    if [ -f "$PID_DIR/backend.pid" ] && kill -0 "$(cat "$PID_DIR/backend.pid")" 2>/dev/null; then
        echo "[backend] already running (pid $(cat "$PID_DIR/backend.pid"))"
        return 0
    fi
    echo "[backend] starting..."
    cd "$APP_DIR"
    setsid python3 run_backend.py >> "$BACKEND_LOG" 2>&1 < /dev/null &
    local pid=$!
    echo $pid > "$PID_DIR/backend.pid"
    # Wait up to 15s for the health endpoint
    for i in $(seq 1 15); do
        if curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8080/health 2>/dev/null | grep -q 200; then
            echo "[backend] up (pid $pid)"
            return 0
        fi
        sleep 1
    done
    echo "[backend] started but health check did not return 200 within 15s"
    tail -5 "$BACKEND_LOG" 2>/dev/null | sed 's/^/[backend] /'
}

start_tunnel() {
    if [ -f "$PID_DIR/tunnel.pid" ] && kill -0 "$(cat "$PID_DIR/tunnel.pid")" 2>/dev/null; then
        echo "[tunnel] already running (pid $(cat "$PID_DIR/tunnel.pid"))"
        return 0
    fi
    echo "[tunnel] starting..."
    cd "$APP_DIR"
    setsid python3 run_panel_tunnel.py >> "$TUNNEL_LOG" 2>&1 < /dev/null &
    local pid=$!
    echo $pid > "$PID_DIR/tunnel.pid"
    # Wait for CONNECTED in the log
    for i in $(seq 1 30); do
        if grep -q "CONNECTED:" "$TUNNEL_LOG" 2>/dev/null; then
            echo "[tunnel] connected (pid $pid)"
            tail -8 "$TUNNEL_LOG" 2>/dev/null | sed 's/^/[tunnel] /'
            return 0
        fi
        sleep 2
    done
    echo "[tunnel] started but did not report CONNECTED within 60s"
    tail -8 "$TUNNEL_LOG" 2>/dev/null | sed 's/^/[tunnel] /'
}

stop_backend() {
    if [ -f "$PID_DIR/backend.pid" ]; then
        local pid
        pid="$(cat "$PID_DIR/backend.pid")"
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
            sleep 1
            kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$PID_DIR/backend.pid"
        echo "[backend] stopped"
    else
        echo "[backend] not running"
    fi
}

stop_tunnel() {
    if [ -f "$PID_DIR/tunnel.pid" ]; then
        local pid
        pid="$(cat "$PID_DIR/tunnel.pid")"
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
            sleep 1
            kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$PID_DIR/tunnel.pid"
        echo "[tunnel] stopped"
    else
        echo "[tunnel] not running"
    fi
    # Also kill any lingering cloudflared/proot children
    pkill -f "cloudflared tunnel" 2>/dev/null || true
    pkill -f "proot.*cloudflared" 2>/dev/null || true
}

case "${1:-start}" in
    start)
        start_backend
        sleep 2
        start_tunnel
        echo ""
        echo "Panel:  http://127.0.0.1:8080"
        echo "Tunnel: https://panel.soldiers1234583.dpdns.org"
        echo "Logs:   tail -f $BACKEND_LOG  $TUNNEL_LOG"
        ;;
    stop)
        stop_tunnel
        stop_backend
        ;;
    restart)
        stop_tunnel
        stop_backend
        sleep 2
        start_backend
        sleep 2
        start_tunnel
        ;;
    status)
        echo "=== Backend ==="
        if [ -f "$PID_DIR/backend.pid" ] && kill -0 "$(cat "$PID_DIR/backend.pid")" 2>/dev/null; then
            echo "  running (pid $(cat "$PID_DIR/backend.pid"))"
            curl -s http://127.0.0.1:8080/health 2>/dev/null || echo "  health check FAILED"
        else
            echo "  NOT running"
        fi
        echo "=== Tunnel ==="
        if [ -f "$PID_DIR/tunnel.pid" ] && kill -0 "$(cat "$PID_DIR/tunnel.pid")" 2>/dev/null; then
            echo "  running (pid $(cat "$PID_DIR/tunnel.pid"))"
        else
            echo "  NOT running"
        fi
        if pgrep -f "cloudflared tunnel" >/dev/null 2>&1; then
            echo "  cloudflared: running"
        else
            echo "  cloudflared: NOT running"
        fi
        echo "=== Recent tunnel log ==="
        tail -6 "$TUNNEL_LOG" 2>/dev/null | sed 's/^/  /' || echo "  (no log)"
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac

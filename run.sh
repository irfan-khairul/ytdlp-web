#!/usr/bin/env bash
# Runs ytdlp-web.
#
#   ./run.sh              run in the foreground (Ctrl+C stops it)
#   ./run.sh start        run in the background; keeps going after you log out
#   ./run.sh stop         stop the background app
#   ./run.sh restart      stop, then start
#   ./run.sh status       is it running, where to open it, free disk space
#   ./run.sh logs         follow the log (Ctrl+C stops following, not the app)
#   ./run.sh check-port   what is using the port and whether other devices can reach it
#
# Settings come from .env (see .env.example) or the environment.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$DIR/.ytdlp-web.pid"
LOG_FILE="$DIR/ytdlp-web.log"

# Prefer the bundled runtime from scripts/setup.sh; fall back to a dev venv.
if [[ -x "$DIR/runtime/python/bin/python3" ]]; then
  PY="$DIR/runtime/python/bin/python3"
  export PATH="$DIR/runtime/bin:$PATH"
elif [[ -x "$DIR/.venv/bin/python" ]]; then
  PY="$DIR/.venv/bin/python"
else
  echo "No runtime found. Run scripts/setup.sh first (or create .venv, see README)." >&2
  exit 1
fi
export PYTHONUNBUFFERED=1

# Reads a setting the way app.py does: the environment wins, then the first
# matching line in .env, then the default.
setting() {
  local key=$1 default=$2 line
  if [[ -n "${!key:-}" ]]; then echo "${!key}"; return; fi
  if [[ -f "$DIR/.env" ]]; then
    line=$(grep -E "^[[:space:]]*(export[[:space:]]+)?$key[[:space:]]*=" "$DIR/.env" | head -n1 || true)
    if [[ -n "$line" ]]; then
      echo "${line#*=}" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e "s/^[\"']\(.*\)[\"']$/\1/"
      return
    fi
  fi
  echo "$default"
}
HOST=$(setting HOST 127.0.0.1)
PORT=$(setting PORT 8080)
# Where to connect to check the app is answering.
case "$HOST" in 0.0.0.0|::|"") PROBE=127.0.0.1 ;; *) PROBE=$HOST ;; esac

# Prints the PID of the background app, or fails if it isn't running.
app_pid() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid; pid=$(cat "$PID_FILE")
  if kill -0 "$pid" 2>/dev/null && ps -o command= -p "$pid" | grep -q "app.py"; then
    echo "$pid"
    return 0
  fi
  rm -f "$PID_FILE"  # stale: the app exited or the PID was reused
  return 1
}

port_open() { (exec 3<>"/dev/tcp/$PROBE/$PORT") 2>/dev/null; }

# Local addresses of whatever is listening on the port, e.g. 0.0.0.0:8080.
listen_addrs() {
  if command -v ss >/dev/null; then
    ss -ltnH "sport = :$PORT" 2>/dev/null | awk '{print $4}'
  else
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -Fn 2>/dev/null | sed -n 's/^n//p' | sort -u
  fi
}

listen_pids() {
  if command -v ss >/dev/null; then
    ss -ltnpH "sport = :$PORT" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u
  else
    lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | sort -u
  fi
}

# "address interface" for each IPv4 address of this machine, loopback excluded.
local_ips() {
  if command -v ip >/dev/null; then
    ip -4 -o addr show | awk '{split($4, a, "/"); print a[1], $2}'
  else
    ifconfig | awk '/^[a-z]/ {iface = $1; sub(":", "", iface)} /inet / {print $2, iface}'
  fi | grep -v '^127\.'
}

print_urls() {
  echo "  http://127.0.0.1:$PORT  (this machine)"
  if [[ "$HOST" == "0.0.0.0" || "$HOST" == "::" ]]; then
    local_ips | while read -r ip iface; do echo "  http://$ip:$PORT  ($iface)"; done
  elif [[ "$HOST" != "127.0.0.1" && "$HOST" != "localhost" ]]; then
    echo "  http://$HOST:$PORT"
  else
    echo "  Only reachable from this machine. Set HOST=0.0.0.0 in .env for other devices."
  fi
}

print_disk() {
  "$PY" - "$PROBE" "$PORT" <<'EOF' 2>/dev/null || true
import json, sys, urllib.request
s = json.load(urllib.request.urlopen(f"http://{sys.argv[1]}:{sys.argv[2]}/api/status", timeout=3))
gb = lambda n: f"{n / 1024**3:.1f} GB"
keep = f", keeps {gb(s['min_free'])} free" if s["min_free"] else ""
print(f"Disk: {gb(s['free'])} free of {gb(s['total'])}{keep}")
EOF
}

cmd_start() {
  local pid
  if pid=$(app_pid); then
    echo "Already running (PID $pid)."
    print_urls
    return 0
  fi
  if port_open; then
    echo "Port $PORT is already in use by another program." >&2
    echo "Run ./run.sh check-port to see what it is, or set a different PORT in .env." >&2
    return 1
  fi

  # Keep the log from growing forever: rotate once it passes 5 MB.
  if [[ -f "$LOG_FILE" && $(wc -c < "$LOG_FILE") -gt 5242880 ]]; then
    mv -f "$LOG_FILE" "$LOG_FILE.1"
  fi
  echo "=== started $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_FILE"

  # nohup (and setsid, where available) detach the app from this terminal so
  # closing the SSH session doesn't stop it.
  local detach=(nohup)
  command -v setsid >/dev/null && detach+=(setsid)
  cd "$DIR"
  "${detach[@]}" "$PY" "$DIR/app.py" >> "$LOG_FILE" 2>&1 < /dev/null &
  pid=$!
  echo "$pid" > "$PID_FILE"

  for _ in $(seq 1 40); do
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$PID_FILE"
      echo "ytdlp-web exited while starting. Last log lines:" >&2
      tail -n 15 "$LOG_FILE" >&2
      return 1
    fi
    if port_open; then
      echo "Started ytdlp-web in the background (PID $pid). Open:"
      print_urls
      echo "Logs: ./run.sh logs    Stop: ./run.sh stop"
      return 0
    fi
    sleep 0.25
  done
  echo "Started (PID $pid) but port $PORT isn't answering yet. Check ./run.sh logs" >&2
  return 1
}

cmd_stop() {
  local pid
  if ! pid=$(app_pid); then
    echo "Not running."
    if port_open; then
      echo "Something else is using port $PORT (started another way?). See ./run.sh check-port"
    fi
    return 0
  fi
  # Stop any ffmpeg/deno the app started, then the app itself.
  pkill -TERM -P "$pid" 2>/dev/null || true
  kill -TERM "$pid" 2>/dev/null || true
  for _ in $(seq 1 40); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.25
  done
  if kill -0 "$pid" 2>/dev/null; then
    pkill -KILL -P "$pid" 2>/dev/null || true
    kill -KILL "$pid" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
  echo "Stopped (PID $pid). Downloads that were in progress were cancelled."
}

cmd_status() {
  local pid
  if pid=$(app_pid); then
    echo "Running (PID $pid, up $(ps -o etime= -p "$pid" | tr -d ' ')). Open:"
    print_urls
    print_disk
  elif port_open; then
    echo "Not started with ./run.sh start, but something is answering on port $PORT."
    echo "See ./run.sh check-port"
  else
    echo "Not running. Start it with ./run.sh start"
  fi
}

cmd_check_port() {
  echo "Port $PORT, HOST=$HOST"
  local addrs pids ours
  addrs=$(listen_addrs)
  if [[ -z "$addrs" ]]; then
    echo "  Nothing is listening on port $PORT."
    return 0
  fi
  echo "  Listening on: $(echo "$addrs" | tr '\n' ' ')"

  pids=$(listen_pids)
  ours=$(app_pid || true)
  for p in $pids; do
    if [[ "$p" == "$ours" ]]; then
      echo "  Process: ytdlp-web (PID $p, started with ./run.sh start)"
    else
      echo "  Process: PID $p: $(ps -o command= -p "$p" | cut -c1-80)"
    fi
  done
  [[ -z "$pids" ]] && echo "  Process: owned by another user (try: sudo ss -ltnp 'sport = :$PORT')"

  if echo "$addrs" | grep -qvE '^(127\.0\.0\.1|\[::1\]|localhost):'; then
    echo "  Reachable from other devices: yes, if the firewall allows it."
  else
    echo "  Reachable from other devices: no, it only listens on this machine."
    [[ -n "$ours" ]] && echo "  Set HOST=0.0.0.0 in .env, then ./run.sh restart"
  fi

  if port_open && "$PY" -c "import urllib.request,sys; urllib.request.urlopen(sys.argv[1], timeout=3)" \
      "http://$PROBE:$PORT/api/status" 2>/dev/null; then
    echo "  ytdlp-web responds: yes"
  else
    echo "  ytdlp-web responds: no (the port is used by something else, or the app is stuck)"
  fi

  if command -v ufw >/dev/null; then
    local fw
    if fw=$(sudo -n ufw status 2>/dev/null); then
      if ! echo "$fw" | grep -q "Status: active"; then
        echo "  Firewall (ufw): inactive"
      elif echo "$fw" | grep -qE "^$PORT(/tcp)?[[:space:]].*ALLOW"; then
        echo "  Firewall (ufw): port $PORT allowed"
      else
        echo "  Firewall (ufw): port $PORT NOT allowed. Fix: sudo ufw allow $PORT/tcp"
      fi
    else
      echo "  Firewall (ufw): run 'sudo ufw status' to check. If blocked: sudo ufw allow $PORT/tcp"
    fi
  fi
}

cmd_logs() {
  [[ -f "$LOG_FILE" ]] || { echo "No log yet ($LOG_FILE)."; return 0; }
  exec tail -n 50 -f "$LOG_FILE"
}

cmd_foreground() {
  local pid
  if pid=$(app_pid); then
    echo "Already running in the background (PID $pid). Use ./run.sh stop first." >&2
    return 1
  fi
  exec "$PY" "$DIR/app.py"
}

case "${1:-}" in
  "")         cmd_foreground ;;
  start)      cmd_start ;;
  stop)       cmd_stop ;;
  restart)    cmd_stop; cmd_start ;;
  status)     cmd_status ;;
  logs)       cmd_logs ;;
  check-port) cmd_check_port ;;
  -h|--help|help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//' ;;
  *) echo "Unknown command: $1 (try ./run.sh help)" >&2; exit 1 ;;
esac

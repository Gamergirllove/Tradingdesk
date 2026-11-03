#!/bin/bash
# Runs a free Cloudflare quick tunnel to the dashboard and pushes the link to your phone via ntfy.
DATA=${DESK_DATA:-/var/lib/desk}
LOG=$DATA/tunnel.log
rm -f "$DATA/public_url"
: > "$LOG"
cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8080 >>"$LOG" 2>&1 &
PID=$!
URL=""
for i in $(seq 1 60); do
  URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$LOG" | head -1)
  [ -n "$URL" ] && break
  sleep 2
done
for i in $(seq 1 30); do
  grep -q "Registered tunnel connection" "$LOG" && break
  sleep 2
done
if [ -n "$URL" ] && grep -q "Registered tunnel connection" "$LOG"; then
  echo "$URL" > "$DATA/public_url"
  TOPIC=$(cat "$DATA/ntfy_topic" 2>/dev/null)
  if [ -n "$TOPIC" ]; then
    if [ -f "$DATA/setup_code" ]; then
      MSG="Tap to set up the desk. Setup code: $(cat "$DATA/setup_code")"
    else
      MSG="Desk is online. Tap to open the dashboard."
    fi
    curl -fsS -m 15 -H "Title: Trading desk" -H "Click: $URL" -H "Tags: chart_with_upwards_trend" \
      -d "$MSG $URL" "https://ntfy.sh/$TOPIC" >/dev/null || true
  fi
fi
wait $PID

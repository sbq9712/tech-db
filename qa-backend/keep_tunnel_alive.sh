#!/bin/bash
# Keep cloudflared tunnel alive - restarts if it dies
TUNNEL_LOG="/tmp/cloudflared.log"
while true; do
    if ! pgrep -f "cloudflared tunnel" > /dev/null 2>&1; then
        echo "$(date): Starting cloudflared tunnel..."
        ~/bin/cloudflared tunnel --url http://localhost:8765 > "$TUNNEL_LOG" 2>&1 &
        echo "$(date): cloudflared started with PID $!"
    fi
    sleep 60
done

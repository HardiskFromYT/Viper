#!/bin/bash
# One-time setup for login screen news.
# Redirects the WoW client's news URLs to localhost.
# Run once with: sudo ./setup_news.sh
# To undo:       sudo ./setup_news.sh --remove

set -e

# Different client builds use different domains
DOMAINS=(
    "www.worldofwarcraft.com"
    "launcher.worldofwarcraft.com"
)

if [ "$(id -u)" -ne 0 ]; then
    echo "This script needs root. Run: sudo $0"
    exit 1
fi

if [ "$1" = "--remove" ]; then
    echo "Removing news redirects..."
    for d in "${DOMAINS[@]}"; do
        sed -i '' "/$d/d" /etc/hosts 2>/dev/null || true
    done
    echo "Done. News redirects removed."
    exit 0
fi

echo "=== Viper News Setup ==="
echo ""

for d in "${DOMAINS[@]}"; do
    if grep -q "$d" /etc/hosts 2>/dev/null; then
        echo "[OK] $d already in hosts."
    else
        echo "127.0.0.1 $d" >> /etc/hosts
        echo "[OK] Added: 127.0.0.1 $d"
    fi
done

# Flush DNS cache
dscacheutil -flushcache 2>/dev/null || true
killall -HUP mDNSResponder 2>/dev/null || true
echo "[OK] DNS cache flushed."

echo ""
echo "Done! Start the server with 'sudo python3 main.py' and restart the WoW client."
echo "To undo: sudo $0 --remove"

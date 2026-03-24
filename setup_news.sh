#!/bin/bash
# One-time setup for login screen news.
# Redirects the WoW client's hardcoded news URL to localhost:8080.
# Run once with: sudo ./setup_news.sh
#
# What it does:
#   1. Adds a hosts entry so the client connects to localhost for news
#   2. Sets up port 80 → 8080 forwarding (macOS pf) so no root is needed at runtime
#
# To undo: sudo ./setup_news.sh --remove

set -e

HOSTS_ENTRY="127.0.0.1 launcher.worldofwarcraft.com"
PF_ANCHOR="com.viper.news"
PF_RULE="rdr pass on lo0 inet proto tcp from any to 127.0.0.1 port 80 -> 127.0.0.1 port 8080"

if [ "$(id -u)" -ne 0 ]; then
    echo "This script needs root. Run: sudo $0"
    exit 1
fi

if [ "$1" = "--remove" ]; then
    echo "Removing news redirect..."
    # Remove hosts entry
    sed -i '' '/launcher\.worldofwarcraft\.com/d' /etc/hosts
    # Remove pf rule
    pfctl -a "$PF_ANCHOR" -F all 2>/dev/null || true
    echo "Done. News redirect removed."
    exit 0
fi

echo "=== Viper News Setup ==="
echo ""

# 1. Hosts entry
if grep -q "launcher.worldofwarcraft.com" /etc/hosts 2>/dev/null; then
    echo "[OK] Hosts entry already present."
else
    echo "$HOSTS_ENTRY" >> /etc/hosts
    echo "[OK] Added hosts entry: $HOSTS_ENTRY"
fi

# 2. Port forwarding (macOS pf)
echo "$PF_RULE" | pfctl -a "$PF_ANCHOR" -f - 2>/dev/null
pfctl -e 2>/dev/null || true
echo "[OK] Port 80 → 8080 forwarding enabled."

echo ""
echo "Done! The WoW client will now show Viper news on the login screen."
echo "To undo: sudo $0 --remove"

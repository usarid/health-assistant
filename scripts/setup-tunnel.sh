#!/bin/bash
# Setup script for Cloudflare Tunnel + Cloudflare Access for Bina Health
# Run this on the Mac Mini after cloning the repo
set -euo pipefail

HOSTNAME="bina.saridium.com"
TUNNEL_NAME="bina-health"
LOCAL_PORT="3080"

echo "=== Bina Health — Cloudflare Tunnel Setup ==="
echo ""

# 1. Check for cloudflared
if ! command -v cloudflared &>/dev/null; then
  echo "Installing cloudflared..."
  brew install cloudflared
else
  echo "cloudflared already installed: $(cloudflared --version)"
fi

# 2. Login (opens browser)
echo ""
echo "Step 1: Authenticate with Cloudflare"
echo "  This will open a browser window. Select saridium.com."
read -p "Press Enter to continue..."
cloudflared tunnel login

# 3. Create tunnel
echo ""
echo "Step 2: Creating tunnel '$TUNNEL_NAME'..."
TUNNEL_OUTPUT=$(cloudflared tunnel create "$TUNNEL_NAME" 2>&1) || {
  if echo "$TUNNEL_OUTPUT" | grep -q "already exists"; then
    echo "  Tunnel '$TUNNEL_NAME' already exists, reusing it."
  else
    echo "  Error: $TUNNEL_OUTPUT"
    exit 1
  fi
}

# Get tunnel ID
TUNNEL_ID=$(cloudflared tunnel list | grep "$TUNNEL_NAME" | awk '{print $1}')
echo "  Tunnel ID: $TUNNEL_ID"

# 4. Write config
CONFIG_DIR="$HOME/.cloudflared"
CONFIG_FILE="$CONFIG_DIR/config.yml"
CREDS_FILE="$CONFIG_DIR/${TUNNEL_ID}.json"

echo ""
echo "Step 3: Writing config to $CONFIG_FILE"
cat > "$CONFIG_FILE" <<EOF
tunnel: $TUNNEL_ID
credentials-file: $CREDS_FILE

ingress:
  - hostname: $HOSTNAME
    service: http://localhost:$LOCAL_PORT
  - service: http_status:404
EOF
echo "  Done."

# 5. Route DNS
echo ""
echo "Step 4: Routing DNS ($HOSTNAME → tunnel)"
cloudflared tunnel route dns "$TUNNEL_NAME" "$HOSTNAME" 2>&1 || echo "  (DNS record may already exist)"

# 6. Install as service
echo ""
echo "Step 5: Installing as launchd service (runs on boot)"
cloudflared service install 2>&1 || echo "  (Service may already be installed)"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Bina Health will be available at: https://$HOSTNAME"
echo ""
echo "To manage:"
echo "  Start:   sudo launchctl start com.cloudflare.cloudflared"
echo "  Stop:    sudo launchctl stop com.cloudflare.cloudflared"
echo "  Status:  cloudflared tunnel info $TUNNEL_NAME"
echo "  Logs:    sudo log show --predicate 'process == \"cloudflared\"' --last 5m"
echo ""
echo "IMPORTANT: Set up Cloudflare Access to protect your health data!"
echo "  See: scripts/setup-access.md"
echo ""

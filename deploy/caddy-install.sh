#!/usr/bin/env bash
#
# Put HTTPS in front of Tracepaper. Run INSIDE the container, as root:
#
#   /opt/tracepaper/deploy/caddy-install.sh
#   DOMAIN=tracepaper.example.net /opt/tracepaper/deploy/caddy-install.sh
#
# Installs Caddy, points it at the app, and switches Tracepaper to listening on
# localhost only so the plain-HTTP port is no longer reachable from the LAN.
# Safe to re-run; it converges on the same state.

set -Eeuo pipefail

# Your LAN-only name, via a local DNS rewrite. A public CA cannot sign for it
# -- there is nothing to validate from outside -- which is why this uses a
# certificate the container issues itself.
DOMAIN="${DOMAIN:-tracepaper.vaibhavbhatia.net}"
UNIT="/etc/systemd/system/tracepaper.service"
CADDYFILE="/etc/caddy/Caddyfile"
APP_DIR="${APP_DIR:-/opt/tracepaper}"

RD=$'\033[01;31m'; GN=$'\033[1;92m'; YW=$'\033[33m'; BL=$'\033[36m'; CL=$'\033[m'
msg_info()  { echo -e " ${BL}➜${CL} $1"; }
msg_ok()    { echo -e " ${GN}✔${CL} $1"; }
msg_warn()  { echo -e " ${YW}!${CL} $1"; }
msg_error() { echo -e " ${RD}✘${CL} $1" >&2; }

[[ $EUID -eq 0 ]] || { msg_error "run as root inside the container."; exit 1; }
[[ -f "$UNIT" ]] || { msg_error "$UNIT not found — is Tracepaper installed here?"; exit 1; }

# The port lives in the unit's ExecStart, not an env file.
APP_PORT="$(grep -oE '\-\-port[= ]+[0-9]+' "$UNIT" | grep -oE '[0-9]+' | head -1)"
APP_PORT="${APP_PORT:-8823}"

# ----------------------------------------------------------------- install ---
if ! command -v caddy >/dev/null 2>&1; then
  msg_info "Installing Caddy…"
  export DEBIAN_FRONTEND=noninteractive
  apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl gnupg >/dev/null
  # The official repo. The keyring goes to /usr/share/keyrings and the source is
  # pinned to it, so this cannot silently start trusting a different signer.
  curl -fsSL 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  echo "deb [signed-by=/usr/share/keyrings/caddy-stable-archive-keyring.gpg] https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main" \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq
  apt-get install -y -qq caddy >/dev/null
  msg_ok "Caddy installed ($(caddy version | head -1))"
else
  msg_ok "Caddy already present ($(caddy version | head -1))"
fi

# --------------------------------------------------------------- configure ---
msg_info "Writing ${CADDYFILE}…"
mkdir -p /etc/caddy
sed -e "s|{\$TRACEPAPER_DOMAIN}|${DOMAIN}|g" \
    -e "s|{\$TRACEPAPER_PORT}|${APP_PORT}|g" \
    "${APP_DIR}/deploy/Caddyfile" > "$CADDYFILE"

# Fails loudly on a syntax error rather than at restart time.
if ! caddy validate --config "$CADDYFILE" >/dev/null 2>&1; then
  msg_error "Caddyfile did not validate:"
  caddy validate --config "$CADDYFILE" || true
  exit 1
fi
msg_ok "Config valid"

# ------------------------------------------------------------- bind inward ---
# With a proxy in front, the app has no reason to accept LAN connections. If it
# still listened on 0.0.0.0 then http://host:8823 would keep working and quietly
# bypass TLS entirely -- the plaintext path has to actually go away.
if grep -q -- '--host 0\.0\.0\.0' "$UNIT"; then
  msg_info "Binding Tracepaper to localhost…"
  sed -i 's|--host 0\.0\.0\.0|--host 127.0.0.1|' "$UNIT"
  systemctl daemon-reload
  systemctl restart tracepaper
  msg_ok "Tracepaper now listens on 127.0.0.1:${APP_PORT} only"
else
  msg_ok "Tracepaper already bound to $(grep -oE '\-\-host [0-9.]+' "$UNIT" | awk '{print $2}' | head -1)"
fi

# ------------------------------------------------------------------- start ---
msg_info "Starting Caddy…"
systemctl enable --now caddy >/dev/null 2>&1 || true
systemctl reset-failed caddy 2>/dev/null || true
systemctl restart caddy

# A health loop that falls through to a success message reports "Done" for a
# dead service. Record whether it ever actually succeeded.
healthy=0
for _ in $(seq 1 20); do
  # -k because the local CA's root is not trusted anywhere yet. This checks the
  # TLS handshake and the proxy hop, not trust.
  if curl -skf --resolve "${DOMAIN}:443:127.0.0.1" "https://${DOMAIN}/api/health" >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 1
done

if [[ "$healthy" -ne 1 ]]; then
  msg_error "Caddy is not serving HTTPS."
  echo
  systemctl status caddy --no-pager -l 2>&1 | head -20
  echo
  msg_warn "Recent log:"
  journalctl -u caddy -n 20 --no-pager 2>&1 | sed 's/^/    /'
  echo
  msg_warn "The app itself is unaffected and still running on 127.0.0.1:${APP_PORT}."
  msg_warn "To undo the localhost binding and go back to plain HTTP on the LAN:"
  echo "    sed -i 's|--host 127.0.0.1|--host 0.0.0.0|' ${UNIT} && systemctl daemon-reload && systemctl restart tracepaper"
  exit 1
fi

msg_ok "HTTPS is serving"

ROOT_CRT="/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt"

echo
msg_ok "Done — https://${DOMAIN}"
echo
msg_warn "One step left: trust the CA root on each device you browse from."
echo "  Copy it off the container, from the Proxmox host:"
echo
echo "    pct pull <CTID> ${ROOT_CRT} tracepaper-root.crt"
echo
echo "  macOS:  sudo security add-trusted-cert -d -r trustRoot \\"
echo "            -k /Library/Keychains/System.keychain tracepaper-root.crt"
echo "  iOS:    AirDrop it, install the profile, then ALSO enable it under"
echo "          Settings > General > About > Certificate Trust Settings"
echo "  Android: Settings > Security > Encryption & credentials > Install a certificate > CA"
echo
echo "  Until then browsers warn — the connection is encrypted regardless."
echo
msg_warn "Point ${DOMAIN} at this container in your DNS if you have not already."

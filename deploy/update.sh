#!/usr/bin/env bash
#
# Update a running Tracepaper install. Run inside the container:
#
#   /opt/tracepaper/deploy/update.sh
#
# Deliberately manual rather than a timer. This pulls code and restarts a
# service that owns the only copy of your corrections — not something to do
# unattended at 3am with nobody watching. It backs up the human-authored layer
# first and rolls back if the new version fails to come up.

set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/tracepaper}"
UNIT_DIR="/etc/systemd/system"
CONFIG="${CONFIG:-/etc/tracepaper.toml}"
VENV="$APP_DIR/.venv"

RD=$'\033[01;31m'; GN=$'\033[1;92m'; YW=$'\033[33m'; BL=$'\033[36m'; CL=$'\033[m'
msg_info()  { echo -e " ${BL}➜${CL} $1"; }
msg_ok()    { echo -e " ${GN}✔${CL} $1"; }
msg_warn()  { echo -e " ${YW}!${CL} $1"; }
msg_error() { echo -e " ${RD}✘${CL} $1" >&2; }

[[ $EUID -eq 0 ]] || { msg_error "run as root inside the container."; exit 1; }
[[ -d "$APP_DIR/.git" ]] || {
  msg_error "$APP_DIR is not a git checkout — this install was copied in, not cloned."
  msg_warn  "Re-copy the source from your laptop, then re-run the install steps in deploy/README.md."
  exit 1
}

cd "$APP_DIR"

PORT="$(grep -oE '\-\-port[= ]+[0-9]+' "$UNIT_DIR/tracepaper.service" 2>/dev/null | grep -oE '[0-9]+' | head -1)"
PORT="${PORT:-8823}"

# ------------------------------------------------------------------ backup ---
# Before anything else, and specifically the human-authored layer: corrections,
# notes, entity and vocabulary merges. Everything else in the index is
# regenerable from your documents; this is not.
BACKUP="/var/backups/tracepaper-pre-update-$(date +%F-%H%M%S).json"
mkdir -p /var/backups
msg_info "Backing up corrections and notes…"
if sudo -u tracepaper "$VENV/bin/tracepaper" --config "$CONFIG" backup "$BACKUP" >/dev/null 2>&1; then
  msg_ok "Saved $BACKUP"
else
  msg_warn "Could not export. Continuing without a fresh backup."
  BACKUP=""
fi

# ------------------------------------------------------------------ update ---
BEFORE="$(git rev-parse HEAD)"
msg_info "Fetching…"
git fetch --quiet origin

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
TARGET="$(git rev-parse "origin/${BRANCH}")"

if [[ "$BEFORE" == "$TARGET" ]]; then
  msg_ok "Already up to date ($(git log -1 --format=%h\ %s))"
  exit 0
fi

echo
git --no-pager log --oneline "${BEFORE}..${TARGET}" | sed 's/^/    /'
echo

# Uncommitted local edits would be silently destroyed by a hard reset.
if ! git diff --quiet || ! git diff --cached --quiet; then
  msg_error "there are uncommitted changes in $APP_DIR."
  msg_warn  "Commit, stash or discard them first — this update would overwrite them."
  exit 1
fi

msg_info "Updating to $(git rev-parse --short "$TARGET")…"
git merge --quiet --ff-only "$TARGET"

# Reinstall in place. `-e` means the checkout is the install, so this only has
# to refresh dependencies and entry points — but skipping it means a new
# dependency is missing and the service fails to import.
msg_info "Installing dependencies…"
EXTRAS="$("$VENV/bin/python" - <<'PY'
# Reuse whatever extras this install already has rather than guessing. Asking
# the venv is more reliable than a flag someone has to remember: reinstalling
# without 'semantic' would silently turn semantic search off.
import importlib.util
have = {
    "formats": importlib.util.find_spec("pypdf"),
    "ocr": importlib.util.find_spec("pytesseract"),
    "photos": importlib.util.find_spec("reverse_geocoder"),
    "web": importlib.util.find_spec("fastapi"),
    "semantic": importlib.util.find_spec("sentence_transformers"),
}
print(",".join(name for name, spec in have.items() if spec))
PY
)"
EXTRAS="${EXTRAS:-formats,ocr,photos,web}"
msg_info "Extras: ${EXTRAS}"
"$VENV/bin/pip" install --quiet -e ".[${EXTRAS}]"

if [[ ! -x "$VENV/bin/tracepaper" ]]; then
  msg_error "install did not produce the \`tracepaper\` entry point."
  exit 1
fi
# The schema is applied on connect and is additive, so there is no separate
# migration step — but proving the module imports catches a broken install
# before the service restart does.
if ! "$VENV/bin/python" -c "import tracepaper.api" >/dev/null 2>&1; then
  msg_error "the package does not import after the update."
  "$VENV/bin/python" -c "import tracepaper.api" 2>&1 | tail -20 || true
  exit 1
fi
chown -R tracepaper:tracepaper "$VENV" 2>/dev/null || true
msg_ok "Installed"

# The units live in /etc, so a pull alone never updates them. Skipping this is
# how a fix to a service file fails to reach a running install.
UNITS_CHANGED=0
for unit in tracepaper.service tracepaper-scan.service tracepaper-scan.timer \
            tracepaper-enrich.service tracepaper-enrich.timer; do
  if [[ -f "deploy/$unit" ]] && ! cmp -s "deploy/$unit" "$UNIT_DIR/$unit"; then
    # Preserve a non-default port rather than resetting it on every update.
    if [[ "$unit" == "tracepaper.service" && "$PORT" != "8823" ]]; then
      sed "s/--port 8823/--port ${PORT}/" "deploy/$unit" > "$UNIT_DIR/$unit"
    else
      cp "deploy/$unit" "$UNIT_DIR/$unit"
    fi
    UNITS_CHANGED=1
    msg_info "Updated $unit"
  fi
done
if [[ "$UNITS_CHANGED" == "1" ]]; then
  systemctl daemon-reload
  msg_ok "Units updated"
fi

# ----------------------------------------------------------------- restart ---
msg_info "Restarting…"
systemctl reset-failed tracepaper 2>/dev/null || true
systemctl restart tracepaper

for _ in $(seq 1 30); do
  if curl -sf "localhost:${PORT}/api/health" >/dev/null 2>&1; then
    msg_ok "Healthy on $(git log -1 --format=%h\ %s)"
    [[ -n "$BACKUP" ]] && msg_info "Pre-update backup: $BACKUP"
    exit 0
  fi
  sleep 2
done

# -------------------------------------------------------------- roll back ---
# An update that leaves the service down is worse than no update. Go back to
# the commit that was running, reinstall it, and say so plainly.
msg_error "service did not come up within 60s — rolling back to ${BEFORE:0:7}."
# Must be a reset, not a merge: BEFORE is an ancestor of what is checked out,
# so --ff-only cannot reach it. Flags go before the revision — `--quiet` after
# it is treated as a pathspec and silently ignored.
git reset --hard --quiet "$BEFORE"
"$VENV/bin/pip" install --quiet -e ".[${EXTRAS}]" >/dev/null 2>&1 || true
chown -R tracepaper:tracepaper "$VENV" 2>/dev/null || true
for unit in tracepaper.service tracepaper-scan.service tracepaper-scan.timer \
            tracepaper-enrich.service tracepaper-enrich.timer; do
  [[ -f "deploy/$unit" ]] && cp "deploy/$unit" "$UNIT_DIR/$unit" 2>/dev/null || true
done
[[ "$PORT" != "8823" ]] && sed -i "s/--port 8823/--port ${PORT}/" "$UNIT_DIR/tracepaper.service"
systemctl daemon-reload
systemctl reset-failed tracepaper 2>/dev/null || true
systemctl restart tracepaper

for _ in $(seq 1 30); do
  if curl -sf "localhost:${PORT}/api/health" >/dev/null 2>&1; then
    msg_ok "Rolled back to ${BEFORE:0:7} and healthy."
    msg_warn "The update failed. Logs: journalctl -u tracepaper -n 50"
    exit 1
  fi
  sleep 2
done

msg_error "rollback also failed to come up. The index is intact; the service is not running."
[[ -n "$BACKUP" ]] && msg_warn "Backup: $BACKUP"
msg_warn "Logs: journalctl -u tracepaper -n 50"
exit 1

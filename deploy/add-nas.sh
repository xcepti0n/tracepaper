#!/usr/bin/env bash
#
# Attach Synology NFS shares to an existing Tracepaper container.
#
# Run this ON THE PROXMOX HOST, any time after the install:
#
#   ./add-nas.sh <CTID> <nas-ip>
#   NAS_DOCS_EXPORT=/volume1/docs ./add-nas.sh 122 192.168.0.20
#
# The installer takes NAS_HOST and does this during setup, but it is optional
# there on purpose: you can install first, confirm the UI works, and attach
# storage once you have the export paths and DSM permissions sorted.
#
# Why this is not a button in the web UI: mounting needs root on the *host*,
# outside the container the app runs in. An app that mounts filesystems turns
# every stale handle and credential problem into its own bug. This script is
# the same code path the installer uses, runnable on its own.
#
# Safe to re-run: existing fstab entries and mount points are left alone.

set -Eeuo pipefail

CTID="${1:-}"
NAS_HOST="${2:-${NAS_HOST:-}}"

NAS_DOCS_EXPORT="${NAS_DOCS_EXPORT:-/volume1/documents}"
NAS_BACKUP_EXPORT="${NAS_BACKUP_EXPORT:-/volume1/backups/tracepaper}"
DOCS_MOUNT="${DOCS_MOUNT:-/mnt/nas/documents}"
BACKUP_MOUNT="${BACKUP_MOUNT:-/mnt/nas/backups/tracepaper}"
NFS_VERS="${NFS_VERS:-4.1}"
CONFIG="${CONFIG:-/etc/tracepaper.toml}"

RD=$'\033[01;31m'; GN=$'\033[1;92m'; YW=$'\033[33m'; BL=$'\033[36m'; CL=$'\033[m'
msg_info()  { echo -e " ${BL}➜${CL} $1"; }
msg_ok()    { echo -e " ${GN}✔${CL} $1"; }
msg_warn()  { echo -e " ${YW}!${CL} $1"; }
msg_error() { echo -e " ${RD}✘${CL} $1" >&2; }

usage() {
  echo "usage: $0 <CTID> <nas-ip-or-hostname>"
  echo
  echo "  e.g. $0 122 192.168.0.20"
  echo
  echo "  NAS_DOCS_EXPORT    default $NAS_DOCS_EXPORT"
  echo "  NAS_BACKUP_EXPORT  default $NAS_BACKUP_EXPORT"
  exit 1
}

[[ -n "$CTID" && -n "$NAS_HOST" ]] || usage
command -v pct >/dev/null 2>&1 || {
  msg_error "\`pct\` not found — run this on the Proxmox host, not inside the container."
  exit 1
}
[[ $EUID -eq 0 ]] || { msg_error "must run as root on the Proxmox host."; exit 1; }
pct status "$CTID" >/dev/null 2>&1 || { msg_error "no container with ID $CTID."; exit 1; }

inct() { pct exec "$CTID" -- env LC_ALL=C LANG=C bash -ec "$1"; }

host_docs="/mnt/pve/tracepaper-documents"
host_backup="/mnt/pve/tracepaper-backups"

# Append an fstab entry only if that mount point is not already configured, so
# re-running this does not stack duplicates.
add_fstab_line() {
  local source="$1" point="$2" options="$3"
  if grep -qE "[[:space:]]${point}[[:space:]]" /etc/fstab 2>/dev/null; then
    msg_info "fstab already has an entry for ${point} — leaving it alone."
    return
  fi
  printf '%s %s nfs %s 0 0\n' "$source" "$point" "$options" >> /etc/fstab
}

msg_info "Mounting NFS on the Proxmox host…"
command -v mount.nfs >/dev/null 2>&1 || \
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nfs-common >/dev/null 2>&1 || true

mkdir -p "$host_docs" "$host_backup"

# soft,timeo=150,retrans=3 rather than the default `hard`: a NAS that goes away
# must not wedge a scan in uninterruptible sleep forever. Soft returns an error,
# the scan hits its vanish guard and aborts, and nothing is deleted.
common="soft,timeo=150,retrans=3,nfsvers=${NFS_VERS},noatime,_netdev,nofail"
add_fstab_line "${NAS_HOST}:${NAS_DOCS_EXPORT}"   "$host_docs"   "ro,${common}"
add_fstab_line "${NAS_HOST}:${NAS_BACKUP_EXPORT}" "$host_backup" "rw,${common}"
systemctl daemon-reload >/dev/null 2>&1 || true

if ! mount "$host_docs" 2>/dev/null && ! mountpoint -q "$host_docs"; then
  msg_error "could not mount ${NAS_HOST}:${NAS_DOCS_EXPORT}."
  msg_warn  "In DSM: Control Panel → Shared Folder → the share → Edit → NFS Permissions."
  msg_warn  "Add a rule for this host's IP, then re-run this script."
  exit 1
fi
msg_ok "Documents mounted read-only at ${host_docs}"

backup_ok=1
if ! mount "$host_backup" 2>/dev/null && ! mountpoint -q "$host_backup"; then
  backup_ok=0
  msg_warn "Could not mount the backup export — continuing without it."
  msg_warn "Create ${NAS_BACKUP_EXPORT} in DSM and re-run to add backups."
fi
[[ "$backup_ok" == "1" ]] && msg_ok "Backups mounted read-write at ${host_backup}" || true

# Bind into the container. mp0 is read-only, so NFR-7 is enforced by the kernel
# rather than promised by the code.
msg_info "Binding the shares into container ${CTID}…"
pct set "$CTID" -mp0 "${host_docs},mp=${DOCS_MOUNT},ro=1" >/dev/null
if [[ "$backup_ok" == "1" ]]; then
  pct set "$CTID" -mp1 "${host_backup},mp=${BACKUP_MOUNT}" >/dev/null
fi

# A bind mount of an existing mount point only appears after a restart.
msg_info "Restarting the container…"
pct stop "$CTID" >/dev/null 2>&1 || true
pct start "$CTID" >/dev/null
for _ in $(seq 1 30); do
  inct "true" >/dev/null 2>&1 && break
  sleep 2
done

inct "test -d '${DOCS_MOUNT}'" || {
  msg_error "the documents path is not visible inside the container."
  exit 1
}
count=$(inct "ls -1 '${DOCS_MOUNT}' 2>/dev/null | head -1000 | wc -l" || echo 0)
msg_ok "Documents visible inside the container (${count// /} entries at the top level)"

# Point the config at what is now mounted. sed rather than a rewrite, so any
# hand edits to the rest of the file survive.
msg_info "Updating ${CONFIG}…"
inct "sed -i 's#^roots = \\[\\]#roots = [\"${DOCS_MOUNT}\"]#' '${CONFIG}'"
if [[ "$backup_ok" == "1" ]] && ! inct "grep -q '^backup_dir' '${CONFIG}'"; then
  inct "sed -i '/^db_path/a backup_dir = \"${BACKUP_MOUNT}\"' '${CONFIG}'"
fi
inct "grep -E '^(roots|db_path|backup_dir)' '${CONFIG}'" | sed 's/^/    /'

inct "systemctl restart tracepaper"
msg_ok "Service restarted"

echo
msg_ok "Storage attached."
echo
echo "  Start the first scan when you are ready to watch it:"
echo
echo "    pct exec ${CTID} -- systemctl start tracepaper-scan"
echo "    pct exec ${CTID} -- journalctl -u tracepaper-scan -f"
echo

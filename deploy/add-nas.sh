#!/usr/bin/env bash
#
# Attach Synology NFS shares to an existing Tracepaper container.
#
# Run this ON THE PROXMOX HOST, any time after the install:
#
#   ./add-nas.sh <CTID> <nas-ip>
#   READ_SHARE=/volume1/data DOCS_SUBDIR=Documents ./add-nas.sh 122 192.168.0.20
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

# The two shares, named for what Tracepaper does with each.
#
# READ_SHARE   the share holding your documents. Mounted READ-ONLY; Tracepaper
#              never writes here, and the kernel enforces that.
# WRITE_SHARE  a DIFFERENT share where Tracepaper writes backups of the things
#              that cannot be regenerated: your corrections, notes and merges.
#
# Both are the "Mount path" DSM shows at the bottom of the NFS Permissions
# dialog -- e.g. /volume1/documents. In NFS jargon that path is called an
# "export", which is why the older names said EXPORT; they are accepted still.
READ_SHARE="${READ_SHARE:-${NAS_DOCS_EXPORT:-/volume1/documents}}"
WRITE_SHARE="${WRITE_SHARE:-${NAS_BACKUP_EXPORT:-/volume1/backups/tracepaper}}"
DOCS_MOUNT="${DOCS_MOUNT:-/mnt/nas/documents}"

# The folder to actually index, relative to DOCS_MOUNT.
#
# NFS usually exports a whole share, but the documents you want are often a
# subfolder of it. Mounting the share and scanning a subtree keeps the mount
# simple and the scan narrow: everything else under the share stays visible but
# is never read.
#
#   READ_SHARE=/volume1/data DOCS_SUBDIR=Documents/Personal
#   -> mounts /volume1/data, scans /mnt/nas/documents/Documents/Personal
DOCS_SUBDIR="${DOCS_SUBDIR:-}"

# nfs (default) or smb.
#
# NFS with sec=sys does not authenticate users at all -- it trusts the uid the
# client sends, and access is granted per client IP. SMB authenticates with a
# real account, which is what you want if you created a dedicated NAS user to
# scope access. SMB also mounts a subfolder directly, so DOCS_SUBDIR is usually
# unnecessary with it.
PROTOCOL="${PROTOCOL:-nfs}"

# SMB only: a credentials file on the HOST, mode 600, containing
#   username=tracepaper
#   password=...
SMB_CREDENTIALS="${SMB_CREDENTIALS:-/etc/samba/tracepaper.cred}"
SMB_VERS="${SMB_VERS:-3.0}"
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
  echo "  READ_SHARE   the share with your documents, mounted READ-ONLY"
  echo "               default $READ_SHARE"
  echo "  DOCS_SUBDIR  folder inside READ_SHARE to actually index"
  echo "               default: the whole share"
  echo "  WRITE_SHARE  a DIFFERENT share, mounted read-write, for backups"
  echo "               default $WRITE_SHARE"
  echo "  PROTOCOL     nfs | smb  (default $PROTOCOL)"
  echo "  SMB_CREDENTIALS  smb only, default $SMB_CREDENTIALS"
  echo
  echo "  Documents in a subfolder of a share:"
  echo "    READ_SHARE=/volume1/data DOCS_SUBDIR=Documents \\"
  echo "      $0 122 192.168.0.20"
  echo
  echo "  Using a dedicated NAS account instead of IP-based access:"
  echo "    PROTOCOL=smb READ_SHARE=/data/Documents $0 122 192.168.0.20"
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

# Echo the settings before doing anything. A shell variable set on its own line
# never reaches this script, so the most common mistake is running with silent
# defaults -- which is visible here and invisible everywhere else.
echo
echo "  Read  ${READ_SHARE}${DOCS_SUBDIR:+  (indexing ${DOCS_SUBDIR})}"
echo "  Write ${WRITE_SHARE}"
echo "  From  ${NAS_HOST} over ${PROTOCOL}  →  container ${CTID}"
echo

mkdir -p "$host_docs" "$host_backup"

case "$PROTOCOL" in
  nfs)
    msg_info "Mounting NFS on the Proxmox host…"
    command -v mount.nfs >/dev/null 2>&1 || \
      DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nfs-common >/dev/null 2>&1 || true

    # soft,timeo=150,retrans=3 rather than the default `hard`: a NAS that goes
    # away must not wedge a scan in uninterruptible sleep forever. Soft returns
    # an error, the scan hits its vanish guard and aborts, nothing is deleted.
    common="soft,timeo=150,retrans=3,nfsvers=${NFS_VERS},noatime,_netdev,nofail"
    add_fstab_line "${NAS_HOST}:${READ_SHARE}"   "$host_docs"   "ro,${common}"
    add_fstab_line "${NAS_HOST}:${WRITE_SHARE}" "$host_backup" "rw,${common}"
    ;;

  smb|cifs)
    msg_info "Mounting SMB on the Proxmox host…"
    command -v mount.cifs >/dev/null 2>&1 || \
      DEBIAN_FRONTEND=noninteractive apt-get install -y -qq cifs-utils >/dev/null 2>&1 || true

    if [[ ! -f "$SMB_CREDENTIALS" ]]; then
      msg_error "no credentials file at ${SMB_CREDENTIALS}."
      msg_warn  "Create it on this host, readable only by root:"
      echo
      echo "    install -m600 /dev/null ${SMB_CREDENTIALS}"
      echo "    cat > ${SMB_CREDENTIALS} <<'EOF'"
      echo "    username=tracepaper"
      echo "    password=<the password you set in DSM>"
      echo "    EOF"
      echo
      exit 1
    fi
    # A credentials file readable by anyone is a password anyone can read.
    perms=$(stat -c '%a' "$SMB_CREDENTIALS")
    if [[ "$perms" != "600" && "$perms" != "400" ]]; then
      msg_warn "${SMB_CREDENTIALS} is mode ${perms}; tightening to 600."
      chmod 600 "$SMB_CREDENTIALS"
    fi

    # uid=100000 is root inside an unprivileged LXC as seen from the host: the
    # default id-map shifts container uids by 100000. Files land owned by the
    # container's root, which the service can read.
    common="credentials=${SMB_CREDENTIALS},vers=${SMB_VERS},iocharset=utf8,uid=100000,gid=100000,_netdev,nofail"
    add_fstab_line "//${NAS_HOST}/${READ_SHARE#/}"   "$host_docs"   "ro,${common}"
    add_fstab_line "//${NAS_HOST}/${WRITE_SHARE#/}" "$host_backup" "rw,${common}"
    ;;

  *)
    msg_error "PROTOCOL must be nfs or smb, got '${PROTOCOL}'."
    exit 1
    ;;
esac

systemctl daemon-reload >/dev/null 2>&1 || true

if ! mount "$host_docs" 2>/dev/null && ! mountpoint -q "$host_docs"; then
  msg_error "could not mount ${READ_SHARE} from ${NAS_HOST}."
  echo
  # The real error, rather than the one this script guessed at.
  msg_warn "What mount actually said:"
  mount "$host_docs" 2>&1 | sed 's/^/    /' || true
  echo

  if [[ "$PROTOCOL" == "nfs" ]]; then
    # The NAS knows the answer, so ask it rather than making the user guess.
    if command -v showmount >/dev/null 2>&1; then
      msg_warn "What ${NAS_HOST} actually exports:"
      if showmount -e "$NAS_HOST" 2>&1 | sed 's/^/    /'; then
        echo
        msg_warn "READ_SHARE must be one of the paths listed above, exactly."
      fi
    else
      msg_warn "Install nfs-common and run: showmount -e ${NAS_HOST}"
      msg_warn "That lists every path the NAS exports and who may mount it."
    fi
    echo
    msg_warn "If the path is right but access is refused, the DSM rule is for the"
    msg_warn "wrong IP: it must name THIS host ($(hostname -I 2>/dev/null | awk '{print $1}')),"
    msg_warn "not the container. NFS grants by client IP; a DSM user is never consulted."
  else
    msg_warn "Check the username and password in ${SMB_CREDENTIALS}, and that the"
    msg_warn "user has at least read access to the share in DSM."
    if command -v smbclient >/dev/null 2>&1; then
      msg_warn "What ${NAS_HOST} shares:"
      smbclient -L "//${NAS_HOST}" -A "$SMB_CREDENTIALS" 2>&1 | sed 's/^/    /' | head -20 || true
    fi
  fi
  echo
  msg_warn "Note: setting VAR=value on its own line sets a SHELL variable, which"
  msg_warn "this script never sees. Use \`export VAR=value\` or put them on the"
  msg_warn "same line as the command."
  exit 1
fi
msg_ok "Documents mounted read-only at ${host_docs}"

backup_ok=1
if ! mount "$host_backup" 2>/dev/null && ! mountpoint -q "$host_backup"; then
  backup_ok=0
  msg_warn "Could not mount the backup export — continuing without it."
  msg_warn "Create ${WRITE_SHARE} in DSM and re-run to add backups."
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

# The folder Tracepaper will actually scan. Checked separately, because a typo
# in DOCS_SUBDIR otherwise surfaces much later as a scan that finds nothing.
SCAN_ROOT="$DOCS_MOUNT"
if [[ -n "$DOCS_SUBDIR" ]]; then
  SCAN_ROOT="${DOCS_MOUNT%/}/${DOCS_SUBDIR#/}"
  if ! inct "test -d '${SCAN_ROOT}'"; then
    msg_error "${DOCS_SUBDIR} does not exist inside the mounted share."
    msg_warn  "What is actually there:"
    inct "ls -1 '${DOCS_MOUNT}' 2>/dev/null | head -20" | sed 's/^/    /' || true
    msg_warn  "The mount itself worked — fix DOCS_SUBDIR and re-run."
    exit 1
  fi
fi

count=$(inct "ls -1 '${SCAN_ROOT}' 2>/dev/null | head -1000 | wc -l" || echo 0)
msg_ok "Will index ${SCAN_ROOT} (${count// /} entries at the top level)"

# Point the config at what is now mounted. sed rather than a rewrite, so any
# hand edits to the rest of the file survive.
msg_info "Updating ${CONFIG}…"
inct "sed -i 's#^roots = \\[\\]#roots = [\"${SCAN_ROOT}\"]#' '${CONFIG}'"
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

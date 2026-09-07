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
  local source="$1" point="$2" fstype="$3" options="$4"
  local wanted="${source} ${point} ${fstype} ${options} 0 0"

  # `|| true` is load-bearing: no match makes grep exit 1, and under
  # `set -eo pipefail` that aborts the script -- on a FRESH host, where there is
  # correctly nothing to find. Not finding a line is the normal case, not an
  # error.
  local existing=""
  existing=$(grep -E "^[^#]\S*[[:space:]]+${point}[[:space:]]" /etc/fstab 2>/dev/null | head -1 || true)

  if [[ -z "$existing" ]]; then
    printf '%s\n' "$wanted" >> /etc/fstab
    return
  fi

  if [[ "$existing" == "$wanted" ]]; then
    msg_info "fstab entry for ${point} is already correct."
    return
  fi

  # Skipping here is what made a first run with the wrong values sticky: the
  # entry was written from the defaults, and every later run "left it alone"
  # while `mount <point>` kept reading the stale source out of fstab. The
  # arguments this script was given are the intent; fstab is the cache.
  msg_warn "fstab entry for ${point} does not match — replacing it."
  echo "    was:  ${existing}"
  echo "    now:  ${wanted}"

  # Timestamped, because /etc/fstab is the file that decides whether the host
  # boots. A backup per change is cheap.
  cp /etc/fstab "/etc/fstab.tracepaper-$(date +%Y%m%d-%H%M%S).bak"

  # The mount point is unique in fstab, so this rewrites exactly one line.
  # awk over sed: the paths contain slashes, and building a sed expression
  # around them needs escaping that is easy to get subtly wrong.
  awk -v point="$point" -v line="$wanted" '
    $0 ~ /^[[:space:]]*#/ { print; next }
    $2 == point           { print line; replaced = 1; next }
                          { print }
    END { if (!replaced) print line }
  ' /etc/fstab > /etc/fstab.tracepaper-new && mv /etc/fstab.tracepaper-new /etc/fstab

  # A stale mount would otherwise keep serving the old export until reboot.
  if mountpoint -q "$point"; then
    msg_info "Unmounting the stale ${point}…"
    umount "$point" 2>/dev/null || umount -l "$point" 2>/dev/null || true
  fi
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
    add_fstab_line "${NAS_HOST}:${READ_SHARE}"   "$host_docs"   "nfs" "ro,${common}"
    add_fstab_line "${NAS_HOST}:${WRITE_SHARE}" "$host_backup" "nfs" "rw,${common}"
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

    # SMB has no uid negotiation: the server authenticates the account, and the
    # client decides what the files look like locally. So the mount must present
    # them as owned by the uid the SERVICE runs as -- not the container's root,
    # which can read a world-readable tree but cannot write the backup share.
    #
    # Asked, not assumed: the service uid is whatever adduser --system picked,
    # and the id-map offset is whatever this container is configured with.
    svc_uid=$(pct exec "$CTID" -- id -u tracepaper 2>/dev/null || echo "")
    svc_gid=$(pct exec "$CTID" -- id -g tracepaper 2>/dev/null || echo "")
    if [[ -z "$svc_uid" || -z "$svc_gid" ]]; then
      msg_error "could not read the tracepaper uid inside container ${CTID}."
      msg_warn  "Is Tracepaper installed there? pct exec ${CTID} -- id tracepaper"
      exit 1
    fi

    # An unprivileged container's uids are offset on the host. Read the offset
    # from the container's own id-map rather than hardcoding 100000: a custom
    # map would silently produce files the service cannot touch.
    id_offset=$(pct config "$CTID" | sed -n 's/^lxc.idmap: u 0 \([0-9]*\) .*/\1/p' | head -1)
    id_offset="${id_offset:-100000}"
    host_uid=$(( svc_uid + id_offset ))
    host_gid=$(( svc_gid + id_offset ))
    msg_info "Mapping files to the service account (uid ${svc_uid} in the container, ${host_uid} here)."

    common="credentials=${SMB_CREDENTIALS},vers=${SMB_VERS},iocharset=utf8,uid=${host_uid},gid=${host_gid},file_mode=0640,dir_mode=0750,_netdev,nofail"
    add_fstab_line "//${NAS_HOST}/${READ_SHARE#/}"   "$host_docs"   "cifs" "ro,${common}"
    add_fstab_line "//${NAS_HOST}/${WRITE_SHARE#/}" "$host_backup" "cifs" "rw,${common}"
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

# Point the config at what is now mounted.
#
# This REPLACES whatever roots was, rather than only filling an empty list.
# Matching `roots = []` alone meant a re-run silently kept the previous value:
# the script reported the path it intended while the config still held the old
# one, and the scan then failed on a path nobody had asked for. What this run
# was told is the intent; the config is the cache.
msg_info "Updating ${CONFIG}…"

# sed rather than a rewrite, so hand edits elsewhere in the file survive.
inct "sed -i 's#^roots = .*#roots = [\"${SCAN_ROOT}\"]#' '${CONFIG}'"

if [[ "$backup_ok" == "1" ]]; then
  if inct "grep -q '^backup_dir' '${CONFIG}'"; then
    inct "sed -i 's#^backup_dir = .*#backup_dir = \"${BACKUP_MOUNT}\"#' '${CONFIG}'"
  else
    inct "sed -i '/^db_path/a backup_dir = \"${BACKUP_MOUNT}\"' '${CONFIG}'"
  fi
fi

inct "grep -E '^(roots|db_path|backup_dir)' '\${CONFIG}'" | sed 's/^/    /'

# Read back what is actually on disk and confirm it matches the intent. The
# previous version printed the file and let a mismatch slide past unnoticed.
if ! inct "grep -qF 'roots = [\"${SCAN_ROOT}\"]' '${CONFIG}'"; then
  msg_error "the config was not updated to ${SCAN_ROOT}."
  msg_warn  "Edit ${CONFIG} in the container by hand, or from the Settings page."
  exit 1
fi

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

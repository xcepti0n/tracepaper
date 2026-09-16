#!/usr/bin/env bash
#
# Attach one more read-only share to an existing Tracepaper container.
#
# Run this ON THE PROXMOX HOST:
#
#   ./add-share.sh <CTID> <nas-ip> <share-path> <mount-name>
#
# Example, for a Photos folder inside a Synology home directory:
#
#   ./add-share.sh 103 192.168.0.28 /homes/vaibhav_bhatia/Photos photos
#
# That mounts it at /mnt/nas/photos inside the container, read-only, and adds
# it to the indexed roots.
#
# add-nas.sh handles the two shares the install is built around: documents and
# a writable backup target. This one is for everything after that, when a
# library lives somewhere else on the NAS. It is read-only with no option to
# change that, because a share Tracepaper only reads cannot be damaged by a
# bug in Tracepaper.
#
# Why this is not a button in the web UI: mounting needs root on the HOST,
# outside the container the app runs in, and the app has no way to reach it.
# See the same note in add-nas.sh.
#
# Safe to re-run: an existing fstab entry or mount point is left alone.
set -Eeuo pipefail

CTID="${1:-}"
NAS_HOST="${2:-${NAS_HOST:-}}"
SHARE="${3:-${SHARE:-}}"
NAME="${4:-${NAME:-}}"

PROTOCOL="${PROTOCOL:-smb}"
SMB_VERS="${SMB_VERS:-3.0}"
NFS_VERS="${NFS_VERS:-4.1}"
SMB_CREDENTIALS="${SMB_CREDENTIALS:-/etc/tracepaper-smb.cred}"
CONFIG="${CONFIG:-/etc/tracepaper/tracepaper.toml}"

RD=$'\033[01;31m'; GN=$'\033[1;92m'; YW=$'\033[33m'; BL=$'\033[36m'; CL=$'\033[m'
msg_info()  { echo -e " ${BL}➜${CL} $1"; }
msg_ok()    { echo -e " ${GN}✔${CL} $1"; }
msg_warn()  { echo -e " ${YW}!${CL} $1"; }
msg_error() { echo -e " ${RD}✘${CL} $1" >&2; }

usage() {
  cat >&2 <<'EOF'
Usage: ./add-share.sh <CTID> <nas-ip> <share-path> <mount-name>

  CTID         the container, e.g. 103
  nas-ip       the NAS address, e.g. 192.168.0.28
  share-path   the path on the NAS, e.g. /homes/vaibhav_bhatia/Photos
  mount-name   a short name; becomes /mnt/nas/<name> in the container

Environment:
  PROTOCOL=smb|nfs          default smb
  SMB_CREDENTIALS=<path>    default /etc/tracepaper-smb.cred
  SMB_VERS / NFS_VERS       protocol version
EOF
  exit 1
}

[[ -n "$CTID" && -n "$NAS_HOST" && -n "$SHARE" && -n "$NAME" ]] || usage
[[ $EUID -eq 0 ]] || { msg_error "run as root on the Proxmox host."; exit 1; }
command -v pct >/dev/null 2>&1 || {
  msg_error "pct not found. Run this on the Proxmox host, not in the container."
  exit 1
}
pct config "$CTID" >/dev/null 2>&1 || { msg_error "no container ${CTID}."; exit 1; }

# A mount name becomes a path, so it must not be able to escape one.
[[ "$NAME" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || {
  msg_error "mount-name must be lowercase letters, digits, dash or underscore."
  exit 1
}

HOST_DIR="/mnt/tracepaper-${CTID}-${NAME}"
GUEST_DIR="/mnt/nas/${NAME}"

# ---------------------------------------------------------------- fstab ------
add_fstab_line() {
  local source="$1" point="$2" fstype="$3" options="$4"
  local wanted="${source} ${point} ${fstype} ${options} 0 0"
  # `|| true`: grep exits 1 when nothing matches, which on a host without this
  # entry is the normal case rather than a failure.
  local existing=""
  existing=$(grep -E "^[^#]\S*[[:space:]]+${point}[[:space:]]" /etc/fstab 2>/dev/null | head -1 || true)
  if [[ -z "$existing" ]]; then
    printf '%s\n' "$wanted" >> /etc/fstab
    msg_ok "Added to /etc/fstab"
  else
    msg_ok "Already in /etc/fstab, left as it is"
  fi
}

mkdir -p "$HOST_DIR"

# The share is mounted as the container's service account, so the files are
# readable inside without granting anyone else access on the host. The uid
# inside an unprivileged container is offset by the idmap.
svc_uid=$(pct exec "$CTID" -- id -u tracepaper 2>/dev/null || echo 1000)
svc_gid=$(pct exec "$CTID" -- id -g tracepaper 2>/dev/null || echo 1000)
id_offset=$(pct config "$CTID" | sed -n 's/^lxc.idmap: u 0 \([0-9]*\) .*/\1/p' | head -1)
id_offset="${id_offset:-100000}"
host_uid=$(( svc_uid + id_offset ))
host_gid=$(( svc_gid + id_offset ))

case "$PROTOCOL" in
  smb|cifs)
    command -v mount.cifs >/dev/null 2>&1 || \
      DEBIAN_FRONTEND=noninteractive apt-get install -y -qq cifs-utils >/dev/null 2>&1 || true

    if [[ ! -f "$SMB_CREDENTIALS" ]]; then
      msg_error "no credentials file at ${SMB_CREDENTIALS}."
      msg_warn  "The documents share already uses one; this reuses it."
      msg_warn  "If it is missing, create it readable only by root:"
      echo
      echo "    install -m600 /dev/null ${SMB_CREDENTIALS}"
      echo "    printf 'username=%s\\npassword=%s\\n' USER PASS > ${SMB_CREDENTIALS}"
      echo
      exit 1
    fi
    chmod 600 "$SMB_CREDENTIALS" 2>/dev/null || true

    # ro here AND ro=1 on the bind below: the kernel refuses a write at both
    # layers, so a bug in Tracepaper cannot touch the originals.
    options="ro,credentials=${SMB_CREDENTIALS},vers=${SMB_VERS},iocharset=utf8"
    options="${options},uid=${host_uid},gid=${host_gid}"
    options="${options},file_mode=0640,dir_mode=0750,_netdev,nofail"
    add_fstab_line "//${NAS_HOST}/${SHARE#/}" "$HOST_DIR" "cifs" "$options"
    ;;

  nfs)
    # soft rather than hard: a NAS that goes away must not wedge a scan in
    # uninterruptible sleep. Soft returns an error and the scan aborts safely.
    options="ro,soft,timeo=150,retrans=3,nfsvers=${NFS_VERS},noatime,_netdev,nofail"
    add_fstab_line "${NAS_HOST}:${SHARE}" "$HOST_DIR" "nfs" "$options"
    ;;

  *)
    msg_error "PROTOCOL must be smb or nfs, got '${PROTOCOL}'."
    exit 1
    ;;
esac

systemctl daemon-reload >/dev/null 2>&1 || true

if ! mount "$HOST_DIR" 2>/dev/null && ! mountpoint -q "$HOST_DIR"; then
  msg_error "could not mount ${SHARE} from ${NAS_HOST}."
  echo
  msg_warn "What mount actually said:"
  mount "$HOST_DIR" 2>&1 | sed 's/^/    /' || true
  echo
  msg_warn "Check that the share path is exactly what the NAS exports, and"
  msg_warn "that the account in ${SMB_CREDENTIALS} can read it."
  exit 1
fi
msg_ok "Mounted on the host at ${HOST_DIR}"

if ! find "$HOST_DIR" -mindepth 1 -maxdepth 2 -print -quit 2>/dev/null | grep -q .; then
  msg_warn "The share mounted but looks empty. Check the path is the folder"
  msg_warn "itself and not its parent."
fi

# ------------------------------------------------- bind into the container ---
# The first free mount point. mp0 and mp1 are the documents and backup shares.
slot=""
for candidate in mp2 mp3 mp4 mp5 mp6 mp7 mp8 mp9; do
  if ! pct config "$CTID" | grep -q "^${candidate}:"; then
    slot="$candidate"
    break
  fi
done

if pct config "$CTID" | grep -q "mp=${GUEST_DIR}\b"; then
  msg_ok "Already bound into the container at ${GUEST_DIR}"
else
  [[ -n "$slot" ]] || { msg_error "no free mount point on ${CTID}."; exit 1; }
  # ro=1 so the container cannot write here even if something tries.
  pct set "$CTID" -"$slot" "${HOST_DIR},mp=${GUEST_DIR},ro=1" >/dev/null
  msg_ok "Bound as ${slot} at ${GUEST_DIR} (read-only)"
  msg_info "Restarting the container so the mount appears…"
  pct reboot "$CTID" >/dev/null 2>&1 || pct restart "$CTID" >/dev/null 2>&1 || true
  for _ in $(seq 1 30); do
    pct exec "$CTID" -- test -d "$GUEST_DIR" 2>/dev/null && break
    sleep 2
  done
fi

if ! pct exec "$CTID" -- test -d "$GUEST_DIR" 2>/dev/null; then
  msg_error "${GUEST_DIR} is not visible in the container yet."
  msg_warn  "Try: pct reboot ${CTID}"
  exit 1
fi

# The service account has to be able to read it, or the scan finds nothing and
# reports an empty folder rather than a permission problem.
if ! pct exec "$CTID" -- sudo -u tracepaper test -r "$GUEST_DIR" 2>/dev/null; then
  msg_warn "${GUEST_DIR} is not readable by the tracepaper user."
  msg_warn "Check uid/gid mapping: mounted as ${host_uid}:${host_gid} on the host."
fi

msg_ok "Visible in the container at ${GUEST_DIR}"
echo
msg_warn "One step left: add it as a folder to index."
echo "  Settings > Storage > Documents to index > + add folder"
echo
echo "    ${GUEST_DIR}"
echo
echo "  Then Settings > General > Scan for new and changed files."
echo
msg_info "Or from the host:"
echo "  pct exec ${CTID} -- sudo -u tracepaper /opt/tracepaper/.venv/bin/tracepaper \\"
echo "    --config ${CONFIG} status"

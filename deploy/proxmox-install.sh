#!/usr/bin/env bash
#
# Tracepaper — one-shot Proxmox LXC installer.
#
# Run this ON THE PROXMOX HOST (not inside a container). It creates an
# unprivileged LXC, installs Python and the app, mounts your two Synology NFS
# shares, and leaves a running systemd service behind.
#
#   ./deploy/proxmox-install.sh
#
# Sized for a small host. The container defaults to 2 cores / 1GB / 8GB disk,
# and the install deliberately leaves out anything heavy:
#
#   * No LLM. The model layer runs at ingest only and is off by default, so
#     search behaves identically without it (FR-6).
#   * No sentence-transformers by default. It pulls PyTorch — roughly 2.5GB on
#     disk and about 1GB resident to load — which is not a reasonable thing to
#     put on a 16GB host already running other services. Keyword search, field
#     answers, events and photo tags all work without it (NFR-9). Pass
#     SEMANTIC=1 if you want it anyway.
#
# Deliberately self-contained: one file you can read before running it.

# -E matters: without it an ERR trap is not inherited by shell functions, so a
# failure inside install_app() would exit silently and leave the half-built
# container behind — which is exactly the case the trap exists for.
set -Eeuo pipefail

# ------------------------------------------------------------------ config ---

APP="Tracepaper"

# Cloned inside the container. Set REPO_URL="" to copy the local checkout
# instead — which is what you want for testing a change before pushing it.
REPO_URL="${REPO_URL-}"
BRANCH="${BRANCH:-main}"

# Defaults, all overridable from the environment:
#   CTID=122 RAM=2048 ./deploy/proxmox-install.sh
CTID="${CTID:-}"
HOSTNAME_="${HOSTNAME_:-tracepaper}"
DISK="${DISK:-8}"                  # GB
CORES="${CORES:-2}"
RAM="${RAM:-1024}"                 # MB
BRIDGE="${BRIDGE:-vmbr0}"
NET="${NET:-dhcp}"                 # dhcp, or CIDR like 192.168.1.50/24
GATEWAY="${GATEWAY:-}"             # required when NET is not dhcp
STORAGE="${STORAGE:-}"             # auto-detected when empty
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"
APP_PORT="${APP_PORT:-8823}"
START_ON_BOOT="${START_ON_BOOT:-1}"
OS_VERSION="${OS_VERSION:-12}"     # Debian 12 (bookworm)

# Synology NFS. Leave NAS_HOST empty to skip mounting entirely and configure it
# later from the Settings page.
NAS_HOST="${NAS_HOST:-}"
NAS_DOCS_EXPORT="${NAS_DOCS_EXPORT:-/volume1/documents}"
NAS_BACKUP_EXPORT="${NAS_BACKUP_EXPORT:-/volume1/backups/tracepaper}"
DOCS_MOUNT="${DOCS_MOUNT:-/mnt/nas/documents}"
BACKUP_MOUNT="${BACKUP_MOUNT:-/mnt/nas/backups/tracepaper}"
NFS_VERS="${NFS_VERS:-4.1}"

# Semantic search. Off by default — see the note at the top.
SEMANTIC="${SEMANTIC:-0}"

# Run a first scan at the end. Off by default: a lifetime of documents can take
# hours, and you want to watch that rather than have an installer hold the
# terminal.
FIRST_SCAN="${FIRST_SCAN:-0}"

# ------------------------------------------------------------------ output ---

RD=$'\033[01;31m'; GN=$'\033[1;92m'; YW=$'\033[33m'; BL=$'\033[36m'; CL=$'\033[m'
msg_info()  { echo -e " ${BL}➜${CL} $1"; }
msg_ok()    { echo -e " ${GN}✔${CL} $1"; }
msg_warn()  { echo -e " ${YW}!${CL} $1"; }
msg_error() { echo -e " ${RD}✘${CL} $1" >&2; }

header() {
  echo -e "${BL}"
  cat <<'BANNER'
   ___          _         __  __
  |   \  __ _  | |_  __ _|  \/  | __ _  _ _   __ _  __ _  ___  _ _
  | |) |/ _` | |  _|/ _` | |\/| |/ _` || ' \ / _` |/ _` |/ -_)| '_|
  |___/ \__,_|  \__|\__,_|_|  |_|\__,_||_||_|\__,_|\__, |\___||_|
                                                   |___/
  Deterministic search over your documents — Proxmox LXC installer
BANNER
  echo -e "${CL}"
}

# Set once the container exists, so a later failure can clean up after itself.
CREATED_CTID=""
NFS_READY=0

# A failed run must not leave a half-built container behind: the next attempt
# would allocate a fresh ID and leak this one. Destroy it unless KEEP_ON_FAIL=1,
# which is what you want when debugging the failure itself.
on_failure() {
  local code=$?
  msg_error "failed at line ${BASH_LINENO[0]}: ${BASH_COMMAND}"
  if [[ -n "$CREATED_CTID" ]]; then
    if [[ "${KEEP_ON_FAIL:-0}" == "1" ]]; then
      msg_warn "Container $CREATED_CTID left in place (KEEP_ON_FAIL=1)."
      msg_warn "Inspect: pct enter $CREATED_CTID    Remove: pct destroy $CREATED_CTID --force"
    else
      msg_warn "Removing the incomplete container ${CREATED_CTID}…"
      pct stop "$CREATED_CTID" >/dev/null 2>&1 || true
      pct destroy "$CREATED_CTID" --force >/dev/null 2>&1 || true
      msg_ok "Cleaned up. Re-run to try again, or set KEEP_ON_FAIL=1 to inspect."
    fi
  fi
  exit $code
}
trap on_failure ERR

# ----------------------------------------------------------- preconditions ---

check_host() {
  if ! command -v pct >/dev/null 2>&1; then
    msg_error "\`pct\` not found — run this on the Proxmox host, not inside a container."
    exit 1
  fi
  if [[ $EUID -ne 0 ]]; then
    msg_error "must run as root on the Proxmox host."
    exit 1
  fi
  msg_ok "Proxmox host detected ($(pveversion 2>/dev/null | head -1))"
}

pick_ctid() {
  if [[ -n "$CTID" ]]; then
    if pct status "$CTID" >/dev/null 2>&1 || qm status "$CTID" >/dev/null 2>&1; then
      msg_error "ID $CTID is already in use."
      exit 1
    fi
    return
  fi
  CTID=$(pvesh get /cluster/nextid 2>/dev/null || echo 100)
  msg_ok "Using container ID $CTID"
}

# Find a storage that can actually hold a container rootfs. Not every storage
# can: 'local' is usually dir-backed for templates only, while rootfs needs a
# storage with the 'rootdir' content type.
pick_storage() {
  if [[ -n "$STORAGE" ]]; then
    msg_ok "Using storage $STORAGE (specified)"
    return
  fi
  STORAGE=$(pvesm status -content rootdir 2>/dev/null | awk 'NR>1 && $3=="active" {print $1; exit}')
  if [[ -z "$STORAGE" ]]; then
    msg_error "no active storage supports container rootfs. Pass STORAGE=<name>."
    pvesm status 2>/dev/null || true
    exit 1
  fi
  msg_ok "Using storage $STORAGE"
}

ensure_template() {
  local pattern="debian-${OS_VERSION}-standard"
  local existing
  existing=$(pveam list "$TEMPLATE_STORAGE" 2>/dev/null | awk -v p="$pattern" '$1 ~ p {print $1; exit}')

  if [[ -n "$existing" ]]; then
    TEMPLATE="$existing"
    msg_ok "Template present: $(basename "$TEMPLATE")"
    return
  fi

  msg_info "Downloading Debian ${OS_VERSION} template…"
  pveam update >/dev/null 2>&1 || true
  local available
  available=$(pveam available -section system 2>/dev/null | awk -v p="$pattern" '$2 ~ p {print $2}' | sort -V | tail -1)
  if [[ -z "$available" ]]; then
    msg_error "no Debian ${OS_VERSION} template available from pveam."
    exit 1
  fi
  pveam download "$TEMPLATE_STORAGE" "$available" >/dev/null
  TEMPLATE="${TEMPLATE_STORAGE}:vztmpl/${available}"
  msg_ok "Template downloaded: $available"
}

# -------------------------------------------------------------- container ---

create_container() {
  local net="name=eth0,bridge=${BRIDGE}"
  if [[ "$NET" == "dhcp" ]]; then
    net="${net},ip=dhcp"
  else
    [[ -z "$GATEWAY" ]] && { msg_error "GATEWAY is required when NET is a static address."; exit 1; }
    net="${net},ip=${NET},gw=${GATEWAY}"
  fi

  msg_info "Creating container ${CTID}…"
  # Unprivileged. An unprivileged LXC cannot mount NFS itself even with
  # CAP_SYS_ADMIN, so the shares are bind-mounted from the host instead — see
  # setup_nfs(). That is the better arrangement anyway: the credentials and the
  # mount live on the host, and the container just sees directories.
  #
  # Nesting off: there is no Docker inside.
  pct create "$CTID" "$TEMPLATE" \
    --hostname "$HOSTNAME_" \
    --cores "$CORES" \
    --memory "$RAM" \
    --swap 512 \
    --rootfs "${STORAGE}:${DISK}" \
    --net0 "$net" \
    --unprivileged 1 \
    --features nesting=0 \
    --onboot "$START_ON_BOOT" \
    --tags "tracepaper;search;documents" \
    --description "Tracepaper — deterministic search over personal documents" >/dev/null

  CREATED_CTID="$CTID"
  pct start "$CTID" >/dev/null
  msg_ok "Container $CTID created and started"

  msg_info "Waiting for network…"
  local _
  for _ in $(seq 1 60); do
    if pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1; then
      msg_ok "Network is up"
      return
    fi
    sleep 2
  done
  msg_error "container has no network after 120s — check the bridge and DHCP."
  exit 1
}

# Run a command inside the container.
#
# LC_ALL=C is set because pct exec passes the host's environment through, and a
# fresh Debian container has not generated en_US.UTF-8 — so every apt call
# emits a wall of perl locale warnings. C is always present.
#
# `set -e` inside matters: these are multi-statement scripts, and bash -c
# otherwise returns only the last command's status — a failed clone followed by
# a successful `rm` would look like success.
inct() { pct exec "$CTID" -- env LC_ALL=C LANG=C bash -ec "$1"; }

# ------------------------------------------------------------------- NFS ---

# Mount both Synology shares on the HOST, then bind-mount them into the
# container.
#
# This is not a workaround; it is the only way that works. An unprivileged LXC
# cannot mount NFS — the kernel refuses mount(2) for network filesystems from a
# user namespace regardless of capabilities. Putting the mounts on the host
# also keeps the export configuration in one place and lets several containers
# share it later.
#
# The documents share is mounted `ro` at BOTH levels: read-only on the host
# mount, and read-only again on the bind mount. The application never writes
# there (NFR-7); this makes the kernel enforce it rather than trusting the code.
setup_nfs() {
  if [[ -z "$NAS_HOST" ]]; then
    msg_warn "No NAS_HOST given — skipping NFS setup."
    msg_warn "Configure the paths later from the Settings page in the web UI."
    return 0
  fi

  msg_info "Mounting Synology NFS shares on the host…"

  if ! command -v mount.nfs >/dev/null 2>&1; then
    # Proxmox ships this, but a minimal install may not have it.
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nfs-common >/dev/null 2>&1 || true
  fi

  local host_docs="/mnt/pve/tracepaper-documents"
  local host_backup="/mnt/pve/tracepaper-backups"
  mkdir -p "$host_docs" "$host_backup"

  # soft,timeo=150,retrans=3 rather than the default `hard`: a NAS that goes
  # away must not wedge a scan in uninterruptible sleep forever. Soft returns
  # an error, the scan hits its vanish guard and aborts, and nothing is deleted.
  local common="soft,timeo=150,retrans=3,nfsvers=${NFS_VERS},noatime,_netdev,nofail"

  _add_fstab_line "${NAS_HOST}:${NAS_DOCS_EXPORT}" "$host_docs" "ro,${common}"
  _add_fstab_line "${NAS_HOST}:${NAS_BACKUP_EXPORT}" "$host_backup" "rw,${common}"

  systemctl daemon-reload >/dev/null 2>&1 || true

  # Not fatal. A wrong export path or a firewall should leave a working install
  # with an obvious next step, not destroy the container through the ERR trap.
  if ! mount "$host_docs" 2>/dev/null && ! mountpoint -q "$host_docs"; then
    msg_warn "Could not mount ${NAS_HOST}:${NAS_DOCS_EXPORT} on the host."
    msg_warn "Check DSM → Control Panel → Shared Folder → NFS Permissions,"
    msg_warn "and that this host's IP is allowed. Then: mount ${host_docs}"
    return 0
  fi
  msg_ok "Documents mounted read-only at ${host_docs}"

  if ! mount "$host_backup" 2>/dev/null && ! mountpoint -q "$host_backup"; then
    msg_warn "Could not mount the backup export — backups will be unavailable."
    msg_warn "Fix it later, then: mount ${host_backup}"
  else
    msg_ok "Backups mounted read-write at ${host_backup}"
  fi

  # Bind them into the container. mp0 is read-only; the kernel enforces NFR-7.
  msg_info "Binding the shares into the container…"
  pct set "$CTID" -mp0 "${host_docs},mp=${DOCS_MOUNT},ro=1" >/dev/null
  pct set "$CTID" -mp1 "${host_backup},mp=${BACKUP_MOUNT}" >/dev/null

  # Bind mounts of an existing mount point need a restart to appear.
  pct stop "$CTID" >/dev/null 2>&1 || true
  pct start "$CTID" >/dev/null
  for _ in $(seq 1 30); do
    inct "true" >/dev/null 2>&1 && break
    sleep 2
  done

  if inct "test -d '${DOCS_MOUNT}'"; then
    local count
    count=$(inct "ls -1 '${DOCS_MOUNT}' 2>/dev/null | head -1000 | wc -l" || echo 0)
    msg_ok "Documents visible inside the container (${count// /} entries at the top level)"
    NFS_READY=1
  else
    msg_warn "The documents path is not visible inside the container."
  fi
}

# Append an fstab entry only if that mount point is not already configured, so
# re-running the installer does not stack duplicates.
_add_fstab_line() {
  local source="$1" point="$2" options="$3"
  if grep -qE "[[:space:]]${point}[[:space:]]" /etc/fstab 2>/dev/null; then
    msg_info "fstab already has an entry for ${point} — leaving it alone."
    return
  fi
  printf '%s %s nfs %s 0 0\n' "$source" "$point" "$options" >> /etc/fstab
}

# ------------------------------------------------------------------- app ---

install_base() {
  msg_info "Installing base packages…"
  # tesseract-ocr reads screenshots and scanned PDFs; poppler-utils rasterises
  # scanned PDF pages for it. Without them those documents still index by
  # filename but their text is unreachable — they land as 'partial' (FR-2).
  #
  # This is the Linux path. On macOS the built-in Vision framework does OCR and
  # object tagging with no install at all, which is why the Mac is the better
  # host for a bulk first-index (deploy/README.md).
  inct "export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq
        apt-get install -y -qq --no-install-recommends \
          python3 python3-venv python3-pip git curl ca-certificates rsync sudo \
          sqlite3 tesseract-ocr poppler-utils >/dev/null"
  msg_ok "Base packages installed"

  local python_version
  python_version=$(inct "python3 --version" | awk '{print $2}')
  # Config parsing uses tomllib, which is 3.11+. Debian 12 ships 3.11.
  local major minor
  major="${python_version%%.*}"
  minor="${python_version#*.}"; minor="${minor%%.*}"
  if (( major < 3 || (major == 3 && minor < 11) )); then
    msg_error "Python $python_version installed, but 3.11+ is required (tomllib)."
    exit 1
  fi
  msg_ok "Python $python_version"
}

install_app() {
  msg_info "Creating service user…"
  # A system account with nologin, the same way www-data and postgres are.
  # Nobody logs in as it; it exists so the service is not root.
  inct "adduser --system --group --home /opt/tracepaper --shell /usr/sbin/nologin tracepaper >/dev/null 2>&1 || true
        mkdir -p /opt/tracepaper /var/lib/tracepaper
        chown tracepaper:tracepaper /var/lib/tracepaper"
  msg_ok "Service user created"

  if [[ -n "$REPO_URL" ]]; then
    msg_info "Cloning ${REPO_URL}…"
    inct "git clone --depth 1 --branch '$BRANCH' '$REPO_URL' /tmp/tracepaper-src >/dev/null
          cp -a /tmp/tracepaper-src/. /opt/tracepaper/
          rm -rf /tmp/tracepaper-src"
    msg_ok "Source cloned"
  else
    # No remote: push the checkout this script is running from.
    local here
    here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    if [[ ! -f "$here/pyproject.toml" ]]; then
      msg_error "no REPO_URL set and no checkout found next to this script."
      msg_warn  "Either set REPO_URL=<git url>, or run this script from inside the repo."
      exit 1
    fi
    msg_info "Copying source from ${here}…"
    # tar over pct exec, so this needs no ssh into the container. .venv is
    # excluded because a macOS virtualenv is useless on Debian — and copying it
    # would shadow the one built here.
    tar -C "$here" \
      --exclude=.venv --exclude=.git --exclude=data --exclude=__pycache__ \
      --exclude='*.pyc' --exclude=.pytest_cache --exclude='*.swp' \
      -cf - . | pct exec "$CTID" -- tar -C /opt/tracepaper -xf -
    msg_ok "Source copied"
  fi

  # Extras, chosen for a small host:
  #   formats  — pdf, docx, xlsx, images
  #   ocr      — pytesseract, for screenshots and scans
  #   photos   — EXIF plus offline reverse geocoding, no network or API key
  #   web      — the UI and REST API
  # 'semantic' is excluded unless asked for: it pulls PyTorch, roughly 2.5GB on
  # disk and about 1GB resident. Search works without it (NFR-9).
  local extras="formats,ocr,photos,web"
  if [[ "$SEMANTIC" == "1" ]]; then
    extras="${extras},semantic"
    msg_warn "SEMANTIC=1 — this pulls PyTorch (~2.5GB). Expect a slow install."
  fi

  msg_info "Installing Python dependencies (${extras})…"
  inct "cd /opt/tracepaper
        python3 -m venv .venv
        .venv/bin/pip install --quiet --upgrade pip setuptools wheel
        .venv/bin/pip install --quiet -e '.[${extras}]'"

  if ! inct "test -x /opt/tracepaper/.venv/bin/tracepaper"; then
    msg_error "install finished but the \`tracepaper\` entry point is missing."
    inct "cd /opt/tracepaper && .venv/bin/pip install -e '.[${extras}]' 2>&1 | tail -20" || true
    exit 1
  fi
  msg_ok "Dependencies installed"

  # The code stays root-owned and world-readable: the service only reads it,
  # and chowning the whole tree makes git refuse to operate as root ("dubious
  # ownership"), which silently breaks every future update. Only the state the
  # service writes belongs to the service account.
  inct "chown -R root:root /opt/tracepaper
        chown -R tracepaper:tracepaper /opt/tracepaper/.venv /var/lib/tracepaper
        chmod 755 /opt/tracepaper"
}

configure_access() {
  msg_info "Configuring container access…"

  if [[ -n "${ROOT_PASSWORD:-}" ]]; then
    inct "echo 'root:${ROOT_PASSWORD}' | chpasswd"
    msg_ok "Root password set"
  else
    # Auto-login on the console only, matching what the community scripts do.
    # That console is reachable from the Proxmox UI and from `pct enter`, both
    # of which already require host access — the LXC boundary is the control
    # here, and a container you cannot get into is one you cannot debug.
    inct "mkdir -p /etc/systemd/system/container-getty@1.service.d
          cat >/etc/systemd/system/container-getty@1.service.d/autologin.conf <<'EOF'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin root --noclear --keep-baud tty%I 115200,38400,9600 \$TERM
EOF
          systemctl daemon-reload
          systemctl restart container-getty@1.service 2>/dev/null || true"
    msg_ok "Console auto-login enabled (no password set)"
  fi

  if [[ -n "${SSH_KEY:-}" ]]; then
    inct "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq openssh-server >/dev/null
          mkdir -p /root/.ssh && chmod 700 /root/.ssh
          echo '${SSH_KEY}' >> /root/.ssh/authorized_keys
          chmod 600 /root/.ssh/authorized_keys
          sed -i 's/^#*PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
          systemctl enable --now ssh >/dev/null 2>&1 || systemctl enable --now sshd >/dev/null 2>&1 || true"
    msg_ok "SSH key installed (key-based root login only)"
  fi
}

configure_service() {
  msg_info "Writing configuration…"

  # backup_dir is only written when the share actually mounted. Pointing it at
  # a path that does not exist would make every backup fail at the moment you
  # most need one to have worked.
  local backup_line=""
  if [[ "$NFS_READY" == "1" ]]; then
    backup_line="backup_dir = \"${BACKUP_MOUNT}\""
  fi
  # Keyed on whether the mount actually worked, not on whether a NAS was
  # named. A configured root that does not exist inside the container makes
  # every scan fail with a confusing error; an empty roots list makes the
  # Settings page say plainly that nothing is configured yet.
  local roots_line="roots = []"
  if [[ "$NFS_READY" == "1" ]]; then
    roots_line="roots = [\"${DOCS_MOUNT}\"]"
  fi

  inct "cat >/etc/tracepaper.toml <<'EOF'
# Written by deploy/proxmox-install.sh. Editable from the Settings page in the
# web UI, which validates every path before saving.
#
# See deploy/tracepaper.toml.example for what each value means.

[index]
# Local disk, never the NAS: SQLite corrupts over NFS and does so silently
# (D-008). The index rebuilds from your documents; the backup below holds the
# part that cannot be rebuilt.
db_path = \"/var/lib/tracepaper/index.db\"
${backup_line}

[scan]
${roots_line}
excludes = [\"@eaDir\", \"#recycle\", \"#snapshot\", \".DS_Store\", \".Trashes\",
            \".Spotlight-V100\", \".fseventsd\", \"__MACOSX\", \".git\", \"@tmp\",
            \"desktop.ini\", \"Thumbs.db\"]
max_file_bytes = 536870912
miss_threshold = 3
vanish_guard = 0.5
follow_symlinks = false

[llm]
# Ingest-only, and off. Search behaves identically either way (FR-6).
enabled = false
endpoint = \"http://localhost:11434\"
model = \"gemma4:e4b-mlx\"

[enrich]
load_threshold = 0.7
EOF
chown root:tracepaper /etc/tracepaper.toml
chmod 640 /etc/tracepaper.toml"
  msg_ok "Configuration written to /etc/tracepaper.toml"

  msg_info "Installing systemd units…"
  # Prefer the units from the checkout, so there is one source of truth.
  if inct "test -f /opt/tracepaper/deploy/tracepaper.service"; then
    inct "cp /opt/tracepaper/deploy/tracepaper.service /etc/systemd/system/
          cp /opt/tracepaper/deploy/tracepaper-scan.service /etc/systemd/system/
          cp /opt/tracepaper/deploy/tracepaper-scan.timer /etc/systemd/system/
          cp /opt/tracepaper/deploy/tracepaper-enrich.service /etc/systemd/system/
          cp /opt/tracepaper/deploy/tracepaper-enrich.timer /etc/systemd/system/"
  else
    msg_error "deploy/ units are missing from the checkout."
    exit 1
  fi

  # The port is a flag in the unit, not an env file, so rewrite it if the user
  # chose a different one.
  if [[ "$APP_PORT" != "8823" ]]; then
    inct "sed -i 's/--port 8823/--port ${APP_PORT}/' /etc/systemd/system/tracepaper.service"
  fi

  # Catch the namespace directives before starting rather than after five
  # failed restarts. They are valid systemd and correct on bare metal; they are
  # only wrong inside an unprivileged LXC, so no syntax check would find them.
  # docs/DEPLOY.md documents the VM variant, which does include them — this is
  # exactly the mix-up worth catching.
  if inct "grep -qE '^(ProtectSystem|PrivateTmp|PrivateDevices|ProtectHome|ProtectKernel|ProtectControlGroups|ReadWritePaths)' /etc/systemd/system/tracepaper.service"; then
    msg_error "the unit contains mount-namespace directives, which an unprivileged LXC cannot honour."
    inct "grep -nE '^(ProtectSystem|PrivateTmp|PrivateDevices|ProtectHome|ProtectKernel|ProtectControlGroups|ReadWritePaths)' /etc/systemd/system/tracepaper.service" || true
    msg_warn "This unit would fail with status=226/NAMESPACE. Remove those lines."
    exit 1
  fi

  inct "systemctl daemon-reload
        systemctl enable --now tracepaper >/dev/null 2>&1
        systemctl enable --now tracepaper-scan.timer >/dev/null 2>&1
        systemctl enable --now tracepaper-enrich.timer >/dev/null 2>&1"
  msg_ok "Service and timers enabled"
}

verify() {
  msg_info "Verifying…"
  local _
  for _ in $(seq 1 45); do
    if inct "curl -sf localhost:${APP_PORT}/api/health >/dev/null 2>&1"; then
      msg_ok "Health check passed"
      # The UI is served by the same process but from different code, so a
      # healthy API does not prove the page renders.
      if inct "curl -sf localhost:${APP_PORT}/ | grep -qi '<title>'"; then
        msg_ok "UI is being served"
      else
        msg_warn "API is up but the UI did not respond — check: pct exec $CTID -- journalctl -u tracepaper -n 50"
      fi
      return 0
    fi
    sleep 2
  done

  msg_error "service did not become healthy within 90s."
  echo

  # A unit that never executed its binary fails differently from an app that
  # crashed, and 226 is the failure this deployment target produces. Name it
  # rather than dumping logs and leaving the reader to spot it.
  if inct "systemctl show tracepaper -p ExecMainStatus --value | grep -qx 226" 2>/dev/null; then
    msg_error "systemd could not set up the unit's mount namespace (status 226)."
    msg_warn  "An unprivileged LXC cannot remount /proc, so ProtectSystem, PrivateTmp and"
    msg_warn  "the ProtectKernel* directives make the unit unstartable."
    echo
  fi

  inct "systemctl status tracepaper --no-pager -l | head -20" || true
  inct "journalctl -u tracepaper -n 30 --no-pager" || true
  exit 1
}

# An optional first scan. Off by default — a lifetime of documents takes hours,
# and it belongs in a terminal you are watching, not at the end of an installer.
first_scan() {
  [[ "$FIRST_SCAN" == "1" && "$NFS_READY" == "1" ]] || return 0
  msg_info "Running the first scan (this can take a long time)…"
  inct "sudo -u tracepaper /opt/tracepaper/.venv/bin/tracepaper --config /etc/tracepaper.toml scan --index" || {
    msg_warn "The first scan did not finish cleanly. Re-run it by hand:"
    msg_warn "  pct exec $CTID -- sudo -u tracepaper /opt/tracepaper/.venv/bin/tracepaper --config /etc/tracepaper.toml scan --index"
    return 0
  }
  msg_ok "First scan complete"
}

container_ip() {
  pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}'
}

finish() {
  local ip; ip=$(container_ip)
  echo
  msg_ok "${APP} is installed and running."
  echo
  echo -e "  ${GN}http://${ip}:${APP_PORT}${CL}"
  echo
  echo "  Container : $CTID ($HOSTNAME_)"
  echo "  Index     : /var/lib/tracepaper/index.db  (local disk, never the NAS)"
  echo "  Config    : /etc/tracepaper.toml"
  if [[ "$NFS_READY" == "1" ]]; then
    echo "  Documents : ${DOCS_MOUNT}  (read-only)"
    echo "  Backups   : ${BACKUP_MOUNT}"
  else
    echo "  Documents : not mounted — set them up on the Settings tab"
  fi
  echo
  echo "  Logs      : pct exec $CTID -- journalctl -u tracepaper -f"
  echo "  Restart   : pct exec $CTID -- systemctl restart tracepaper"
  echo "  Scan now  : pct exec $CTID -- systemctl start tracepaper-scan"
  echo "  Scan log  : pct exec $CTID -- journalctl -u tracepaper-scan -f"
  echo "  Status    : pct exec $CTID -- sudo -u tracepaper /opt/tracepaper/.venv/bin/tracepaper --config /etc/tracepaper.toml status"
  echo
  if [[ "$FIRST_SCAN" != "1" && "$NFS_READY" == "1" ]]; then
    echo "  Nothing is indexed yet. The hourly timer will pick it up, or start now:"
    echo
    echo -e "    ${BL}pct exec $CTID -- systemctl start tracepaper-scan${CL}"
    echo
    echo "  A first pass over a lifetime of documents takes hours. It is resumable —"
    echo "  interrupting it costs only the document in flight."
    echo
  fi
  if [[ "$NFS_READY" != "1" ]]; then
    msg_warn "No documents are configured, so there is nothing to index yet."
    msg_warn "Open the Settings tab and point it at your documents folder — it"
    msg_warn "validates each path and explains anything it cannot use."
    echo
  fi
  if [[ "$SEMANTIC" != "1" ]]; then
    msg_info "Semantic search is off (it needs PyTorch, ~2.5GB). Keyword search, field"
    msg_info "answers, events and photo tags all work without it. See deploy/README.md."
  fi
  msg_warn "No authentication yet — do not port-forward this. Reach it over the LAN or a VPN."
  if [[ "$NET" == "dhcp" ]]; then
    msg_warn "Address came from DHCP. Give $HOSTNAME_ a static lease so it does not move."
  fi
  echo
}

# ------------------------------------------------------------------ review ---

# Read a line from the user's terminal.
#
# Not from stdin: under `bash -c "$(curl ...)"` stdin is not the keyboard, and
# under a piped `curl | bash` it is the script itself — reading it would eat the
# remaining source. /dev/tty is the terminal regardless of how stdin is wired.
ask() {
  local prompt="$1" default="$2" answer=""
  if [[ ! -r /dev/tty ]]; then
    echo "$default"
    return
  fi
  read -r -p "$prompt" answer </dev/tty || answer=""
  echo "${answer:-$default}"
}

show_settings() {
  local net_desc="$NET"
  [[ "$NET" != "dhcp" ]] && net_desc="$NET via $GATEWAY"
  local src_desc="$REPO_URL"
  [[ -z "$REPO_URL" ]] && src_desc="local checkout"

  echo
  echo "  Container ID   ${CTID:-<next free>}"
  echo "  Hostname       $HOSTNAME_"
  echo "  Cores          $CORES"
  echo "  RAM            ${RAM} MB"
  echo "  Disk           ${DISK} GB"
  echo "  Network        $net_desc  (bridge $BRIDGE)"
  echo "  Storage        ${STORAGE:-<auto-detect>}"
  echo "  App port       $APP_PORT"
  echo "  Source         $src_desc"
  echo
  echo "  NAS host       ${NAS_HOST:-<none — configure later in the UI>}"
  if [[ -n "$NAS_HOST" ]]; then
  echo "  Documents      ${NAS_DOCS_EXPORT}  →  ${DOCS_MOUNT}  (read-only)"
  echo "  Backups        ${NAS_BACKUP_EXPORT}  →  ${BACKUP_MOUNT}"
  fi
  echo "  Semantic       $([[ "$SEMANTIC" == "1" ]] && echo "on (+2.5GB PyTorch)" || echo "off (keyword search only)")"
  echo "  First scan     $([[ "$FIRST_SCAN" == "1" ]] && echo "yes" || echo "no — start it yourself afterwards")"
  echo
}

customise() {
  echo
  echo "  Press Enter to keep the value shown in brackets."
  echo
  CTID=$(ask       "  Container ID [${CTID:-next free}]: " "$CTID")
  HOSTNAME_=$(ask  "  Hostname [$HOSTNAME_]: " "$HOSTNAME_")
  CORES=$(ask      "  Cores [$CORES]: " "$CORES")
  RAM=$(ask        "  RAM in MB [$RAM]: " "$RAM")
  DISK=$(ask       "  Disk in GB [$DISK]: " "$DISK")
  APP_PORT=$(ask   "  App port [$APP_PORT]: " "$APP_PORT")
  BRIDGE=$(ask     "  Bridge [$BRIDGE]: " "$BRIDGE")
  NET=$(ask        "  Network — 'dhcp' or CIDR e.g. 192.168.1.50/24 [$NET]: " "$NET")
  if [[ "$NET" != "dhcp" ]]; then
    GATEWAY=$(ask  "  Gateway [${GATEWAY:-required}]: " "$GATEWAY")
    while [[ -z "$GATEWAY" ]]; do
      msg_warn "A gateway is required with a static address."
      GATEWAY=$(ask "  Gateway: " "")
    done
  fi
  STORAGE=$(ask    "  Storage [${STORAGE:-auto}]: " "$STORAGE")

  echo
  echo "  Your Synology. An IP is more reliable than mDNS from inside an LXC."
  echo "  Leave blank to skip and set the paths up later in the web UI."
  NAS_HOST=$(ask   "  NAS address [${NAS_HOST:-none}]: " "$NAS_HOST")
  if [[ "${NAS_HOST,,}" == "none" ]]; then
    NAS_HOST=""
  fi
  if [[ -n "$NAS_HOST" ]]; then
    NAS_DOCS_EXPORT=$(ask   "  Documents export [$NAS_DOCS_EXPORT]: " "$NAS_DOCS_EXPORT")
    NAS_BACKUP_EXPORT=$(ask "  Backups export [$NAS_BACKUP_EXPORT]: " "$NAS_BACKUP_EXPORT")
  fi

  echo
  echo "  Semantic search finds \"sprinkler valve\" in a document that says"
  echo "  \"irrigation solenoid\". It needs PyTorch — about 2.5GB on disk and 1GB"
  echo "  of RAM to load — which is a lot for a small host. Everything else"
  echo "  works without it."
  local semantic_answer
  semantic_answer=$(ask "  Install semantic search? [y/N]: " "$([[ "$SEMANTIC" == "1" ]] && echo y || echo n)")
  if [[ "${semantic_answer,,}" == "y" || "${semantic_answer,,}" == "yes" ]]; then
    SEMANTIC=1
  else
    SEMANTIC=0
  fi

  # Numeric fields would otherwise fail deep inside `pct create`, where the
  # error says nothing about which value was wrong.
  local field
  for field in CORES RAM DISK APP_PORT; do
    if ! [[ "${!field}" =~ ^[0-9]+$ ]]; then
      msg_error "$field must be a number, got '${!field}'."
      exit 1
    fi
  done
  if [[ -n "$CTID" ]] && ! [[ "$CTID" =~ ^[0-9]+$ ]]; then
    msg_error "Container ID must be a number, got '$CTID'."
    exit 1
  fi
}

confirm_settings() {
  # Non-interactive by design: a run with no terminal (cron, a pipe with stdin
  # closed) proceeds on defaults rather than hanging forever waiting for input.
  if [[ "${ASSUME_YES:-0}" == "1" || ! -r /dev/tty ]]; then
    show_settings
    msg_info "Proceeding with these settings."
    return
  fi

  while true; do
    show_settings
    local reply
    reply=$(ask "  [D]efaults shown above, [C]ustomise, or [Q]uit? [D]: " "D")
    case "${reply,,}" in
      d|y|yes|"") return ;;
      c) customise ;;
      q|n|no) msg_info "Nothing was created."; exit 0 ;;
      *) msg_warn "Please answer D, C or Q." ;;
    esac
  done
}

# -------------------------------------------------------------------- main ---

main() {
  header
  check_host
  confirm_settings
  pick_ctid
  pick_storage
  ensure_template
  create_container
  setup_nfs
  install_base
  install_app
  configure_access
  configure_service
  verify
  first_scan
  CREATED_CTID=""   # success — nothing to clean up
  finish
}

main "$@"

# Deploying Tracepaper to Proxmox

Two steps, deliberately separate: install the service, then attach the folders
you want indexed. Storage is not an install-time decision — it changes, there
can be several folders, and they can live on different shares.

## 1. Copy the code over

There is no git remote yet, so send the checkout:

```bash
# On your Mac
cd ~/Workspace/home_server
COPYFILE_DISABLE=1 tar --no-xattrs --no-mac-metadata \
    --exclude=.venv --exclude=.git --exclude=data --exclude=__pycache__ \
    --exclude=.pytest_cache --exclude='*.swp' \
    -czf tracepaper.tar.gz tracepaper

scp tracepaper.tar.gz root@192.168.0.136:/root/
```

`COPYFILE_DISABLE=1 --no-xattrs --no-mac-metadata` matters only for the noise:
without them macOS tar writes Apple extended attributes that GNU tar on Debian
does not recognise, and it prints a `LIBARCHIVE.xattr.com.apple.provenance`
warning per file. The extraction succeeds either way — the files are fine — but
it reads like a failure.

## 2. Install

```bash
ssh root@192.168.0.136
tar xzf tracepaper.tar.gz
cd tracepaper
./deploy/proxmox-install.sh
```

It shows the settings and waits: **D** accepts, **C** customises, **Q** quits.
Creates an unprivileged LXC, installs the app, enables the service and the scan
and enrichment timers, and prints the URL. No NAS details needed.

## 3. Attach a folder

```bash
./deploy/add-nas.sh <CTID> <synology-ip>
```

Mounts the export on the Proxmox host, binds it into the container read-only,
updates the config and restarts the service. Run it again for each additional
share:

```bash
NAS_DOCS_EXPORT=/volume1/photos DOCS_MOUNT=/mnt/nas/photos \
  ./deploy/add-nas.sh 122 192.168.0.20
```

Re-running is safe — existing fstab entries and mounts are left alone.

**Why this is not a button in the web UI:** mounting needs root on the *host*,
outside the container the app runs in. An app that mounts filesystems turns
every stale handle and credential problem into its own bug. The Settings page
verifies what it finds and explains what to fix; `/etc/fstab` does the mounting.

## 4. First scan

```bash
pct exec <CTID> -- systemctl start tracepaper-scan
pct exec <CTID> -- journalctl -u tracepaper-scan -f
```

Hours for a lifetime of documents, and resumable — interrupting it costs only
the document in flight. The hourly timer picks up everything after that.

---

## Sized for a small host

Your Proxmox box is 16GB and already busy, so the install deliberately leaves
out the two heavy things:

**No LLM.** The model layer runs at ingest only and is off by default. It fills
gaps in free prose where no `Label: value` pattern exists — without it you get
fewer structured fields from letters and emails, and nothing else changes. It
is *never* called at query time, so search results are identical either way
(FR-6). If you want it, point `[llm]` at Ollama on your Mac rather than running
a model here.

**No semantic search by default.** `sentence-transformers` pulls PyTorch:
roughly 2.5GB on disk and about 1GB resident to load a model. On a host with
other services that is a real cost, so it is opt-in:

```bash
SEMANTIC=1 ./deploy/proxmox-install.sh
```

Without it you keep keyword search, direct field answers ("what is my passport
expiry date"), events, entities, photo tags, and every filter. What you lose is
matching *"sprinkler valve"* against a document that says *"irrigation
solenoid"* (NFR-9).

The container itself defaults to 2 cores / 1GB / 8GB disk. The service's steady
state is tens of MB — it holds a SQLite connection and serves a page. The
`MemoryMax=768M` in the unit is a ceiling to turn a runaway query into a
restart, not a reservation.

---

## What the installer does

| Step | What it means |
|---|---|
| Creates an unprivileged LXC | Debian 12, no nesting, no capabilities |
| Mounts NFS **on the host** | An unprivileged LXC cannot mount NFS itself |
| Bind-mounts documents `ro` | Read-only at both levels — the kernel enforces NFR-7 |
| Bind-mounts backups `rw` | The only state that cannot be regenerated |
| Installs Python + tesseract | OCR for screenshots and scanned PDFs |
| Writes `/etc/tracepaper.toml` | Index on local disk, never the NAS |
| Enables service + 2 timers | Hourly scan, nightly enrichment |

Nothing is indexed when it finishes. Start the first scan when you are ready to
watch it:

```bash
pct exec <CTID> -- systemctl start tracepaper-scan
pct exec <CTID> -- journalctl -u tracepaper-scan -f
```

A first pass over a lifetime of documents takes hours. It is resumable —
interrupting it costs only the document in flight.

---

## The two shares

You asked for one share to read and one to write, and that is exactly the
shape. The third path is the one worth understanding.

```
Synology /volume1/documents        →  /mnt/nas/documents   read-only
Synology /volume1/backups/...      →  /mnt/nas/backups/... read-write
Container local disk               →  /var/lib/tracepaper/index.db
```

**The index does not go on the NAS.** SQLite corrupts over NFS and SMB: their
advisory locking is unreliable across clients, and WAL mode depends on it. It
does not fail loudly — it fails silently, weeks later, in the one file holding
your corrections. The Settings page refuses an NFS path for the index rather
than warning about it (D-008).

That is survivable precisely because of the split: the index rebuilds from your
documents, and the backup share holds the human-authored layer — corrections,
notes, entity merges — which cannot be rebuilt. Lose the container and you
re-scan and restore; you lose nothing you typed.

### If NFS does not mount

The installer warns rather than failing — you get a working install and a clear
next step. The usual cause is DSM's export permissions.

On the Synology: **Control Panel → Shared Folder → *share* → Edit → NFS
Permissions**. Add a rule for the Proxmox host's IP. For the backup share set
squash to *No mapping* (or map to the right uid), otherwise it mounts but is
not writable.

Then on the Proxmox host:

```bash
mount /mnt/pve/tracepaper-documents
mount /mnt/pve/tracepaper-backups
```

`deploy/nas.fstab.example` documents every mount option and why it is there —
particularly `soft`, which stops an unreachable NAS wedging a scan forever.

---

## Doing the first index on your Mac

Optional, and worth it if you have a lot of photos or scans.

macOS has the Vision framework built in: OCR and ~1300 object labels with no
install and no model download. On Linux, OCR falls back to tesseract (fine) and
object tagging does not run at all — so photos indexed on Proxmox get EXIF,
dates and places, but not "dog", "beach", "whiteboard".

The seam already exists: the index is a file.

```bash
# On the Mac, against the same share, writing a local index
tracepaper --config tracepaper.toml scan --index

# Then copy it in, with the service stopped
pct exec <CTID> -- systemctl stop tracepaper
pct push <CTID> index.db /var/lib/tracepaper/index.db
pct exec <CTID> -- chown tracepaper:tracepaper /var/lib/tracepaper/index.db
pct exec <CTID> -- systemctl start tracepaper
```

Stop the service first. Copying over a live SQLite file is the same class of
mistake as putting it on NFS.

---

## Updating

```bash
pct exec <CTID> -- /opt/tracepaper/deploy/update.sh
```

Backs up the human-authored layer, fast-forwards, reinstalls, restarts, and
**rolls back automatically** if the new version does not come up healthy within
60 seconds. It reuses whatever extras are already installed, so an update never
silently turns semantic search off.

Manual rather than a timer, on purpose: this restarts the service that owns
your index, and that should happen when you are watching.

Only works if the install came from a git clone (`REPO_URL=...`). A copied
checkout has no remote to pull from.

---

## Operating

```bash
# Health and shape of the index
pct exec <CTID> -- sudo -u tracepaper /opt/tracepaper/.venv/bin/tracepaper \
  --config /etc/tracepaper.toml status

# Logs
pct exec <CTID> -- journalctl -u tracepaper -f
pct exec <CTID> -- journalctl -u tracepaper-scan -n 100

# Timers
pct exec <CTID> -- systemctl list-timers 'tracepaper*'

# Back up the irreplaceable layer by hand
pct exec <CTID> -- sudo -u tracepaper /opt/tracepaper/.venv/bin/tracepaper \
  --config /etc/tracepaper.toml backup /mnt/nas/backups/tracepaper/manual.json
```

**"Scan aborted, the share is probably not mounted."** Working as intended:
more than half the known paths vanished at once, so the scan refused to
soft-delete the index. Fix the mount and re-run.

**Items stuck at `partial`.** Something is missing rather than broken — usually
a scanned PDF and no OCR backend. The document stays findable by filename.

**A field extracted wrongly.** Correct it in the UI. That value then outranks
every extractor and survives a full reindex forever, matched by content hash
even if you move the file.

---

## Files here

| File | Purpose |
|---|---|
| `proxmox-install.sh` | The installer. Run on the Proxmox host. |
| `add-nas.sh` | Attach an NFS share to an existing container. Re-runnable, once per folder. |
| `update.sh` | Update in place, with automatic rollback. Run in the container. |
| `tracepaper.service` | The web UI and API. |
| `tracepaper-scan.{service,timer}` | Hourly scan and index. |
| `tracepaper-enrich.{service,timer}` | Nightly, idle-only: embeddings and photo tags. |
| `tracepaper.toml.example` | Annotated copy of what the installer writes. |
| `nas.fstab.example` | Both NFS mounts, with every option explained. |

`docs/DEPLOY.md` covers the same ground for a plain VM or bare metal, where the
systemd hardening can be stricter than an unprivileged LXC allows.

---

## Installer options

All environment variables:

```bash
CTID=122              # container ID (default: next free)
HOSTNAME_=tracepaper
CORES=2  RAM=1024  DISK=8
BRIDGE=vmbr0
NET=dhcp              # or a CIDR like 192.168.1.50/24 (then set GATEWAY)
GATEWAY=192.168.1.1
STORAGE=local-lvm     # default: auto-detected
APP_PORT=8823

SEMANTIC=1            # install PyTorch and semantic search
REPO_URL=https://...  # clone instead of copying the local checkout
ROOT_PASSWORD=...     # otherwise console auto-login only
SSH_KEY="ssh-ed25519 ..."
ASSUME_YES=1          # skip the confirmation prompt
KEEP_ON_FAIL=1        # keep a failed container for inspection
```

`add-nas.sh <CTID> <nas-ip>`:

```bash
NAS_DOCS_EXPORT=/volume1/documents          # the export to index
NAS_BACKUP_EXPORT=/volume1/backups/tracepaper
DOCS_MOUNT=/mnt/nas/documents               # where it lands in the container
BACKUP_MOUNT=/mnt/nas/backups/tracepaper
NFS_VERS=4.1                                # 3 for older DSM
CONFIG=/etc/tracepaper.toml
```

---

**No authentication yet.** Do not port-forward this. Reach it over the LAN or a
VPN.

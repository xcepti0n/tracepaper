# Deploying DataManager to Proxmox

One script. Run it on the Proxmox host, get a working install with the UI
reachable on your LAN.

```bash
# From a checkout on the Proxmox host:
NAS_HOST=192.168.1.10 ./deploy/proxmox-install.sh
```

It creates an unprivileged LXC, mounts your two Synology shares, installs the
app, and enables the service plus an hourly scan and a nightly enrichment
timer. It asks before it does any of that, and cleans up after itself if
anything fails.

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
| Writes `/etc/datamanager.toml` | Index on local disk, never the NAS |
| Enables service + 2 timers | Hourly scan, nightly enrichment |

Nothing is indexed when it finishes. Start the first scan when you are ready to
watch it:

```bash
pct exec <CTID> -- systemctl start datamanager-scan
pct exec <CTID> -- journalctl -u datamanager-scan -f
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
Container local disk               →  /var/lib/datamanager/index.db
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
mount /mnt/pve/datamanager-documents
mount /mnt/pve/datamanager-backups
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
dm --config datamanager.toml scan --index

# Then copy it in, with the service stopped
pct exec <CTID> -- systemctl stop datamanager
pct push <CTID> index.db /var/lib/datamanager/index.db
pct exec <CTID> -- chown datamanager:datamanager /var/lib/datamanager/index.db
pct exec <CTID> -- systemctl start datamanager
```

Stop the service first. Copying over a live SQLite file is the same class of
mistake as putting it on NFS.

---

## Updating

```bash
pct exec <CTID> -- /opt/datamanager/deploy/update.sh
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
pct exec <CTID> -- sudo -u datamanager /opt/datamanager/.venv/bin/dm \
  --config /etc/datamanager.toml status

# Logs
pct exec <CTID> -- journalctl -u datamanager -f
pct exec <CTID> -- journalctl -u datamanager-scan -n 100

# Timers
pct exec <CTID> -- systemctl list-timers 'datamanager*'

# Back up the irreplaceable layer by hand
pct exec <CTID> -- sudo -u datamanager /opt/datamanager/.venv/bin/dm \
  --config /etc/datamanager.toml backup /mnt/nas/backups/datamanager/manual.json
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
| `update.sh` | Update in place, with automatic rollback. Run in the container. |
| `datamanager.service` | The web UI and API. |
| `datamanager-scan.{service,timer}` | Hourly scan and index. |
| `datamanager-enrich.{service,timer}` | Nightly, idle-only: embeddings and photo tags. |
| `datamanager.toml.example` | Annotated copy of what the installer writes. |
| `nas.fstab.example` | Both NFS mounts, with every option explained. |

`docs/DEPLOY.md` covers the same ground for a plain VM or bare metal, where the
systemd hardening can be stricter than an unprivileged LXC allows.

---

## Installer options

All environment variables:

```bash
CTID=122              # container ID (default: next free)
HOSTNAME_=datamanager
CORES=2  RAM=1024  DISK=8
BRIDGE=vmbr0
NET=dhcp              # or a CIDR like 192.168.1.50/24 (then set GATEWAY)
GATEWAY=192.168.1.1
STORAGE=local-lvm     # default: auto-detected
APP_PORT=8823

NAS_HOST=192.168.1.10         # blank skips NFS setup entirely
NAS_DOCS_EXPORT=/volume1/documents
NAS_BACKUP_EXPORT=/volume1/backups/datamanager

SEMANTIC=1            # install PyTorch and semantic search
FIRST_SCAN=1          # run a full scan before finishing
REPO_URL=https://...  # clone instead of copying the local checkout
ROOT_PASSWORD=...     # otherwise console auto-login only
SSH_KEY="ssh-ed25519 ..."
ASSUME_YES=1          # skip the confirmation prompt
KEEP_ON_FAIL=1        # keep a failed container for inspection
```

---

**No authentication yet.** Do not port-forward this. Reach it over the LAN or a
VPN.

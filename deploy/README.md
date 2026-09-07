# Deploying Tracepaper to Proxmox

Install the service, then attach the folders you want indexed. Storage is
deliberately a separate step — it changes, there can be several folders, and
they can live on different shares.

## 1. Install

On the Proxmox host, one line — it clones the repo inside the container itself:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/xcepti0n/tracepaper/main/deploy/proxmox-install.sh)"
```

It shows the settings and waits: **D** accepts, **C** customises, **Q** quits.
Nothing is created until you answer. Override anything up front:

```bash
CTID=122 RAM=2048 TLS_DOMAIN=tracepaper.example.net \
  bash -c "$(curl -fsSL https://raw.githubusercontent.com/xcepti0n/tracepaper/main/deploy/proxmox-install.sh)"
```

<details>
<summary>Installing from a local checkout instead</summary>

Useful for testing a change before pushing it. `COPYFILE_DISABLE=1
--no-xattrs --no-mac-metadata` only suppresses noise: without them macOS tar
writes Apple extended attributes that GNU tar on Debian does not recognise, and
it prints a `LIBARCHIVE.xattr.com.apple.provenance` warning per file. The
extraction succeeds either way.

```bash
# On your Mac
COPYFILE_DISABLE=1 tar --no-xattrs --no-mac-metadata \
    --exclude=.venv --exclude=.git --exclude=data --exclude=__pycache__ \
    --exclude=.pytest_cache --exclude='*.swp' \
    -czf tracepaper.tar.gz tracepaper
scp tracepaper.tar.gz root@<proxmox-host>:/root/

# On the host
tar xzf tracepaper.tar.gz && cd tracepaper
REPO_URL= ./deploy/proxmox-install.sh
```

`REPO_URL=` (empty) is what selects the local copy. Note that update.sh needs a
remote, so a copied install cannot update itself in place.

</details>

## 2. Attach a folder

```bash
READ_SHARE=/volume1/documents \
WRITE_SHARE=/volume1/backups \
  ./deploy/add-nas.sh <CTID> <synology-ip>
```

| | what it is | mounted |
|---|---|---|
| `READ_SHARE` | the share holding your documents | **read-only** |
| `DOCS_SUBDIR` | the folder inside it to index — optional | — |
| `WRITE_SHARE` | a **different** share, for backups | read-write |

Both are the **Mount path** DSM shows at the bottom of the NFS Permissions
dialog, e.g. `/volume1/documents`.

Mounts the export on the Proxmox host, binds it into the container read-only,
updates the config and restarts the service. Run it again for each additional
share:

```bash
READ_SHARE=/volume1/photos DOCS_MOUNT=/mnt/nas/photos \
  ./deploy/add-nas.sh 122 <synology-ip>
```

Re-running is safe — existing fstab entries and mounts are left alone.

**Why this is not a button in the web UI:** mounting needs root on the *host*,
outside the container the app runs in. An app that mounts filesystems turns
every stale handle and credential problem into its own bug. The Settings page
verifies what it finds and explains what to fix; `/etc/fstab` does the mounting.

### Documents in a subfolder

NFS exports a whole share, but your documents are usually a folder inside one.
Mount the share, index the subtree:

```bash
READ_SHARE=/volume1/data DOCS_SUBDIR=Documents \
  ./add-nas.sh <CTID> <synology-ip>
```

Everything else under the share stays visible to the container but is never
read — the scan only walks the root it is given.

### Restricting access to Tracepaper alone

**NFS cannot do this.** It grants by client IP, and that IP is the Proxmox
host — so the grant belongs to the host, not to one container. Any container
you bind that path into gets the same access. The containment is that only you
run `pct set`; it is host-admin discipline, not something the NAS enforces.

**SMB can.** The NAS authenticates a real account, which you can revoke without
touching host-level rules. It also sidesteps NFS's uid squashing entirely: the
server checks the account, and the mount decides what the files look like
locally.

If you want access scoped to Tracepaper, or you are fighting `Permission
denied` on an NFS mount, SMB is the better trade despite being slower to scan.

### Using a dedicated NAS account

**NFS does not authenticate users.** With `sec=sys` it trusts whatever uid the
client sends, and access is granted per client IP. A DSM account is never
consulted, so a user created to scope access does nothing on an NFS mount.

SMB does authenticate, and mounts a subfolder directly:

```bash
# On the Proxmox host
install -m600 /dev/null /etc/samba/tracepaper.cred
cat > /etc/samba/tracepaper.cred <<'CRED'
username=tracepaper
password=<the password you set in DSM>
CRED

PROTOCOL=smb READ_SHARE=/data/Documents \
  ./add-nas.sh <CTID> <synology-ip>
```

Give that user **Read only** on the documents share and **Read/Write** on
backups, under Control Panel → User → Permissions.

The tradeoff: credentials live in a file on the host, and SMB is slower at the
many-small-file walking a scan does. For read-only documents on a home LAN, NFS
by IP is usually the better trade; use SMB when you want the access tied to an
account you can revoke.

## 3. First scan

**From the UI:** Settings → Jobs → *Run now* next to "Scan for new and changed
files". The panel polls while it runs, so progress is visible without a
terminal, and the button stays disabled until it finishes.

From the command line, if you prefer:

```bash
pct exec <CTID> -- systemctl start --no-block tracepaper-scan
pct exec <CTID> -- journalctl -u tracepaper-scan -f
```

`--no-block` matters: the unit is `Type=oneshot`, so a plain `systemctl start`
waits for the whole scan to finish and looks like a hung terminal. It is not
hung; it is scanning silently, because the output goes to the journal.

Hours for a lifetime of documents, and resumable — interrupting it costs only
the document in flight. The hourly timer picks up everything after that.

Progress is easier to read from the index than the log, since the scanner logs
once per root rather than per file:

```bash
watch -n 10 'curl -sk https://<your-host>/api/status'
```

`items` climbing means it is working. To check without following:

```bash
pct exec <CTID> -- systemctl is-active tracepaper-scan   # activating = running
```

---

## 4. HTTPS (optional)

Serves on a hostname instead of `host:8823`, with the plain-HTTP port closed.

```bash
pct exec <CTID> -- env DOMAIN=tracepaper.example.net \
  /opt/tracepaper/deploy/caddy-install.sh
```

Or during install: `TLS_DOMAIN=tracepaper.example.net ./deploy/proxmox-install.sh`.

**Why a self-issued certificate.** A hostname that resolves only inside your
network cannot be validated from outside, so no public CA will sign for it —
Let's Encrypt says as much in "Certificates for localhost" and recommends
issuing your own. `tls internal` runs a small CA inside the container, signs for
this host, and renews indefinitely. Nothing leaves the LAN and there is no API
token anywhere.

The cost is trusting that CA root once per device:

```bash
pct pull <CTID> \
  /var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt \
  tracepaper-root.crt

# macOS
sudo security add-trusted-cert -d -r trustRoot \
  -k /Library/Keychains/System.keychain tracepaper-root.crt
```

iOS needs the profile installed *and* enabled under Settings → General → About →
Certificate Trust Settings. Android: Settings → Security → Encryption &
credentials → Install a certificate → CA.

Until then browsers warn. The connection is encrypted either way — the warning
is about trust, not encryption.

The script switches the app to `--host 127.0.0.1`, so `http://host:8823` stops
working. That is the point: while it still listened on `0.0.0.0` the plaintext
path would quietly bypass TLS. To undo it, the script prints the exact command.

---

## 5. Updating

**From the UI:** Settings → Updates → *Check for updates* → *Update now*. The
page waits for the restart and reloads itself.

**From the command line, either of:**

```bash
pct exec <CTID> -- systemctl start tracepaper-update   # same path as the button
pct exec <CTID> -- /opt/tracepaper/deploy/update.sh    # run it in the foreground
journalctl -u tracepaper-update -f                     # follow either
```

Both back up the human-authored layer first, fast-forward, reinstall
dependencies, reinstall any changed unit files, restart, and **roll back
automatically** if the new version is not healthy within 60 seconds.

**Why the button is safe.** The app never applies the update itself — it runs
as an unprivileged user with no capabilities and cannot. It asks systemd to
start `tracepaper-update.service`, which root owns, and a polkit rule grants
that one user permission to start that one unit with that one verb. Nothing
else. The worst anyone reaching the endpoint can do is make the machine install
the code already published at your configured remote.

The endpoint also requires a custom header and rejects cross-site requests.
Neither is authentication — they close the case where another site makes your
browser POST here, and nothing more.

Updating needs a **cloned** install (the default). A copied checkout has no
remote to pull from, and the UI says so instead of offering a dead button.

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
pct exec <CTID> -- systemctl start --no-block tracepaper-scan
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

## Operating

Scan, enrich and backup all run on timers, and all three have a *Run now*
button under Settings → Jobs — the panel shows whether one is running, when it
last ran, and whether it failed. The buttons appear only when polkit actually
permits this user to start the unit, so one that appears will work.

The rest is easier from a shell:

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
| `caddy-install.sh` | HTTPS on a hostname, with a certificate the container issues itself. |
| `Caddyfile` | The proxy config it installs. |
| `tracepaper-update.service` | The privileged half of an update. On-demand only. |
| `49-tracepaper-update.rules` | polkit grant: the app may start the update and the three job units, nothing else. |
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
TLS_DOMAIN=host.example.net   # serve HTTPS instead of plain HTTP on APP_PORT
REPO_URL=             # empty copies the local checkout instead of cloning
ROOT_PASSWORD=...     # otherwise console auto-login only
SSH_KEY="ssh-ed25519 ..."
ASSUME_YES=1          # skip the confirmation prompt
KEEP_ON_FAIL=1        # keep a failed container for inspection
```

`add-nas.sh <CTID> <nas-ip>`:

```bash
READ_SHARE=/volume1/documents          # the export to index
WRITE_SHARE=/volume1/backups/tracepaper
DOCS_MOUNT=/mnt/nas/documents               # where it lands in the container
BACKUP_MOUNT=/mnt/nas/backups/tracepaper
NFS_VERS=4.1                                # 3 for older DSM
CONFIG=/etc/tracepaper.toml
```

---

**No authentication yet.** Do not port-forward this. Reach it over the LAN or a
VPN.

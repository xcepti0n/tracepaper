# Deploying Tracepaper

## Two NFS shares: one to read, one to write

Mount both with the OS, not with this app. Mounting needs root, and an app that
mounts filesystems turns every stale handle and credential problem into its own
bug. `/etc/fstab` handles it properly and survives a reboot.

```
# /etc/fstab — Synology NFS
nas.local:/volume1/documents  /mnt/nas/documents  nfs  ro,soft,timeo=30,_netdev  0 0
nas.local:/volume1/backups    /mnt/nas/backups    nfs  rw,soft,timeo=30,_netdev  0 0
```

`ro` on the documents share makes NFR-7 enforced by the kernel as well as by
the application. `soft,timeo=30` means an unreachable NAS returns errors
instead of hanging the service forever.

Then point Tracepaper at the mounted paths from **Settings** in the web UI,
which validates each one before saving — or set them in the config file below.

### Why the index does not go on the NAS

SQLite over NFS or SMB corrupts. Their file locking is unreliable across
clients, and SQLite's WAL mode depends on it. This does not fail loudly; it
fails silently, weeks later. The settings page refuses an NFS index path for
that reason.

You still get one read share and one write share. The difference is that the
write share receives **backups**, not the live database — and since the index
rebuilds from your documents, the NAS still holds everything irreplaceable.

| Path | Where | Why |
|---|---|---|
| Documents | Synology NFS, `ro` | Read-only, never modified |
| Live index | Local disk | SQLite needs real file locking |
| Backups | Synology NFS, `rw` | Survives losing the index machine |

## Read one folder, write another

Tracepaper **never writes to the folder it reads**. The source tree is opened
read-only and is never modified, moved, or renamed (NFR-7).

```toml
[index]
db_path = "/var/lib/tracepaper/index.db"   # written here, and nowhere else

[scan]
roots = ["/mnt/nas/documents"]              # read-only, never touched
```

### Where the index should live

**On the machine running the service, not on the NAS.** SQLite over SMB/NFS
corrupts under concurrent access — this is a well-known failure, not a
theoretical one, and it is the reason the design puts the index on local disk
(D-008).

Back the index up *to* the NAS instead. Two files matter, and they are very
different in kind:

| File | Size | Rebuildable? |
|---|---|---|
| `index.db` | grows with the corpus | **Yes** — from the source folder |
| `human-layer.json` | kilobytes | **No** — this is the irreplaceable part |

The second holds your corrections, notes, entity merges, pinned vocabulary and
face-cluster names. Everything else regenerates:

```bash
tracepaper backup /mnt/nas/backups/tracepaper-human-layer.json
```

Restore into a rebuilt index with `tracepaper backup <file> --restore`. Corrections are
matched by content hash, so a file that moved since the backup still matches.

---

## Test on the Mac first

**Recommendation: yes — one afternoon on the Mac, then deploy.**

Not because Proxmox is risky, but because the two machines answer different
questions and the Mac answers the important one sooner:

- Extraction quality on *your* documents. Do your bank statements, visas and
  leases actually yield fields? This is where the work will be, and it is the
  same code on either machine.
- OCR quality on your scans. The Mac has Vision built in and needs no install;
  Proxmox needs Tesseract and poppler.
- Whether the UI shows you what you need.

What testing on the Mac will *not* tell you: how long a full scan of 40k files
takes over SMB, or how the service behaves on a 16 GB box already running other
things. Those need the real deployment, and neither is likely to surprise you.

```bash
# On the Mac, against a mounted share, read-only
git clone <this repo> && cd tracepaper
python3 -m venv .venv && .venv/bin/pip install -e ".[formats,semantic,web,ocr-macos]"

cp tracepaper.example.toml tracepaper.toml
# edit: roots = ["/Volumes/YourShare/Documents"], db_path = "./data/index.db"

.venv/bin/tracepaper scan --index          # start with a subfolder, not everything
.venv/bin/tracepaper status
.venv/bin/tracepaper keys                  # what did it actually find?
.venv/bin/tracepaper serve                 # http://127.0.0.1:8823
```

Point `roots` at **one subfolder** first — a few hundred documents. A full
first scan of a lifetime archive will take a while and tells you less than a
small one you can actually inspect.

---

## Deploy to Proxmox

**There is an installer for this.** `deploy/proxmox-install.sh`, run on the
Proxmox host, does everything in this section: creates an unprivileged LXC,
mounts both Synology shares, installs the app, and enables the service and
timers. See `deploy/README.md`.

```bash
NAS_HOST=192.168.1.10 ./deploy/proxmox-install.sh
```

The rest of this section is the manual equivalent, and the reference for a VM
or bare metal — where the systemd hardening below can be stricter than an
unprivileged LXC allows. `deploy/tracepaper.service` is the LXC variant and
deliberately omits the mount-namespace directives, which cannot work there.

```bash
sudo apt install -y python3-venv tesseract-ocr poppler-utils
#                                 ^ OCR         ^ rasterises scanned PDFs

sudo useradd -r -s /usr/sbin/nologin tracepaper
sudo mkdir -p /var/lib/tracepaper /opt/tracepaper
sudo chown tracepaper: /var/lib/tracepaper

cd /opt/tracepaper
sudo -u tracepaper python3 -m venv .venv
sudo -u tracepaper .venv/bin/pip install -e ".[formats,semantic,web,ocr]"
```

Mount the NAS share **read-only** — belt and braces alongside the application's
own guarantee:

```
# /etc/fstab
//nas.local/documents /mnt/nas/documents cifs ro,credentials=/etc/samba/creds,uid=tracepaper,iocharset=utf8 0 0
```

### Service

```ini
# /etc/systemd/system/tracepaper.service
[Unit]
Description=Tracepaper
After=network-online.target remote-fs.target

[Service]
User=tracepaper
WorkingDirectory=/opt/tracepaper
ExecStart=/opt/tracepaper/.venv/bin/tracepaper --config /etc/tracepaper.toml serve --host 0.0.0.0
Restart=on-failure

# The service needs to write only its own index directory.
ProtectSystem=strict
ReadWritePaths=/var/lib/tracepaper
PrivateTmp=true
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

### Scheduled scans

Filesystem events do not cross SMB/NFS, so discovery is a scheduled scan
(D-009). A missed run costs nothing — the next pass reconciles.

```ini
# /etc/systemd/system/tracepaper-scan.service
[Unit]
Description=Tracepaper reconciliation scan
After=remote-fs.target

[Service]
Type=oneshot
User=tracepaper
ExecStart=/opt/tracepaper/.venv/bin/tracepaper --config /etc/tracepaper.toml scan --index --embed
ExecStartPost=/opt/tracepaper/.venv/bin/tracepaper --config /etc/tracepaper.toml backup /mnt/nas/backups/tracepaper-human-layer.json
```

```ini
# /etc/systemd/system/tracepaper-scan.timer
[Unit]
Description=Scan the document folder hourly

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl enable --now tracepaper.service tracepaper-scan.timer
```

### Nightly enrichment

Object tagging, captions and embeddings are slow and optional. They run when
the machine is idle and stop the moment it is busy, so this is safe to leave
running overnight.

```ini
# /etc/systemd/system/tracepaper-enrich.service
[Unit]
Description=Tracepaper background enrichment

[Service]
Type=oneshot
User=tracepaper
Nice=19
IOSchedulingClass=idle
ExecStart=/opt/tracepaper/.venv/bin/tracepaper --config /etc/tracepaper.toml enrich --wait
```

```ini
# /etc/systemd/system/tracepaper-enrich.timer
[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true
RandomizedDelaySec=1h

[Install]
WantedBy=timers.target
```

Note that `tracepaper enrich` uses macOS Vision for object tagging, which exists only
on the Mac. On Proxmox this pass does embeddings only, unless you enable
captions against an Ollama endpoint.

---

## Split deployment: heavy work on the Mac

The design anticipates the ingest worker running on the Mac while the service
stays on Proxmox (D-007). Worth doing only if Proxmox struggles with embeddings
or you want the LLM layer; otherwise keep it simple and run everything on
Proxmox.

The seam already exists — the index is a file, and `tracepaper scan --index --embed`
against it is the worker. Point the Mac at the same index over a share **only
while the Proxmox service is stopped**, or better: run ingest on the Mac
against a local copy and rsync the result. Concurrent SQLite writes over a
network share are exactly what D-008 warns about.

If you enable the LLM layer, it runs at ingest only and never at query time —
stopping Ollama changes no search result.

```toml
[llm]
enabled = true
endpoint = "http://mac.local:11434"
model = "gemma4:e4b-mlx"
```

---

## MCP: connecting an agent

```json
{
  "mcpServers": {
    "tracepaper": {
      "command": "/opt/tracepaper/.venv/bin/tracepaper-mcp",
      "args": ["/var/lib/tracepaper/index.db"]
    }
  }
}
```

Eleven tools: `search`, `get_value`, `aggregate`, `get_events`,
`gather_evidence`, `list_keys`, `list_values`, `get_item`, `find_entity`,
`add_note`, `correct_value`. They return data, never prose — the agent
narrates, Tracepaper does not.

---

## Operating notes

**Scan aborted, "the share is probably not mounted".** Working as intended: more
than half the known paths vanished at once, so the scan refused to soft-delete
the index. Fix the mount and re-run.

**Items stuck at `partial`.** Something is missing rather than broken. Check
`tracepaper status`; usually a scanned PDF with no OCR backend installed, or an image
OCR could not read. The document stays findable by filename either way.

**A field extracted wrongly.** `tracepaper correct <item_id> <key> <value>`. That value
then outranks every extractor and survives reindexing forever — including a
re-run by a better model in 2028.

**Search feels wrong.** `tracepaper search "..." --explain` shows every ranking signal
and its contribution. Nothing about ranking is hidden or learned.

**Two names for one merchant.** `tracepaper entities` to find both ids, then
`tracepaper entities --merge <from> <to>`. Permanent.

**Two names for one field.** `tracepaper vocab --suggest` proposes merges;
`tracepaper vocab --merge <from> <to>` applies one and rewrites stored rows.

**Changing the embedding model.** Set it in config, then
`sqlite3 index.db 'DELETE FROM embeddings'` and `tracepaper embed`. Vectors are a
rebuildable cache; nothing else is affected.

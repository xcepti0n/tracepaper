# Deploying DataManager

## Read one folder, write another

DataManager **never writes to the folder it reads**. The source tree is opened
read-only and is never modified, moved, or renamed (NFR-7).

```toml
[index]
db_path = "/var/lib/datamanager/index.db"   # written here, and nowhere else

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
dm backup /mnt/nas/backups/datamanager-human-layer.json
```

Restore into a rebuilt index with `dm backup <file> --restore`. Corrections are
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
git clone <this repo> && cd DataManager
python3 -m venv .venv && .venv/bin/pip install -e ".[formats,semantic,web,ocr-macos]"

cp datamanager.example.toml datamanager.toml
# edit: roots = ["/Volumes/YourShare/Documents"], db_path = "./data/index.db"

.venv/bin/dm scan --index          # start with a subfolder, not everything
.venv/bin/dm status
.venv/bin/dm keys                  # what did it actually find?
.venv/bin/dm serve                 # http://127.0.0.1:8823
```

Point `roots` at **one subfolder** first — a few hundred documents. A full
first scan of a lifetime archive will take a while and tells you less than a
small one you can actually inspect.

---

## Deploy to Proxmox

```bash
sudo apt install -y python3-venv tesseract-ocr poppler-utils
#                                 ^ OCR         ^ rasterises scanned PDFs

sudo useradd -r -s /usr/sbin/nologin datamanager
sudo mkdir -p /var/lib/datamanager /opt/datamanager
sudo chown datamanager: /var/lib/datamanager

cd /opt/datamanager
sudo -u datamanager python3 -m venv .venv
sudo -u datamanager .venv/bin/pip install -e ".[formats,semantic,web,ocr]"
```

Mount the NAS share **read-only** — belt and braces alongside the application's
own guarantee:

```
# /etc/fstab
//nas.local/documents /mnt/nas/documents cifs ro,credentials=/etc/samba/creds,uid=datamanager,iocharset=utf8 0 0
```

### Service

```ini
# /etc/systemd/system/datamanager.service
[Unit]
Description=DataManager
After=network-online.target remote-fs.target

[Service]
User=datamanager
WorkingDirectory=/opt/datamanager
ExecStart=/opt/datamanager/.venv/bin/dm --config /etc/datamanager.toml serve --host 0.0.0.0
Restart=on-failure

# The service needs to write only its own index directory.
ProtectSystem=strict
ReadWritePaths=/var/lib/datamanager
PrivateTmp=true
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

### Scheduled scans

Filesystem events do not cross SMB/NFS, so discovery is a scheduled scan
(D-009). A missed run costs nothing — the next pass reconciles.

```ini
# /etc/systemd/system/datamanager-scan.service
[Unit]
Description=DataManager reconciliation scan
After=remote-fs.target

[Service]
Type=oneshot
User=datamanager
ExecStart=/opt/datamanager/.venv/bin/dm --config /etc/datamanager.toml scan --index --embed
ExecStartPost=/opt/datamanager/.venv/bin/dm --config /etc/datamanager.toml backup /mnt/nas/backups/datamanager-human-layer.json
```

```ini
# /etc/systemd/system/datamanager-scan.timer
[Unit]
Description=Scan the document folder hourly

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl enable --now datamanager.service datamanager-scan.timer
```

### Nightly enrichment

Object tagging, captions and embeddings are slow and optional. They run when
the machine is idle and stop the moment it is busy, so this is safe to leave
running overnight.

```ini
# /etc/systemd/system/datamanager-enrich.service
[Unit]
Description=DataManager background enrichment

[Service]
Type=oneshot
User=datamanager
Nice=19
IOSchedulingClass=idle
ExecStart=/opt/datamanager/.venv/bin/dm --config /etc/datamanager.toml enrich --wait
```

```ini
# /etc/systemd/system/datamanager-enrich.timer
[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true
RandomizedDelaySec=1h

[Install]
WantedBy=timers.target
```

Note that `dm enrich` uses macOS Vision for object tagging, which exists only
on the Mac. On Proxmox this pass does embeddings only, unless you enable
captions against an Ollama endpoint.

---

## Split deployment: heavy work on the Mac

The design anticipates the ingest worker running on the Mac while the service
stays on Proxmox (D-007). Worth doing only if Proxmox struggles with embeddings
or you want the LLM layer; otherwise keep it simple and run everything on
Proxmox.

The seam already exists — the index is a file, and `dm scan --index --embed`
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
    "datamanager": {
      "command": "/opt/datamanager/.venv/bin/dm-mcp",
      "args": ["/var/lib/datamanager/index.db"]
    }
  }
}
```

Eleven tools: `search`, `get_value`, `aggregate`, `get_events`,
`gather_evidence`, `list_keys`, `list_values`, `get_item`, `find_entity`,
`add_note`, `correct_value`. They return data, never prose — the agent
narrates, DataManager does not.

---

## Operating notes

**Scan aborted, "the share is probably not mounted".** Working as intended: more
than half the known paths vanished at once, so the scan refused to soft-delete
the index. Fix the mount and re-run.

**Items stuck at `partial`.** Something is missing rather than broken. Check
`dm status`; usually a scanned PDF with no OCR backend installed, or an image
OCR could not read. The document stays findable by filename either way.

**A field extracted wrongly.** `dm correct <item_id> <key> <value>`. That value
then outranks every extractor and survives reindexing forever — including a
re-run by a better model in 2028.

**Search feels wrong.** `dm search "..." --explain` shows every ranking signal
and its contribution. Nothing about ranking is hidden or learned.

**Two names for one merchant.** `dm entities` to find both ids, then
`dm entities --merge <from> <to>`. Permanent.

**Two names for one field.** `dm vocab --suggest` proposes merges;
`dm vocab --merge <from> <to>` applies one and rewrites stored rows.

**Changing the embedding model.** Set it in config, then
`sqlite3 index.db 'DELETE FROM embeddings'` and `dm embed`. Vectors are a
rebuildable cache; nothing else is affected.

"""REST API and web UI (FR-12).

One query engine, two front doors. Every endpoint here is a thin wrapper over
the same deterministic query layer the CLI uses -- no model is loaded in this
process, and none is called to serve a request (NFR-1).

Read-only by default. The two write paths are corrections and notes, which are
the human-authored layer the whole design treats as authoritative (FR-10).
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import corrections, embed, entities, events, notes, settings, storage, vocabulary
from .config import Config
from .db import connect
from .index.indexer import Indexer
from .query.evidence import EvidenceQuery
from .query.fields import FieldQuery
from .query.search import SearchEngine
from .scan.scanner import ScanAborted, Scanner
from .web import render_page

_config: Config = Config()


def get_config() -> Config:
    return _config


def set_config(cfg: Config) -> None:
    global _config
    _config = cfg


def open_connection() -> sqlite3.Connection:
    """A connection per request.

    SQLite connections are not safe to share across threads, and a request-
    scoped connection also means a failed request cannot leave a transaction
    open for the next one.
    """
    return connect(_config.db_path)


def create_app(cfg: Config | None = None) -> FastAPI:
    if cfg is not None:
        set_config(cfg)

    # Load the embedding model once, at startup. An embedding model is a
    # deterministic text-to-vector function, so it is welcome in the query
    # path; what is not welcome is loading it mid-request, which reaches the
    # network and crashed the worker.
    if embed.available():
        if embed.preload(_config.embed_model):
            print(f"semantic search ready ({_config.embed_model})")
        else:
            print(f"WARNING: could not load {_config.embed_model}; "
                  f"search will be keyword-only")
    else:
        print("sentence-transformers not installed; search will be keyword-only")
    embed.set_lazy_load(False)

    app = FastAPI(title="Tracepaper", version="0.1.0",
                  description="Deterministic search over personal documents")

    # ------------------------------------------------------------ web UI

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, q: str = "", tab: str = "search",
             limit: int = 20, semantic: bool = True) -> str:
        conn = open_connection()
        try:
            return render_page(conn, query=q, tab=tab, limit=limit,
                               semantic=semantic)
        finally:
            conn.close()

    # -------------------------------------------------------------- API

    @app.get("/api/search")
    def api_search(q: str = Query(..., min_length=1), limit: int = 20,
                   offset: int = 0, kind: str | None = None,
                   semantic: bool = True) -> dict[str, Any]:
        conn = open_connection()
        try:
            response = SearchEngine(conn).search(
                q, limit=limit, offset=offset, kind=kind, semantic=semantic)
            return {
                "query": response.query,
                "total": response.total,
                "hits": [
                    {
                        "item_id": hit.item_id, "passage_id": hit.passage_id,
                        "title": hit.title, "uri": hit.uri, "page": hit.page,
                        "snippet": hit.snippet, "score": round(hit.score, 6),
                        "signals": {k: round(v, 6) for k, v in hit.signals.items()},
                    }
                    for hit in response.hits
                ],
            }
        finally:
            conn.close()

    @app.get("/api/get")
    def api_get(key: str, where: list[str] = Query(default=[]),
                record_type: str | None = None, limit: int = 5) -> dict[str, Any]:
        """Tier 1: a value with its citation."""
        conn = open_connection()
        try:
            answer = FieldQuery(conn).get(key, where=_parse_where(where),
                                          record_type=record_type, limit=limit)
            if not answer.values:
                return {"key": key, "found": False, "values": []}
            return {
                "key": answer.key,
                "found": True,
                "unambiguous": answer.is_unambiguous,
                "values": [_field_json(v) for v in answer.values],
            }
        finally:
            conn.close()

    @app.get("/api/aggregate")
    def api_aggregate(key: str, op: str = "sum",
                      where: list[str] = Query(default=[]),
                      record_type: str | None = None) -> dict[str, Any]:
        conn = open_connection()
        try:
            result, contributing = FieldQuery(conn).aggregate(
                key, op, where=_parse_where(where), record_type=record_type)
            return {
                "key": key, "op": op, "result": result,
                # Every contributor is returned so the number can be audited.
                "contributing": [_field_json(v) for v in contributing],
            }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        finally:
            conn.close()

    @app.get("/api/events")
    def api_events(event_type: str | None = None, entity: str | None = None,
                   since: str | None = None, until: str | None = None,
                   limit: int = 50) -> dict[str, Any]:
        conn = open_connection()
        try:
            rows = events.query(conn, event_type=event_type, entity=entity,
                                since=since, until=until, limit=limit)
            return {"events": [
                {
                    "id": int(row["id"]), "type": row["event_type"],
                    "date": row["occurred_on"],
                    "precision": row["occurred_precision"],
                    "title": row["title"],
                    "entities": [
                        {"name": e["canonical_name"], "role": e["role"]}
                        for e in events.event_entities(conn, int(row["id"]))
                    ],
                    "evidence": [
                        {"item_id": int(e["item_id"]), "title": e["title"],
                         "uri": e["uri"]}
                        for e in events.evidence(conn, int(row["id"]))
                    ],
                }
                for row in rows
            ]}
        finally:
            conn.close()

    @app.get("/api/ask")
    def api_ask(q: str, entity: str | None = None,
                key: list[str] = Query(default=[]),
                limit: int = 8) -> dict[str, Any]:
        """Tier 2: an evidence set. Never a conclusion."""
        conn = open_connection()
        try:
            return EvidenceQuery(conn).gather(
                q, entity=entity, keys=key, limit=limit).as_dict()
        finally:
            conn.close()

    @app.get("/api/items/{item_id}")
    def api_item(item_id: int) -> dict[str, Any]:
        conn = open_connection()
        try:
            item = conn.execute("SELECT * FROM items WHERE id = ?",
                                (item_id,)).fetchone()
            if item is None:
                raise HTTPException(status_code=404, detail="no such item")
            return {
                "id": int(item["id"]), "kind": item["kind"], "uri": item["uri"],
                "title": item["title"], "mime": item["mime"],
                "status": item["extraction_status"],
                "indexed_at": item["indexed_at"],
                "records": _item_records(conn, item_id),
                "text": _item_text(conn, item_id),
            }
        finally:
            conn.close()

    @app.get("/api/keys")
    def api_keys(prefix: str | None = None) -> dict[str, Any]:
        conn = open_connection()
        try:
            return {"keys": [{"key": k, "count": n}
                             for k, n in FieldQuery(conn).list_keys(prefix)]}
        finally:
            conn.close()

    @app.get("/api/values/{key}")
    def api_values(key: str) -> dict[str, Any]:
        conn = open_connection()
        try:
            return {"key": key, "values": [
                {"value": v, "count": n}
                for v, n in FieldQuery(conn).list_values(key)]}
        finally:
            conn.close()

    @app.get("/api/entities")
    def api_entities(name: str | None = None) -> dict[str, Any]:
        conn = open_connection()
        try:
            if name:
                rows = entities.find(conn, name)
                return {"entities": [
                    {"id": int(r["id"]), "name": r["canonical_name"],
                     "type": r["entity_type"],
                     "aliases": entities.aliases(conn, int(r["id"]))}
                    for r in rows]}
            return {"entities": [
                {"id": int(r["id"]), "name": r["canonical_name"],
                 "type": r["entity_type"], "events": int(r["event_count"])}
                for r in entities.list_all(conn)]}
        finally:
            conn.close()

    @app.get("/api/storage")
    def api_storage() -> dict[str, Any]:
        """Validate every configured path, and list mounted network shares."""
        return {
            "checks": storage.check_all(
                [str(r) for r in _config.roots],
                str(_config.db_path),
                str(_config.backup_dir) if _config.backup_dir else None),
            "mounts": storage.mounts(),
            "config_file": str(settings.find_config() or ""),
        }

    @app.post("/api/storage/check")
    def api_storage_check(payload: dict) -> dict[str, Any]:
        """Validate a proposed path before it is saved."""
        path = payload.get("path", "")
        kind = payload.get("kind", "source")
        if not path:
            raise HTTPException(status_code=400, detail="path is required")
        checker = {"source": storage.check_source, "index": storage.check_index,
                   "backup": storage.check_backup}.get(kind)
        if checker is None:
            raise HTTPException(status_code=400, detail=f"unknown kind: {kind}")
        return checker(path).as_dict()

    @app.post("/api/settings")
    def api_save_settings(payload: dict) -> dict[str, Any]:
        config_file = settings.find_config() or Path("tracepaper.toml")
        roots = payload.get("roots") or []
        db_path = payload.get("db_path") or str(_config.db_path)
        backup_dir = payload.get("backup_dir") or None

        ok, problems = settings.save(
            config_file, roots=roots, db_path=db_path, backup_dir=backup_dir,
            llm_enabled=payload.get("llm_enabled"),
            llm_endpoint=payload.get("llm_endpoint"),
            llm_model=payload.get("llm_model"))
        if not ok:
            return {"ok": False, "problems": problems}

        # Apply immediately, so a saved change takes effect without a restart.
        set_config(Config.load(config_file))
        return {"ok": True, "config_file": str(config_file),
                "restart_required": str(_config.db_path) != db_path}

    @app.get("/api/health")
    def api_health() -> dict[str, Any]:
        """Liveness for the installer, systemd and any monitor.

        Deliberately cheap and deliberately not a status page: it opens the
        index and reads one row. A health check that counted rows would get
        slower as the corpus grew, which is backwards -- the check matters most
        on the largest install.

        An unreadable index is reported as unhealthy with the reason, rather
        than raising: a 200 with `ok: false` is something a script can act on.
        """
        try:
            conn = open_connection()
            try:
                conn.execute("SELECT 1 FROM items LIMIT 1").fetchone()
            finally:
                conn.close()
        except Exception as exc:
            return {"ok": False, "error": str(exc),
                    "db_path": str(_config.db_path)}
        return {"ok": True, "db_path": str(_config.db_path)}

    @app.get("/api/updates")
    def api_updates() -> dict[str, Any]:
        from . import updates
        return updates.check().as_dict()

    @app.post("/api/updates/apply")
    def api_updates_apply(request: Request) -> dict[str, Any]:
        """Trigger the privileged update unit.

        There is no authentication on this server yet, so this endpoint gets
        two cheap defences against a drive-by request from a page the user
        happens to have open:

        - a custom header, which a cross-site form cannot set without CORS
          preflight, and
        - a Sec-Fetch-Site check, which browsers send and cannot be forged
          from script.

        Neither is authentication and neither should be described as such. What
        they close is the case where some other site makes your browser POST
        here; they do nothing against anyone who can already reach the port.
        The real containment is that this only ever installs the code already
        published at the configured remote.
        """
        from . import updates

        if request.headers.get("x-tracepaper-request") != "1":
            raise HTTPException(
                status_code=403,
                detail="Missing X-Tracepaper-Request header. Trigger updates "
                       "from the web UI, or run `systemctl start "
                       "tracepaper-update` in the container.")
        if request.headers.get("sec-fetch-site", "same-origin") not in (
                "same-origin", "none"):
            raise HTTPException(status_code=403,
                                detail="Cross-site update requests are refused.")

        started, message = updates.apply()
        if not started:
            raise HTTPException(status_code=409, detail=message)
        return {"ok": True, "message": message}

    @app.get("/api/jobs")
    def api_jobs() -> dict[str, Any]:
        from . import jobs
        return jobs.status().as_dict()

    @app.post("/api/jobs/{name}/start")
    def api_jobs_start(name: str, request: Request) -> dict[str, Any]:
        """Trigger one background unit (scan, enrich, backup).

        Same two cheap defences as the update endpoint, for the same reason:
        there is no authentication here, so a page the user happens to have
        open must not be able to POST work onto this server. Neither check is
        authentication and neither should be described as such -- what they
        close is drive-by CSRF, not anyone who can already reach the port.

        The blast radius is smaller than the update endpoint's: these units run
        on timers anyway, so the worst case is running scheduled work early.
        """
        from . import jobs

        if request.headers.get("x-tracepaper-request") != "1":
            raise HTTPException(
                status_code=403,
                detail="Missing X-Tracepaper-Request header. Start jobs from "
                       "the web UI, or run `systemctl start tracepaper-"
                       f"{name}` in the container.")
        if request.headers.get("sec-fetch-site", "same-origin") not in (
                "same-origin", "none"):
            raise HTTPException(status_code=403,
                                detail="Cross-site job requests are refused.")

        if name not in jobs.UNITS:
            raise HTTPException(status_code=404, detail=f"unknown job {name!r}.")

        started, message = jobs.start(name)
        if not started:
            raise HTTPException(status_code=409, detail=message)
        return {"ok": True, "message": message}

    @app.get("/api/status")
    def api_status() -> dict[str, Any]:
        conn = open_connection()
        try:
            return _status(conn)
        finally:
            conn.close()

    # ------------------------------------------------------------ writes

    @app.post("/api/correct")
    def api_correct(payload: dict) -> dict[str, Any]:
        item_id = payload.get("item_id")
        key = payload.get("key")
        value = payload.get("value")
        if item_id is None or not key or value is None:
            raise HTTPException(status_code=400,
                                detail="item_id, key and value are required")
        conn = open_connection()
        try:
            corrections.correct_field(conn, int(item_id), str(key), str(value),
                                      unit=payload.get("unit"))
            return {"ok": True, "item_id": int(item_id), "key": key,
                    "value": value, "source": "human"}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        finally:
            conn.close()

    @app.delete("/api/correct")
    def api_uncorrect(item_id: int, key: str) -> dict[str, Any]:
        conn = open_connection()
        try:
            return {"ok": corrections.remove_correction(conn, item_id, key)}
        finally:
            conn.close()

    @app.post("/api/notes")
    def api_create_note(payload: dict) -> dict[str, Any]:
        title = payload.get("title")
        text = payload.get("text", "")
        if not title:
            raise HTTPException(status_code=400, detail="title is required")
        conn = open_connection()
        try:
            item_id = notes.create_note(conn, str(title), str(text))
            Indexer(conn, _config).run_pending()
            return {"ok": True, "item_id": item_id}
        finally:
            conn.close()

    @app.put("/api/notes/{item_id}")
    def api_update_note(item_id: int, payload: dict) -> dict[str, Any]:
        conn = open_connection()
        try:
            version = notes.update_note(conn, item_id, str(payload.get("text", "")),
                                        payload.get("title"))
            Indexer(conn, _config).run_pending()
            return {"ok": True, "item_id": item_id, "version": version}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        finally:
            conn.close()

    @app.post("/api/entities/merge")
    def api_merge_entities(payload: dict) -> dict[str, Any]:
        source_id, target_id = payload.get("source_id"), payload.get("target_id")
        if source_id is None or target_id is None:
            raise HTTPException(status_code=400,
                                detail="source_id and target_id are required")
        conn = open_connection()
        try:
            entities.merge(conn, int(source_id), int(target_id))
            return {"ok": True}
        finally:
            conn.close()

    @app.post("/api/vocab/merge")
    def api_merge_keys(payload: dict) -> dict[str, Any]:
        source, target = payload.get("from_key"), payload.get("to_key")
        if not source or not target:
            raise HTTPException(status_code=400,
                                detail="from_key and to_key are required")
        conn = open_connection()
        try:
            return {"ok": True, "moved": vocabulary.merge(conn, source, target)}
        finally:
            conn.close()

    # ------------------------------------------------------------ ingest

    @app.post("/api/scan")
    def api_scan(payload: dict | None = None) -> dict[str, Any]:
        """Trigger a reconciliation scan. Long-running; call sparingly."""
        payload = payload or {}
        roots = payload.get("roots") or [str(r) for r in _config.roots]
        if not roots:
            raise HTTPException(status_code=400, detail="no scan roots configured")

        conn = open_connection()
        try:
            scanner = Scanner(conn, _config)
            summary = []
            for root in roots:
                try:
                    result = scanner.scan(Path(root))
                    summary.append({"root": root, "added": result.added,
                                    "changed": result.changed,
                                    "moved": result.moved,
                                    "removed": result.removed,
                                    "seen": result.seen})
                except ScanAborted as exc:
                    summary.append({"root": root, "aborted": str(exc)})

            indexed = Indexer(conn, _config).run_pending()
            return {"scans": summary, "index": {
                "processed": indexed.processed, "records": indexed.records,
                "fields": indexed.fields, "events": indexed.events}}
        finally:
            conn.close()

    @app.exception_handler(sqlite3.Error)
    def _sqlite_error(request: Request, exc: sqlite3.Error) -> JSONResponse:
        return JSONResponse(status_code=500,
                            content={"detail": f"database error: {exc}"})

    return app


# ------------------------------------------------------------------ helpers

def _parse_where(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs:
        if "=" in pair:
            key, value = pair.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def _field_json(value) -> dict[str, Any]:
    return {
        "key": value.key, "value": value.value, "text": value.value_text,
        "number": value.value_num, "date": value.value_date, "unit": value.unit,
        "item_id": value.item_id, "title": value.item_title, "uri": value.uri,
        "page": value.page, "source": value.source,
        "confidence": value.confidence, "citation": value.citation(),
    }


def _item_records(conn: sqlite3.Connection, item_id: int) -> list[dict]:
    out = []
    for record in conn.execute(
        "SELECT id, record_type, source, confidence, page FROM records "
        "WHERE item_id = ? ORDER BY id", (item_id,)
    ).fetchall():
        fields = conn.execute(
            "SELECT key, value_text, value_num, value_date, unit, confidence "
            "FROM record_fields WHERE record_id = ? ORDER BY key",
            (record["id"],)
        ).fetchall()
        out.append({
            "id": int(record["id"]), "type": record["record_type"],
            "source": record["source"], "confidence": record["confidence"],
            "page": record["page"],
            "fields": [dict(f) for f in fields],
        })
    return out


def _item_text(conn: sqlite3.Connection, item_id: int) -> str:
    row = conn.execute(
        "SELECT text FROM item_versions WHERE item_id = ? AND valid_to IS NULL "
        "ORDER BY version DESC LIMIT 1", (item_id,)
    ).fetchone()
    return (row["text"] or "") if row else ""


def _status(conn: sqlite3.Connection) -> dict[str, Any]:
    def count(sql: str) -> int:
        row = conn.execute(sql).fetchone()
        return int(row["n"]) if row else 0

    by_status = {
        r["extraction_status"]: int(r["n"]) for r in conn.execute(
            "SELECT extraction_status, COUNT(*) AS n FROM items "
            "WHERE deleted_at IS NULL GROUP BY extraction_status")
    }
    last_scan = conn.execute(
        "SELECT root, started_at, finished_at, seen, added, changed, moved, "
        "removed, status FROM scans ORDER BY id DESC LIMIT 1"
    ).fetchone()

    return {
        "items": count("SELECT COUNT(*) AS n FROM items WHERE deleted_at IS NULL"),
        "deleted": count("SELECT COUNT(*) AS n FROM items WHERE deleted_at IS NOT NULL"),
        "by_status": by_status,
        "passages": count("SELECT COUNT(*) AS n FROM passages"),
        "records": count("SELECT COUNT(*) AS n FROM records"),
        "fields": count("SELECT COUNT(*) AS n FROM record_fields"),
        "corrections": count("SELECT COUNT(*) AS n FROM records WHERE source='human'"),
        "events": count("SELECT COUNT(*) AS n FROM events"),
        "entities": count("SELECT COUNT(*) AS n FROM entities"),
        "embeddings": embed.stats(conn),
        # Pending work is surfaced, never silently missing (NFR-4).
        "pending": count(
            "SELECT COUNT(*) AS n FROM items WHERE deleted_at IS NULL "
            "AND extraction_status IN ('pending','partial')"),
        "queued_jobs": count("SELECT COUNT(*) AS n FROM jobs WHERE state='queued'"),
        "last_scan": dict(last_scan) if last_scan else None,
    }

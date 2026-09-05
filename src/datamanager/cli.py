"""Command-line interface.

    dm scan /Volumes/NAS/documents    reconcile the index against the NAS
    dm index                          process queued extraction jobs
    dm search "sprinkler valve"       deterministic keyword search
    dm status                         index health
    dm note add / edit / show         native notes
    dm show <id>                      item detail
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import notes
from .config import Config
from .db import connect
from .index.indexer import Indexer
from .query.search import SearchEngine
from .scan.scanner import ScanAborted, Scanner


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dm", description="DataManager — deterministic search over your documents")
    parser.add_argument("--db", type=Path, default=None, help="index path")
    parser.add_argument("--config", type=Path, default=None, help="TOML config path")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="reconcile the index against a directory")
    p_scan.add_argument("root", type=Path, nargs="?", help="directory to scan")
    p_scan.add_argument("--force-hash", action="store_true",
                        help="hash every file, ignoring size/mtime shortcuts")
    p_scan.add_argument("--index", action="store_true",
                        help="run extraction immediately after scanning")

    p_index = sub.add_parser("index", help="process queued extraction jobs")
    p_index.add_argument("--limit", type=int, default=None)
    p_index.add_argument("--item", type=int, default=None, help="reindex one item")

    p_search = sub.add_parser("search", help="search the index")
    p_search.add_argument("query", nargs="+")
    p_search.add_argument("-n", "--limit", type=int, default=10)
    p_search.add_argument("--kind", choices=["document", "photo", "note"])
    p_search.add_argument("--explain", action="store_true", help="show ranking signals")

    sub.add_parser("status", help="index health")

    p_show = sub.add_parser("show", help="show one item")
    p_show.add_argument("item_id", type=int)
    p_show.add_argument("--text", action="store_true", help="print extracted text")

    p_note = sub.add_parser("note", help="native notes")
    note_sub = p_note.add_subparsers(dest="note_command", required=True)
    p_add = note_sub.add_parser("add")
    p_add.add_argument("title")
    p_add.add_argument("--text", help="note body; omit to read stdin")
    p_edit = note_sub.add_parser("edit")
    p_edit.add_argument("item_id", type=int)
    p_edit.add_argument("--text", help="new body; omit to read stdin")
    p_edit.add_argument("--title")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s" if args.verbose else "%(message)s",
    )

    cfg = Config.load(args.config)
    if args.db:
        from dataclasses import replace
        cfg = replace(cfg, db_path=args.db)

    conn = connect(cfg.db_path)
    try:
        return _dispatch(args, cfg, conn)
    finally:
        conn.close()


def _dispatch(args, cfg: Config, conn) -> int:
    if args.command == "scan":
        return _cmd_scan(args, cfg, conn)
    if args.command == "index":
        return _cmd_index(args, cfg, conn)
    if args.command == "search":
        return _cmd_search(args, conn)
    if args.command == "status":
        return _cmd_status(conn)
    if args.command == "show":
        return _cmd_show(args, conn)
    if args.command == "note":
        return _cmd_note(args, conn)
    return 1


def _cmd_scan(args, cfg: Config, conn) -> int:
    roots = [args.root] if args.root else list(cfg.roots)
    if not roots:
        print("no scan root given: pass a directory or set scan.roots in config",
              file=sys.stderr)
        return 2

    scanner = Scanner(conn, cfg)
    failed = False
    for root in roots:
        try:
            result = scanner.scan(Path(root))
        except ScanAborted as exc:
            print(f"scan aborted: {exc}", file=sys.stderr)
            failed = True
            continue
        print(f"{root}: {result.summary()}")
        for uri, error in result.errors[:10]:
            print(f"  error: {uri}: {error}", file=sys.stderr)

    if failed:
        return 1

    if args.index:
        result = Indexer(conn, cfg).run_pending()
        print(f"index: {result.summary()}")
    return 0


def _cmd_index(args, cfg: Config, conn) -> int:
    indexer = Indexer(conn, cfg)
    if args.item is not None:
        result = indexer.reindex_item(args.item)
    else:
        result = indexer.run_pending(limit=args.limit)
    print(result.summary())
    return 0


def _cmd_search(args, conn) -> int:
    engine = SearchEngine(conn)
    response = engine.search(" ".join(args.query), limit=args.limit, kind=args.kind)

    if not response.hits:
        print("no results")
        return 0

    print(f"{response.total} match(es), showing {len(response.hits)}\n")
    for rank, hit in enumerate(response.hits, start=1):
        location = hit.uri or f"note:{hit.item_id}"
        page = f" p.{hit.page}" if hit.page else ""
        print(f"{rank:2}. {hit.title}{page}  [item {hit.item_id}]")
        print(f"    {location}")
        if hit.snippet:
            print(f"    {hit.snippet}")
        if args.explain:
            print(f"    {hit.explain()}")
        print()
    return 0


def _cmd_status(conn) -> int:
    rows = conn.execute(
        "SELECT extraction_status, COUNT(*) AS n FROM items "
        "WHERE deleted_at IS NULL GROUP BY extraction_status"
    ).fetchall()
    total = sum(int(r["n"]) for r in rows)

    passages = conn.execute("SELECT COUNT(*) AS n FROM passages").fetchone()["n"]
    deleted = conn.execute(
        "SELECT COUNT(*) AS n FROM items WHERE deleted_at IS NOT NULL"
    ).fetchone()["n"]
    jobs = conn.execute(
        "SELECT state, COUNT(*) AS n FROM jobs GROUP BY state"
    ).fetchall()
    last_scan = conn.execute(
        "SELECT root, started_at, finished_at, seen, added, changed, moved, "
        "removed, status FROM scans ORDER BY id DESC LIMIT 1"
    ).fetchone()

    print(f"items:    {total} indexed, {deleted} soft-deleted")
    for row in rows:
        print(f"          {row['extraction_status']}: {row['n']}")
    print(f"passages: {passages}")
    if jobs:
        print("jobs:     " + ", ".join(f"{r['state']}={r['n']}" for r in jobs))
    if last_scan:
        print(f"last scan: {last_scan['root']} [{last_scan['status']}] "
              f"{last_scan['started_at']} → {last_scan['finished_at'] or '—'}")
        print(f"          seen={last_scan['seen']} added={last_scan['added']} "
              f"changed={last_scan['changed']} moved={last_scan['moved']} "
              f"removed={last_scan['removed']}")

    # Pending enrichment must be visible, never silently missing (NFR-4).
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM items "
        "WHERE deleted_at IS NULL AND extraction_status IN ('pending','partial')"
    ).fetchone()["n"]
    if pending:
        print(f"\n{pending} item(s) awaiting full extraction "
              f"(searchable by text already indexed).")
    return 0


def _cmd_show(args, conn) -> int:
    item = conn.execute(
        "SELECT * FROM items WHERE id = ?", (args.item_id,)
    ).fetchone()
    if item is None:
        print(f"no such item: {args.item_id}", file=sys.stderr)
        return 1

    print(f"item {item['id']}  [{item['kind']}]")
    print(f"title:   {item['title']}")
    print(f"uri:     {item['uri'] or '—'}")
    print(f"mime:    {item['mime'] or '—'}")
    print(f"status:  {item['extraction_status']}")
    print(f"hash:    {(item['content_hash'] or '—')[:16]}")
    print(f"indexed: {item['indexed_at'] or '—'}")
    if item["deleted_at"]:
        print(f"deleted: {item['deleted_at']}")

    versions = conn.execute(
        "SELECT version, valid_from, valid_to FROM item_versions "
        "WHERE item_id = ? ORDER BY version", (args.item_id,)
    ).fetchall()
    if versions:
        print(f"\nversions: {len(versions)}")
        for v in versions:
            current = " (current)" if v["valid_to"] is None else ""
            print(f"  v{v['version']}  {v['valid_from']}{current}")

    count = conn.execute(
        "SELECT COUNT(*) AS n FROM passages WHERE item_id = ?", (args.item_id,)
    ).fetchone()["n"]
    print(f"passages: {count}")

    if args.text:
        row = conn.execute(
            "SELECT text FROM item_versions WHERE item_id = ? AND valid_to IS NULL "
            "ORDER BY version DESC LIMIT 1", (args.item_id,)
        ).fetchone()
        if row and row["text"]:
            print("\n--- text ---")
            print(row["text"])
    return 0


def _cmd_note(args, conn) -> int:
    if args.note_command == "add":
        text = args.text if args.text is not None else sys.stdin.read()
        item_id = notes.create_note(conn, args.title, text)
        print(f"created note {item_id}")
    else:
        text = args.text if args.text is not None else sys.stdin.read()
        version = notes.update_note(conn, args.item_id, text, args.title)
        print(f"updated note {args.item_id} → v{version}")

    cfg = Config()
    result = Indexer(conn, cfg).run_pending()
    if result.processed:
        print(f"index: {result.summary()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

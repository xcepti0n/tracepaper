"""Command-line interface.

    dm scan /Volumes/NAS/documents    reconcile the index against the NAS
    dm index                          process queued extraction jobs
    dm search "sprinkler valve"       deterministic keyword search
    dm get expiry_date                direct answer with citation (Tier 1)
    dm get gross_salary --where tax_year=2023
    dm agg amount sum --where merchant=Costco
    dm keys / dm values <key>         discovered vocabulary
    dm correct <id> <key> <value>     hand-correct an extracted value
    dm status                         index health
    dm note add / edit                native notes
    dm show <id>                      item detail
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import backup, corrections, embed, entities, enrich, events, notes, vocabulary
from .config import Config
from .db import connect
from .index.indexer import Indexer
from .query.evidence import EvidenceQuery
from .query.fields import FieldQuery
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
    p_scan.add_argument("--embed", action="store_true",
                        help="also compute embeddings after indexing")

    p_index = sub.add_parser("index", help="process queued extraction jobs")
    p_index.add_argument("--limit", type=int, default=None)
    p_index.add_argument("--item", type=int, default=None, help="reindex one item")
    p_index.add_argument("--embed", action="store_true",
                         help="also compute embeddings for semantic search")

    p_embed = sub.add_parser("embed", help="compute passage embeddings")
    p_embed.add_argument("--model", default=embed.DEFAULT_MODEL)
    p_embed.add_argument("--limit", type=int, default=None)
    p_embed.add_argument("--stats", action="store_true")

    p_search = sub.add_parser("search", help="search the index")
    p_search.add_argument("query", nargs="+")
    p_search.add_argument("-n", "--limit", type=int, default=10)
    p_search.add_argument("--kind", choices=["document", "photo", "note"])
    p_search.add_argument("--explain", action="store_true", help="show ranking signals")
    p_search.add_argument("--no-semantic", action="store_true",
                          help="keyword only, skip vector search")

    p_get = sub.add_parser("get", help="direct answer for a field (Tier 1)")
    p_get.add_argument("key")
    p_get.add_argument("--where", action="append", default=[],
                       metavar="KEY=VALUE",
                       help="constrain within the same record, repeatable")
    p_get.add_argument("--type", dest="record_type", help="restrict record type")
    p_get.add_argument("-n", "--limit", type=int, default=5)

    p_agg = sub.add_parser("agg", help="aggregate a field across documents")
    p_agg.add_argument("key")
    p_agg.add_argument("op", choices=["sum", "avg", "count", "min", "max"])
    p_agg.add_argument("--where", action="append", default=[], metavar="KEY=VALUE")
    p_agg.add_argument("--type", dest="record_type")

    p_keys = sub.add_parser("keys", help="field vocabulary discovered in the corpus")
    p_keys.add_argument("prefix", nargs="?")

    p_values = sub.add_parser("values", help="distinct values for a field")
    p_values.add_argument("key")

    p_correct = sub.add_parser("correct", help="hand-correct a field (outranks extractors)")
    p_correct.add_argument("item_id", type=int)
    p_correct.add_argument("key")
    p_correct.add_argument("value")
    p_correct.add_argument("--unit")
    p_correct.add_argument("--remove", action="store_true",
                           help="drop the correction instead of setting it")

    sub.add_parser("corrections", help="list every hand-corrected value")

    p_ask = sub.add_parser(
        "ask", help="gather evidence for a question (Tier 2 — no answer, just evidence)")
    p_ask.add_argument("question", nargs="+")
    p_ask.add_argument("--entity", help="focus on a merchant, employer, airline")
    p_ask.add_argument("--key", action="append", default=[],
                       help="include this field, repeatable")
    p_ask.add_argument("-n", "--limit", type=int, default=8)
    p_ask.add_argument("--json", action="store_true",
                       help="machine-readable, for an agent to reason over")

    p_events = sub.add_parser("events", help="things that happened, with evidence")
    p_events.add_argument("--type", dest="event_type",
                          help="flight, purchase, payment, income, ...")
    p_events.add_argument("--entity", help="Alaska Airlines, Costco, ...")
    p_events.add_argument("--since", help="ISO date lower bound")
    p_events.add_argument("--until", help="ISO date upper bound")
    p_events.add_argument("--last", action="store_true",
                          help="only the most recent match")
    p_events.add_argument("-n", "--limit", type=int, default=20)

    p_entities = sub.add_parser("entities", help="people, merchants, organizations")
    p_entities.add_argument("name", nargs="?", help="look one up by any alias")
    p_entities.add_argument("--type", dest="entity_type")
    p_entities.add_argument("--merge", nargs=2, metavar=("SOURCE_ID", "TARGET_ID"),
                            help="merge two entities permanently")

    p_vocab = sub.add_parser("vocab", help="key vocabulary and merges")
    p_vocab.add_argument("--suggest", action="store_true",
                         help="show keys whose canonical form differs")
    p_vocab.add_argument("--merge", nargs=2, metavar=("FROM_KEY", "TO_KEY"),
                         help="merge one key into another, rewriting stored rows")

    p_enrich = sub.add_parser(
        "enrich", help="background pass: photo tags, captions, embeddings")
    p_enrich.add_argument("--limit", type=int, default=None)
    p_enrich.add_argument("--captions", action="store_true",
                          help="also caption photos with thin object tags (slow)")
    p_enrich.add_argument("--now", action="store_true",
                          help="run even if the machine is busy")
    p_enrich.add_argument("--wait", action="store_true",
                          help="wait for the machine to go idle, then run")

    p_backup = sub.add_parser(
        "backup", help="export the human-authored layer (the only irreplaceable state)")
    p_backup.add_argument("path", type=Path)
    p_backup.add_argument("--restore", action="store_true",
                          help="restore from this file instead of writing it")

    sub.add_parser("status", help="index health")

    p_serve = sub.add_parser("serve", help="run the web UI and REST API")
    p_serve.add_argument("--host", default="127.0.0.1",
                         help="0.0.0.0 to reach it from other machines")
    p_serve.add_argument("--port", type=int, default=8823)
    p_serve.add_argument("--reload", action="store_true")

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
    if args.command == "embed":
        return _cmd_embed(args, conn)
    if args.command == "search":
        return _cmd_search(args, conn)
    if args.command == "get":
        return _cmd_get(args, conn)
    if args.command == "agg":
        return _cmd_agg(args, conn)
    if args.command == "keys":
        return _cmd_keys(args, conn)
    if args.command == "values":
        return _cmd_values(args, conn)
    if args.command == "correct":
        return _cmd_correct(args, cfg, conn)
    if args.command == "corrections":
        return _cmd_corrections(conn)
    if args.command == "ask":
        return _cmd_ask(args, conn)
    if args.command == "events":
        return _cmd_events(args, conn)
    if args.command == "entities":
        return _cmd_entities(args, conn)
    if args.command == "vocab":
        return _cmd_vocab(args, conn)
    if args.command == "serve":
        return _cmd_serve(args, cfg, conn)
    if args.command == "enrich":
        return _cmd_enrich(args, cfg, conn)
    if args.command == "backup":
        return _cmd_backup(args, conn)
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
        if args.embed and embed.available():
            print(f"embed: {embed.embed_pending(conn).summary()}")
    return 0


def _cmd_index(args, cfg: Config, conn) -> int:
    indexer = Indexer(conn, cfg)
    if args.item is not None:
        result = indexer.reindex_item(args.item)
    else:
        result = indexer.run_pending(limit=args.limit)
    print(result.summary())

    if args.embed and embed.available():
        print(embed.embed_pending(conn).summary())
    return 0


def _cmd_embed(args, conn) -> int:
    if args.stats:
        info = embed.stats(conn)
        print(f"passages with text: {info['passages']}")
        if not info["by_model"]:
            print("no embeddings yet — run: dm embed")
            return 0
        for model_id, count in info["by_model"].items():
            missing = info["passages"] - count
            print(f"  {model_id}: {count} embedded"
                  + (f", {missing} pending" if missing > 0 else ""))
        return 0

    if not embed.available():
        print("sentence-transformers is not installed", file=sys.stderr)
        print("install it with: pip install -e '.[semantic]'", file=sys.stderr)
        return 1

    print(f"embedding with {args.model} (first run downloads the model)...")
    result = embed.embed_pending(conn, model_id=args.model, limit=args.limit)
    print(result.summary())
    return 0


def _cmd_search(args, conn) -> int:
    engine = SearchEngine(conn)
    response = engine.search(" ".join(args.query), limit=args.limit,
                             kind=args.kind, semantic=not args.no_semantic)

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


def _parse_where(pairs: list[str]) -> dict[str, str]:
    where: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--where expects KEY=VALUE, got: {pair}")
        key, value = pair.split("=", 1)
        where[key.strip()] = value.strip()
    return where


def _cmd_get(args, conn) -> int:
    answer = FieldQuery(conn).get(args.key, where=_parse_where(args.where),
                                  record_type=args.record_type, limit=args.limit)
    if not answer.values:
        print(f"no value found for '{args.key}'"
              + (f" with {args.where}" if args.where else ""))
        print("\ntry: dm keys        (what fields exist)")
        print("     dm search ...  (full-text instead)")
        return 0

    best = answer.best
    unit = f" {best.unit}" if best.unit else ""
    print(f"{answer.key}: {best.value}{unit}")
    print(f"  source: {best.citation()}")
    print(f"  via:    {best.source} (confidence {best.confidence:.2f})")

    if len(answer.values) > 1:
        if answer.is_unambiguous:
            print(f"\n{len(answer.values)} documents agree.")
        else:
            # Never silently collapse disagreement into one answer.
            print(f"\n{len(answer.values) - 1} other candidate(s):")
            for value in answer.values[1:]:
                extra = f" {value.unit}" if value.unit else ""
                print(f"  {value.value}{extra}  — {value.citation()} [{value.source}]")
    return 0


def _cmd_agg(args, conn) -> int:
    result, contributing = FieldQuery(conn).aggregate(
        args.key, args.op, where=_parse_where(args.where),
        record_type=args.record_type)

    if result is None:
        print(f"no numeric values for '{args.key}'")
        return 0

    unit = next((v.unit for v in contributing if v.unit), "") or ""
    shown = int(result) if args.op == "count" else round(result, 2)
    print(f"{args.op}({args.key}) = {shown} {unit}".rstrip())
    # The number is auditable: every contributing document is listed (FR-8).
    print(f"\nfrom {len(contributing)} document(s):")
    for value in sorted(contributing, key=lambda v: v.item_id):
        extra = f" {value.unit}" if value.unit else ""
        print(f"  {value.value}{extra}  — {value.citation()}")
    return 0


def _cmd_keys(args, conn) -> int:
    rows = FieldQuery(conn).list_keys(args.prefix)
    if not rows:
        print("no fields extracted yet — run: dm index")
        return 0
    width = max(len(k) for k, _ in rows)
    for key, count in rows:
        print(f"{key:<{width}}  {count}")
    return 0


def _cmd_values(args, conn) -> int:
    rows = FieldQuery(conn).list_values(args.key)
    if not rows:
        print(f"no values for '{args.key}'")
        return 0
    width = max(len(v or "") for v, _ in rows)
    for value, count in rows:
        print(f"{value:<{width}}  {count}")
    return 0


def _cmd_correct(args, cfg: Config, conn) -> int:
    if args.remove:
        if corrections.remove_correction(conn, args.item_id, args.key):
            print(f"removed correction {args.key} on item {args.item_id}")
            print("re-run 'dm index --item {}' to restore the extracted value"
                  .format(args.item_id))
        else:
            print(f"no correction for '{args.key}' on item {args.item_id}")
        return 0

    corrections.correct_field(conn, args.item_id, args.key, args.value,
                              unit=args.unit)
    print(f"set {args.key} = {args.value} on item {args.item_id}")
    print("this value outranks every extractor and survives reindex")
    return 0


def _cmd_corrections(conn) -> int:
    rows = corrections.list_corrections(conn)
    if not rows:
        print("no hand-corrected values")
        return 0
    print(f"{len(rows)} correction(s) — back these up; they cannot be regenerated\n")
    for row in rows:
        unit = f" {row['unit']}" if row["unit"] else ""
        print(f"  item {row['item_id']}  {row['key']} = {row['value_text']}{unit}")
        print(f"    {row['title']}")
    return 0


def _cmd_ask(args, conn) -> int:
    question = " ".join(args.question)
    evidence = EvidenceQuery(conn).gather(
        question, entity=args.entity, keys=args.key, limit=args.limit)

    if args.json:
        import json
        print(json.dumps(evidence.as_dict(), indent=2))
        return 0

    if evidence.is_empty:
        print("no evidence found")
        return 0

    print(f"evidence for: {question}\n")
    if evidence.entities:
        print(f"entities: {', '.join(evidence.entities)}\n")

    if evidence.facts:
        print("facts:")
        for fact in evidence.facts:
            unit = f" {fact.unit}" if fact.unit else ""
            print(f"  {fact.key}: {fact.value}{unit}")
            print(f"    {fact.citation()} [{fact.source}]")
        print()

    if evidence.events:
        print("events:")
        for event in evidence.events:
            print(f"  {event.date or '—'}  {event.detail}")
            print(f"    {event.citation()}")
        print()

    if evidence.passages:
        print("passages:")
        for passage in evidence.passages:
            print(f"  {passage.detail}")
            print(f"    {passage.citation()}")
        print()

    # The engine returns evidence, never a verdict. The reasoning step belongs
    # to the caller, and that boundary is what keeps results reproducible.
    print("DataManager returns evidence, not conclusions — reason over the above.")
    return 0


def _cmd_events(args, conn) -> int:
    limit = 1 if args.last else args.limit
    rows = events.query(conn, event_type=args.event_type, entity=args.entity,
                        since=args.since, until=args.until, limit=limit)
    if not rows:
        print("no matching events")
        print("\ntry: dm events            (everything)")
        print("     dm entities          (which names are known)")
        return 0

    for row in rows:
        when = row["occurred_on"] or "undated"
        if row["occurred_precision"] == "year" and row["occurred_on"]:
            when = row["occurred_on"][:4]
        print(f"{when}  {row['title'] or row['event_type']}")

        people = events.event_entities(conn, int(row["id"]))
        if people:
            joined = ", ".join(f"{p['canonical_name']} ({p['role']})" for p in people)
            print(f"    {joined}")
        # An event is only as good as the documents behind it (FR-5).
        for ev in events.evidence(conn, int(row["id"])):
            print(f"    evidence: {ev['title']}  [item {ev['item_id']}]")
        print()
    return 0


def _cmd_entities(args, conn) -> int:
    if args.merge:
        source_id, target_id = int(args.merge[0]), int(args.merge[1])
        entities.merge(conn, source_id, target_id)
        print(f"merged entity {source_id} into {target_id} (permanent)")
        return 0

    if args.name:
        found = entities.find(conn, args.name)
        if not found:
            print(f"no entity matching '{args.name}'")
            return 0
        for row in found:
            print(f"[{row['id']}] {row['canonical_name']}  ({row['entity_type']})")
            known = entities.aliases(conn, int(row["id"]))
            if len(known) > 1:
                print(f"    also known as: {', '.join(known)}")
            evs = events.query(conn, entity=args.name, limit=10)
            for ev in evs:
                print(f"    {ev['occurred_on'] or '—'}  {ev['title']}")
        return 0

    rows = entities.list_all(conn, args.entity_type)
    if not rows:
        print("no entities yet — run: dm index")
        return 0
    for row in rows:
        print(f"[{row['id']:>4}] {row['canonical_name']:<40} "
              f"{row['entity_type']:<14} events={row['event_count']}")
    return 0


def _cmd_vocab(args, conn) -> int:
    if args.merge:
        moved = vocabulary.merge(conn, args.merge[0], args.merge[1])
        print(f"merged '{args.merge[0]}' into '{args.merge[1]}' "
              f"({moved} field(s) rewritten)")
        return 0

    if args.suggest:
        rows = vocabulary.suggestions(conn)
        if not rows:
            print("no merge candidates")
            return 0
        print("keys whose canonical form differs (use: dm vocab --merge FROM TO)\n")
        for key, canonical, count in rows:
            print(f"  {key:<40} → {canonical:<30} seen {count}x")
        return 0

    rows = conn.execute(
        "SELECT key, canonical_key, occurrences, pinned_by_user "
        "FROM key_vocabulary ORDER BY occurrences DESC, key"
    ).fetchall()
    if not rows:
        print("vocabulary is empty — run: dm index")
        return 0
    for row in rows:
        pin = " (pinned)" if row["pinned_by_user"] else ""
        arrow = "" if row["key"] == row["canonical_key"] else f" → {row['canonical_key']}"
        print(f"  {row['key']}{arrow}  [{row['occurrences']}]{pin}")
    return 0


def _cmd_serve(args, cfg: Config, conn) -> int:
    try:
        import uvicorn
    except ImportError:
        print("fastapi and uvicorn are required: pip install -e '.[web]'",
              file=sys.stderr)
        return 1

    from .api import create_app

    # The serving process opens its own per-request connections.
    conn.close()

    print(f"DataManager → http://{args.host}:{args.port}")
    print(f"index: {cfg.db_path}")
    if args.host == "127.0.0.1":
        print("(bind --host 0.0.0.0 to reach it from other machines)")

    uvicorn.run(create_app(cfg), host=args.host, port=args.port,
                log_level="warning")
    return 0


def _cmd_enrich(args, cfg: Config, conn) -> int:
    if args.wait:
        print("waiting for the machine to go idle...")
        if not enrich.wait_until_idle(threshold=cfg.enrich_load_threshold):
            print("still busy after 5 minutes — nothing done")
            return 0

    if not args.now and enrich.system_busy(cfg.enrich_load_threshold):
        print("machine is busy — skipping (use --now to override, "
              "--wait to hold)")
        return 0

    enricher = enrich.Enricher(conn, cfg, respect_load=not args.now,
                               load_threshold=cfg.enrich_load_threshold)
    result = enricher.run(limit=args.limit, captions=args.captions)
    print(result.summary())
    if result.skipped_busy:
        print("stopped early: the machine got busy. Re-run to continue "
              "where it left off.")
    return 0


def _cmd_backup(args, conn) -> int:
    if args.restore:
        applied = backup.restore_backup(conn, args.path)
        print(f"restored from {args.path}:")
        for name, count in applied.items():
            if count:
                print(f"  {name}: {count}")
        if applied.get("unmatched"):
            print(f"\n{applied['unmatched']} correction(s) had no matching "
                  f"document — scan first, then restore again.")
        return 0

    counts = backup.write_backup(conn, args.path)
    total = sum(counts.values())
    print(f"wrote {args.path}")
    for name, count in counts.items():
        if count:
            print(f"  {name}: {count}")
    if total == 0:
        print("  (nothing authored by hand yet)")
    else:
        print("\nEverything else regenerates from the source folder. "
              "Keep this file safe.")
    return 0


def _cmd_status(conn) -> int:
    rows = conn.execute(
        "SELECT extraction_status, COUNT(*) AS n FROM items "
        "WHERE deleted_at IS NULL GROUP BY extraction_status"
    ).fetchall()
    total = sum(int(r["n"]) for r in rows)

    passages = conn.execute("SELECT COUNT(*) AS n FROM passages").fetchone()["n"]
    records = conn.execute("SELECT COUNT(*) AS n FROM records").fetchone()["n"]
    fields = conn.execute("SELECT COUNT(*) AS n FROM record_fields").fetchone()["n"]
    human = conn.execute(
        "SELECT COUNT(*) AS n FROM records WHERE source = 'human'"
    ).fetchone()["n"]
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
    print(f"records:  {records} ({fields} fields, {human} hand-corrected)")
    event_count = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    entity_count = conn.execute("SELECT COUNT(*) AS n FROM entities").fetchone()["n"]
    print(f"events:   {event_count} across {entity_count} entities")
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

    records = conn.execute(
        "SELECT r.id, r.record_type, r.source, r.confidence, r.page "
        "FROM records r WHERE r.item_id = ? ORDER BY r.id", (args.item_id,)
    ).fetchall()
    for record in records:
        page = f" p.{record['page']}" if record["page"] else ""
        print(f"\nrecord {record['id']}: {record['record_type']} "
              f"[{record['source']} {record['confidence']:.2f}]{page}")
        for f in conn.execute(
            "SELECT key, value_text, value_num, value_date, unit "
            "FROM record_fields WHERE record_id = ? ORDER BY key", (record["id"],)
        ).fetchall():
            typed = f["value_date"] or (
                f["value_num"] if f["value_num"] is not None else None)
            shown = f"{f['value_text']}"
            if typed is not None and str(typed) != f["value_text"]:
                shown += f"  → {typed}"
            if f["unit"]:
                shown += f" {f['unit']}"
            print(f"    {f['key']}: {shown}")

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

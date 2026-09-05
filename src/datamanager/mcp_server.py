"""MCP server (FR-12).

Typed tools an agent calls to reach the same deterministic query engine the CLI
and web UI use. No model runs in this process.

The contracts here are deliberately narrow and data-shaped: every tool returns
values, citations and evidence -- never prose, never a conclusion. That is what
keeps them stable across model generations. A weaker model only has to pick a
tool and read a number back; it is never asked to judge relevance, because the
engine already ranked deterministically.

Speaks MCP over stdio using the official SDK when installed, and falls back to
a minimal JSON-RPC loop otherwise, so the server has no hard dependency.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import corrections, entities, events, notes
from .config import Config
from .db import connect
from .index.indexer import Indexer
from .query.evidence import EvidenceQuery
from .query.fields import FieldQuery
from .query.search import SearchEngine

TOOLS: list[dict[str, Any]] = [
    {
        "name": "search",
        "description": (
            "Full-text and semantic search across every indexed document, "
            "photo and note. Returns ranked passages with citations. Use this "
            "when you need context rather than one specific value."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search terms"},
                "limit": {"type": "integer", "default": 10},
                "kind": {"type": "string",
                         "enum": ["document", "photo", "note"]},
                "semantic": {"type": "boolean", "default": True},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_value",
        "description": (
            "Look up a specific fact and get the value plus its citation. "
            "Use `where` to constrain by a sibling fact in the SAME document: "
            "get_value('gross_salary', where={'tax_year': 2023}) reads the "
            "salary from the form that also states 2023. Prefer this over "
            "search whenever the question asks for a specific value."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string",
                        "description": "Field name, e.g. gross_salary, expiry_date"},
                "where": {"type": "object",
                          "description": "Sibling constraints within one record"},
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["key"],
        },
    },
    {
        "name": "aggregate",
        "description": (
            "Sum, average, count, min or max a numeric field across documents. "
            "Returns the result and every contributing document so the number "
            "can be audited."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "op": {"type": "string",
                       "enum": ["sum", "avg", "count", "min", "max"],
                       "default": "sum"},
                "where": {"type": "object"},
            },
            "required": ["key"],
        },
    },
    {
        "name": "get_events",
        "description": (
            "Things that happened, with dates and supporting documents. Use "
            "for questions about when something occurred: 'when did I last "
            "fly Alaska', 'what did I buy at Costco last year'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "event_type": {"type": "string",
                               "description": "flight, purchase, payment, income"},
                "entity": {"type": "string",
                           "description": "Alaska Airlines, Costco, an employer"},
                "since": {"type": "string", "description": "ISO date lower bound"},
                "until": {"type": "string", "description": "ISO date upper bound"},
                "limit": {"type": "integer", "default": 20},
            },
        },
    },
    {
        "name": "gather_evidence",
        "description": (
            "Assemble everything relevant to a question that has no single "
            "stored answer -- 'which card is best at Costco'. Returns facts, "
            "events and passages with citations for YOU to reason over. The "
            "evidence set is identical every time; the reasoning is yours."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "entity": {"type": "string"},
                "keys": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer", "default": 8},
            },
            "required": ["question"],
        },
    },
    {
        "name": "list_keys",
        "description": (
            "The field vocabulary discovered from the documents. Call this "
            "first when unsure what a fact is called."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"prefix": {"type": "string"}},
        },
    },
    {
        "name": "list_values",
        "description": (
            "Distinct values for a field -- 'which tax years do I have?'"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
    {
        "name": "get_item",
        "description": "Full detail for one item: text, records, fields.",
        "inputSchema": {
            "type": "object",
            "properties": {"item_id": {"type": "integer"}},
            "required": ["item_id"],
        },
    },
    {
        "name": "find_entity",
        "description": (
            "Look up a merchant, employer or organization by any spelling, "
            "and see the variants folded into it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "add_note",
        "description": (
            "Store a note in DataManager. Use for something the user wants "
            "remembered that is not in any file."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["title", "text"],
        },
    },
    {
        "name": "correct_value",
        "description": (
            "Fix an extracted value by hand. The correction outranks every "
            "extractor and survives future re-indexing. Only call this when "
            "the user has told you the correct value."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "item_id": {"type": "integer"},
                "key": {"type": "string"},
                "value": {"type": "string"},
            },
            "required": ["item_id", "key", "value"],
        },
    },
]


class Handler:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        # As with the web service: load once here, never inside a tool call.
        from . import embed
        if embed.available():
            embed.preload(cfg.embed_model)
        embed.set_lazy_load(False)

    def _conn(self):
        return connect(self.cfg.db_path)

    def call(self, name: str, arguments: dict) -> dict:
        method = getattr(self, f"_tool_{name}", None)
        if method is None:
            return {"error": f"unknown tool: {name}"}
        conn = self._conn()
        try:
            return method(conn, arguments)
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
        finally:
            conn.close()

    # ------------------------------------------------------------- tools

    def _tool_search(self, conn, args) -> dict:
        response = SearchEngine(conn).search(
            args["query"], limit=args.get("limit", 10),
            kind=args.get("kind"), semantic=args.get("semantic", True))
        return {
            "total": response.total,
            "hits": [
                {"item_id": h.item_id, "title": h.title, "uri": h.uri,
                 "page": h.page, "snippet": h.snippet,
                 "score": round(h.score, 6)}
                for h in response.hits
            ],
        }

    def _tool_get_value(self, conn, args) -> dict:
        answer = FieldQuery(conn).get(
            args["key"], where=args.get("where") or {},
            limit=args.get("limit", 5))
        if not answer.values:
            return {"found": False, "key": args["key"],
                    "hint": "call list_keys to see available fields"}
        return {
            "found": True, "key": answer.key,
            "unambiguous": answer.is_unambiguous,
            "values": [
                {"value": v.value, "unit": v.unit, "citation": v.citation(),
                 "item_id": v.item_id, "source": v.source,
                 "confidence": v.confidence}
                for v in answer.values
            ],
        }

    def _tool_aggregate(self, conn, args) -> dict:
        result, contributing = FieldQuery(conn).aggregate(
            args["key"], args.get("op", "sum"), where=args.get("where") or {})
        return {
            "key": args["key"], "op": args.get("op", "sum"), "result": result,
            "contributing": [
                {"value": v.value, "unit": v.unit, "citation": v.citation(),
                 "item_id": v.item_id}
                for v in contributing
            ],
        }

    def _tool_get_events(self, conn, args) -> dict:
        rows = events.query(
            conn, event_type=args.get("event_type"), entity=args.get("entity"),
            since=args.get("since"), until=args.get("until"),
            limit=args.get("limit", 20))
        return {"events": [
            {
                "date": r["occurred_on"], "precision": r["occurred_precision"],
                "type": r["event_type"], "title": r["title"],
                "entities": [
                    {"name": e["canonical_name"], "role": e["role"]}
                    for e in events.event_entities(conn, int(r["id"]))],
                "evidence": [
                    {"item_id": int(e["item_id"]), "title": e["title"],
                     "uri": e["uri"]}
                    for e in events.evidence(conn, int(r["id"]))],
            }
            for r in rows
        ]}

    def _tool_gather_evidence(self, conn, args) -> dict:
        return EvidenceQuery(conn).gather(
            args["question"], entity=args.get("entity"),
            keys=args.get("keys") or [], limit=args.get("limit", 8)).as_dict()

    def _tool_list_keys(self, conn, args) -> dict:
        return {"keys": [{"key": k, "documents": n}
                         for k, n in FieldQuery(conn).list_keys(args.get("prefix"))]}

    def _tool_list_values(self, conn, args) -> dict:
        return {"key": args["key"],
                "values": [{"value": v, "count": n}
                           for v, n in FieldQuery(conn).list_values(args["key"])]}

    def _tool_get_item(self, conn, args) -> dict:
        from .api import _item_records, _item_text

        item = conn.execute("SELECT * FROM items WHERE id = ?",
                            (args["item_id"],)).fetchone()
        if item is None:
            return {"found": False}
        return {
            "found": True, "id": int(item["id"]), "title": item["title"],
            "uri": item["uri"], "kind": item["kind"],
            "status": item["extraction_status"],
            "records": _item_records(conn, int(item["id"])),
            "text": _item_text(conn, int(item["id"]))[:8000],
        }

    def _tool_find_entity(self, conn, args) -> dict:
        rows = entities.find(conn, args["name"])
        return {"entities": [
            {"id": int(r["id"]), "name": r["canonical_name"],
             "type": r["entity_type"],
             "aliases": entities.aliases(conn, int(r["id"]))}
            for r in rows
        ]}

    def _tool_add_note(self, conn, args) -> dict:
        item_id = notes.create_note(conn, args["title"], args["text"])
        Indexer(conn, self.cfg).run_pending()
        return {"ok": True, "item_id": item_id}

    def _tool_correct_value(self, conn, args) -> dict:
        corrections.correct_field(conn, int(args["item_id"]), args["key"],
                                  str(args["value"]))
        return {"ok": True, "item_id": args["item_id"], "key": args["key"],
                "value": args["value"], "source": "human"}


# --------------------------------------------------------------- transport

def serve_stdio(cfg: Config) -> int:
    """Run the MCP server over stdio.

    Uses the official SDK when present; otherwise a minimal JSON-RPC loop that
    implements the same three methods, so the server works with no extra
    dependency installed.
    """
    handler = Handler(cfg)
    try:
        return _serve_with_sdk(handler)
    except ImportError:
        return _serve_minimal(handler)


def _serve_with_sdk(handler: Handler) -> int:
    import asyncio

    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool

    server = Server("datamanager")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [Tool(name=t["name"], description=t["description"],
                     inputSchema=t["inputSchema"]) for t in TOOLS]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        result = handler.call(name, arguments or {})
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    async def run() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(run())
    return 0


def _serve_minimal(handler: Handler) -> int:
    """Line-delimited JSON-RPC over stdio -- the MCP wire format."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = request.get("method")
        request_id = request.get("id")

        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "datamanager", "version": "0.1.0"},
            }
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = request.get("params", {})
            payload = handler.call(params.get("name", ""),
                                   params.get("arguments") or {})
            result = {"content": [{"type": "text",
                                   "text": json.dumps(payload, indent=2)}]}
        elif method in ("notifications/initialized", "initialized"):
            continue
        else:
            result = {"error": f"unknown method: {method}"}

        if request_id is not None:
            sys.stdout.write(json.dumps(
                {"jsonrpc": "2.0", "id": request_id, "result": result}) + "\n")
            sys.stdout.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    cfg = Config.load()
    if argv and argv[0] not in ("--help", "-h"):
        cfg = replace(cfg, db_path=Path(argv[0]))
    return serve_stdio(cfg)


if __name__ == "__main__":
    raise SystemExit(main())

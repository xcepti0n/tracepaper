"""Server-rendered web UI (FR-12).

Deliberately plain HTML with a little inline JavaScript -- no build step, no
framework, nothing to keep current. The UI is a window onto the same query
engine the CLI and MCP server use; it holds no logic of its own.

What it must make visible, because these are the properties the design rests
on: where every value came from, which layer produced it, what is still pending
extraction, and an easy way to correct a mistake.
"""

from __future__ import annotations

import html
import sqlite3

from . import entities as entity_module
from . import events as event_module
from . import vocabulary
from .query.fields import FieldQuery
from .query.search import SearchEngine

STYLE = """
:root {
  --bg: #fbfbfa; --fg: #1a1a18; --muted: #6b6b66; --line: #e2e2dd;
  --accent: #2d5f4f; --accent-soft: #eef4f1; --warn: #8a5a2b;
  --card: #ffffff;
}
@media (prefers-color-scheme: dark) {
  :root { --bg:#16171a; --fg:#e8e8e4; --muted:#9a9a94; --line:#2c2e33;
          --accent:#7fb3a0; --accent-soft:#1e2624; --warn:#d4a06a;
          --card:#1c1d21; }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
header { border-bottom:1px solid var(--line); background:var(--card);
         position:sticky; top:0; z-index:10; }
.wrap { max-width:1040px; margin:0 auto; padding:0 20px; }
h1 { font-size:17px; margin:0; padding:14px 0 0; letter-spacing:-.01em; }
h1 small { color:var(--muted); font-weight:400; font-size:13px; margin-left:8px; }
nav { display:flex; gap:2px; padding-top:10px; }
nav a { padding:7px 13px; text-decoration:none; color:var(--muted);
        border-radius:6px 6px 0 0; font-size:14px; }
nav a.on { color:var(--fg); background:var(--accent-soft); font-weight:600; }
main { padding:22px 0 60px; }
form.search { display:flex; gap:8px; margin-bottom:6px; }
input[type=text] { flex:1; padding:10px 13px; border:1px solid var(--line);
       border-radius:7px; background:var(--card); color:var(--fg); font-size:15px; }
button { padding:10px 17px; border:0; border-radius:7px; background:var(--accent);
         color:#fff; font-size:14px; cursor:pointer; font-weight:500; }
button.ghost { background:transparent; color:var(--accent);
               border:1px solid var(--line); }
.hint { color:var(--muted); font-size:13px; margin:0 0 20px; }
.hit { background:var(--card); border:1px solid var(--line); border-radius:9px;
       padding:14px 16px; margin-bottom:10px; }
.hit h3 { margin:0 0 3px; font-size:15px; }
.hit .path { color:var(--muted); font-size:12px; font-family:ui-monospace,monospace;
             word-break:break-all; margin-bottom:7px; }
.hit .snip { font-size:14px; }
.sig { color:var(--muted); font-size:11.5px; font-family:ui-monospace,monospace;
       margin-top:8px; }
.answer { background:var(--accent-soft); border:1px solid var(--accent);
          border-radius:9px; padding:16px 18px; margin-bottom:14px; }
.answer .val { font-size:26px; font-weight:600; letter-spacing:-.02em; }
.answer .cite { color:var(--muted); font-size:13px; margin-top:6px; }
table { width:100%; border-collapse:collapse; background:var(--card);
        border:1px solid var(--line); border-radius:9px; overflow:hidden; }
th,td { text-align:left; padding:9px 13px; border-bottom:1px solid var(--line);
        font-size:14px; }
th { background:var(--accent-soft); font-weight:600; font-size:13px; }
tr:last-child td { border-bottom:0; }
.pill { display:inline-block; padding:1px 7px; border-radius:20px; font-size:11px;
        background:var(--accent-soft); color:var(--accent); font-weight:600; }
.pill.human { background:#2d5f4f; color:#fff; }
.pill.warn { background:var(--warn); color:#fff; }
.stat { display:inline-block; margin-right:26px; margin-bottom:12px; }
.stat b { display:block; font-size:22px; font-weight:600; }
.stat span { color:var(--muted); font-size:12.5px; }
.empty { color:var(--muted); padding:36px 0; text-align:center; }
code { background:var(--accent-soft); padding:1px 5px; border-radius:4px;
       font-size:13px; }
.row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
label.chk { color:var(--muted); font-size:13px; display:flex; gap:5px;
            align-items:center; }
"""


def render_page(conn: sqlite3.Connection, *, query: str = "", tab: str = "search",
                limit: int = 20, semantic: bool = True) -> str:
    tabs = [("search", "Search"), ("facts", "Facts"), ("events", "Events"),
            ("entities", "Entities"), ("status", "Status")]
    nav = "".join(
        f'<a href="/?tab={name}" class="{"on" if name == tab else ""}">{label}</a>'
        for name, label in tabs
    )

    body = {
        "search": lambda: _search_tab(conn, query, limit, semantic),
        "facts": lambda: _facts_tab(conn, query),
        "events": lambda: _events_tab(conn, query),
        "entities": lambda: _entities_tab(conn, query),
        "status": lambda: _status_tab(conn),
    }.get(tab, lambda: _search_tab(conn, query, limit, semantic))()

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DataManager</title><style>{STYLE}</style></head>
<body>
<header><div class="wrap">
  <h1>DataManager <small>deterministic search — no model in the query path</small></h1>
  <nav>{nav}</nav>
</div></header>
<main><div class="wrap">{body}</div></main>
</body></html>"""


def _esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def _search_form(query: str, tab: str, placeholder: str,
                 semantic: bool = True) -> str:
    checkbox = ""
    if tab == "search":
        checked = "checked" if semantic else ""
        checkbox = (f'<label class="chk"><input type="checkbox" name="semantic" '
                    f'value="true" {checked}> semantic</label>')
    return f"""<form class="search" method="get">
  <input type="hidden" name="tab" value="{tab}">
  <input type="text" name="q" value="{_esc(query)}" placeholder="{placeholder}"
         autofocus autocomplete="off">
  {checkbox}
  <button type="submit">Search</button>
</form>"""


def _search_tab(conn: sqlite3.Connection, query: str, limit: int,
                semantic: bool) -> str:
    out = [_search_form(query, "search",
                        "sprinkler valve, Costco, passport…", semantic)]

    if not query:
        out.append('<p class="hint">Full text across every document, plus '
                   'semantic matching for wording you do not remember exactly.</p>')
        return "".join(out)

    response = SearchEngine(conn).search(query, limit=limit, semantic=semantic)
    if not response.hits:
        out.append('<div class="empty">No results.</div>')
        return "".join(out)

    out.append(f'<p class="hint">{response.total} match(es), '
               f'showing {len(response.hits)}</p>')
    for hit in response.hits:
        page = f' <span class="pill">p.{hit.page}</span>' if hit.page else ""
        signals = " ".join(f"{k}={v:+.4f}" for k, v in sorted(hit.signals.items()))
        out.append(f"""<div class="hit">
  <h3><a href="/api/items/{hit.item_id}">{_esc(hit.title)}</a>{page}</h3>
  <div class="path">{_esc(hit.uri or f"note:{hit.item_id}")}</div>
  <div class="snip">{_esc(hit.snippet)}</div>
  <div class="sig">score={hit.score:.4f} · {_esc(signals)}</div>
</div>""")
    return "".join(out)


def _facts_tab(conn: sqlite3.Connection, query: str) -> str:
    """Tier 1: a value with its citation, or the vocabulary to pick from."""
    out = [_search_form(query, "facts", "expiry_date, gross_salary, amount…")]
    fq = FieldQuery(conn)

    if query:
        answer = fq.get(query.strip(), limit=10)
        if answer.values:
            best = answer.best
            unit = f" {best.unit}" if best.unit else ""
            source_class = "human" if best.source == "human" else ""
            out.append(f"""<div class="answer">
  <div class="val">{_esc(best.value)}{_esc(unit)}</div>
  <div class="cite">{_esc(best.citation())}
    &middot; <span class="pill {source_class}">{_esc(best.source)}</span>
    confidence {best.confidence:.2f}</div>
</div>""")
            if len(answer.values) > 1 and not answer.is_unambiguous:
                # Disagreement is shown, never collapsed into one answer.
                rows = "".join(
                    f"<tr><td>{_esc(v.value)}{_esc(' ' + v.unit if v.unit else '')}</td>"
                    f"<td>{_esc(v.item_title)}</td>"
                    f'<td><span class="pill">{_esc(v.source)}</span></td></tr>'
                    for v in answer.values[1:]
                )
                out.append('<p class="hint">Other candidates — these disagree, '
                           'so none is silently chosen:</p>')
                out.append(f"<table><tr><th>Value</th><th>Document</th>"
                           f"<th>Layer</th></tr>{rows}</table>")
            return "".join(out)
        out.append(f'<div class="empty">No value for '
                   f'<code>{_esc(query)}</code>.</div>')

    keys = fq.list_keys(limit=100)
    if not keys:
        out.append('<div class="empty">No fields extracted yet. '
                   'Run <code>dm scan --index</code>.</div>')
        return "".join(out)

    rows = "".join(
        f'<tr><td><a href="/?tab=facts&q={_esc(key)}"><code>{_esc(key)}</code></a></td>'
        f"<td>{count}</td></tr>" for key, count in keys
    )
    out.append('<p class="hint">Vocabulary discovered from your documents — '
               'nothing here was declared in advance.</p>')
    out.append(f"<table><tr><th>Field</th><th>Documents</th></tr>{rows}</table>")
    return "".join(out)


def _events_tab(conn: sqlite3.Connection, query: str) -> str:
    out = [_search_form(query, "events", "Alaska Airlines, Costco…")]
    rows = event_module.query(conn, entity=query or None, limit=100)

    if not rows:
        out.append('<div class="empty">No events.</div>')
        return "".join(out)

    out.append('<p class="hint">Things that happened, with every document '
               'that evidences them.</p>')
    for row in rows:
        when = row["occurred_on"] or "undated"
        if row["occurred_precision"] == "year" and row["occurred_on"]:
            when = row["occurred_on"][:4]
        people = ", ".join(
            f'{e["canonical_name"]} ({e["role"]})'
            for e in event_module.event_entities(conn, int(row["id"])))
        evidence = " ".join(
            f'<a href="/api/items/{e["item_id"]}">{_esc(e["title"])}</a>'
            for e in event_module.evidence(conn, int(row["id"])))
        out.append(f"""<div class="hit">
  <h3>{_esc(row["title"] or row["event_type"])}</h3>
  <div class="path">{_esc(when)}{" · " + _esc(people) if people else ""}</div>
  <div class="snip">evidence: {evidence}</div>
</div>""")
    return "".join(out)


def _entities_tab(conn: sqlite3.Connection, query: str) -> str:
    out = [_search_form(query, "entities", "Costco, ACME…")]

    if query:
        found = entity_module.find(conn, query)
        if not found:
            out.append('<div class="empty">No matching entity.</div>')
            return "".join(out)
        for row in found:
            aliases = entity_module.aliases(conn, int(row["id"]))
            out.append(f"""<div class="hit">
  <h3>{_esc(row["canonical_name"])}</h3>
  <div class="path">also known as: {_esc(", ".join(aliases))}</div>
</div>""")
        return "".join(out)

    rows = entity_module.list_all(conn)
    if not rows:
        out.append('<div class="empty">No entities yet.</div>')
        return "".join(out)
    body = "".join(
        f'<tr><td><a href="/?tab=entities&q={_esc(r["canonical_name"])}">'
        f'{_esc(r["canonical_name"])}</a></td>'
        f'<td>{_esc(r["entity_type"])}</td><td>{r["event_count"]}</td></tr>'
        for r in rows
    )
    out.append('<p class="hint">Merchants, employers and organizations, with '
               'their spelling variants folded together.</p>')
    out.append(f"<table><tr><th>Name</th><th>Type</th><th>Events</th></tr>"
               f"{body}</table>")
    return "".join(out)


def _status_tab(conn: sqlite3.Connection) -> str:
    from .api import _status

    info = _status(conn)
    stats = [
        ("Items", info["items"]), ("Passages", info["passages"]),
        ("Records", info["records"]), ("Fields", info["fields"]),
        ("Events", info["events"]), ("Entities", info["entities"]),
        ("Corrections", info["corrections"]),
    ]
    tiles = "".join(f'<div class="stat"><b>{value}</b><span>{label}</span></div>'
                    for label, value in stats)

    out = [tiles]

    # Pending enrichment is stated plainly rather than left to be discovered.
    if info["pending"]:
        out.append(f'<p class="hint"><span class="pill warn">pending</span> '
                   f'{info["pending"]} item(s) awaiting full extraction — '
                   f'already searchable by whatever text was indexed.</p>')

    embeddings = info["embeddings"]
    embedded = sum(embeddings["by_model"].values())
    if embedded < embeddings["passages"]:
        out.append(f'<p class="hint">{embedded} of {embeddings["passages"]} '
                   f'passages embedded — run <code>dm embed</code> for '
                   f'semantic search.</p>')

    scan = info["last_scan"]
    if scan:
        out.append(f"""<table><tr><th>Last scan</th><th></th></tr>
  <tr><td>root</td><td><code>{_esc(scan["root"])}</code></td></tr>
  <tr><td>status</td><td>{_esc(scan["status"])}</td></tr>
  <tr><td>finished</td><td>{_esc(scan["finished_at"] or "—")}</td></tr>
  <tr><td>seen / added / changed / moved / removed</td>
      <td>{scan["seen"]} / {scan["added"]} / {scan["changed"]}
          / {scan["moved"]} / {scan["removed"]}</td></tr>
</table>""")

    suggestions = vocabulary.suggestions(conn, limit=10)
    if suggestions:
        rows = "".join(
            f"<tr><td><code>{_esc(k)}</code></td><td><code>{_esc(c)}</code></td>"
            f"<td>{n}</td></tr>" for k, c, n in suggestions)
        out.append('<p class="hint">Key variants folded to a canonical name:</p>')
        out.append(f"<table><tr><th>Seen as</th><th>Stored as</th>"
                   f"<th>Count</th></tr>{rows}</table>")

    return "".join(out)

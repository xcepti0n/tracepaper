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
            align-items:center; white-space:nowrap; }
h2 { font-size:13px; text-transform:uppercase; letter-spacing:.07em;
     color:var(--muted); margin:26px 0 10px; font-weight:600; }
h2 .count { background:var(--accent-soft); color:var(--accent); padding:1px 7px;
            border-radius:20px; font-size:11px; margin-left:6px; }
.answer .lbl { color:var(--muted); font-size:12px; text-transform:uppercase;
               letter-spacing:.07em; margin-bottom:4px; }
button.fix { background:transparent; color:var(--muted); border:1px solid var(--line);
             padding:1px 9px; font-size:11px; border-radius:5px; margin-left:8px; }
button.fix:hover { color:var(--accent); border-color:var(--accent); }
.toast { position:fixed; bottom:22px; left:50%; transform:translateX(-50%);
         background:var(--accent); color:#fff; padding:11px 20px; border-radius:8px;
         font-size:14px; box-shadow:0 4px 16px rgba(0,0,0,.2); z-index:50; }
"""

SCRIPT = """
// Inline correction. The value you set outranks every extractor and survives
// reindexing, so this is the one place the UI writes to the index.
async function fixValue(itemId, key, current) {
  const value = prompt(`Correct value for "${key}":`, current);
  if (value === null || value === String(current)) return;
  const response = await fetch('/api/correct', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({item_id: itemId, key: key, value: value}),
  });
  if (response.ok) {
    toast('Saved — this now outranks every extractor');
    setTimeout(() => location.reload(), 900);
  } else {
    toast('Could not save: ' + response.status);
  }
}

function toast(message) {
  const el = document.createElement('div');
  el.className = 'toast';
  el.textContent = message;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 2600);
}
"""


def render_page(conn: sqlite3.Connection, *, query: str = "", tab: str = "search",
                limit: int = 20, semantic: bool = True) -> str:
    """One search box over every layer, plus a browse view for exploring.

    The user should not have to know whether a word is an entity, a field or
    passage text before typing it -- so there is one box, and the answer types
    are grouped in the result rather than split across separate searches.
    """
    tabs = [("search", "Search"), ("browse", "Browse"), ("status", "Status")]
    nav = "".join(
        f'<a href="/?tab={name}" class="{"on" if name == tab else ""}">{label}</a>'
        for name, label in tabs
    )

    if tab == "status":
        body = _status_tab(conn)
    elif tab == "browse":
        body = _browse_tab(conn, query)
    else:
        body = _unified_tab(conn, query, limit, semantic)

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
<script>{SCRIPT}</script>
</body></html>"""


def _esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def _search_form(query: str, tab: str, placeholder: str,
                 semantic: bool = True) -> str:
    checked = "checked" if semantic else ""
    return f"""<form class="search" method="get">
  <input type="hidden" name="tab" value="{tab}">
  <input type="text" name="q" value="{_esc(query)}" placeholder="{placeholder}"
         autofocus autocomplete="off">
  <label class="chk"><input type="checkbox" name="semantic" value="true"
         {checked}> semantic</label>
  <button type="submit">Search</button>
</form>"""


def _unified_tab(conn: sqlite3.Connection, query: str, limit: int,
                 semantic: bool) -> str:
    """One query, every layer, grouped by what kind of answer it is."""
    from .query.unified import UnifiedSearch

    out = [_search_form(query, "search",
                        "passport expiry · salary 2023 · Alaska · sprinkler valve",
                        semantic)]

    if not query:
        from . import embed
        if semantic and not embed.is_loaded():
            out.append('<p class="hint">Ask for a value, a merchant, a date, or '
                       'just words you remember. <b>Keyword only</b> — no '
                       'embedding model loaded; run <code>dm embed</code> and '
                       'restart.</p>')
        else:
            out.append('<p class="hint">Ask for a value ("passport expiry"), '
                       'something that happened ("Alaska"), or words you half '
                       'remember ("sprinkler valve"). One box searches '
                       'everything.</p>')
        return "".join(out)

    result = UnifiedSearch(conn).query(query, limit=limit, semantic=semantic)

    if result.is_empty:
        out.append('<div class="empty">Nothing found.<br>'
                   '<span class="hint">Try fewer words, or Browse to see what '
                   'was extracted.</span></div>')
        return "".join(out)

    # 1. A direct answer, when the query named a field we hold.
    if result.answer:
        best = result.answer
        unit = f" {best.unit}" if best.unit else ""
        human = "human" if best.source == "human" else ""
        out.append(f"""<div class="answer">
  <div class="lbl">{_esc(result.answer_key)}</div>
  <div class="val">{_esc(best.value)}{_esc(unit)}</div>
  <div class="cite">
    <a href="/api/items/{best.item_id}">{_esc(best.item_title)}</a>
    {f"&middot; p.{best.page}" if best.page else ""}
    &middot; <span class="pill {human}">{_esc(best.source)}</span>
    {_fix_button(best.item_id, result.answer_key, best.value)}
  </div>
</div>""")
        if result.alternatives:
            rows = "".join(
                f"<tr><td>{_esc(v.value)}{_esc(' ' + v.unit if v.unit else '')}</td>"
                f'<td><a href="/api/items/{v.item_id}">{_esc(v.item_title)}</a></td>'
                f'<td><span class="pill">{_esc(v.source)}</span></td></tr>'
                for v in result.alternatives)
            out.append('<p class="hint">Documents disagree — none is chosen '
                       'for you:</p>')
            out.append(f"<table><tr><th>Value</th><th>Document</th>"
                       f"<th>Layer</th></tr>{rows}</table>")

    # 2. Things that happened.
    if result.events:
        out.append(f'<h2>Events <span class="count">{len(result.events)}</span></h2>')
        for event in result.events:
            when = event["date"] or "undated"
            if event["precision"] == "year" and event["date"]:
                when = event["date"][:4]
            who = ", ".join(f'{e["name"]} ({e["role"]})' for e in event["entities"])
            evidence = " ".join(
                f'<a href="/api/items/{e["item_id"]}">{_esc(e["title"])}</a>'
                for e in event["evidence"])
            out.append(f"""<div class="hit">
  <h3>{_esc(event["title"])}</h3>
  <div class="path">{_esc(when)}{" · " + _esc(who) if who else ""}</div>
  <div class="snip">evidence: {evidence}</div>
</div>""")

    # 3. Who or what the query named.
    if result.entities:
        out.append('<h2>Entities</h2>')
        for entity in result.entities:
            others = [a for a in entity.get("aliases", [])
                      if a != entity["canonical_name"]]
            also = (f'<div class="path">also seen as: {_esc(", ".join(others))}</div>'
                    if others else "")
            out.append(f"""<div class="hit">
  <h3>{_esc(entity["canonical_name"])}</h3>{also}
</div>""")

    # 4. Photos matching the tags named in the query.
    if result.photos:
        filters = ", ".join(result.photo_filters)
        out.append(f'<h2>Photos <span class="count">{len(result.photos)}</span>'
                   f'</h2><p class="hint">matching {_esc(filters)}</p>')
        for photo in result.photos:
            tags = " ".join(f'<span class="pill">{_esc(t)}</span>'
                            for t in photo["tags"][:6])
            out.append(f"""<div class="hit">
  <h3><a href="/api/items/{photo["item_id"]}">{_esc(photo["title"])}</a></h3>
  <div class="path">{_esc(photo["uri"] or "")}</div>
  <div class="snip">{tags}</div>
</div>""")

    # 5. Matching documents -- the floor that always has something to say.
    if result.hits:
        out.append(f'<h2>Documents <span class="count">{result.total_hits}</span></h2>')
        for hit in result.hits:
            page = f' <span class="pill">p.{hit.page}</span>' if hit.page else ""
            signals = " ".join(f"{k}={v:+.4f}"
                               for k, v in sorted(hit.signals.items())
                               if not k.startswith("_"))
            out.append(f"""<div class="hit">
  <h3><a href="/api/items/{hit.item_id}">{_esc(hit.title)}</a>{page}</h3>
  <div class="path">{_esc(hit.uri or f"note:{hit.item_id}")}</div>
  <div class="snip">{_esc(hit.snippet)}</div>
  <div class="sig">score={hit.score:.4f} · {_esc(signals)}</div>
</div>""")

    return "".join(out)


def _fix_button(item_id: int, key: str, current) -> str:
    """Inline correction. A value you fix outranks every extractor, forever."""
    args = f"{item_id}, {_js(key)}, {_js(current)}"
    return f'<button class="fix" onclick="fixValue({args})">fix</button>'


def _js(value) -> str:
    """A JavaScript string literal, safely quoted."""
    import json
    return html.escape(json.dumps(str(value)), quote=True)


def _browse_tab(conn: sqlite3.Connection, query: str) -> str:
    """Everything extracted, for exploring rather than searching."""
    fq = FieldQuery(conn)
    out = [_search_form(query, "browse", "filter fields…")]

    if query:
        values = fq.list_values(query)
        if values:
            rows = "".join(
                f'<tr><td><a href="/?q={_esc(query)}+{_esc(v)}">{_esc(v)}</a></td>'
                f"<td>{n}</td></tr>" for v, n in values)
            out.append(f'<h2>Values of <code>{_esc(query)}</code></h2>')
            out.append(f"<table><tr><th>Value</th><th>Documents</th></tr>"
                       f"{rows}</table>")
            return "".join(out)

    keys = fq.list_keys(query or None, limit=200)
    if keys:
        rows = "".join(
            f'<tr><td><a href="/?tab=browse&q={_esc(k)}"><code>{_esc(k)}</code>'
            f"</a></td><td>{n}</td></tr>" for k, n in keys)
        out.append('<h2>Fields</h2>')
        out.append('<p class="hint">Discovered from your documents — nothing '
                   'here was declared in advance.</p>')
        out.append(f"<table><tr><th>Field</th><th>Documents</th></tr>"
                   f"{rows}</table>")

    rows = entity_module.list_all(conn, limit=100)
    if rows:
        body = "".join(
            f'<tr><td><a href="/?q={_esc(r["canonical_name"])}">'
            f'{_esc(r["canonical_name"])}</a></td>'
            f'<td>{_esc(r["entity_type"])}</td><td>{r["event_count"]}</td></tr>'
            for r in rows)
        out.append('<h2>Entities</h2>')
        out.append(f"<table><tr><th>Name</th><th>Type</th><th>Events</th></tr>"
                   f"{body}</table>")

    if len(out) == 1:
        out.append('<div class="empty">Nothing indexed yet. Run '
                   '<code>dm scan --index</code>.</div>')
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

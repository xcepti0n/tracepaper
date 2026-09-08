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
.field { margin:8px 0 16px; }
.field input[type=text] { width:100%; }
.field.root { display:flex; gap:8px; flex-wrap:wrap; }
.field.root input { flex:1; min-width:260px; }
.field.root .status, .field .status { flex-basis:100%; }
.field.job { display:flex; gap:12px; align-items:center;
             justify-content:space-between; flex-wrap:wrap; }
.field.job .actions { margin:0; }
.status { font-size:12.5px; margin-top:5px; min-height:17px; }
.status .ok { color:var(--accent); font-weight:600; }
.status .bad { color:#b3261e; font-weight:600; }
.status .muted { color:var(--muted); }
.bad-line { color:#b3261e; margin-top:4px; line-height:1.45; }
.warn-line { color:var(--warn); margin-top:4px; line-height:1.45; }
.actions { display:flex; gap:12px; align-items:center; margin-top:26px;
           padding-top:18px; border-top:1px solid var(--line); }
.problems { background:#fdeceb; color:#8c1d18; border-radius:8px;
            padding:12px 15px; margin:14px 0; font-size:13.5px; }
@media (prefers-color-scheme: dark) { .problems { background:#3a1f1d;
            color:#f2b8b5; } }
.problems div { margin:3px 0; }
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

// Settings: every path is checked against the filesystem before it is saved,
// so a typo cannot leave the service pointing at nothing.
function addRoot() {
  const row = document.createElement('div');
  row.className = 'field root';
  row.innerHTML = `<input type="text" placeholder="/mnt/nas/documents"
      onchange="checkPath(this,'source',null)">
    <button type="button" class="ghost"
      onclick="this.parentElement.remove()">remove</button>
    <div class="status"></div>`;
  document.getElementById('roots').appendChild(row);
}

async function checkUpdates(options) {
  const quiet = options && options.quiet;
  const box = document.getElementById('update_status');
  const detail = document.getElementById('update_detail');
  if (!box) return;
  box.innerHTML = quiet ? '' : '<span class="muted">checking…</span>';
  if (!quiet) detail.innerHTML = '';

  let status;
  try {
    const response = await fetch('/api/updates');
    if (!response.ok) throw new Error('HTTP ' + response.status);
    status = await response.json();
  } catch (error) {
    // On a background check, a network failure is not worth shouting about --
    // the page is still perfectly usable and the user did not ask.
    if (!quiet) {
      box.innerHTML = '<span class="bad">could not check: ' +
                      escapeHtml(String(error.message)) + '</span>';
    }
    return;
  }

  // A reason WITH an update waiting is explanatory, not an error: the commit
  // subjects are not readable without writing to the checkout. A reason with
  // nothing waiting is a genuine failure to check.
  if (status.reason && !status.behind) {
    if (!quiet) box.innerHTML = '<span class="bad">' +
                                escapeHtml(status.reason) + '</span>';
    return;
  }
  if (!status.behind) {
    box.innerHTML = '<span class="ok">✓ up to date</span>';
    return;
  }

  const plural = status.behind === 1 ? '' : 's';
  box.innerHTML = '<span class="warn-line">' + status.behind +
                  ' update' + plural + ' available</span>';

  const list = status.commits.map(c =>
    '<div class="commit"><code>' + escapeHtml(c.short) + '</code> ' +
    escapeHtml(c.subject) + '</div>').join('');

  // Only offer the button when the server said it can actually use it: one
  // that appears and then fails on a permission error is worse than none.
  const button = status.can_apply
    ? '<button type="button" onclick="applyUpdate(this)">Update now</button>'
    : '<p class="hint">Run <code>systemctl start tracepaper-update</code> ' +
      'in the container to apply these.</p>';

  const heading = status.commits.length
    ? '<p class="hint">Changes since ' +
      escapeHtml(status.current ? status.current.short : 'the running version') +
      ':</p>'
    : '<p class="hint">' + escapeHtml(status.reason || '') + '</p>';

  detail.innerHTML = heading + list + '<div class="actions">' + button + '</div>';
}

async function applyUpdate(button) {
  button.disabled = true;
  button.textContent = 'Updating…';
  const box = document.getElementById('update_status');

  try {
    const response = await fetch('/api/updates/apply', {
      method: 'POST',
      // Not authentication -- a cross-site form cannot set a custom header
      // without a CORS preflight, which closes the drive-by case.
      headers: {'Content-Type': 'application/json', 'X-Tracepaper-Request': '1'},
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || ('HTTP ' + response.status));
    box.innerHTML = '<span class="ok">' + escapeHtml(body.message) + '</span>';
    // The service restarts as part of the update, so this page goes away for a
    // moment. Poll until it answers again rather than leaving a dead page up.
    setTimeout(waitForRestart, 5000);
  } catch (error) {
    button.disabled = false;
    button.textContent = 'Update now';
    box.innerHTML = '<span class="bad">' + escapeHtml(String(error.message)) +
                    '</span>';
  }
}

async function waitForRestart() {
  const box = document.getElementById('update_status');
  for (let attempt = 0; attempt < 60; attempt++) {
    try {
      const response = await fetch('/api/health', {cache: 'no-store'});
      if (response.ok) { location.reload(); return; }
    } catch (error) { /* still restarting */ }
    await new Promise(resolve => setTimeout(resolve, 2000));
  }
  box.innerHTML = '<span class="bad">still restarting — check ' +
                  '<code>journalctl -u tracepaper-update -f</code></span>';
}

// Poll while any job runs so a scan's progress is visible without a terminal.
// Slow enough (5s) that an idle Settings tab costs nothing.
let jobsTimer = null;

async function refreshJobs(options) {
  const quiet = options && options.quiet;
  const box = document.getElementById('jobs_list');
  if (!box) return;

  let status;
  try {
    const response = await fetch('/api/jobs', {cache: 'no-store'});
    if (!response.ok) throw new Error('HTTP ' + response.status);
    status = await response.json();
  } catch (error) {
    if (!quiet) {
      box.innerHTML = '<span class="bad">could not read jobs: ' +
                      escapeHtml(String(error.message)) + '</span>';
    }
    return;
  }

  if (!status.available) {
    box.innerHTML = '<p class="hint">' + escapeHtml(status.detail) + '</p>';
    return;
  }

  box.innerHTML = status.jobs.map(job => {
    let state;
    if (job.running) {
      state = '<span class="warn-line">running…</span>';
    } else if (job.result && job.result !== 'success') {
      state = '<span class="bad">last run: ' + escapeHtml(job.result) + '</span>';
    } else if (job.last_run) {
      state = '<span class="muted">last run ' + escapeHtml(job.last_run) + '</span>';
    } else {
      state = '<span class="muted">not run yet</span>';
    }

    // Only offer the button when the server said polkit will allow it.
    // The name rides in a data attribute rather than an inline onclick: this
    // string passes through a Python literal before it reaches the browser,
    // and the nested quotes an onclick needs did not survive that -- the whole
    // script block failed to parse, taking every other handler with it.
    const button = job.running
      ? '<button type="button" disabled>Running…</button>'
      : (job.can_start
          ? '<button type="button" class="run-job" data-job="' +
            escapeHtml(job.name) + '">Run now</button>'
          : '<span class="hint">systemctl start ' + escapeHtml(job.unit) + '</span>');

    return '<div class="field job"><div><strong>' + escapeHtml(job.label) +
           '</strong><div class="status">' + state + '</div></div>' +
           '<div class="actions">' + button + '</div></div>';
  }).join('');

  box.querySelectorAll('button.run-job').forEach(function (button) {
    button.addEventListener('click', function () {
      startJob(button, button.dataset.job);
    });
  });

  // Keep polling only while something is running.
  const anyRunning = status.jobs.some(job => job.running);
  if (jobsTimer) { clearTimeout(jobsTimer); jobsTimer = null; }
  if (anyRunning) {
    jobsTimer = setTimeout(() => refreshJobs({quiet: true}), 5000);
  }
}

async function startJob(button, name) {
  button.disabled = true;
  button.textContent = 'Starting…';
  try {
    const response = await fetch('/api/jobs/' + encodeURIComponent(name) + '/start', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Tracepaper-Request': '1'},
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || ('HTTP ' + response.status));
  } catch (error) {
    button.disabled = false;
    button.textContent = 'Run now';
    const box = document.getElementById('jobs_list');
    if (box) {
      box.insertAdjacentHTML('afterbegin', '<div class="status"><span class="bad">' +
        escapeHtml(String(error.message)) + '</span></div>');
    }
    return;
  }
  // systemd takes a moment to report the unit as active.
  setTimeout(() => refreshJobs({quiet: true}), 1000);
}

async function checkPath(input, kind, statusId) {
  const box = statusId ? document.getElementById(statusId)
                       : input.parentElement.querySelector('.status');
  if (!input.value.trim()) { box.innerHTML = ''; return; }
  box.innerHTML = '<span class="muted">checking…</span>';

  const response = await fetch('/api/storage/check', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({path: input.value.trim(), kind: kind}),
  });
  if (!response.ok) { box.innerHTML = '<span class="bad">check failed</span>'; return; }

  const check = await response.json();
  if (check.ok) {
    const bits = [check.filesystem,
                  check.file_count !== null ? check.file_count + ' files' : null,
                  check.free_human ? check.free_human + ' free' : null]
                 .filter(Boolean).join(' · ');
    const warnings = (check.warnings || [])
      .map(w => `<div class="warn-line">${escapeHtml(w)}</div>`).join('');
    box.innerHTML = `<span class="ok">✓ ready</span>
      <span class="muted">${escapeHtml(bits)}</span>${warnings}`;
  } else {
    const problems = (check.problems || [])
      .map(p => `<div class="bad-line">${escapeHtml(p)}</div>`).join('');
    box.innerHTML = `<span class="bad">✗ not usable</span>${problems}`;
  }
}

async function saveSettings(event) {
  event.preventDefault();
  const roots = [...document.querySelectorAll('#roots input')]
    .map(i => i.value.trim()).filter(Boolean);

  const response = await fetch('/api/settings', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      roots: roots,
      db_path: document.getElementById('db_path').value.trim(),
      backup_dir: document.getElementById('backup_dir').value.trim() || null,
    }),
  });
  const result = await response.json();

  document.querySelectorAll('.problems').forEach(el => el.remove());
  if (result.ok) {
    toast(result.restart_required
      ? 'Saved — restart the service to use the new index'
      : 'Saved');
    setTimeout(() => location.reload(), 1200);
  } else {
    const box = document.createElement('div');
    box.className = 'problems';
    box.innerHTML = '<b>Not saved:</b>' + (result.problems || [])
      .map(p => `<div>${escapeHtml(p)}</div>`).join('');
    document.getElementById('settings').prepend(box);
  }
}

function escapeHtml(text) {
  const el = document.createElement('div');
  el.textContent = text;
  return el.innerHTML;
}

function toast(message) {
  const el = document.createElement('div');
  el.className = 'toast';
  el.textContent = message;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 2600);
}


// Panels render before this block, so they queue their startup call rather
// than invoking a function that does not exist yet. Draining here runs them in
// order, once every definition above is parsed.
(window.__tpOnReady || []).forEach(function (fn) {
  try {
    fn();
  } catch (error) {
    // One panel failing to start must not stop the others.
    console.error('startup task failed', error);
  }
});
window.__tpOnReady = {push: function (fn) { fn(); }};
"""


def render_page(conn: sqlite3.Connection, *, query: str = "", tab: str = "search",
                limit: int = 20, semantic: bool = True) -> str:
    """One search box over every layer, plus a browse view for exploring.

    The user should not have to know whether a word is an entity, a field or
    passage text before typing it -- so there is one box, and the answer types
    are grouped in the result rather than split across separate searches.
    """
    tabs = [("search", "Search"), ("browse", "Browse"), ("status", "Status"),
            ("settings", "Settings")]
    nav = "".join(
        f'<a href="/?tab={name}" class="{"on" if name == tab else ""}">{label}</a>'
        for name, label in tabs
    )

    if tab == "settings":
        body = _settings_tab(conn)
    elif tab == "status":
        body = _status_tab(conn)
    elif tab == "browse":
        body = _browse_tab(conn, query)
    else:
        body = _unified_tab(conn, query, limit, semantic)

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tracepaper</title><style>{STYLE}</style></head>
<body>
<header><div class="wrap">
  <h1>Tracepaper <small>deterministic search — no model in the query path</small></h1>
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
                       'embedding model loaded; run <code>tracepaper embed</code> and '
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
                   '<code>tracepaper scan --index</code>.</div>')
    return "".join(out)


def _settings_tab(conn: sqlite3.Connection) -> str:
    """Storage configuration, with every path validated before saving."""
    from . import settings as settings_module
    from . import storage
    from .api import get_config

    cfg = get_config()
    config_file = settings_module.find_config()
    roots = [str(r) for r in cfg.roots]
    checks = storage.check_all(
        roots, str(cfg.db_path),
        str(cfg.backup_dir) if cfg.backup_dir else None)
    found_mounts = storage.mounts()

    out = ['<h2>Storage</h2>']
    out.append('<p class="hint">Mount your NFS shares with the OS '
               '(<code>/etc/fstab</code> or a systemd mount unit) — that '
               'survives reboots and keeps credentials out of this app. '
               'Point Tracepaper at the mounted paths here.</p>')

    if found_mounts:
        rows = "".join(
            f'<tr><td><code>{_esc(m["path"])}</code></td>'
            f'<td>{_esc(m["source"])}</td><td>{_esc(m["type"])}</td>'
            f'<td>{"read-only" if m["read_only"] else "read-write"}</td></tr>'
            for m in found_mounts)
        out.append('<h2>Network shares detected</h2>')
        out.append(f'<table><tr><th>Mounted at</th><th>Source</th>'
                   f'<th>Type</th><th>Access</th></tr>{rows}</table>')

    out.append(f"""
<form id="settings" onsubmit="saveSettings(event)">
  <h2>Documents to index <span class="pill">read-only</span></h2>
  <p class="hint">Your Synology NFS read share. Never written to.</p>
  <div id="roots">{_root_rows(roots, checks["sources"])}</div>
  <button type="button" class="ghost" onclick="addRoot()">+ add folder</button>

  <h2>Index location <span class="pill warn">local disk only</span></h2>
  <p class="hint">SQLite corrupts over NFS and SMB — their file locking is
    unreliable across clients, and it fails silently, weeks later. Keep this on
    local disk; it rebuilds from your documents anyway.</p>
  <div class="field">
    <input type="text" id="db_path" value="{_esc(cfg.db_path)}"
           onchange="checkPath(this,'index','db_status')">
    <div id="db_status" class="status">{_check_badge(checks["index"])}</div>
  </div>

  <h2>Backups <span class="pill">the NAS belongs here</span></h2>
  <p class="hint">Your Synology NFS write share. Holds the corrections, notes
    and merges that cannot be regenerated — the copy that survives losing the
    index machine.</p>
  <div class="field">
    <input type="text" id="backup_dir"
           value="{_esc(cfg.backup_dir or "")}"
           placeholder="/mnt/nas/backups/tracepaper"
           onchange="checkPath(this,'backup','backup_status')">
    <div id="backup_status" class="status">
      {_check_badge(checks["backup"]) if checks["backup"] else ""}</div>
  </div>

  <div class="actions">
    <button type="submit">Save</button>
    <span class="hint">Saved to
      <code>{_esc(config_file or "tracepaper.toml")}</code></span>
  </div>
</form>""")

    out.append(_jobs_panel())
    out.append(_updates_panel())
    return "".join(out)


def _jobs_panel() -> str:
    """Buttons for the background units, so routine work needs no terminal.

    The list is filled in from the browser rather than server-side: reading
    systemd state costs several `systemctl` calls, and a page that blocks on
    those is worse than one that fills in a moment later. It also lets the
    panel keep polling while a scan runs.
    """
    return """<h2>Jobs</h2>
<p class="hint">These run on timers already &mdash; the buttons run them now.
A first scan can take hours; it is safe to leave this page.</p>
<div id="jobs_list"><span class="muted">loading&hellip;</span></div>
<script>(window.__tpOnReady = window.__tpOnReady || []).push(function () { refreshJobs({quiet: true}); });</script>"""


def _updates_panel() -> str:
    """Update status and, where permitted, a button to apply one.

    Rendered server-side from the local git state only -- no network call on
    page load. Checking the remote is a `git fetch`, which is slow and can hang
    on a bad connection, so it happens when the user asks for it.
    """
    from . import updates

    status = updates.check_local()

    if not status.supported:
        return f'''<h2>Updates</h2>
<p class="hint">{_esc(status.reason)}</p>
<p class="hint">To update a copied install, re-copy the source and re-run the
install steps, or reinstall from the git remote so future updates work in
place.</p>'''

    current = ""
    if status.current:
        current = (f'<p>Running <code>{_esc(status.current.sha[:7])}</code> '
                   f'{_esc(status.current.subject)} '
                   f'on <code>{_esc(status.branch)}</code></p>')

    if status.can_apply:
        action = ('<button type="button" onclick="checkUpdates()">Check again</button>'
                  '<span id="update_status" class="status"></span>')
    else:
        # A button that appears and then fails is worse than one that never
        # appears, so say what to run instead.
        action = ('<p class="hint">This server cannot apply updates itself '
                  '— <code>tracepaper-update.service</code> is not installed, '
                  'or polkit does not permit this user to start it. Run '
                  '<code>systemctl start tracepaper-update</code> in the '
                  'container.</p>'
                  '<button type="button" onclick="checkUpdates()">Check again</button>'
                  '<span id="update_status" class="status"></span>')

    # Check on load rather than waiting to be asked. The point of the panel is
    # to TELL you an update is waiting; one that only answers when clicked is
    # one you have to remember to click.
    #
    # The check runs from the browser after the page renders, not server-side:
    # it is a `git fetch`, which is slow on a good connection and hangs on a
    # bad one, and a page that blocks on the network is worse than one that
    # fills in a moment later.
    return f'''<h2>Updates</h2>
{current}
<div class="actions">{action}</div>
<div id="update_detail"></div>
<script>(window.__tpOnReady = window.__tpOnReady || []).push(function () {{ checkUpdates({{quiet: true}}); }});</script>'''


def _root_rows(roots: list[str], checks: list[dict]) -> str:
    if not roots:
        return _root_row("", None)
    return "".join(_root_row(root, check)
                   for root, check in zip(roots, checks + [None] * len(roots)))


def _root_row(value: str, check: dict | None) -> str:
    badge = _check_badge(check) if check else ""
    return f"""<div class="field root">
  <input type="text" value="{_esc(value)}" placeholder="/mnt/nas/documents"
         onchange="checkPath(this,'source',null)">
  <button type="button" class="ghost" onclick="this.parentElement.remove()">
    remove</button>
  <div class="status">{badge}</div>
</div>"""


def _check_badge(check: dict | None) -> str:
    """A one-line verdict for a path, with what to do when it is wrong."""
    if not check:
        return ""
    if check["ok"]:
        bits = []
        if check.get("filesystem"):
            bits.append(check["filesystem"])
        if check.get("file_count") is not None:
            bits.append(f'{check["file_count"]} files')
        if check.get("free_human"):
            bits.append(f'{check["free_human"]} free')
        detail = " · ".join(bits)
        warnings = "".join(f'<div class="warn-line">{_esc(w)}</div>'
                           for w in check.get("warnings", []))
        return (f'<span class="ok">✓ ready</span> '
                f'<span class="muted">{_esc(detail)}</span>{warnings}')

    problems = "".join(f'<div class="bad-line">{_esc(p)}</div>'
                       for p in check.get("problems", []))
    return f'<span class="bad">✗ not usable</span>{problems}'


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
        # Telling someone to run `tracepaper embed` on an install without the
        # semantic extras sends them to a command that exits 1 -- the model is
        # what is missing, not the run. Say which of the two it actually is.
        from . import embed as _embed

        if _embed.available():
            action = ('run <code>tracepaper embed</code>, or start '
                      '<em>Enrich</em> under Jobs')
        else:
            action = ('install semantic search first — this build has no '
                      'embedding model, so <code>tracepaper embed</code> would '
                      'exit with an error')
        out.append(f'<p class="hint">{embedded} of {embeddings["passages"]} '
                   f'passages embedded — {action}.</p>')

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

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
from pathlib import Path
from urllib.parse import quote, urlencode
import sqlite3

from . import entities as entity_module
from . import events as event_module
from . import vocabulary
from .query.fields import FieldQuery
from .query.search import SearchEngine

STYLE = """
:root {
  /* One accent hue with real steps, so emphasis has somewhere to go. The old
     palette had a single --accent doing every job, which is why links, active
     tabs and buttons all read as the same flat green. */
  --bg: #f7f8fa; --fg: #14161a; --muted: #5d6470; --line: #e4e7ec;
  --card: #ffffff; --card-2: #fbfcfd;
  --accent: #0f766e; --accent-hover: #0d5f59; --accent-ink: #ffffff;
  --accent-soft: #e6f4f1; --accent-line: #a7d7cd;
  --link: #0b6bcb; --link-hover: #094f97;
  --ok: #15803d; --ok-soft: #e8f6ed;
  --warn: #b45309; --warn-soft: #fdf3e7;
  --bad: #be123c; --bad-soft: #fdebef;
  --shadow: 0 1px 2px rgba(16,24,40,.06), 0 1px 3px rgba(16,24,40,.10);
  --shadow-lg: 0 4px 12px rgba(16,24,40,.10), 0 2px 4px rgba(16,24,40,.06);
  --radius: 10px;
}
@media (prefers-color-scheme: dark) {
  :root { --bg:#0f1115; --fg:#e7e9ee; --muted:#9aa2b1; --line:#262a33;
          --card:#161922; --card-2:#1b1f29;
          --accent:#2dd4bf; --accent-hover:#5eead4; --accent-ink:#06211e;
          --accent-soft:#122b28; --accent-line:#1f4d47;
          --link:#7cc0ff; --link-hover:#a5d5ff;
          --ok:#4ade80; --ok-soft:#10241a;
          --warn:#fbbf24; --warn-soft:#2a1f0d;
          --bad:#fb7185; --bad-soft:#2c1119;
          --shadow: 0 1px 2px rgba(0,0,0,.4), 0 1px 3px rgba(0,0,0,.3);
          --shadow-lg: 0 4px 14px rgba(0,0,0,.5), 0 2px 6px rgba(0,0,0,.35); }
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
nav a { transition:background .12s, color .12s; font-weight:500; }
nav a:hover { background:var(--accent-soft); color:var(--accent); }
nav a.on { color:var(--accent); background:var(--accent-soft); font-weight:650;
  box-shadow:inset 0 -2px 0 var(--accent); }
main { padding:22px 0 60px; }
form.search { margin:0 0 6px; }
input[type=text] { flex:1; padding:10px 13px; border:1px solid var(--line);
  border-radius:8px; background:var(--card); color:var(--fg); font-size:15px;
  transition:border-color .12s, box-shadow .12s; }
input[type=text]:focus { border-color:var(--accent); outline:none;
  box-shadow:0 0 0 3px var(--accent-soft); }
input[type=text]::placeholder { color:var(--muted); opacity:.65; }
/* The search box is the one control on this page that matters, so it is
   sized like it: a tall pill, the way every search engine draws one. */
.search .row.main { gap:0; border:1px solid var(--line); border-radius:26px;
  background:var(--card); padding:4px 4px 4px 20px; transition:box-shadow .12s;
  box-shadow:0 1px 3px rgba(0,0,0,.05); }
.search .row.main:focus-within { box-shadow:0 1px 10px rgba(0,0,0,.12);
  border-color:var(--muted); }
.search .row.main input[type=text] { border:0; background:transparent;
  padding:13px 6px; font-size:17px; box-shadow:none; outline:none; }
.search .row.main button { border-radius:22px; padding:11px 24px;
  font-size:15px; }
button { padding:10px 17px; border:1px solid transparent;
  border-radius:8px; background:var(--accent); color:var(--accent-ink);
  font-size:14px; cursor:pointer; font-weight:600; letter-spacing:.005em;
  transition:background .12s, border-color .12s, transform .06s, box-shadow .12s;
  box-shadow:var(--shadow); }
button:hover { background:var(--accent-hover); }
button:active { transform:translateY(1px); box-shadow:none; }
/* Focus was invisible before, so the whole UI was unusable from the keyboard. */
button:focus-visible, a:focus-visible, input:focus-visible,
select:focus-visible { outline:2px solid var(--link); outline-offset:2px; }
button.ghost { background:var(--card); color:var(--fg);
  border-color:var(--line); box-shadow:none; font-weight:500; }
button.ghost:hover { background:var(--accent-soft); color:var(--accent);
  border-color:var(--accent-line); }
button.primary { background:var(--accent); color:var(--accent-ink); }
button[disabled] { opacity:.5; cursor:not-allowed; }
button[disabled]:hover { background:var(--accent); }
/* The folder list needs its own save, next to the control it belongs to: the
   only Save used to sit two sections below, past Index location and Backups,
   so from the folder list there was no visible way to commit a change. */
.row-actions { display:flex; gap:10px; align-items:center; flex-wrap:wrap;
  margin-top:4px; }
.hint.subtle { font-size:12.5px; margin:8px 0 22px; }
a { color:var(--link); text-decoration-color:color-mix(in srgb, var(--link) 35%, transparent);
    text-underline-offset:2px; }
a:hover { color:var(--link-hover); text-decoration-color:currentColor; }
a:visited { color:var(--link); }
.hint { color:var(--muted); font-size:13.5px; margin:0 0 20px; max-width:68ch; }
.hint code { background:var(--accent-soft); color:var(--accent);
  padding:1px 5px; border-radius:4px; font-size:12.5px; }
.hit { background:var(--card); border:1px solid var(--line);
  border-radius:var(--radius); padding:15px 17px; margin-bottom:10px;
  box-shadow:var(--shadow); transition:box-shadow .12s, border-color .12s; }
.hit:hover { box-shadow:var(--shadow-lg); border-color:var(--accent-line); }
.hit h3 { margin:0 0 3px; font-size:15px; }
.hit .path a { color:var(--muted); text-decoration:none; }
.hit .path a:hover { color:var(--accent); text-decoration:underline; }
.hit .path { color:var(--muted); font-size:12px; font-family:ui-monospace,monospace;
             word-break:break-all; margin-bottom:7px; }
.hit .snip { font-size:14px; }
/* Sub-navigation inside a tab. Looks like the main nav but lighter, so the
   two levels stay distinguishable. */
.crumbs { margin:0 0 16px; font-size:13.5px; color:var(--muted); }
.crumbs a { color:var(--accent); text-decoration:none; }
.crumbs a:hover { text-decoration:underline; }
code.dim { color:var(--muted); font-size:11.5px; margin-left:6px; }
.subnav { display:flex; gap:4px; flex-wrap:wrap; margin:0 0 4px;
  border-bottom:1px solid var(--line); padding-bottom:10px; }
.subnav a { padding:6px 12px; text-decoration:none; color:var(--muted);
  border-radius:6px; font-size:13.5px; }
.subnav a:hover { color:var(--fg); background:var(--accent-soft); }
.subnav a.on { color:var(--fg); background:var(--accent-soft); font-weight:600; }
.tips { margin:8px 0 0; padding-left:18px; line-height:1.75; }
.tips li { font-size:13.5px; color:var(--muted); }
.tips li b { color:var(--fg); font-weight:600; }
.search .row { display:flex; gap:8px; align-items:center; }
.search .row.opts { margin-top:8px; gap:14px; }
.search select { padding:9px 10px; border:1px solid var(--line); border-radius:7px;
  background:var(--card); color:var(--fg); font-size:14px; }
.mode-help { color:var(--muted); font-size:12.5px; }
.rule-add { display:flex; gap:8px; margin-top:10px; }
.rule-add input[type=text] { flex:1; }
button.small { padding:4px 10px; font-size:12px; }
/* A ranked result that is an image: picture on the left, details beside it. */
.hit.with-thumb { display:flex; gap:14px; align-items:flex-start; }
.hit .hit-body { flex:1; min-width:0; }
.hit-thumb { flex:none; display:block; }
.hit-thumb img { width:96px; height:96px; object-fit:cover; border-radius:6px;
  border:1px solid var(--line); background:var(--bg); display:block; }
.vote { white-space:nowrap; }
.thumb { background:transparent; border:1px solid var(--line); color:var(--muted);
  border-radius:5px; padding:2px 8px; font-size:11px; cursor:pointer;
  margin-left:4px; font-weight:400; }
.thumb:hover { color:var(--fg); border-color:var(--muted); }
.thumb.done { background:var(--accent-soft); color:var(--fg);
  border-color:var(--accent); }
.pager { display:flex; align-items:center; gap:14px; margin:18px 0 4px; }
.pager .range { color:var(--muted); font-size:12.5px; }
.pager a.page { padding:7px 13px; border:1px solid var(--line); border-radius:7px;
  text-decoration:none; font-size:13px; background:var(--card); }
.pager a.page:hover { border-color:var(--muted); }
.hit .more { margin:6px 0 2px; }
.hit .more summary { cursor:pointer; color:var(--muted); font-size:12px;
  user-select:none; }
.hit .more summary:hover { color:var(--fg); }
.hit .more ul { margin:6px 0 0; padding:0 0 0 14px; list-style:none;
  border-left:2px solid var(--line); }
.hit .more li { margin:0 0 5px; font-size:13px; }
.hit .more li a { font-family:ui-monospace,monospace; font-size:11.5px;
  margin-right:6px; white-space:nowrap; }
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
th { background:var(--card-2); font-weight:600; font-size:12.5px;
  color:var(--muted); text-transform:uppercase; letter-spacing:.05em; }
tbody tr:hover td { background:var(--accent-soft); }
tr:last-child td { border-bottom:0; }
/* File browser. One table for folders and files together, the way every file
   manager does it, because splitting them into two tables loses the sense of
   being in a single place. */
.fb { width:100%; border-collapse:collapse; background:var(--card);
      border:1px solid var(--line); border-radius:9px; overflow:hidden; }
.fb th { background:var(--accent-soft); font-weight:600; font-size:12px;
         text-transform:uppercase; letter-spacing:.03em; color:var(--muted);
         text-align:left; padding:8px 13px; border-bottom:1px solid var(--line); }
.fb td { padding:0; border-bottom:1px solid var(--line); font-size:14px;
         vertical-align:middle; }
.fb tr:last-child td { border-bottom:0; }
.fb tbody tr:hover { background:var(--accent-soft); }
/* The whole name cell is the click target, not just the text. */
.fb .nm a { display:flex; align-items:center; gap:9px; padding:9px 13px;
            text-decoration:none; color:var(--fg); min-width:0; }
.fb .nm a:hover .t { text-decoration:underline; color:var(--accent); }
.fb .t { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.fb .ic { flex:none; width:19px; text-align:center; font-size:15px;
          line-height:1; }
.fb .meta { padding:9px 13px; color:var(--muted); font-size:13px;
            white-space:nowrap; }
.fb .num { text-align:right; font-variant-numeric:tabular-nums; }
.fb .act { padding:6px 13px; text-align:right; white-space:nowrap; }
.fb .dir .t { font-weight:600; }
.fb .tags { padding:9px 4px; white-space:nowrap; }
/* Folders first, and a faint tint so the boundary reads without a header row. */
.fb .dir { background:linear-gradient(90deg,var(--accent-soft) 0 3px,transparent 3px); }
.fb-bar { display:flex; align-items:center; justify-content:space-between;
          gap:12px; margin:0 0 10px; flex-wrap:wrap; }
.fb-bar .sum { color:var(--muted); font-size:13px; }
@media (max-width:640px) {
  .fb .hide-sm { display:none; }
}
.share-build { display:grid; gap:10px; margin:12px 0;
  grid-template-columns:repeat(auto-fit,minmax(210px,1fr)); }
.share-build label { display:flex; flex-direction:column; gap:4px;
  font-size:13px; color:var(--muted); }
.share-out { background:var(--accent-soft); border:1px solid var(--line);
  border-radius:7px; padding:12px 14px; margin:12px 0 6px; font-size:13px;
  overflow-x:auto; white-space:pre; }
.pill { display:inline-block; padding:1px 7px; border-radius:20px; font-size:11px;
        background:var(--accent-soft); color:var(--accent); font-weight:600; }
.pill { border:1px solid var(--accent-line); }
.pill.human { background:var(--accent); color:var(--accent-ink);
  border-color:transparent; }
.pill.warn { background:var(--warn-soft); color:var(--warn);
  border-color:color-mix(in srgb, var(--warn) 35%, transparent); }
.pill.ok { background:var(--ok-soft); color:var(--ok);
  border-color:color-mix(in srgb, var(--ok) 35%, transparent); }
.stat { display:inline-block; margin-right:26px; margin-bottom:12px; }
.stat b { display:block; font-size:22px; font-weight:600; }
.stat span { color:var(--muted); font-size:12.5px; }
.empty { color:var(--muted); padding:36px 0; text-align:center; }
code { background:var(--accent-soft); padding:1px 5px; border-radius:4px;
       font-size:13px; }
.row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
label.chk { color:var(--muted); font-size:13px; display:flex; gap:5px;
            align-items:center; white-space:nowrap; }
h2 { font-size:15px; letter-spacing:-.01em; color:var(--fg);
  margin:30px 0 10px; font-weight:650; display:flex; align-items:center;
  gap:9px; flex-wrap:wrap; }
h2::before { content:""; width:3px; height:15px; border-radius:2px;
  background:var(--accent); flex:none; }
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
.excludes { line-height:2; }
.photos { display:grid; gap:12px; margin:12px 0;
          grid-template-columns:repeat(auto-fill, minmax(160px, 1fr)); }
.photo { margin:0; border:1px solid var(--line); border-radius:var(--radius);
  overflow:hidden; background:var(--card); box-shadow:var(--shadow);
  transition:box-shadow .12s, transform .12s; }
.photo:hover { box-shadow:var(--shadow-lg); transform:translateY(-2px); }
.photo img { display:block; width:100%; height:150px; object-fit:cover;
  background:var(--card-2); }
.photo figcaption { padding:6px 8px; font-size:12px; word-break:break-word; }
.photo .tags { margin-top:4px; }
.excludes code { margin-right:4px; }
.status { font-size:12.5px; margin-top:5px; min-height:17px; }
.status .ok { color:var(--ok); font-weight:600; }
.status .bad { color:var(--bad); font-weight:600; }
.status .muted { color:var(--muted); }
.bad-line { color:var(--bad); margin-top:4px; line-height:1.45; }
.warn-line { color:var(--warn); margin-top:4px; line-height:1.45; }
.actions { display:flex; gap:12px; align-items:center; margin-top:26px;
           padding-top:18px; border-top:1px solid var(--line); }
.problems { background:var(--bad-soft); color:var(--bad);
  border:1px solid color-mix(in srgb, var(--bad) 30%, transparent);
  border-left:3px solid var(--bad); border-radius:8px;
  padding:12px 15px; margin:14px 0; font-size:13.5px; }
.problems div { margin:3px 0; }
.toast { position:fixed; bottom:22px; left:50%; transform:translateX(-50%);
         background:var(--accent); color:var(--accent-ink); padding:11px 20px;
         box-shadow:var(--shadow-lg); border-radius:8px;
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
    toast('Saved. This now outranks every extractor');
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
  box.innerHTML = '<span class="bad">still restarting. Check ' +
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
      // systemd's raw Result string ("signal", "oom-kill", "exit-code") is
      // not English. Say what happened, and keep the raw word for the journal.
      const reasons = {
        'signal': 'stopped before it finished',
        'oom-kill': 'ran out of memory',
        'exit-code': 'exited with an error',
        'timeout': 'timed out',
        'core-dump': 'crashed',
      };
      const why = reasons[job.result] || ('failed: ' + job.result);
      state = '<span class="bad">last run ' + escapeHtml(why) + '</span>';
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
  // Drop blank rows before sending. "+ add folder" appends an empty input, and
  // one left empty used to fail validation for the WHOLE form, so the folders
  // that were already correct were not saved either. An empty row means "I
  // changed my mind", not "save nothing".
  const inputs = [...document.querySelectorAll('#roots input')];
  inputs.filter(i => !i.value.trim())
        .forEach(i => { if (i.parentElement) i.parentElement.remove(); });
  const roots = inputs.map(i => i.value.trim()).filter(Boolean);

  let result;
  try {
    const response = await fetch('/api/settings', {
      method: 'POST',
      headers: {'Content-Type': 'application/json',
                'X-Tracepaper-Request': '1'},
      body: JSON.stringify({
        roots: roots,
        db_path: document.getElementById('db_path').value.trim(),
        backup_dir: document.getElementById('backup_dir').value.trim() || null,
      }),
    });
    // A restart mid-request gives 502 from the proxy with an HTML body, so
    // json() throws and the old code showed nothing at all: the click looked
    // like it did nothing. Say what happened instead.
    if (!response.ok) {
      showSettingsProblems(['The server returned ' + response.status + '. ' +
        (response.status >= 500
          ? 'It may be restarting after an update. Wait a moment and try again.'
          : 'Nothing was saved.')]);
      return;
    }
    result = await response.json();
  } catch (error) {
    showSettingsProblems(['Could not reach the server. Nothing was saved. ' +
                          '(' + error + ')']);
    return;
  }

  document.querySelectorAll('.problems').forEach(el => el.remove());
  if (result.ok) {
    toast(result.restart_required
      ? 'Saved. Restart the service to use the new index'
      : 'Saved');
    setTimeout(() => location.reload(), 1200);
  } else {
    showSettingsProblems(result.problems || ['Not saved.']);
  }
}

// Problems are shown at the top of the form AND scrolled to. The form is
// taller than a screen, so a message prepended to it was off-screen for
// anyone whose Save button sat below the fold: the save looked silent.
function showSettingsProblems(problems) {
  document.querySelectorAll('.problems').forEach(el => el.remove());
  const box = document.createElement('div');
  box.className = 'problems';
  box.innerHTML = '<b>Not saved:</b>' +
    problems.map(p => `<div>${escapeHtml(p)}</div>`).join('');
  const form = document.getElementById('settings');
  form.prepend(box);
  box.scrollIntoView({behavior: 'smooth', block: 'center'});
}

async function testLlm() {
  const box = document.getElementById('llm_status');
  const endpoint = document.getElementById('llm_endpoint').value.trim();
  box.textContent = 'Checking ' + endpoint + '...';
  let result;
  try {
    const response = await fetch('/api/llm/test', {
      method: 'POST',
      headers: {'Content-Type': 'application/json',
                'X-Tracepaper-Request': '1'},
      body: JSON.stringify({endpoint: endpoint}),
    });
    result = await response.json();
  } catch (err) {
    box.textContent = 'Could not run the test.';
    return;
  }
  if (!result.ok) {
    box.textContent = result.error || 'No answer.';
    return;
  }
  const names = (result.models || []).map(m => m.name);
  box.textContent = 'Reached it. ' + names.length + ' model(s) installed.';
  const hint = document.getElementById('vlm_hint');
  if (names.length) {
    // Listed as text rather than a dropdown: Ollama does not reliably flag
    // which models read images, so the choice cannot be narrowed for you.
    hint.textContent = 'Installed: ' + names.join(', ');
  }
}

async function saveCaptions(event) {
  event.preventDefault();
  const response = await fetch('/api/settings', {
    method: 'POST',
    headers: {'Content-Type': 'application/json',
              'X-Tracepaper-Request': '1'},
    body: JSON.stringify({
      llm_enabled: document.getElementById('llm_enabled').checked,
      llm_endpoint: document.getElementById('llm_endpoint').value.trim(),
      vlm_model: document.getElementById('vlm_model').value.trim(),
    }),
  });
  const result = await response.json();
  if (result.ok) {
    toast('Saved');
    setTimeout(() => location.reload(), 1200);
  } else {
    toast((result.problems || ['Not saved']).join(' '));
  }
}

function buildShareCommand() {
  const host = document.getElementById('sh_host').value.trim();
  const share = document.getElementById('sh_share').value.trim();
  const name = document.getElementById('sh_name').value.trim().toLowerCase();
  const ctid = document.getElementById('sh_ctid').value.trim() || '103';
  const out = document.getElementById('sh_out');
  const note = document.getElementById('sh_note');

  const missing = [];
  if (!host) missing.push('the NAS address');
  if (!share) missing.push('the folder on the NAS');
  if (!name) missing.push('a short name');
  if (missing.length) {
    out.hidden = false;
    note.hidden = true;
    out.textContent = 'Still need ' + missing.join(', ') + '.';
    return;
  }
  // The name becomes a path, so refuse anything that could escape one rather
  // than printing a command that would.
  if (!/^[a-z0-9][a-z0-9_-]*$/.test(name)) {
    out.hidden = false;
    note.hidden = true;
    out.textContent =
      'The short name can use lowercase letters, digits, dash and underscore.';
    return;
  }

  const folder = share.startsWith('/') ? share : '/' + share;
  out.hidden = false;
  note.hidden = false;
  // Fetched from the repo rather than run from a path: the Proxmox host has no
  // checkout, and /opt/tracepaper lives inside THIS container, not there.
  // Arguments go in as environment variables because the script arrives on a
  // pipe, where positional arguments cannot be passed.
  // The commit this build runs, not `main`: a branch path is CDN-cached for
  // minutes, so a just-pushed fix comes back stale.
  const rev = document.querySelector('[data-revision]')
    ? document.querySelector('[data-revision]').dataset.revision : 'main';
  out.textContent =
    'CTID=' + ctid + ' NAS_HOST=' + host +
    " SHARE='" + folder + "' NAME=" + name + ' \\\n' +
    '  bash -c "$(curl -fsSL ' +
    'https://raw.githubusercontent.com/xcepti0n/tracepaper/' + rev + '/' +
    'deploy/add-share.sh)"';
  document.getElementById('sh_path').textContent = '/mnt/nas/' + name;
  document.getElementById('sh_after').hidden = false;
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
// ---- Folder rules and index cleanup (Settings) ----

function postJson(url, body, method) {
  return fetch(url, {
    method: method || 'POST',
    headers: {'Content-Type': 'application/json', 'X-Tracepaper-Request': '1'},
    body: body ? JSON.stringify(body) : undefined,
  }).then(function (response) {
    return response.json().then(function (data) {
      if (!response.ok) { throw new Error(data.detail || ('HTTP ' + response.status)); }
      return data;
    });
  });
}

function addRule() {
  const prefix = document.getElementById('rule_prefix');
  const kind = document.getElementById('rule_kind');
  const status = document.getElementById('rule_status');
  if (!prefix || !prefix.value.trim()) {
    status.textContent = 'Enter a folder path first.';
    return;
  }
  status.textContent = 'Saving…';
  postJson('/api/rules', {prefix: prefix.value.trim(), rule: kind.value})
    .then(function (data) {
      // A rule matching nothing is nearly always a mistyped path, so say so
      // now rather than leaving it to be discovered by its absence.
      if (!data.items) {
        status.textContent = 'Saved, but no indexed file is under that path. '
          + 'check the spelling.';
        return;
      }
      status.textContent = 'Saved. ' + data.items.toLocaleString()
        + ' file(s) affected.';
      setTimeout(function () { location.reload(); }, 900);
    })
    .catch(function (error) { status.textContent = error.message; });
}

function removeRule(prefix) {
  const status = document.getElementById('rule_status');
  status.textContent = 'Removing…';
  postJson('/api/rules?prefix=' + encodeURIComponent(prefix), null, 'DELETE')
    .then(function () { location.reload(); })
    .catch(function (error) { status.textContent = error.message; });
}

function refreshPrune(options) {
  const status = document.getElementById('prune_status');
  const apply = document.getElementById('prune_apply');
  if (!status) { return; }
  if (!(options && options.quiet)) { status.textContent = 'Checking…'; }

  fetch('/api/prune').then(function (r) { return r.json(); })
    .then(function (data) {
      const orphans = data.orphans || 0;
      if (!data.items && !orphans) {
        status.textContent = 'Nothing to clean up.';
        if (apply) { apply.hidden = true; }
        return;
      }
      const reasons = (data.reasons || []).slice(0, 6).map(function (entry) {
        return escapeHtml(entry.reason) + ' (' + entry.items.toLocaleString() + ')';
      }).join(' · ');
      const head = data.items
        ? '<b>' + data.items.toLocaleString() + '</b> of '
          + data.total.toLocaleString() + ' indexed files would be removed.'
        : '<b>' + orphans.toLocaleString() + '</b> stale scan record(s) would '
          + 'be cleared. No indexed file is affected.';
      status.innerHTML = head + '<br><span class="hint">' + reasons + '</span>';
      if (apply) {
        apply.hidden = false;
        apply.textContent = data.items
          ? 'Remove ' + data.items.toLocaleString() + ' files'
          : 'Clear ' + orphans.toLocaleString() + ' stale records';
      }
    })
    .catch(function (error) { status.textContent = error.message; });
}

function applyPrune() {
  const status = document.getElementById('prune_status');
  const apply = document.getElementById('prune_apply');
  apply.disabled = true;
  status.textContent = 'Removing…';
  postJson('/api/prune')
    .then(function (data) {
      status.textContent = 'Removed ' + data.removed.toLocaleString()
        + ' files from the index. Your files are untouched.';
      apply.hidden = true;
    })
    .catch(function (error) { status.textContent = error.message; })
    .finally(function () { apply.disabled = false; });
}

function markFolderAsCode(path) {
  const status = document.getElementById('browse_status');
  if (status) { status.textContent = 'Saving…'; }
  postJson('/api/rules', {prefix: path, rule: 'code'})
    .then(function () { location.reload(); })
    .catch(function (error) {
      if (status) { status.textContent = error.message; }
    });
}

document.addEventListener('click', function (event) {
  if (event.target.dataset && event.target.dataset.markCode) {
    markFolderAsCode(event.target.dataset.markCode);
    return;
  }
  if (event.target.id === 'rule_add') { addRule(); }
  else if (event.target.id === 'prune_check') { refreshPrune(); }
  else if (event.target.id === 'prune_apply') { applyPrune(); }
  else if (event.target.dataset && event.target.dataset.rulePrefix) {
    removeRule(event.target.dataset.rulePrefix);
  }
});

// Ranking feedback. Delegated, like every other handler here: inline
// onclick attributes did not survive being written from a Python literal
// and took the whole script down with them.
document.addEventListener('click', function (event) {
  const button = event.target.closest('.thumb');
  if (!button) { return; }
  const holder = button.closest('.vote');
  const query = new URLSearchParams(location.search).get('q');
  if (!holder || !query) { return; }

  fetch('/api/feedback', {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Tracepaper-Request': '1'},
    body: JSON.stringify({
      query: query,
      item_id: Number(holder.dataset.item),
      signal: button.dataset.signal,
    }),
  }).then(function (response) {
    if (!response.ok) { throw new Error('HTTP ' + response.status); }
    // Both buttons reset, so a change of mind reads correctly.
    holder.querySelectorAll('.thumb').forEach(function (other) {
      other.classList.remove('done');
    });
    button.classList.add('done');
    button.textContent = button.dataset.signal === 'up' ? 'noted' : 'noted';
  }).catch(function (error) {
    console.error('feedback failed', error);
    button.textContent = 'failed';
  });
});

window.__tpOnReady = {push: function (fn) { fn(); }};
"""


def render_page(conn: sqlite3.Connection, *, query: str = "", tab: str = "search",
                limit: int = 20, semantic: bool = True,
                mode: str = "everything", offset: int = 0,
                section: str = "general", path: str = "",
                show_code: bool = False) -> str:
    """One search box over every layer, plus a browse view for exploring.

    The user should not have to know whether a word is an entity, a field or
    passage text before typing it -- so there is one box, and the answer types
    are grouped in the result rather than split across separate searches.
    """
    tabs = [("search", "Search"), ("browse", "Browse"), ("status", "Status"),
            ("settings", "Settings"), ("how", "How it works")]
    nav = "".join(
        f'<a href="/?tab={name}" class="{"on" if name == tab else ""}">{label}</a>'
        for name, label in tabs
    )

    if tab == "how":
        body = _how_tab()
    elif tab == "settings":
        body = _settings_tab(conn, section)
    elif tab == "status":
        body = _status_tab(conn)
    elif tab == "browse":
        body = _browse_tab(conn, query, path, show_code)
    else:
        body = _unified_tab(conn, query, limit, semantic, mode, offset)

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tracepaper</title><style>{STYLE}</style></head>
<body>
<header><div class="wrap">
  <h1>Tracepaper <small>deterministic search, no model in the query path</small></h1>
  <nav>{nav}</nav>
</div></header>
<main><div class="wrap">{body}</div></main>
<script>{SCRIPT}</script>
</body></html>"""


def _esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


# What each search mode looks for, in plain words. Shown under the box so the
# effect of the choice is visible before you search, not after.
# How many photos a mixed search previews before deferring to Photos mode.
PHOTO_PREVIEW = 5

# "Everything" was a lie: it always excluded code. The label now says what
# the mode does, so no mode silently includes something you did not ask for.
_MODE_HELP = {
    "everything": "Your files and photos. No code.",
    "documents": "Files only. No photos, no code.",
    "photos": "Photos only. Searches what is in the picture.",
    "code": "Code and config files only.",
}

_MODE_LABELS = (("everything", "My documents"), ("documents", "Files only"),
                ("photos", "Photos only"), ("code", "Code"))
_MODE_LABELS_BY_VALUE = dict(_MODE_LABELS)


def _search_form(query: str, tab: str, placeholder: str,
                 semantic: bool = True, mode: str = "everything") -> str:
    """The search box, its mode, and the one switch worth exposing.

    "Semantic" was the old label. It named the implementation, not the effect,
    so it now says what it does: find words that mean the same thing.
    """
    checked = "checked" if semantic else ""
    options = "".join(
        f'<option value="{value}"{" selected" if value == mode else ""}>{label}</option>'
        for value, label in _MODE_LABELS)
    return f"""<form class="search" method="get">
  <input type="hidden" name="tab" value="{tab}">
  <div class="row main">
    <input type="text" name="q" value="{_esc(query)}" placeholder="{placeholder}"
           autofocus autocomplete="off">
    <select name="mode" aria-label="What to search">{options}</select>
    <button type="submit">Search</button>
  </div>
  <div class="row opts">
    <label class="chk"><input type="checkbox" name="semantic" value="true"
           {checked}> Match similar words</label>
    <span class="mode-help">{_MODE_HELP.get(mode, "")}</span>
  </div>
</form>"""


def _unified_tab(conn: sqlite3.Connection, query: str, limit: int,
                 semantic: bool, mode: str = "everything",
                 offset: int = 0) -> str:
    """One query, every layer, grouped by what kind of answer it is."""
    from .query.unified import UnifiedSearch

    out = [_search_form(query, "search",
                        "passport expiry · salary 2023 · Alaska · sprinkler valve",
                        semantic, mode)]

    if not query:
        from . import embed
        # is_loaded() reflects THIS process's cache, and the query path is
        # forbidden from loading a model lazily -- so a service that started
        # before the model was on disk says "keyword only" forever. Fall back
        # to asking whether the model exists at all, which is the thing the
        # reader actually needs to know.
        if semantic and not embed.is_loaded():
            if embed.available() and embed.local_path() is not None:
                hint = ('Right now it only matches exact words. The model is '
                        'ready, but Tracepaper started before it. Restart to '
                        'turn this on.')
            elif embed.available():
                hint = ('Right now it only matches exact words. The model is '
                        'still downloading. Search works in the meantime.')
            else:
                hint = ('Right now it only matches exact words. To match '
                        'similar words, install the <code>semantic</code> '
                        'extra.')
            out.append('<p class="hint">' + hint + '</p>')
        else:
            out.append('<p class="hint">Type what you remember. '
                       'Tracepaper looks in three places at once:</p>'
                       '<ul class="tips">'
                       '<li><b>A value you need.</b> "passport expiry" gives '
                       'you the date, and the page it came from.</li>'
                       '<li><b>Something that happened.</b> "Alaska" gives you '
                       'the flight, with the booking that proves it.</li>'
                       '<li><b>Words from a file.</b> "sprinkler valve" finds '
                       'the file, even if it says "irrigation solenoid".</li>'
                       '</ul>')
        return "".join(out)

    result = UnifiedSearch(conn).query(query, limit=limit, semantic=semantic,
                                       mode=mode, offset=offset)

    if result.is_empty:
        suggestions = ['Use fewer words.']
        if mode != "everything":
            suggestions.append(f'You searched {_MODE_LABELS_BY_VALUE[mode]} '
                               f'only. Try <b>Everything</b>.')
        if not semantic:
            suggestions.append('Tick <b>Match similar words</b>.')
        suggestions.append('Open <b>Browse</b> to see what Tracepaper found '
                           'in your files.')
        tips = "".join(f"<li>{tip}</li>" for tip in suggestions)
        out.append(f'<div class="empty"><b>No results.</b>'
                   f'<ul class="tips">{tips}</ul></div>')
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
    <a href="/file/{best.item_id}" target="_blank" rel="noopener"
       >{_esc(best.item_title)}</a>
    {f"&middot; p.{best.page}" if best.page else ""}
    &middot; <span class="pill {human}">{_esc(best.source)}</span>
    {_fix_button(best.item_id, result.answer_key, best.value)}
  </div>
</div>""")
        if result.alternatives:
            rows = "".join(
                f"<tr><td>{_esc(v.value)}{_esc(' ' + v.unit if v.unit else '')}</td>"
                f'<td><a href="/file/{v.item_id}" target="_blank" '
                f'rel="noopener">{_esc(v.item_title)}</a></td>'
                f'<td><span class="pill">{_esc(v.source)}</span></td></tr>'
                for v in result.alternatives)
            out.append('<p class="hint">Your files do not agree. '
                       'Tracepaper will not pick one for you.</p>')
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
                f'<a href="/file/{e["item_id"]}" target="_blank" '
                f'rel="noopener">{_esc(e["title"])}</a>'
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

    # 4. Photos. In a mixed search these compete with the documents rather
    # than always sitting on top: the strip is placed at the rank its best
    # photo earns, so a weak tag match ("2019" matches a whole year) appears
    # below the files instead of above them.
    photo_block = _photo_strip(result, query, mode, semantic)
    best_photo = max((p.get("score", 0.0) for p in result.photos), default=0.0)
    photos_placed = False
    if photo_block and mode != "everything":
        out.append(photo_block)
        photos_placed = True

    # 5. Matching documents -- the floor that always has something to say.
    if result.hits:
        # Documents are rescaled so the best is 1.0, and photo scores are on
        # the same 0-1 scale, so the two are directly comparable.
        if photo_block and not photos_placed and best_photo >= result.hits[0].score:
            out.append(photo_block)
            photos_placed = True
        out.append(f'<h2>Documents <span class="count">{result.total_hits}</span></h2>')
        for hit in result.hits:
            if (photo_block and not photos_placed
                    and best_photo >= hit.score):
                out.append(photo_block)
                photos_placed = True
            page = f' <span class="pill">p.{hit.page}</span>' if hit.page else ""
            signals = " ".join(f"{k}={v:+.4f}"
                               for k, v in sorted(hit.signals.items())
                               if not k.startswith("_"))
            # The title opens the document, not its confidence scores. The
            # scores stay reachable, because "why did this match?" is a real
            # question -- just not the one a title click is asking.
            title_link = (f'/file/{hit.item_id}' if hit.uri
                          else f'/api/items/{hit.item_id}')

            # A document appears once. Its other matching pages go here, so a
            # 40-page manual is one result you can open at the right page
            # instead of forty results that are all the same file.
            more = ""
            if hit.more:
                rows = "".join(
                    f'<li><a href="{title_link}{_page_anchor(m.page)}" '
                    f'target="_blank" rel="noopener">'
                    f'{f"p.{m.page}" if m.page else "another passage"}</a> '
                    f'<span class="snip">{_esc(m.snippet)}</span></li>'
                    for m in hit.more)
                label = (f"{hit.passage_count} matching passages"
                         if hit.passage_count > 2 else "2 matching passages")
                more = (f'<details class="more"><summary>{label}</summary>'
                        f'<ul>{rows}</ul></details>')

            # An image that ranked here (scanned documents do, via OCR) gets
            # its picture, not just its filename. The thumbnail IS the useful
            # part of an image result, and a scan of a passport with no
            # extracted text was rendering as a bare line of grey path.
            thumb = ""
            if _is_image(hit.uri):
                thumb = (f'<a class="hit-thumb" href="{title_link}" '
                         f'target="_blank" rel="noopener">'
                         f'<img src="/thumb/{hit.item_id}?size=160" alt="" '
                         f'loading="lazy"></a>')

            out.append(f"""<div class="hit{' with-thumb' if thumb else ''}">
  {thumb}
  <div class="hit-body">
  <h3><a href="{title_link}{_page_anchor(hit.page)}" target="_blank" rel="noopener">{_esc(hit.title)}</a>{page}</h3>
  <div class="path">{_folder_link(hit.uri, hit.item_id)}</div>
  <div class="snip">{_esc(hit.snippet)}</div>
  {more}
  <div class="sig">score={hit.score:.4f} · {_esc(signals)}
    · <a href="/api/items/{hit.item_id}">why</a>
    · <span class="vote" data-item="{hit.item_id}">
        <button type="button" class="thumb" data-signal="up"
                title="This is what I wanted for these words">&#9650; better</button>
        <button type="button" class="thumb" data-signal="down"
                title="Not what I wanted for these words">&#9660; worse</button>
      </span></div>
  </div>
</div>""")

        out.append(_pager(query, result, limit, semantic, mode, offset))

    # Photos that outranked nothing still belong on the page, at the bottom.
    if photo_block and not photos_placed:
        out.append(photo_block)

    return "".join(out)


def _photo_strip(result, query: str, mode: str, semantic: bool) -> str:
    """The photo grid, as one block that can be placed by rank."""
    if not result.photos:
        return ""

    filters = ", ".join(result.photo_filters)
    shown = result.photos
    more_link = ""
    if mode != "photos" and len(result.photos) > PHOTO_PREVIEW:
        shown = result.photos[:PHOTO_PREVIEW]
        params = urlencode({"tab": "search", "q": query, "mode": "photos",
                            **({"semantic": "true"} if semantic else {})})
        more_link = (f' &middot; <a href="/?{params}">'
                     f'See all {len(result.photos)}</a>')

    out = [f'<h2>Photos <span class="count">{len(result.photos)}</span></h2>'
           f'<p class="hint">Matched on {_esc(filters)}.{more_link}</p>',
           '<div class="photos">']
    # Thumbnails, not filenames: the picture is what you recognise, and
    # clicking it opens the photo rather than its confidence scores.
    for photo in shown:
        tags = " ".join(f'<span class="pill">{_esc(t)}</span>'
                        for t in photo["tags"][:6])
        out.append(f"""<figure class="photo">
  <a href="/file/{photo["item_id"]}" target="_blank" rel="noopener">
    <img src="/thumb/{photo["item_id"]}?size=320" alt="{_esc(photo["title"])}"
         loading="lazy">
  </a>
  <figcaption>
    <a href="/file/{photo["item_id"]}" target="_blank" rel="noopener"
       >{_esc(photo["title"])}</a>
    <div class="tags">{tags}</div>
  </figcaption>
</figure>""")
    out.append('</div>')
    return "".join(out)


def _pager(query: str, result, limit: int, semantic: bool,
           mode: str, offset: int) -> str:
    """Next/previous links. Plain links, so a page can be bookmarked.

    There was no way past the first page at all: a query whose answer sat at
    rank 21 was simply unreachable.
    """
    def link(new_offset: int, label: str, rel: str) -> str:
        params = urlencode({"tab": "search", "q": query, "mode": mode,
                            "offset": new_offset,
                            **({"semantic": "true"} if semantic else {})})
        return f'<a class="page" rel="{rel}" href="/?{params}">{label}</a>'

    shown_to = offset + len(result.hits)
    # `total` counts documents, but a grouped page can be shorter than `limit`
    # even when more remain, so trust the count rather than the page length.
    has_more = shown_to < result.total_hits
    if offset == 0 and not has_more:
        return ""

    parts = []
    if offset > 0:
        parts.append(link(max(0, offset - limit), "← Previous", "prev"))
    if has_more:
        parts.append(link(offset + limit, "Next →", "next"))

    return (f'<div class="pager"><span class="range">'
            f'{offset + 1}–{shown_to} of {result.total_hits}</span>'
            f'{"".join(parts)}</div>')


def _is_image(uri: str | None) -> bool:
    """Whether a result is a picture, and so deserves a thumbnail."""
    if not uri:
        return False
    from .extract.text import IMAGE_SUFFIXES
    return Path(uri).suffix.lower() in IMAGE_SUFFIXES


def _folder_link(uri: str | None, item_id: int) -> str:
    """The path under a result, with the folder part clickable.

    It was dead grey text. Now the folder opens in Browse, which is how you
    get from "this one file" to "what else is in here".
    """
    if not uri:
        return _esc(f"note:{item_id}")
    folder, _, name = uri.rpartition("/")
    if not folder:
        return _esc(uri)
    return (f'<a href="/?tab=browse&amp;path={quote(folder)}">{_esc(folder)}</a>'
            f'/{_esc(name)}')


def _page_anchor(page: int | None) -> str:
    """Open a PDF at the matching page.

    `#page=N` is the PDF Open Parameters fragment, understood by Chrome's and
    Firefox's built-in viewers. It is a fragment, so a viewer that does not
    support it ignores it and opens page 1 -- never an error.
    """
    return f"#page={page}" if page else ""


def _fix_button(item_id: int, key: str, current) -> str:
    """Inline correction. A value you fix outranks every extractor, forever."""
    args = f"{item_id}, {_js(key)}, {_js(current)}"
    return f'<button class="fix" onclick="fixValue({args})">fix</button>'


def _js(value) -> str:
    """A JavaScript string literal, safely quoted."""
    import json
    return html.escape(json.dumps(str(value)), quote=True)


def _browse_tab(conn: sqlite3.Connection, query: str, path: str = "",
                show_code: bool = False) -> str:
    """Your folders and files, the way a file manager shows them.

    This used to list extracted field names. That answers a real question but
    not the one "Browse" sets up, and on a corpus holding source code it led
    with `namespace_winrt`. The field list is still here, below, cleaned up
    and answering the question it is actually good for: what can I ask for.
    """
    from . import browse as browse_module
    from .api import get_config

    roots = [str(r) for r in get_config().roots]
    view = browse_module.listing(conn, roots, path or None)

    out: list[str] = []

    if view.get("outside_roots"):
        out.append('<div class="empty"><b>That folder is not indexed.</b>'
                   '<p class="hint">Browse only shows folders inside the '
                   'places Tracepaper was pointed at.</p></div>')
        return "".join(out)

    out.append(_breadcrumbs(view["path"], roots))

    rule = view.get("rule")
    if rule:
        out.append(f'<p class="hint">This folder is marked '
                   f'<span class="pill">{_esc(rule)}</span>. '
                   f'Change it under Settings, Search rules.</p>')

    folders = view["folders"]
    files = view["files"]
    visible = files if show_code else [f for f in files if not f["is_code"]]
    hidden = len(files) - len(visible)

    if not folders and not visible:
        if hidden:
            params = urlencode({"tab": "browse", "path": view["path"] or "",
                                "code": "1"})
            out.append(f'<div class="empty"><b>Only code in this folder.</b>'
                       f'<p class="hint">{hidden:,} code file(s) hidden. '
                       f'<a href="/?{params}">Show them</a></p></div>')
        else:
            out.append('<div class="empty"><b>Nothing indexed here.</b>'
                       '<p class="hint">Either this folder is empty, or a '
                       'scan has not reached it yet.</p></div>')
        out.append('<div id="browse_status" class="status"></div>')
        out.append(_fields_panel(conn, query))
        return "".join(out)

    # One count line instead of two section headings, so the eye goes to the
    # listing rather than to furniture.
    counts = []
    if folders:
        counts.append(f'{len(folders):,} folder' + ("s" if len(folders) != 1 else ""))
    if visible:
        counts.append(f'{len(visible):,} file' + ("s" if len(visible) != 1 else ""))
    out.append('<div class="fb-bar"><span class="sum">'
               + ", ".join(counts) + '</span>')
    if hidden:
        params = urlencode({"tab": "browse", "path": view["path"] or "",
                            "code": "1"})
        out.append(f'<span class="sum">{hidden:,} code file(s) hidden. '
                   f'<a href="/?{params}">Show them</a></span>')
    elif show_code and files:
        params = urlencode({"tab": "browse", "path": view["path"] or ""})
        out.append(f'<span class="sum"><a href="/?{params}">Hide code</a></span>')
    out.append('</div>')

    rows: list[str] = []

    # Folders first, as a file manager does, then files. Both in one table so
    # the columns line up and the folder is plainly the same kind of thing.
    for folder in folders:
        marked = (f' <span class="pill">{_esc(folder["rule"])}</span>'
                  if folder.get("rule") else "")
        if folder["items"] and folder["code_items"] / folder["items"] > 0.6:
            marked += ' <span class="pill warn">mostly code</span>'
        inner = folder.get("subfolders") or 0
        detail = f'{folder["items"]:,} file' + ("s" if folder["items"] != 1 else "")
        if inner:
            detail += f', {inner:,} folder' + ("s" if inner != 1 else "")
        rows.append(
            f'<tr class="dir">'
            f'<td class="nm"><a href="/?tab=browse&amp;path='
            f'{quote(folder["path"])}">'
            f'<span class="ic">\U0001F4C1</span>'
            f'<span class="t">{_esc(_short_name(folder["name"]))}</span></a></td>'
            f'<td class="tags">{marked}</td>'
            f'<td class="meta num hide-sm">{_esc(detail)}</td>'
            f'<td class="meta num">{_human_size(folder.get("size_bytes"))}</td>'
            f'<td class="meta hide-sm">'
            f'{_esc(_short_date(folder.get("modified_at")))}</td>'
            f'<td class="act"><button type="button" class="ghost small" '
            f'data-mark-code="{_esc(folder["path"])}">Mark as code</button>'
            f'</td></tr>')

    for f in visible:
        tags = ""
        if f["is_code"]:
            tags = '<span class="pill">code</span>'
        elif f["status"] != "complete":
            # Worth saying: these are findable by name but their text is not
            # searchable yet.
            tags = '<span class="pill warn">text pending</span>'
        rows.append(
            f'<tr>'
            f'<td class="nm"><a href="/file/{f["item_id"]}" target="_blank" '
            f'rel="noopener">'
            f'<span class="ic">'
            f'{_file_icon(f["name"], f.get("kind"), f["is_code"])}</span>'
            f'<span class="t">{_esc(f["name"])}</span></a></td>'
            f'<td class="tags">{tags}</td>'
            f'<td class="meta num hide-sm"></td>'
            f'<td class="meta num">{_human_size(f["size_bytes"])}</td>'
            f'<td class="meta hide-sm">'
            f'{_esc(_short_date(f.get("modified_at")))}</td>'
            f'<td class="act"></td></tr>')

    out.append(
        '<table class="fb"><thead><tr><th>Name</th><th></th>'
        '<th class="num hide-sm">Contents</th><th class="num">Size</th>'
        '<th class="hide-sm">Modified</th><th></th></tr></thead><tbody>'
        + "".join(rows) + '</tbody></table>')

    if view["truncated"]:
        out.append('<p class="hint">Showing the first few hundred. Use '
                   'Search to find something specific.</p>')

    out.append('<div id="browse_status" class="status"></div>')
    out.append(_fields_panel(conn, query))
    return "".join(out)


def _breadcrumbs(path: str | None, roots: list[str]) -> str:
    """Where you are, with every level above it clickable."""
    home = '<a href="/?tab=browse">All folders</a>'
    if not path:
        return f'<nav class="crumbs">{home}</nav>'

    root = next((r.rstrip("/") for r in roots
                 if path == r.rstrip("/") or path.startswith(r.rstrip("/") + "/")),
                None)
    parts = [home]
    if root:
        parts.append(f'<a href="/?tab=browse&amp;path={quote(root)}">'
                     f'{_esc(root)}</a>')
        walked = root
        for segment in path[len(root):].strip("/").split("/"):
            if not segment:
                continue
            walked = f"{walked}/{segment}"
            parts.append(f'<a href="/?tab=browse&amp;path={quote(walked)}">'
                         f'{_esc(segment)}</a>')
    return f'<nav class="crumbs">{" / ".join(parts)}</nav>'


# A glyph per file type. Emoji rather than an icon font or SVG sprite: no
# extra request, no build step, and it survives the copy-paste of this file.
_ICONS = (
    (("pdf",), "\U0001F4C4"),
    (("doc", "docx", "odt", "rtf", "pages"), "\U0001F4DD"),
    (("xls", "xlsx", "csv", "ods", "numbers"), "\U0001F4CA"),
    (("ppt", "pptx", "odp", "key"), "\U0001F4D1"),
    (("jpg", "jpeg", "png", "gif", "heic", "webp", "tif", "tiff", "bmp",
      "svg", "raw", "dng", "cr2", "nef"), "\U0001F5BC\uFE0F"),
    (("mp4", "mov", "avi", "mkv", "webm", "m4v", "wmv"), "\U0001F3AC"),
    (("mp3", "wav", "flac", "m4a", "aac", "ogg"), "\U0001F3B5"),
    (("zip", "tar", "gz", "bz2", "7z", "rar", "xz"), "\U0001F5DC\uFE0F"),
    (("txt", "md", "rst", "log"), "\U0001F4C3"),
    (("epub", "mobi", "azw3"), "\U0001F4D5"),
    (("ttf", "otf", "woff", "woff2"), "\U0001F524"),
)


def _file_icon(name: str, kind: str | None = None, is_code: bool = False) -> str:
    """A glyph for this file, chosen on extension.

    `is_code` wins over the extension, since the point of the code marking is
    to make build output and source obvious at a glance in a folder that also
    holds real documents.
    """
    if is_code:
        return "\u2699\uFE0F"
    suffix = name.rpartition(".")[2].lower() if "." in name else ""
    for suffixes, glyph in _ICONS:
        if suffix in suffixes:
            return glyph
    if kind == "photo":
        return "\U0001F5BC\uFE0F"
    return "\U0001F4C4"


def _short_date(value: str | None) -> str:
    """`2026-09-14T03:59:28` as `14 Sep 2026`, and "" for anything unparseable.

    Dates come from the filesystem via the scanner, so the format is whatever
    was stored. Anything unexpected prints nothing rather than raising.
    """
    if not value:
        return ""
    try:
        from datetime import datetime

        text = str(value).strip().replace("Z", "+00:00")
        stamp = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return str(value)[:10]
    return f"{stamp.day} {stamp:%b %Y}"


def _short_name(name: str) -> str:
    """The last segment, so a configured root does not print in full."""
    return name.rstrip("/").rpartition("/")[2] or name


def _human_size(size: int | None) -> str:
    if not size:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


def _prettify_key(key: str) -> str:
    """`interest_paid_year_to_date` as `Interest paid year to date`."""
    return key.replace("_", " ").strip().capitalize() or key


def _fields_panel(conn: sqlite3.Connection, query: str) -> str:
    """What Tracepaper pulled out of your documents, as searchable words.

    Its one real use is telling you what you can ask for: nobody guesses
    `interest_paid_year_to_date`. So it is filtered to fields that came from
    actual documents, and the raw key stays visible because that is what you
    type into the search box.
    """
    fq = FieldQuery(conn)

    if query:
        values = fq.list_values(query)
        if values:
            rows = "".join(
                f'<tr><td><a href="/?q={quote(query)}+{quote(v)}">{_esc(v)}</a>'
                f"</td><td>{n}</td></tr>" for v, n in values)
            return (f'<h2>Values of {_esc(_prettify_key(query))}</h2>'
                    f'<p class="hint">Click one to search for it.</p>'
                    f'<table><tr><th>Value</th><th>Files</th></tr>{rows}'
                    f'</table>')

    keys = fq.list_keys(query or None, limit=60, documents_only=True)
    if not keys:
        return ""

    rows = "".join(
        f'<tr><td><a href="/?tab=browse&amp;q={quote(k)}">{_esc(_prettify_key(k))}'
        f'</a> <code class="dim">{_esc(k)}</code></td><td>{n}</td></tr>'
        for k, n in keys)
    return (f'<h2>Data found in your documents</h2>'
            f'<p class="hint">Tracepaper pulled these out of your files. '
            f'Search any of them by name, for example <code>expiry date</code>. '
            f'Code files are not counted here.</p>'
            f'<table><tr><th>What it is</th><th>Files</th></tr>{rows}</table>')


def _how_tab() -> str:
    """Explain the scoring, with the numbers read from the code.

    Every constant below is interpolated from query.search rather than typed
    out, so this page cannot drift from the ranking it describes -- a docs page
    that quietly disagrees with the code is worse than none.
    """
    from .query import search as S

    return f"""<h2>How a search is scored</h2>
<p class="hint">Every number in a result line comes from the formula below.
Nothing here is learned or tuned at runtime: the same query over the same index
returns the same order, forever.</p>

<h3>The formula</h3>
<pre>score = {S.RRF_WEIGHT_BM25} / ({S.RRF_K} + keyword_rank)
      + {S.RRF_WEIGHT_VECTOR} / ({S.RRF_K} + vector_rank)
      + title_match / 100
      + recency / 100</pre>

<h3>What each signal means</h3>
<table>
  <tr><th>Signal</th><th>Meaning</th></tr>
  <tr><td><code>rrf_bm25</code></td>
      <td>Where the passage ranked on <b>keyword</b> relevance (BM25). Weight
          {S.RRF_WEIGHT_BM25}. Read it backwards to recover the rank:
          {S.RRF_WEIGHT_BM25}/({S.RRF_K}+3) = {S.RRF_WEIGHT_BM25 / (S.RRF_K + 3):.6f}
          means rank 3.</td></tr>
  <tr><td><code>rrf_vector</code></td>
      <td>Where it ranked on <b>meaning</b>. Cosine similarity between the
          query's embedding and the passage's. Weight {S.RRF_WEIGHT_VECTOR},
          deliberately below keyword's.</td></tr>
  <tr><td><code>title_match</code></td>
      <td>Query words appear in the filename. Up to {S.BOOST_TITLE_MATCH},
          divided by 100.</td></tr>
  <tr><td><code>recency</code></td>
      <td>Recently modified files edge ahead of identical older ones. Up to
          {S.BOOST_RECENCY_MAX}, divided by 100.</td></tr>
  <tr><td><code>_raw_rrf</code></td>
      <td>The sum before display rescaling. The audit number.</td></tr>
</table>

<h3>Why <code>score</code> is 1.0</h3>
<p class="hint">RRF produces values around 0.01–0.03, which round to 0.00 on
screen. The top hit is rescaled to 1.0 and the rest shown relative to it. The
order is untouched, and <code>_raw_rrf</code> keeps the true value.</p>

<h3>Three deliberate choices</h3>
<p><b>Ranks, not scores.</b> BM25 is unbounded; cosine similarity runs −1 to 1.
Combining them directly needs a normalisation that shifts as the corpus grows,
so the same query could reorder as unrelated documents arrive. Ranks are
comparable by construction.</p>

<p><b>Keyword outranks meaning.</b> {S.RRF_WEIGHT_BM25} against
{S.RRF_WEIGHT_VECTOR}. A document containing your exact words should never lose
to one that merely seems related. Vectors are there to find
<em>irrigation solenoid</em> when you typed <em>sprinkler valve</em>, to add
recall, not to overrule evidence.</p>

<p><b>A similarity floor of {S.MIN_VECTOR_SIMILARITY}.</b> Brute-force vector
search always returns <em>something</em>. Without a floor, on a query with no
real semantic match, the least-unrelated passage lands at vector rank 1 and
fusion promotes it. Below {S.MIN_VECTOR_SIMILARITY} it is discarded as noise.</p>

<h3>Where the method comes from</h3>
<p class="hint">Both retrieval halves are published IR work, not invented here.
Links go to the primary sources, open-access where one exists.</p>
<table>
  <tr><th>Piece</th><th>Source</th></tr>
  <tr><td>Reciprocal Rank Fusion,<br>and <code>k={S.RRF_K}</code></td>
      <td>Cormack, Clarke &amp; Büttcher,
          <a href="https://cormack.uwaterloo.ca/cormacksigir09-rrf.pdf"
             target="_blank" rel="noopener"><i>Reciprocal Rank Fusion
          Outperforms Condorcet and Individual Rank Learning Methods</i></a>,
          SIGIR 2009, pp. 758–759
          (<a href="https://dblp.org/rec/conf/sigir/CormackCB09.html"
              target="_blank" rel="noopener">dblp</a>).
          Two pages, and the whole method is one formula.
          <br><span class="hint">The paper says k={S.RRF_K} "was fixed during a
          pilot investigation and not altered during subsequent validation",
          and that it "was near-optimal, but that the choice was not
          critical". Used unchanged here.</span></td></tr>
  <tr><td>BM25</td>
      <td>Robertson, Walker, Jones, Hancock-Beaulieu &amp; Gatford,
          <a href="https://trec.nist.gov/pubs/trec3/papers/city.ps.gz"
             target="_blank" rel="noopener"><i>Okapi at TREC-3</i></a>, 1994.
          The readable modern treatment is Robertson &amp; Zaragoza,
          <a href="https://www.staff.city.ac.uk/~sbrp622/papers/foundations_bm25_review.pdf"
             target="_blank" rel="noopener"><i>The Probabilistic Relevance
          Framework: BM25 and Beyond</i></a> (2009), which is the place to
          start. Implemented by
          <a href="https://www.sqlite.org/fts5.html" target="_blank"
             rel="noopener">SQLite FTS5</a>, not by this project.
          <br><span class="hint">Background:
          <a href="https://en.wikipedia.org/wiki/Okapi_BM25" target="_blank"
             rel="noopener">Okapi BM25</a>.</span></td></tr>
  <tr><td>Embeddings</td>
      <td><a href="https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2"
             target="_blank" rel="noopener"><code>all-MiniLM-L6-v2</code></a>,
          384 dimensions, run locally. The method is Reimers &amp; Gurevych,
          <a href="https://arxiv.org/abs/1908.10084" target="_blank"
             rel="noopener"><i>Sentence-BERT: Sentence Embeddings using Siamese
          BERT-Networks</i></a> (EMNLP 2019).
          <br><span class="hint">An embedding turns text into numbers. The
          same query always gives the same numbers. That is why it is allowed
          here.</span></td></tr>
</table>

<p class="hint">The weights ({S.RRF_WEIGHT_BM25}/{S.RRF_WEIGHT_VECTOR}), the
two boosts and the similarity floor are our own choices. They are fixed
constants in <code>query/search.py</code>. You can read them, and they never
change from one search to the next.</p>

<h3>What an LLM does and does not do</h3>
<p class="hint">No language model takes part in ranking or in reading your
documents to answer. Search is SQL, BM25 and arithmetic over stored vectors.
An embedding model is allowed here because it always gives the same answer
for the same input. A language model is not. Language models run only when
files are read in, and what they produce is saved with a note saying where it
came from. Anything you correct yourself wins over all of it, for good.</p>"""


# Settings holds ten sections. As one page, the two you actually press
# buttons on (Updates, Jobs) sat at the bottom behind a 6KB reference table,
# so every update meant scrolling past everything else. Split into sections
# ordered by how often they are used, not by how the code is arranged.
SETTINGS_SECTIONS = (
    ("general", "General", "Update Tracepaper and run jobs."),
    ("search", "Search rules", "Change what search shows and clean the index."),
    ("storage", "Storage", "Where your files, index and backups live."),
    ("formats", "What gets indexed", "Which files are read, and which are skipped."),
    ("ai", "Photo captions", "Describe photos so you can search them by content."),
)


def _settings_tab(conn: sqlite3.Connection, section: str = "general") -> str:
    """Settings, split into sections so nothing needs a long scroll."""
    if section not in {name for name, _, _ in SETTINGS_SECTIONS}:
        section = "general"

    nav = "".join(
        f'<a href="/?tab=settings&amp;section={name}" '
        f'class="{"on" if name == section else ""}" title="{_esc(blurb)}">{label}</a>'
        for name, label, blurb in SETTINGS_SECTIONS)
    blurb = next(b for n, _, b in SETTINGS_SECTIONS if n == section)
    head = f'<nav class="subnav">{nav}</nav><p class="hint">{_esc(blurb)}</p>'

    if section == "general":
        # Updates first: it is the button pressed most and was hardest to find.
        body = _updates_panel() + _jobs_panel()
    elif section == "search":
        body = _rules_panel(conn) + _cleanup_panel()
    elif section == "formats":
        body = _coverage_panel(conn)
    elif section == "ai":
        body = _captions_panel(conn)
    else:
        body = _storage_panel(conn)
    return head + body


def _storage_panel(conn: sqlite3.Connection) -> str:
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
    out.append('<p class="hint">A share has to be mounted by the operating '
               'system before Tracepaper can read it. Use the builder below '
               'to get the exact command, then add the folder under '
               '<b>Documents to index</b>.</p>')
    out.append(_add_share_panel([str(m["source"]) for m in found_mounts]))

    if found_mounts:
        rows = "".join(
            f'<tr><td><code>{_esc(m["path"])}</code></td>'
            f'<td>{_esc(m["source"])}</td><td>{_esc(m["type"])}</td>'
            f'<td>{"read-only" if m["read_only"] else "read-write"}</td></tr>'
            for m in found_mounts)
        out.append('<h2>Network shares found</h2>')
        out.append(f'<table><tr><th>Mounted at</th><th>Source</th>'
                   f'<th>Type</th><th>Access</th></tr>{rows}</table>')

    out.append(f"""
<form id="settings" onsubmit="saveSettings(event)">
  <h2>Documents to index <span class="pill">read-only</span></h2>
  <p class="hint">The folder holding your files. Tracepaper only reads it.
    It never writes here, and never changes or deletes your files.</p>
  <div id="roots">{_root_rows(roots, checks["sources"])}</div>
  <div class="row-actions">
    <button type="button" class="ghost" onclick="addRoot()">+ add folder</button>
    <button type="submit" class="primary">Save folders</button>
  </div>
  <p class="hint subtle">A folder is only indexed after you save.</p>

  <h2>Index location <span class="pill warn">local disk only</span></h2>
  <p class="hint">Keep this on local disk. On a network share the index can
    corrupt, and you would not find out for weeks. Losing it costs nothing
    permanent: it is rebuilt from your files.</p>
  <div class="field">
    <input type="text" id="db_path" value="{_esc(cfg.db_path)}"
           onchange="checkPath(this,'index','db_status')">
    <div id="db_status" class="status">{_check_badge(checks["index"])}</div>
  </div>

  <h2>Backups <span class="pill">the NAS belongs here</span></h2>
  <p class="hint">A folder on your NAS that Tracepaper can write to. It
    holds your corrections and notes. That is the one thing here you cannot
    get back by rebuilding, so it is kept off this machine.</p>
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

    return "".join(out)


def _captions_panel(conn: sqlite3.Connection) -> str:
    """Turn photo captions on, and point them at a model.

    Most photos are named `IMG_4821.jpg`, so a caption is the only text many
    of them will ever have. This is the one place the product uses a model at
    query-adjacent time, and it stays confined to ingest: captions are written
    once into the index, and searching them afterwards is plain text matching.
    """
    from .api import get_config

    cfg = get_config()
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM items WHERE kind = 'photo' "
        "AND deleted_at IS NULL").fetchone()["n"]
    done = conn.execute(
        "SELECT COUNT(DISTINCT item_id) AS n FROM tags "
        "WHERE namespace = 'caption'").fetchone()["n"]
    pending = max(0, int(total) - int(done))

    out = ['<h2>Photo captions</h2>']
    out.append('<p class="hint">Photos are hard to search because the file '
               'name rarely says what is in the picture. Tracepaper can ask a '
               'model on your own machine to describe each photo in one '
               'sentence, then search that text. Nothing leaves your '
               'network.</p>')

    out.append(f'<div><span class="stat"><b>{int(total):,}</b>'
               f'<span>photos</span></span>'
               f'<span class="stat"><b>{int(done):,}</b>'
               f'<span>described</span></span>'
               f'<span class="stat"><b>{pending:,}</b>'
               f'<span>still to do</span></span></div>')

    if not cfg.llm_enabled:
        out.append('<p class="hint">Captions are off. Turn them on below, '
                   'then run the Describe photos job under General.</p>')
    elif pending:
        # Each run describes up to 200 photos and the timer is hourly, so a
        # count that has not moved is usually a wait, not a fault. Saying so
        # stops a working backlog reading as a stall.
        hours = max(1, -(-pending // 200))
        out.append(f'<p class="hint"><span class="pill">in progress</span> '
                   f'Up to 200 photos are described each hour, so the rest '
                   f'should be done in about {hours} hour(s). You can keep '
                   f'using search while it runs.</p>')

    checked = " checked" if cfg.llm_enabled else ""
    out.append(f"""
<form id="captions" onsubmit="saveCaptions(event)">
  <h2>Model</h2>
  <p class="hint">Ollama on your own machine or your network. Use the address
    of the computer running it, not localhost, unless it runs here.</p>
  <div class="field">
    <label><input type="checkbox" id="llm_enabled"{checked}>
      Describe photos with a model</label>
  </div>
  <div class="rule-add">
    <input type="text" id="llm_endpoint" value="{_esc(cfg.llm_endpoint)}"
           placeholder="http://mac.studio.local:11434">
    <button type="button" class="ghost" onclick="testLlm()">Test</button>
  </div>
  <div id="llm_status" class="status"></div>
  <div class="field">
    <label class="hint" for="vlm_model">Model for photos</label>
    <input type="text" id="vlm_model" value="{_esc(cfg.vlm_model)}"
           placeholder="gemma4:e4b">
    <div id="vlm_hint" class="hint">Press Test to list what is installed.</div>
  </div>
  <div class="actions">
    <button type="submit">Save</button>
  </div>
</form>
<p class="hint">Not every model can read images. Some accept a photo and
  answer as if none arrived, and one was seen inventing a confident
  description of a picture it never saw. Tracepaper throws those replies away
  rather than storing them, so a bad model means no captions, never wrong
  ones. <b>gemma4:e4b</b> was tested and works.</p>""")
    return "".join(out)


def _add_share_panel(known_sources: list[str]) -> str:
    """Build the command that mounts another NAS folder.

    Not a button that mounts it, and this is a kernel limit rather than a
    decision: the app runs in an unprivileged LXC, where mount(2) is refused
    for cifs and nfs outright. Only a handful of filesystems (proc, tmpfs,
    devpts and friends) may be mounted in a user namespace at all. Mounting
    happens on the Proxmox host, which the container cannot reach by design,
    since reaching it would mean handing the app root outside its own box.
    What the UI can do is stop making you look up the syntax.

    The NAS address is pre-filled from a share already mounted, because it is
    almost always the same NAS.
    """
    # Pin the script to the commit this app is running, not to `main`.
    # raw.githubusercontent caches a branch path for minutes, so a freshly
    # pushed fix is served stale: the user ran a corrected command twice and
    # got the identical old error both times. A commit path is immutable and
    # never cached wrong, and it also guarantees the script matches this build
    # rather than whatever main happens to hold.
    revision = "main"
    try:
        from . import updates

        local = updates.check_local()
        if getattr(local, "current", None) and local.current.sha:
            revision = local.current.sha
    except Exception:                                     # noqa: BLE001
        pass

    guess = ""
    for source in known_sources:
        # A cifs source looks like //192.168.0.28/documents, an nfs one like
        # 192.168.0.28:/volume1/documents. Either way the host is what matters.
        text = str(source).lstrip("/")
        host = text.split("/")[0].split(":")[0]
        if host and any(ch.isdigit() for ch in host):
            guess = host
            break

    return f"""
<h2>Add another folder from the NAS</h2>
<p class="hint">Tracepaper cannot mount a share itself. It runs in a
  container that the kernel does not allow to mount network drives, which is
  also what stops a bug here from touching your files. Fill this in and it
  writes the command to run on your Proxmox host, once per share.</p>
<div class="share-build">
  <label>NAS address
    <input type="text" id="sh_host" value="{_esc(guess)}"
           placeholder="192.168.0.28"></label>
  <label>Folder on the NAS
    <input type="text" id="sh_share"
           placeholder="/homes/your_name/Photos"></label>
  <label>Short name
    <input type="text" id="sh_name" placeholder="photos"></label>
  <label>Container ID
    <input type="text" id="sh_ctid" value="103" placeholder="103"></label>
</div>
<button type="button" class="ghost" onclick="buildShareCommand()"
        data-revision="{_esc(revision)}">
  Build the command</button>
<pre id="sh_out" class="share-out" hidden></pre>
<p class="hint" id="sh_note" hidden>Run that on the <b>Proxmox host</b>, not in
  this container and not on the NAS. It downloads the mount script, mounts the
  folder read-only so Tracepaper can never change what is in it, and restarts
  the container so the folder appears.</p>
<p class="hint" id="sh_after" hidden>Then add
  <code id="sh_path"></code> under <b>Documents to index</b> below, press Save,
  and run a scan from Settings, General.</p>"""


def _rules_panel(conn: sqlite3.Connection) -> str:
    """Folder rules: the part of search you can actually change.

    Everything else on this page describes what Tracepaper decided. This is
    where you overrule it, and the rules stick -- they are keyed on the path,
    so a rescan does not clear them.
    """
    from . import rules as rules_module

    existing = rules_module.list_rules(conn)
    if existing:
        rows = "".join(
            f'<tr><td><code>{_esc(r["prefix"])}</code></td>'
            f'<td><span class="pill">{_esc(r["rule"])}</span></td>'
            f'<td>{int(r["items"]):,}</td>'
            f'<td><button type="button" class="ghost small" data-rule-prefix='
            f'"{_esc(r["prefix"])}">Remove</button></td></tr>'
            for r in existing)
        table = (f'<table><tr><th>Folder</th><th>Rule</th><th>Files</th>'
                 f'<th></th></tr>{rows}</table>')
    else:
        table = ('<p class="hint">No rules yet. Add one below to change what '
                 'search does with a folder.</p>')

    return f"""<h2>Folder rules</h2>
<p class="hint">Teach search about a folder. Rules apply to everything inside
it and stay after a rescan.</p>
<ul class="tips">
  <li><b>Code</b>: keep it out of normal results. Still there under
      <b>Code</b> in the search box.</li>
  <li><b>Hide</b>: never show it in search at all.</li>
  <li><b>Boost</b>: rank files here higher when they match.</li>
</ul>
{table}
<div class="field rule-add">
  <input type="text" id="rule_prefix" placeholder="/mnt/nas/documents/Projects">
  <select id="rule_kind">
    <option value="code">Code</option>
    <option value="hide">Hide</option>
    <option value="boost">Boost</option>
  </select>
  <button type="button" id="rule_add">Add rule</button>
</div>
<div id="rule_status" class="status"></div>"""


def _cleanup_panel() -> str:
    """Remove indexed files that today's rules would not index.

    Exists because the alternative was telling you to run
    `tracepaper prune --apply` in a terminal, and a setting you have to leave
    the page to apply is not really a setting.
    """
    return """<h2>Clean up the index</h2>
<p class="hint">Rules only apply to the <b>next</b> scan. Files indexed before
you set a rule stay until you remove them here.</p>
<p class="hint">This deletes index entries only. <b>Your files are never
touched</b>, and a scan rebuilds anything removed by mistake.</p>
<div class="actions">
  <button type="button" id="prune_check" class="ghost">See what would go</button>
  <button type="button" id="prune_apply" hidden>Remove them</button>
</div>
<div id="prune_status" class="status"></div>
<script>(window.__tpOnReady = window.__tpOnReady || []).push(function () {
  refreshPrune({quiet: true});
});</script>"""



def _coverage_panel(conn: sqlite3.Connection) -> str:
    """What gets indexed, what gets skipped, and what is actually in there.

    Added because a note vault's bundled plugin JavaScript turned up in search
    results and there was no way to see why: the excludes and the understood
    formats were both invisible, so "why is this here" and "why is that
    missing" were equally unanswerable.
    """
    from .extract import text as text_extract

    machine = " ".join(f"<code>{_esc(x)}</code>"
                       for x in sorted(text_extract.MACHINE_SUFFIXES))

    groups = [
        ("Text and markup", text_extract.TEXT_SUFFIXES),
        ("Spreadsheets", text_extract.CSV_SUFFIXES | text_extract.XLSX_SUFFIXES),
        ("PDF", text_extract.PDF_SUFFIXES),
        ("Word", text_extract.DOCX_SUFFIXES),
        ("Email", text_extract.EML_SUFFIXES),
        ("Images", text_extract.IMAGE_SUFFIXES),
    ]
    format_rows = "".join(
        f"<tr><td>{_esc(label)}</td><td><code>"
        + "</code> <code>".join(sorted(_esc(x) for x in suffixes))
        + "</code></td></tr>"
        for label, suffixes in groups)

    # The EFFECTIVE list, not the module defaults: a config file that set its
    # own `excludes` used to replace them, so showing the defaults here would
    # have displayed names the running scanner was not actually using.
    try:
        from .api import get_config
        active = get_config().excludes
    except Exception:
        from .config import DEFAULT_EXCLUDES
        active = DEFAULT_EXCLUDES
    excluded = " ".join(f"<code>{_esc(name)}</code>"
                        for name in sorted(active))

    try:
        from .api import get_config
        pattern_list = get_config().exclude_patterns
    except Exception:
        from .config import DEFAULT_EXCLUDE_PATTERNS
        pattern_list = DEFAULT_EXCLUDE_PATTERNS
    patterns = " ".join(f"<code>{_esc(x)}</code>" for x in sorted(pattern_list))

    from .query import modes
    code_suffixes = " ".join(
        f"<code>{_esc(x)}</code>" for x in sorted(modes.CODE_SUFFIXES))
    code_dirs = " ".join(
        f"<code>{_esc(x)}</code>" for x in
        sorted(modes.CODE_DIRS) + [f"*{x}" for x in modes.CODE_DIR_SUFFIXES])

    # What is actually indexed, by extension -- the honest answer to "is my
    # stuff in there", and where an unwanted pattern shows up first.
    try:
        rows = conn.execute(
            "SELECT LOWER(CASE WHEN instr(uri, '.') > 0 "
            "  THEN replace(uri, rtrim(uri, replace(uri, '.', '')), '') "
            "  ELSE '(none)' END) AS ext, COUNT(*) AS n "
            "FROM items WHERE deleted_at IS NULL AND uri IS NOT NULL "
            "GROUP BY ext ORDER BY n DESC LIMIT 15"
        ).fetchall()
        indexed = "".join(
            f"<tr><td><code>.{_esc(r['ext'])}</code></td>"
            f"<td>{int(r['n']):,}</td></tr>" for r in rows if r["ext"])
    except sqlite3.Error:
        indexed = ""

    indexed_table = (
        f"<h3>Most indexed extensions</h3><table>"
        f"<tr><th>Extension</th><th>Items</th></tr>{indexed}</table>"
        if indexed else "")

    return f"""<h2>What gets indexed</h2>
<p class="hint">You can find <b>every</b> file by its name and folder.
The formats below are also read <b>inside</b>, so you can search their
words too.</p>
<table><tr><th>Kind</th><th>Extensions</th></tr>{format_rows}</table>

<h3>Found by name only</h3>
<p class="hint">These are machine files. They are technically text, but the
text means nothing to a person. One <code>.gcode</code> file made 104,227
passages on its own, more than every real document next to it. So Tracepaper
reads the name and skips what is inside.</p>
<p class="excludes">{machine}</p>

<h3>Hidden from search by default</h3>
<p class="hint">Code and config files stay in the index, but they are kept out
of results. To see them, pick <b>Code</b> next to the search box. Nothing is
deleted.</p>
<p class="excludes">{code_suffixes}</p>
<p class="hint">Everything inside these folders counts as code too, whatever
the file is called. Build output is full of files like <code>LICENSE</code>
and <code>METADATA</code> that have no file extension at all.</p>
<p class="excludes">{code_dirs}</p>

<h3>Never indexed</h3>
<p class="hint">These folder and file names are skipped wherever they appear.
They hold app data, caches and build output, not your documents.</p>
<p class="excludes">{excluded}</p>
<p class="hint">These name patterns are skipped too. Build tools generate
these folders with a version number in the name, so they cannot be listed
one by one.</p>
<p class="excludes">{patterns}</p>
<p class="hint">This list applies to the <b>next</b> scan. Files already
indexed stay until you remove them, which you can do under
<b>Clean up the index</b> above.</p>
{indexed_table}"""


def _jobs_panel() -> str:
    """Buttons for the background units, so routine work needs no terminal.

    The list is filled in from the browser rather than server-side: reading
    systemd state costs several `systemctl` calls, and a page that blocks on
    those is worse than one that fills in a moment later. It also lets the
    panel keep polling while a scan runs.
    """
    return """<h2>Jobs</h2>
<p class="hint">These run on timers already. The buttons run them now.
A first scan can take hours. It is safe to leave this page.</p>
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
        # appears, so say what to run instead. Name the one check that is
        # actually failing: listing every possible cause meant the reader had
        # to run diagnostics to find out which one applied.
        reason, fix = updates.BLOCKER_FIXES.get(
            status.blocker,
            ("This server cannot apply updates itself.",
             "systemctl start tracepaper-update"))
        action = (f'<p class="hint">{_esc(reason)} Run '
                  f'<code>{_esc(fix)}</code> in the container.</p>'
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
        skipped = max(0, int(info.get("pending_all") or 0)
                      - int(info["pending"]))
        note = ""
        if skipped:
            note = (f' A further {skipped:,} are code, or formats that hold '
                    f'no text, so they need nothing.')
        out.append(f'<p class="hint"><span class="pill warn">pending</span> '
                   f'{info["pending"]:,} file(s) have no searchable text yet. '
                   f'They are findable by name.{note}</p>')

        # A total is not actionable. Whether it is scanned PDFs worth running
        # OCR over, or videos that will never hold text, decides whether there
        # is work to do at all.
        formats = info.get("pending_formats") or []
        if formats:
            rows = "".join(
                f'<tr><td><code>{_esc(f["suffix"])}</code></td>'
                f'<td>{f["items"]:,}</td>'
                f'<td><span class="pill">{_esc(f["reason"])}</span></td>'
                f'<td class="hint">{_esc(f["detail"])}</td></tr>'
                for f in formats)
            out.append(f'<table><tr><th>Type</th><th>Files</th>'
                       f'<th>Why</th><th>What it means</th></tr>{rows}</table>')

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
            action = ('install semantic search first. This build has no '
                      'embedding model, so <code>tracepaper embed</code> would '
                      'exit with an error')
        out.append(f'<p class="hint">{embedded} of {embeddings["passages"]} '
                   f'passages embedded. {action}</p>')

    # A model that failed to load is the difference between semantic search and
    # keyword-only, and nothing on this page used to say so. It went unnoticed
    # for days: the service wrote one warning to the journal and carried on.
    from . import embed as _embed_status

    load_error = _embed_status.load_error()
    if load_error:
        out.append(
            f'<p class="hint"><span class="pill warn">keyword only</span> '
            f'The word-meaning model did not load, so search is matching exact '
            f'words only. {_esc(load_error[:300])}</p>')

    scan = info["last_scan"]
    if scan:
        # A failure has to say why on the page. "failed" on its own sent you
        # to the journal, which is not where someone reading a web page is.
        failure = ""
        if scan["status"] == "failed" and scan.get("message"):
            failure = (f'<p class="hint"><span class="pill warn">scan failed'
                       f'</span> {_esc(scan["message"])}</p>')
        out.append(failure)
        out.append(f"""<table><tr><th>Last scan</th><th></th></tr>
  <tr><td>root</td><td><code>{_esc(scan["root"])}</code></td></tr>
  <tr><td>status</td><td>{_esc(scan["status"])}</td></tr>
  <tr><td>finished</td><td>{_esc(scan["finished_at"] or "-")}</td></tr>
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

"""REST API, MCP tools and Tier 2 evidence (FR-12, requirements §3)."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from tracepaper.index.indexer import Indexer
from tracepaper.mcp_server import TOOLS, Handler
from tracepaper.query.evidence import EvidenceQuery
from tracepaper.scan.scanner import Scanner

W2 = """Form W-2 Wage and Tax Statement
Tax Year: 2023
Employer name: ACME Corporation
1 Wages, tips, other compensation 91500.00
"""

FLIGHT = """From: noreply@alaskaair.com
Subject: Alaska Airlines itinerary
Confirmation code: ABC123
Flight AS 1234, SEA to PDX
Departure date: 2023-04-15
"""


@pytest.fixture
def populated(conn, cfg, nas):
    (nas / "w2.txt").write_text(W2)
    (nas / "flight.eml").write_text(FLIGHT)
    Scanner(conn, cfg).scan(nas)
    Indexer(conn, cfg).run_pending()
    return conn


@pytest.fixture
def client(populated, cfg):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from tracepaper.api import create_app

    return TestClient(create_app(cfg))


# ------------------------------------------------------------------- REST

def test_ui_renders(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Tracepaper" in response.text


def test_api_health(client):
    """The installer and update script gate on this, so it must stay cheap
    and must report a broken index rather than raising."""
    data = client.get("/api/health").json()
    assert data["ok"] is True
    assert data["db_path"]


def test_api_search(client):
    data = client.get("/api/search",
                      params={"q": "wages", "semantic": False}).json()
    assert data["total"] >= 1
    assert data["hits"][0]["title"] == "w2.txt"
    assert "signals" in data["hits"][0], "ranking must stay inspectable"


def test_api_get_with_constraint(client):
    """The binding property, over HTTP."""
    data = client.get("/api/get", params={"key": "gross_salary",
                                          "where": ["tax_year=2023"]}).json()
    assert data["found"]
    assert data["values"][0]["value"] == 91500.0
    assert data["values"][0]["citation"]


def test_api_get_missing_key(client):
    data = client.get("/api/get", params={"key": "no_such_field"}).json()
    assert data["found"] is False


def test_api_aggregate_lists_contributors(client):
    data = client.get("/api/aggregate",
                      params={"key": "gross_salary", "op": "sum"}).json()
    assert data["result"] == 91500.0
    assert data["contributing"], "a total must be auditable"


def test_api_events(client):
    data = client.get("/api/events", params={"entity": "Alaska Airlines"}).json()
    assert data["events"]
    assert data["events"][0]["evidence"], "an event must cite its documents"


def test_api_item_detail(client):
    item_id = client.get("/api/search",
                         params={"q": "wages", "semantic": False}
                         ).json()["hits"][0]["item_id"]
    data = client.get(f"/api/items/{item_id}").json()
    assert data["records"]
    assert data["text"]


def test_api_unknown_item_is_404(client):
    assert client.get("/api/items/99999").status_code == 404


def test_api_correction_round_trip(client):
    item_id = client.get("/api/search",
                         params={"q": "wages", "semantic": False}
                         ).json()["hits"][0]["item_id"]

    posted = client.post("/api/correct", json={
        "item_id": item_id, "key": "gross_salary", "value": "92000"})
    assert posted.json()["ok"]

    data = client.get("/api/get", params={"key": "gross_salary"}).json()
    assert data["values"][0]["source"] == "human"


def test_api_note_creation(client):
    created = client.post("/api/notes", json={
        "title": "Test note", "text": "irrigation solenoid replaced"}).json()
    assert created["ok"]

    found = client.get("/api/search",
                       params={"q": "irrigation solenoid",
                               "semantic": False}).json()
    assert found["total"] >= 1


def test_api_status_reports_pending(client):
    data = client.get("/api/status").json()
    assert data["items"] >= 2
    assert "pending" in data, "pending work must be visible (NFR-4)"


# -------------------------------------------------------------------- MCP

def test_every_tool_has_a_schema():
    for tool in TOOLS:
        assert tool["name"] and tool["description"]
        assert tool["inputSchema"]["type"] == "object"


def test_mcp_get_value(populated, cfg):
    result = Handler(cfg).call("get_value", {"key": "gross_salary",
                                             "where": {"tax_year": 2023}})
    assert result["found"]
    assert result["values"][0]["value"] == 91500.0


def test_mcp_search(populated, cfg):
    result = Handler(cfg).call("search", {"query": "wages", "semantic": False})
    assert result["total"] >= 1


def test_mcp_events(populated, cfg):
    result = Handler(cfg).call("get_events", {"entity": "Alaska Airlines"})
    assert result["events"]


def test_mcp_list_keys(populated, cfg):
    result = Handler(cfg).call("list_keys", {})
    assert any(k["key"] == "gross_salary" for k in result["keys"])


def test_mcp_unknown_tool_errors_cleanly(populated, cfg):
    assert "error" in Handler(cfg).call("no_such_tool", {})


def test_mcp_bad_arguments_do_not_crash(populated, cfg):
    """A malformed call must return an error, never take the server down."""
    result = Handler(cfg).call("get_value", {})
    assert "error" in result


def test_mcp_results_are_json_serialisable(populated, cfg):
    """Every tool result crosses the wire as JSON."""
    handler = Handler(cfg)
    for name, args in [("search", {"query": "wages"}),
                       ("get_value", {"key": "gross_salary"}),
                       ("get_events", {}),
                       ("list_keys", {}),
                       ("gather_evidence", {"question": "acme"})]:
        json.dumps(handler.call(name, args))


# ----------------------------------------------------------------- Tier 2

def test_evidence_gathering_returns_no_verdict(populated):
    """Tier 2 hands back evidence; the reasoning belongs to the caller."""
    evidence = EvidenceQuery(populated).gather("what do I know about ACME",
                                               entity="ACME Corporation")
    assert not evidence.is_empty
    assert evidence.facts
    payload = evidence.as_dict()
    assert set(payload) == {"question", "entities", "facts", "events", "passages"}
    assert "answer" not in payload and "conclusion" not in payload


def test_evidence_is_reproducible(populated):
    """NFR-2 extends to Tier 2: the same question, the same evidence."""
    query = EvidenceQuery(populated)
    runs = [json.dumps(query.gather("acme salary", entity="ACME Corporation",
                                    semantic=False).as_dict())
            for _ in range(3)]
    assert all(run == runs[0] for run in runs)


def test_evidence_facts_carry_citations(populated):
    evidence = EvidenceQuery(populated).gather("acme", entity="ACME Corporation")
    for fact in evidence.facts:
        assert fact.citation()


# ------------------------------------------------------------------ backup

def test_human_layer_survives_a_rebuilt_index(populated, cfg, nas, tmp_path):
    """NFR-6: everything else regenerates; this layer must be restorable.

    Simulates the real disaster: the index is lost and rebuilt from the source
    folder, where every row id differs.
    """
    from tracepaper import backup, corrections, notes
    from tracepaper.db import connect
    from tracepaper.query.fields import FieldQuery

    item_id = int(populated.execute(
        "SELECT id FROM items WHERE title = 'w2.txt'").fetchone()["id"])
    corrections.correct_field(populated, item_id, "gross_salary", "92000")
    notes.create_note(populated, "Sprinkler repair", "irrigation solenoid")
    Indexer(populated, cfg).run_pending()

    backup_file = tmp_path / "human.json"
    counts = backup.write_backup(populated, backup_file)
    assert counts["corrections"] == 1
    assert counts["notes"] == 1
    populated.close()

    rebuilt_path = tmp_path / "rebuilt.db"
    rebuilt = connect(rebuilt_path)
    rebuilt_cfg = replace(cfg, db_path=rebuilt_path)
    Scanner(rebuilt, rebuilt_cfg).scan(nas)
    Indexer(rebuilt, rebuilt_cfg).run_pending()

    assert FieldQuery(rebuilt).get("gross_salary").best.value == 91500.0, \
        "a fresh index starts from the extracted value"

    applied = backup.restore_backup(rebuilt, backup_file)

    assert applied["corrections"] == 1
    best = FieldQuery(rebuilt).get("gross_salary").best
    assert best.value == 92000.0, "the correction must come back"
    assert best.source == "human"
    assert rebuilt.execute(
        "SELECT COUNT(*) AS n FROM items WHERE kind = 'note'"
    ).fetchone()["n"] == 1
    rebuilt.close()


def test_backup_matches_documents_that_moved(populated, cfg, nas, tmp_path):
    """A file that moved since the backup is still matched, by content hash."""
    from tracepaper import backup, corrections
    from tracepaper.db import connect
    from tracepaper.query.fields import FieldQuery

    item_id = int(populated.execute(
        "SELECT id FROM items WHERE title = 'w2.txt'").fetchone()["id"])
    corrections.correct_field(populated, item_id, "gross_salary", "92000")
    backup_file = tmp_path / "human.json"
    backup.write_backup(populated, backup_file)
    populated.close()

    archive = nas / "archive"
    archive.mkdir()
    (nas / "w2.txt").rename(archive / "w2_filed.txt")

    rebuilt_path = tmp_path / "rebuilt.db"
    rebuilt = connect(rebuilt_path)
    rebuilt_cfg = replace(cfg, db_path=rebuilt_path)
    Scanner(rebuilt, rebuilt_cfg).scan(nas)
    Indexer(rebuilt, rebuilt_cfg).run_pending()

    assert backup.restore_backup(rebuilt, backup_file)["corrections"] == 1
    assert FieldQuery(rebuilt).get("gross_salary").best.value == 92000.0
    rebuilt.close()


def test_api_updates_reports_unsupported_for_a_non_checkout(client, monkeypatch):
    """A copied-in install has no remote, and the UI must say so rather than
    offering a button that cannot work."""
    from tracepaper import updates
    monkeypatch.setattr(updates, "APP_DIR", Path("/nonexistent-tracepaper"))
    data = client.get("/api/updates").json()
    assert data["supported"] is False
    assert data["can_apply"] is False
    assert "not a git checkout" in data["reason"]


def test_api_update_apply_requires_the_custom_header(client):
    """A cross-site form cannot set a custom header without a CORS preflight,
    which closes the drive-by case. Not authentication, and not claimed to be."""
    response = client.post("/api/updates/apply")
    assert response.status_code == 403
    assert "X-Tracepaper-Request" in response.json()["detail"]


def test_api_update_apply_refuses_cross_site(client):
    response = client.post("/api/updates/apply",
                           headers={"X-Tracepaper-Request": "1",
                                    "Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 403
    assert "Cross-site" in response.json()["detail"]


def test_api_update_apply_reports_when_it_cannot(client, monkeypatch):
    """With the unit absent, this must be a clean 409 naming the manual
    command -- not a 500."""
    from tracepaper import updates
    monkeypatch.setattr(updates, "can_apply", lambda: False)
    response = client.post("/api/updates/apply",
                           headers={"X-Tracepaper-Request": "1"})
    assert response.status_code == 409
    assert "systemctl start tracepaper-update" in response.json()["detail"]


def test_settings_page_checks_for_updates_on_load(client, monkeypatch):
    """A panel that only answers when clicked is one you have to remember to
    click. The check runs in the browser, not server-side, so a slow or hanging
    `git fetch` never blocks the page render."""
    from tracepaper import updates
    # Point at this repo, which is a real checkout; the test's tmp dir is not.
    monkeypatch.setattr(updates, "APP_DIR",
                        Path(__file__).resolve().parent.parent)
    html = client.get("/?tab=settings").text
    assert "<h2>Updates</h2>" in html
    assert "checkUpdates({quiet: true})" in html


def test_settings_page_is_honest_when_updates_are_impossible(client, monkeypatch):
    """A copied-in install cannot update. Say so, rather than showing a button
    that fails."""
    from tracepaper import updates
    monkeypatch.setattr(updates, "APP_DIR", Path("/nonexistent-tracepaper"))
    html = client.get("/?tab=settings").text
    assert "not a git checkout" in html


def test_background_update_check_is_quiet_on_failure(client):
    """An unreachable remote on a check the user did not ask for should not
    paint an error over a page that is working fine."""
    html = client.get("/?tab=settings").text
    script = html[html.index("async function checkUpdates"):]
    script = script[:script.index("async function applyUpdate")]
    assert "if (!quiet)" in script, (
        "a background check must suppress its own failure messages")


def test_update_check_never_writes_to_the_checkout(client, monkeypatch):
    """`git fetch` writes FETCH_HEAD, objects and a lock file, and the service
    cannot write to its own code -- the tree is root-owned so a compromised
    service cannot rewrite what it runs next. Fetching as this user fails with
    a generic transport error that hides the permission problem."""
    from tracepaper import updates
    source = Path(updates.__file__).read_text()
    assert '"ls-remote"' in source
    assert '"fetch"' not in source, (
        "the read-only check must not fetch; the privileged unit does that")


# ------------------------------------------------------------------- jobs
#
# The job endpoints start privileged systemd units, so their guards matter as
# much as their happy path. These pin both.


def test_jobs_endpoint_reports_unavailable_off_systemd(client):
    """Off a systemd host nothing is loaded, and the UI must say so rather
    than offering buttons that cannot work."""
    body = client.get("/api/jobs").json()
    assert body["available"] is False
    assert body["jobs"] == []
    assert body["detail"]


def test_starting_a_job_requires_the_custom_header(client):
    """Without the header a cross-site form could POST work onto the server."""
    response = client.post("/api/jobs/scan/start")
    assert response.status_code == 403
    assert "X-Tracepaper-Request" in response.json()["detail"]


def test_starting_a_job_refuses_a_cross_site_request(client):
    response = client.post("/api/jobs/scan/start", headers={
        "X-Tracepaper-Request": "1", "Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 403
    assert "Cross-site" in response.json()["detail"]


def test_unknown_job_names_are_refused(client):
    """The name reaches a systemd unit lookup, so it must be an allowlist --
    never interpolated into a unit name."""
    response = client.post("/api/jobs/../../etc/passwd/start",
                           headers={"X-Tracepaper-Request": "1"})
    assert response.status_code in (403, 404)

    response = client.post("/api/jobs/tracepaper/start",
                           headers={"X-Tracepaper-Request": "1"})
    assert response.status_code == 404


def test_job_names_map_to_a_fixed_unit_list():
    """jobs.start must never build a unit name from its argument."""
    from tracepaper import jobs
    for name, unit in jobs.UNITS.items():
        assert unit.startswith("tracepaper-") and unit.endswith(".service")
    assert jobs.start("nope; systemctl stop tracepaper")[0] is False


def test_starting_a_job_reports_why_it_could_not(client, monkeypatch):
    """A refusal must arrive as a message the UI can show, not a 500."""
    from tracepaper import jobs
    monkeypatch.setattr(jobs, "start",
                        lambda name: (False, "scan is already running."))
    response = client.post("/api/jobs/scan/start",
                           headers={"X-Tracepaper-Request": "1"})
    assert response.status_code == 409
    assert response.json()["detail"] == "scan is already running."


def test_running_oneshot_units_are_reported_as_running(monkeypatch):
    """A oneshot unit is 'activating' for its entire run -- treating only
    'active' as running would report a multi-hour scan as idle, and light up
    a Run button that then refuses."""
    from tracepaper import jobs

    monkeypatch.setattr(jobs, "_show", lambda unit: {
        "LoadState": "loaded", "ActiveState": "activating",
        "Result": "success", "ExecMainStartTimestamp": "Sun 2026-09-07 21:57"})
    monkeypatch.setattr(jobs, "_can_start", lambda unit: True)

    status = jobs.status()
    assert status.available
    for job in status.jobs:
        assert job.running is True
        assert job.can_start is False, "a running job must not offer a button"


def test_a_job_already_running_is_not_started_again(monkeypatch):
    """systemd treats starting a running oneshot as a no-op, so reporting it
    as started would be a lie the UI shows as a fresh run."""
    from tracepaper import jobs
    monkeypatch.setattr(jobs, "_show", lambda unit: {
        "LoadState": "loaded", "ActiveState": "activating"})

    started, message = jobs.start("scan")
    assert started is False
    assert "already running" in message


def test_a_failed_job_is_reported_not_hidden(monkeypatch):
    """`systemctl show` exits 0 for a failed unit, which is why it is used
    instead of `status` -- the failure must reach the UI."""
    from tracepaper import jobs
    monkeypatch.setattr(jobs, "_show", lambda unit: {
        "LoadState": "loaded", "ActiveState": "failed",
        "Result": "exit-code", "ExecMainStartTimestamp": "Sun 2026-09-07 21:57"})
    monkeypatch.setattr(jobs, "_can_start", lambda unit: True)

    job = jobs.status().jobs[0]
    assert job.running is False
    assert job.result == "exit-code"
    assert job.can_start is True, "a failed job must be retryable"


def test_jobs_panel_polls_only_while_something_runs(client):
    """An idle Settings tab must not poll forever."""
    page = client.get("/?tab=settings").text
    assert 'id="jobs_list"' in page
    assert "refreshJobs" in page
    script = page[page.index("async function refreshJobs"):]
    script = script[:script.index("async function startJob")]
    assert "anyRunning" in script
    assert "clearTimeout" in script, "a finished job must stop the timer"


def test_can_start_is_false_without_a_polkit_daemon(monkeypatch):
    """`systemctl start --dry-run` does NOT consult polkit: it plans the job
    and exits 0 even where the real start would be refused. A container with
    no polkitd running therefore passed the check and then failed the actual
    start with exit 4 (EXIT_NOPERMISSION), lighting up a button that could
    never work. The daemon has to be checked separately."""
    from tracepaper import jobs

    calls = []

    def fake_run(args):
        calls.append(args)
        if "is-active" in args:
            raise subprocess.CalledProcessError(3, args)
        return ""

    monkeypatch.setattr(jobs, "_run", fake_run)
    assert jobs._can_start("tracepaper-scan.service") is False
    assert not any("--dry-run" in a for a in calls), (
        "the dry run must not even be attempted once polkit is known to be down")


def test_update_can_apply_is_false_without_a_polkit_daemon(monkeypatch):
    from tracepaper import updates

    def fake_run(args, cwd=None):
        if "is-active" in args:
            raise subprocess.CalledProcessError(3, args)
        return ""

    monkeypatch.setattr(updates, "_run", fake_run)
    monkeypatch.setattr(Path, "exists", lambda self: True)
    assert updates.can_apply() is False


def test_installer_installs_the_polkit_daemon():
    """The rule is inert without a daemon to read it, and the restart was
    swallowed by `|| true` -- so a container missing polkitd looked like a
    clean install and only failed when someone pressed a button."""
    installer = (Path(__file__).resolve().parents[1] / "deploy"
                 / "proxmox-install.sh").read_text()
    assert "polkitd" in installer, "polkitd must be installed, not assumed"


def _script_blocks(html: str) -> list[str]:
    import re
    return re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)


@pytest.mark.parametrize("tab", ["search", "browse", "settings", "status"])
def test_served_javascript_parses(client, tab):
    """Syntax-check the JS the browser actually receives, not the source.

    This caught a real outage: the jobs button was built with an inline
    onclick, whose nested quotes had to survive a Python string literal on the
    way out. They did not -- the browser received `'' +`, the whole <script>
    block failed to parse, and every handler in it died at once. The Jobs panel
    sat on "loading…" and the update button stopped responding.

    Reading web.py showed correct-looking JS, because the escaping was correct
    *there*. Only the rendered output shows the bug, so that is what is checked.
    """
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is needed to parse the served JavaScript")

    blocks = _script_blocks(client.get(f"/?tab={tab}").text)
    assert blocks, f"the {tab} tab served no script at all"

    for index, block in enumerate(blocks):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
            handle.write(block)
            path = handle.name
        result = subprocess.run([node, "--check", path],
                                capture_output=True, text=True)
        assert result.returncode == 0, (
            f"script block {index} on the {tab} tab is not valid JavaScript:\n"
            f"{result.stderr}")


def test_job_buttons_do_not_build_inline_onclick_handlers(client):
    """An inline onclick needs quotes nested inside the HTML attribute, inside
    the JS string, inside the Python literal. That is three levels of escaping
    for one click handler, and it broke. A data attribute plus a delegated
    listener has none."""
    page = client.get("/?tab=settings").text
    assert "startJob(this, " not in page, (
        "build the handler from data-job, not an inline onclick")
    assert 'class="run-job"' in page or "run-job" in page


@pytest.mark.parametrize("tab", ["search", "browse", "settings", "status"])
def test_no_script_calls_a_function_before_it_is_defined(client, tab):
    """Panels render inside {body}, which comes before the {SCRIPT} block that
    defines the page's functions. A panel that called one inline therefore threw
    ReferenceError on load.

    That is how the Jobs panel sat on "loading…" forever: its only invocation
    was that broken auto-call. The update panel had the identical bug but hid
    it, because its manual "Check again" button worked once the page finished
    loading -- so the same defect looked like two different problems.

    Panels must queue their startup call; the definitions block drains it.
    """
    html_text = client.get(f"/?tab={tab}").text
    for name in ("refreshJobs", "checkUpdates"):
        call = html_text.find(f"<script>{name}(")
        if call == -1:
            continue
        definition = html_text.find(f"async function {name}")
        assert definition != -1 and definition < call, (
            f"{name} is called at {call} but defined at {definition}: it does "
            "not exist yet. Queue it on window.__tpOnReady instead.")


def test_startup_queue_is_drained_after_the_definitions(client):
    """The queue is only useful if something empties it."""
    html_text = client.get("/?tab=settings").text
    assert "__tpOnReady" in html_text, "panels must queue their startup call"
    drain = html_text.find("(window.__tpOnReady || []).forEach")
    assert drain != -1, "nothing drains the startup queue"
    assert drain > html_text.find("async function refreshJobs"), (
        "the queue must drain after the functions it calls are defined")


def test_one_failing_startup_task_does_not_stop_the_others(client):
    """A panel whose endpoint is unavailable must not leave every other panel
    unstarted -- they share one queue."""
    html_text = client.get("/?tab=settings").text
    drain = html_text[html_text.find("(window.__tpOnReady || []).forEach"):]
    drain = drain[:400]
    assert "try {" in drain and "catch" in drain, (
        "draining must isolate each task, or the first failure ends startup")


def test_embedding_hint_does_not_suggest_a_command_that_cannot_work(client, monkeypatch):
    """`tracepaper embed` exits 1 when sentence-transformers is absent, so
    telling someone to run it on a keyword-only install sends them to an error
    and reads like the index is broken. The missing piece is the model, and the
    hint has to say so."""
    from tracepaper import embed

    monkeypatch.setattr(embed, "available", lambda: False)
    page = client.get("/?tab=status").text
    if "passages embedded" not in page:
        pytest.skip("no pending embeddings in this fixture")
    hint = page[page.index("passages embedded"):][:300]
    assert "install semantic search first" in hint
    assert "run <code>tracepaper embed</code>," not in hint


def test_embedding_hint_suggests_the_command_when_it_would_work(client, monkeypatch):
    from tracepaper import embed

    monkeypatch.setattr(embed, "available", lambda: True)
    page = client.get("/?tab=status").text
    if "passages embedded" not in page:
        pytest.skip("no pending embeddings in this fixture")
    hint = page[page.index("passages embedded"):][:300]
    assert "tracepaper embed" in hint


def test_status_is_cached_so_a_large_index_cannot_stall_the_page(client):
    """/api/status is ten full table scans; COUNT(*) over millions of passages
    has no shortcut in SQLite. On a real index that timed the request out and
    made a healthy server look broken. The figures are a dashboard, so a few
    seconds stale is invisible -- a page that renders beats a count exact to
    the row."""
    from tracepaper import api

    api._STATUS_CACHE["value"] = None
    first = client.get("/api/status").json()

    calls = []
    real = api._status_uncached

    def counting(conn):
        calls.append(1)
        return real(conn)

    api._status_uncached = counting
    try:
        for _ in range(5):
            assert client.get("/api/status").json() == first
        assert not calls, "repeat reads inside the TTL must not re-scan"
    finally:
        api._status_uncached = real
        api._STATUS_CACHE["value"] = None


def test_status_cache_expires(client, monkeypatch):
    """Stale forever would be worse than slow: the page has to catch up."""
    from tracepaper import api

    api._STATUS_CACHE["value"] = None
    client.get("/api/status")
    assert api._STATUS_CACHE["value"] is not None

    # Age the entry past the TTL and confirm the next read recomputes.
    api._STATUS_CACHE["at"] -= (api.STATUS_CACHE_SECONDS + 1)
    calls = []
    real = api._status_uncached

    def counting(conn):
        calls.append(1)
        return real(conn)

    api._status_uncached = counting
    try:
        client.get("/api/status")
        assert calls, "an expired entry must be recomputed"
    finally:
        api._status_uncached = real
        api._STATUS_CACHE["value"] = None


def test_a_running_backfill_is_visible_even_though_it_is_transient(monkeypatch):
    """A long embed backfill is usually a systemd-run transient unit, so it is
    not in UNITS -- the panel reported nothing running while the box sat at
    99% CPU. It must be shown, but never offered as a button: the panel cannot
    control a unit it did not define."""
    from tracepaper import jobs

    def fake_show(unit):
        if unit in jobs.WATCHED_UNITS:
            return {"LoadState": "loaded", "ActiveState": "activating",
                    "Result": "success", "ExecMainStartTimestamp": "now"}
        return {"LoadState": "loaded", "ActiveState": "inactive",
                "Result": "success", "ExecMainStartTimestamp": ""}

    monkeypatch.setattr(jobs, "_show", fake_show)
    monkeypatch.setattr(jobs, "_can_start", lambda unit: True)

    backfill = [j for j in jobs.status().jobs if j.unit in jobs.WATCHED_UNITS]
    assert backfill, "a running backfill must appear in the panel"
    assert backfill[0].running is True
    assert backfill[0].can_start is False, "it must not offer a Run button"


def test_a_finished_transient_unit_is_not_listed(monkeypatch):
    """Only show it while it is actually running; a finished transient unit
    lingers briefly with nothing useful to say."""
    from tracepaper import jobs

    monkeypatch.setattr(jobs, "_show", lambda unit: {
        "LoadState": "loaded", "ActiveState": "inactive", "Result": "success"})
    monkeypatch.setattr(jobs, "_can_start", lambda unit: True)
    assert not [j for j in jobs.status().jobs if j.unit in jobs.WATCHED_UNITS]


def test_systemd_result_strings_are_translated(client):
    """"last run: signal" is systemd's vocabulary, not the reader's."""
    page = client.get("/?tab=settings").text
    assert "'oom-kill': 'ran out of memory'" in page
    assert "'signal': 'stopped before it finished'" in page


def test_startup_does_not_block_on_loading_the_model():
    """preload() ran before the app was created, so a cold cache or a slow HF
    round-trip delayed binding the port -- the health check gave up at 60s and
    update.sh rolled back a good release. Serving keyword-only for the seconds
    a model takes to load beats not serving at all."""
    from tracepaper import api

    source = Path(api.__file__).read_text()
    body = source[source.index("def create_app"):]
    body = body[:body.index("\n    app = FastAPI")]
    assert "threading.Thread" in body, (
        "the model must load off the startup path")
    assert "embed.preload" in body


def test_a_failed_model_load_does_not_take_the_service_down(monkeypatch, cfg):
    """A model that cannot load must degrade to keyword search, not crash the
    app -- that is the whole NFR-9 guarantee."""
    from fastapi.testclient import TestClient

    from tracepaper import api, embed

    monkeypatch.setattr(embed, "available", lambda: True)
    monkeypatch.setattr(embed, "preload",
                        lambda model_id: (_ for _ in ()).throw(OSError("boom")))

    client = TestClient(api.create_app(cfg))
    assert client.get("/api/health").json()["ok"] is True


def test_search_results_link_to_the_file_not_the_json(client):
    """Clicking a result opened /api/items/<id> -- a JSON blob of confidence
    scores. The title should open the document; the scores stay reachable
    behind a "why" link, because that is a real question, just not the one a
    title click is asking."""
    page = client.get("/?q=invoice").text
    if 'class="hit"' not in page:
        pytest.skip("no hits in this fixture")
    assert "/file/" in page
    assert ">why</a>" in page


def test_serving_a_file_refuses_paths_outside_the_roots(client, cfg, monkeypatch):
    """The path comes from the index, but a stored uri is not a capability to
    read the whole filesystem -- if the roots change, or a row is wrong, the
    server must refuse rather than serve /etc/passwd."""
    from tracepaper import api

    conn = api.open_connection()
    try:
        conn.execute("INSERT INTO items (kind, uri, title, extraction_status) "
                     "VALUES ('document', '/etc/passwd', 'passwd', 'complete')")
        item_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
    finally:
        conn.close()

    response = client.get(f"/file/{item_id}")
    assert response.status_code == 403
    assert "outside the indexed roots" in response.json()["detail"]


def test_serving_a_file_returns_it_when_inside_a_root(cfg, nas):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from tracepaper import api

    client = TestClient(api.create_app(replace(cfg, roots=[str(nas)])))
    target = nas / "readable.txt"
    target.write_text("the irrigation solenoid valve")

    conn = api.open_connection()
    try:
        conn.execute("INSERT INTO items (kind, uri, title, mime, "
                     "extraction_status) VALUES ('document', ?, 'readable', "
                     "'text/plain', 'complete')", (str(target),))
        item_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
    finally:
        conn.close()

    response = client.get(f"/file/{item_id}")
    assert response.status_code == 200
    assert "irrigation solenoid" in response.text


def test_serving_a_missing_file_says_the_share_may_be_unmounted(cfg, nas):
    """An indexed file that is not there now is usually a mount problem, not a
    404 -- say so, because the two have very different fixes."""
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from tracepaper import api

    client = TestClient(api.create_app(replace(cfg, roots=[str(nas)])))

    conn = api.open_connection()
    try:
        conn.execute("INSERT INTO items (kind, uri, title, extraction_status) "
                     "VALUES ('document', ?, 'gone', 'complete')",
                     (str(nas / "vanished.txt"),))
        item_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
    finally:
        conn.close()

    response = client.get(f"/file/{item_id}")
    assert response.status_code == 404
    assert "mounted" in response.json()["detail"]


def test_settings_shows_what_is_indexed_and_what_is_skipped(client):
    """A note vault's bundled plugin JavaScript turned up in results and there
    was no way to see why: neither the excludes nor the understood formats were
    visible anywhere, so "why is this here" and "why is that missing" were
    equally unanswerable."""
    page = client.get("/?tab=settings&section=formats").text
    assert "What gets indexed" in page
    assert "Never indexed" in page
    assert ".obsidian" in page, "the exclude list must be visible"
    assert ".pdf" in page and ".heic" in page
    assert "Clean up the index" in page, (
        "excluding something later must offer a way to drop what is indexed")


def test_obsidian_internals_are_excluded():
    """Plugin JavaScript is minified code that matches half the English
    language and buries the notes it sits beside."""
    from tracepaper.config import DEFAULT_EXCLUDES

    for name in (".obsidian", ".stfolder", ".stversions", "site-packages"):
        assert name in DEFAULT_EXCLUDES, f"{name} should be skipped"


def test_excludes_have_no_duplicates():
    from collections import Counter

    from tracepaper.config import DEFAULT_EXCLUDES

    dupes = [name for name, count in Counter(DEFAULT_EXCLUDES).items() if count > 1]
    assert not dupes, f"duplicated excludes: {dupes}"


def test_how_it_works_page_matches_the_real_constants(client):
    """A docs page that quietly disagrees with the ranking it describes is
    worse than no docs page.

    These are LITERALS on purpose. Asserting against the same constants the
    page interpolates is worthless -- both move together, so the test can
    never fail; I checked, by changing RRF_K and watching it still pass.
    Pinning the published values means changing the ranking forces a
    deliberate look at what the docs promise.
    """
    from tracepaper.query import search as S

    assert S.RRF_K == 60, "k=60 is the constant from the SIGIR 2009 paper"
    assert S.RRF_WEIGHT_BM25 == 1.0
    assert S.RRF_WEIGHT_VECTOR == 0.8, "vectors must not outrank exact matches"
    assert S.MIN_VECTOR_SIMILARITY == 0.25

    page = client.get("/?tab=how").text
    assert "How a search is scored" in page
    assert "60 + keyword_rank" in page
    assert "0.8" in page and "0.25" in page
    # The worked example must be arithmetic on the real constants, not a
    # number typed into the prose.
    assert "0.015873" in page, "1.0/(60+3) must appear as computed"


def test_how_it_works_cites_its_sources(client):
    """The formula is published IR work; saying so is the difference between
    a citation and a number someone made up."""
    page = client.get("/?tab=how").text
    assert "SIGIR 2009" in page
    assert "Cormack" in page
    assert "Okapi at TREC-3" in page


def test_how_it_works_is_reachable_from_the_nav(client):
    page = client.get("/").text
    assert "tab=how" in page, "the docs page must be linked, not hidden"


def test_photo_results_are_a_thumbnail_grid(client, cfg, nas):
    """A list of filenames is unusable for photos -- the picture is the thing
    you recognise. And clicking one must open the photo, not its JSON."""
    page = client.get("/?q=photo").text
    if 'class="photos"' not in page:
        pytest.skip("no photo hits in this fixture")
    assert "/thumb/" in page
    assert 'loading="lazy"' in page, "a grid of originals would be enormous"


def test_no_document_title_links_to_the_json_api(client):
    """Every citation of a document should open the document. The only
    /api/items link left is the deliberate "why" affordance next to the score,
    which is asking a different question."""
    import re

    for tab in ("search", "browse", "status"):
        page = client.get(f"/?tab={tab}&q=invoice").text
        for match in re.finditer(r'<a href="/api/items/[^"]*"[^>]*>(.*?)</a>',
                                 page, re.S):
            assert match.group(1).strip() == "why", (
                f"a title still opens JSON on the {tab} tab: {match.group(0)!r}")


def test_how_it_works_links_to_the_papers(client):
    """A citation without a link is a dead end. These are the primary sources,
    open-access where one exists -- the ACM copies are paywalled, so they are
    deliberately not what is linked."""
    page = client.get("/?tab=how").text

    for url in ("cormack.uwaterloo.ca/cormacksigir09-rrf.pdf",
                "trec.nist.gov/pubs/trec3/papers/city.ps.gz",
                "arxiv.org/abs/1908.10084",
                "sqlite.org/fts5.html"):
        assert url in page, f"missing link: {url}"

    assert "dl.acm.org" not in page, "prefer the open-access copy"
    # External links must not hand the opener a window reference.
    import re
    for match in re.finditer(r'<a href="https?://[^>]*>', page):
        assert 'rel="noopener"' in match.group(0), match.group(0)


def test_ranking_doc_and_ui_cite_the_same_sources():
    """Two places describing one formula drift apart unless something checks."""
    from pathlib import Path

    doc = (Path(__file__).resolve().parents[1] / "docs" / "04-ranking.md").read_text()
    for url in ("cormacksigir09-rrf.pdf", "city.ps.gz", "1908.10084",
                "all-MiniLM-L6-v2"):
        assert url in doc, f"docs/04-ranking.md is missing {url}"


def test_search_api_groups_passages_under_their_document(cfg, conn, nas):
    """One document is one hit, with its other pages attached."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from tracepaper.api import create_app

    (nas / "manual.txt").write_text("\n\n".join(
        f"Section {n}. Setting up the 3d printer. " +
        f"Bed levelling for the 3d printer is described here. " * 30
        for n in range(1, 12)))
    Scanner(conn, cfg).scan(nas)
    Indexer(conn, cfg).run_pending()

    data = TestClient(create_app(cfg)).get(
        "/api/search", params={"q": "3d printer", "semantic": False}).json()

    assert len(data["hits"]) == 1, "the manual is one document, not eleven"
    hit = data["hits"][0]
    assert hit["passage_count"] > 1
    assert hit["more"], "its other matching pages must still be reachable"
    assert all("page" in m and "snippet" in m for m in hit["more"])


def test_search_page_links_each_passage_to_its_page(cfg, conn, nas):
    """The expander's links open the PDF at the matching page."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from tracepaper.api import create_app

    (nas / "guide.txt").write_text("\n\n".join(
        f"Part {n}. Calibrating the 3d printer nozzle. " +
        f"Nozzle height on this 3d printer matters. " * 30
        for n in range(1, 12)))
    Scanner(conn, cfg).scan(nas)
    Indexer(conn, cfg).run_pending()

    html = TestClient(create_app(cfg)).get(
        "/", params={"q": "3d printer", "semantic": "false"}).text

    assert "<details class=\"more\"" in html, "grouped pages should be shown"
    assert "matching passages</summary>" in html


def test_settings_offers_the_controls_instead_of_terminal_commands(client):
    """Settings was half documentation. The things it describes are now doable."""
    page = client.get("/?tab=settings&section=search").text

    assert "Folder rules" in page, "rules must be settable from the page"
    assert "Clean up the index" in page, "prune must not need a terminal"
    assert 'id="rule_add"' in page
    assert 'id="prune_apply"' in page
    # The old instruction to go and run a command is gone.
    assert "tracepaper prune</code> (add" not in page


def test_search_results_carry_ranking_feedback_controls(client):
    page = client.get("/", params={"q": "wages", "semantic": "false"}).text
    assert 'class="thumb"' in page
    assert 'data-signal="up"' in page and 'data-signal="down"' in page


def test_no_mode_is_called_everything(client):
    """"Everything" never included code, so the label was a lie."""
    page = client.get("/?tab=search").text
    assert ">Everything<" not in page
    assert ">My documents<" in page


def test_feedback_endpoint_records_and_clears(client, populated):
    item_id = populated.execute("SELECT id FROM items LIMIT 1").fetchone()["id"]
    headers = {"X-Tracepaper-Request": "1"}

    posted = client.post("/api/feedback", headers=headers, json={
        "query": "the Wages", "item_id": item_id, "signal": "up"})
    assert posted.status_code == 200
    # Stored against the normalised query, so rephrasing still benefits.
    assert posted.json()["normalized"] == "wages"

    cleared = client.request("DELETE", "/api/feedback", headers=headers,
                             params={"query": "wages"})
    assert cleared.json()["removed"] == 1


def test_writes_refuse_a_cross_site_request(client, populated):
    """The same guard the update and job endpoints use."""
    item_id = populated.execute("SELECT id FROM items LIMIT 1").fetchone()["id"]

    without_header = client.post("/api/feedback", json={
        "query": "wages", "item_id": item_id, "signal": "up"})
    assert without_header.status_code == 403

    cross_site = client.post(
        "/api/rules",
        headers={"X-Tracepaper-Request": "1", "Sec-Fetch-Site": "cross-site"},
        json={"prefix": "/nas", "rule": "code"})
    assert cross_site.status_code == 403


def test_adding_a_rule_reports_how_many_files_it_covers(client):
    """A rule matching nothing is a typo, and must say so immediately."""
    response = client.post("/api/rules",
                           headers={"X-Tracepaper-Request": "1"},
                           json={"prefix": "/no/such/place", "rule": "code"})
    assert response.status_code == 200
    assert response.json()["items"] == 0


def test_prune_preview_is_a_get_and_changes_nothing(client, populated):
    before = populated.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
    assert client.get("/api/prune").status_code == 200
    after = populated.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
    assert after == before


@pytest.mark.parametrize("tab", ["search", "browse", "status", "settings", "how"])
def test_no_em_dashes_anywhere_in_the_ui(client, tab):
    """A house style rule, enforced on the rendered page.

    Checking the source would miss text built at runtime and would also flag
    comments, which nobody reads. This checks what is actually served.
    """
    page = client.get(f"/?tab={tab}").text
    assert "—" not in page, (
        f"em dash in the {tab} tab: "
        f"{page[max(0, page.find(chr(0x2014)) - 70):page.find(chr(0x2014)) + 70]!r}")


def _without_quoted_text(page: str) -> str:
    """The page minus anything quoted from the user's own files.

    Snippets appear as a div in a result and as a span inside the grouped
    pages expander, so both shapes have to go.
    """
    page = re.sub(r'<div class="snip">.*?</div>', "", page, flags=re.S)
    return re.sub(r'<span class="snip">.*?</span>', "", page, flags=re.S)


def test_no_em_dashes_in_rendered_search_results(client):
    """Results carry headings and messages built per query, so check those too.

    Snippets are excluded deliberately. They are quoted from the user's own
    files, and OCR of a passport scan really does produce em dashes. Rewriting
    what a document says, to satisfy a house style rule about what Tracepaper
    says, would be the worse bug.
    """
    for query in ("wages", "nothing will match this zzz"):
        page = client.get("/", params={"q": query, "semantic": "false"}).text
        assert "—" not in _without_quoted_text(page), \
            f"em dash in results for {query!r}"


def test_an_image_result_shows_its_thumbnail(cfg, conn, nas):
    """A scanned image ranks via OCR, so it appears among the documents.

    It was rendering as a bare filename and an empty snippet: the picture,
    which is the only useful part of an image result, was missing.
    """
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from tracepaper.api import create_app

    # A photo with searchable text, the way an OCR'd scan arrives.
    conn.execute(
        "INSERT INTO items (id, kind, uri, title, extraction_status) "
        "VALUES (1, 'photo', ?, 'digital-passport.jpg', 'complete')",
        (str(nas / "digital-passport.jpg"),))
    conn.execute("INSERT INTO passages (id, item_id, version, ordinal, text) "
                 "VALUES (1, 1, 1, 0, 'passport number and expiry')")
    conn.execute("INSERT INTO passages_fts_src (id, text, title) "
                 "VALUES (1, 'passport number and expiry', 'digital-passport.jpg')")
    conn.execute("INSERT INTO passages_fts (rowid, text, title) "
                 "VALUES (1, 'passport number and expiry', 'digital-passport.jpg')")
    conn.commit()

    page = TestClient(create_app(cfg)).get(
        "/", params={"q": "passport", "semantic": "false"}).text

    assert "digital-passport.jpg" in page, "the image should rank"
    assert "/thumb/1" in page, "an image result must show its picture"
    assert 'class="hit with-thumb"' in page


@pytest.mark.parametrize("section,expected", [
    ("general", "Updates"),
    ("search", "Folder rules"),
    ("storage", "Index location"),
    ("formats", "What gets indexed"),
])
def test_each_settings_section_renders(client, section, expected):
    page = client.get(f"/?tab=settings&section={section}").text
    assert expected in page
    assert 'class="subnav"' in page, "every section needs the section links"


def test_settings_opens_on_the_things_you_press(client):
    """Updates and Jobs were at the bottom, behind a long reference table.

    Updating meant scrolling past everything else on the page, so they are
    what Settings now opens on.
    """
    page = client.get("/?tab=settings").text

    assert "Updates" in page and "Jobs" in page
    # The long reference table is not on the landing section.
    assert "Found by name only" not in page


def test_an_unknown_section_falls_back_rather_than_erroring(client):
    """A stale bookmark or a typo should not produce an empty page."""
    page = client.get("/?tab=settings&section=nonsense")
    assert page.status_code == 200
    assert "Updates" in page.text


def test_no_settings_section_is_a_long_scroll(client):
    """The reason for splitting the page: no section should be huge.

    The formats section is the reference material and is allowed to be the
    longest, but the sections with controls on them must stay short.
    """
    import re

    for section in ("general", "search", "storage"):
        page = client.get(f"/?tab=settings&section={section}").text
        body = re.search(r"<main>.*</main>", page, re.S).group(0)
        assert len(body) < 6000, (
            f"the {section} section is {len(body)} bytes; it should not need "
            f"a long scroll")


# --- Browse: folders and files, not a field dump -------------------------

@pytest.fixture
def browsable(conn, cfg, nas):
    """A tree with documents on one side and code on the other."""
    (nas / "Personal").mkdir(parents=True)
    (nas / "Code" / "src").mkdir(parents=True)
    (nas / "Personal" / "payslip.txt").write_text(
        "Pay Date: 2022-06-30\nGross Salary: 91500\n")
    (nas / "Code" / "src" / "app.py").write_text("onclick: () => void;\n")
    (nas / "Code" / "src" / "conf.json").write_text('{"winrt": "x"}\n')

    from dataclasses import replace as dc_replace

    from tracepaper import api as api_module
    from tracepaper.index.indexer import Indexer
    from tracepaper.scan.scanner import Scanner

    cfg = dc_replace(cfg, roots=(nas,))
    Scanner(conn, cfg).scan(nas)
    Indexer(conn, cfg).run_pending()
    api_module.set_config(cfg)
    return cfg


def test_browse_lists_folders(browsable, nas, conn):
    from tracepaper.web import _browse_tab

    page = _browse_tab(conn, "", str(nas))
    assert 'class="fb"' in page, "the listing is a file-browser table"
    assert ">Personal<" in page and ">Code<" in page
    # Folders are visibly folders, not just rows of text.
    assert "\U0001F4C1" in page, "folder rows need a folder icon"
    assert "2 folders" in page, "the count line replaces the section headings"


def test_browse_hides_code_files_until_asked(browsable, nas, conn):
    from tracepaper.web import _browse_tab

    page = _browse_tab(conn, "", str(nas / "Code" / "src"))
    assert "app.py" not in page
    assert "code file(s) hidden" in page

    shown = _browse_tab(conn, "", str(nas / "Code" / "src"), show_code=True)
    assert "app.py" in shown and "conf.json" in shown


def test_browse_field_list_excludes_code_derived_fields(browsable, nas, conn):
    """The reported problem: Browse led with namespace_winrt and onclick."""
    from tracepaper.web import _browse_tab

    page = _browse_tab(conn, "", str(nas))

    assert "Pay date" in page, "real document fields must still be listed"
    assert "winrt" not in page, "a field only found in code must not be listed"


def test_browse_field_names_are_readable_but_keep_the_raw_key(browsable, nas, conn):
    from tracepaper.web import _browse_tab

    page = _browse_tab(conn, "", str(nas))
    assert "Gross salary" in page, "shown in words"
    assert "gross_salary" in page, "the raw key is what you type into search"


def test_browse_refuses_a_path_outside_the_roots(browsable, conn):
    """The URL is user input; it must not become a filesystem walk."""
    from tracepaper.web import _browse_tab

    page = _browse_tab(conn, "", "/etc")
    assert "not indexed" in page
    assert "passwd" not in page


def test_a_result_path_links_into_browse(browsable, conn):
    from tracepaper.web import render_page

    page = render_page(conn, query="pay date", tab="search", semantic=False)
    assert 'tab=browse&amp;path=' in page, \
        "the folder under a result should be clickable"


def test_browse_shows_size_and_date_like_a_file_manager(browsable, nas, conn):
    """A listing without size or a date is a list of names, not a browser."""
    from tracepaper.web import _browse_tab

    page = _browse_tab(conn, "", str(nas / "Personal"))

    assert ">Size<" in page and ">Modified<" in page
    # Folder rows carry the same columns as file rows, which is the point of
    # putting both in one table.
    assert 'class="fb"' in page


def test_browse_rolls_folder_totals_up(browsable, nas, conn):
    """A folder's size and date come from what is inside it."""
    from tracepaper import browse as browse_module

    view = browse_module.listing(conn, [str(nas)], str(nas))
    folders = {f["name"]: f for f in view["folders"]}

    assert folders, "expected folders directly under the root"
    for folder in folders.values():
        assert "size_bytes" in folder and "modified_at" in folder
        assert "subfolders" in folder
    assert any(f["size_bytes"] > 0 for f in folders.values()), (
        "a folder holding indexed files should report a non-zero size")


def test_file_icons_distinguish_types():
    from tracepaper.web import _file_icon

    pdf = _file_icon("statement.pdf")
    photo = _file_icon("IMG_4821.jpg")
    sheet = _file_icon("budget.xlsx")

    assert len({pdf, photo, sheet}) == 3, "each type needs its own glyph"
    # Code wins over the extension: the whole point is spotting build output
    # in a folder that also holds real documents.
    assert _file_icon("LICENSE", is_code=True) != _file_icon("LICENSE")


def test_dates_render_readably_and_never_raise():
    from tracepaper.web import _short_date

    assert _short_date("2026-09-14T03:59:28") == "14 Sep 2026"
    assert _short_date(None) == ""
    # Whatever the scanner stored, a listing must not blow up on it.
    for bad in ("", "not-a-date", "0000", 12345):
        _short_date(bad)


def test_every_setting_the_api_accepts_has_a_field_in_the_ui(conn):
    """A setting saveable by the API but absent from the UI is invisible.

    This existed for llm_endpoint and llm_model: the API took them, the form
    never sent them, and there was no input to type them into. The user was
    told to "go to Settings" for a control that was not there.
    """
    from tracepaper.web import _captions_panel

    page = _captions_panel(conn)

    for field in ("llm_enabled", "llm_endpoint", "vlm_model"):
        assert f'id="{field}"' in page, f"{field} has no input in the UI"


def test_captions_panel_reports_progress(conn):
    from tracepaper.web import _captions_panel

    page = _captions_panel(conn)

    assert "photos" in page and "described" in page


def test_captions_settings_survive_a_save(tmp_path):
    """Saving the model must actually persist, including the vision model."""
    from tracepaper import settings as settings_module
    from tracepaper.config import Config

    config_file = tmp_path / "tracepaper.toml"
    ok, problems = settings_module.save(
        config_file, roots=[str(tmp_path)], db_path=str(tmp_path / "i.db"),
        llm_enabled=True, llm_endpoint="http://mac.studio.local:11434",
        vlm_model="gemma4:e4b", validate_paths=False)

    assert ok, problems
    loaded = Config.load(config_file)
    assert loaded.llm_enabled is True
    assert loaded.llm_endpoint == "http://mac.studio.local:11434"
    # vlm_model is the one captions actually use, and it was not saveable.
    assert loaded.vlm_model == "gemma4:e4b"


def test_llm_test_endpoint_reports_an_unreachable_address(client):
    """Finding out weeks later via an empty caption count is the failure this
    button exists to prevent."""
    result = client.post("/api/llm/test",
                         json={"endpoint": "http://127.0.0.1:9"},
                         headers={"X-Tracepaper-Request": "1"}).json()

    assert result["ok"] is False
    assert "127.0.0.1:9" in result["error"]


def test_saving_one_settings_form_does_not_wipe_another(populated, cfg, tmp_path,
                                                       monkeypatch):
    """Settings is several forms, each sending only the fields it owns.

    The captions form sends no roots. Defaulting them to an empty list failed
    validation with "At least one documents folder is required", so captions
    could not be turned on at all, and a caller sending roots=null would have
    erased the folder list outright.
    """
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from tracepaper import settings as settings_module
    from tracepaper.api import create_app
    from tracepaper.config import Config

    config_file = tmp_path / "tracepaper.toml"
    monkeypatch.setattr(settings_module, "find_config", lambda: config_file)
    configured = replace(cfg, roots=[tmp_path])
    client = TestClient(create_app(configured))

    result = client.post(
        "/api/settings",
        json={"llm_enabled": True,
              "llm_endpoint": "http://192.168.0.133:11434",
              "vlm_model": "gemma4:e4b"},
        headers={"X-Tracepaper-Request": "1"}).json()

    assert result.get("ok") is True, result.get("problems")
    # The folder list the captions form never sent must still be there.
    saved = Config.load(config_file)
    assert [str(r) for r in saved.roots] == [str(tmp_path)]
    assert saved.vlm_model == "gemma4:e4b"


def test_settings_save_writes_the_file_the_service_reads(populated, cfg,
                                                         tmp_path):
    """A guessed relative path resolves against the working directory.

    On the container that is /opt/tracepaper, which the service user cannot
    write, so saving raised PermissionError. Worse than the crash: had it
    succeeded it would have written a second config the service never reads,
    and the save would have silently done nothing.
    """
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from tracepaper.api import create_app
    from tracepaper.config import Config

    real_config = tmp_path / "etc" / "tracepaper.toml"
    real_config.parent.mkdir(parents=True)
    real_config.write_text("[index]\n")

    configured = replace(cfg, roots=[tmp_path], source_path=real_config)
    client = TestClient(create_app(configured))

    result = client.post(
        "/api/settings", json={"vlm_model": "gemma4:e4b"},
        headers={"X-Tracepaper-Request": "1"}).json()

    assert result.get("ok") is True, result.get("problems")
    assert result["config_file"] == str(real_config)
    assert Config.load(real_config).vlm_model == "gemma4:e4b"


def test_config_remembers_where_it_was_loaded_from(tmp_path):
    from tracepaper.config import Config

    path = tmp_path / "custom.toml"
    path.write_text('[index]\ndb_path = "/tmp/x.db"\n')

    assert Config.load(path).source_path == path
    assert Config.load(None).source_path is None


def test_an_unwritable_config_explains_itself(populated, cfg, tmp_path):
    """The container ships the config as 640 root:tracepaper, so this is the
    likely real-world failure. A 500 tells the user nothing."""
    import os
    from dataclasses import replace

    import pytest
    from fastapi.testclient import TestClient

    from tracepaper.api import create_app

    if os.geteuid() == 0:
        pytest.skip("root can write anything")

    locked = tmp_path / "locked"
    locked.mkdir()
    config_file = locked / "tracepaper.toml"
    config_file.write_text("[index]\n")
    locked.chmod(0o500)                      # no write on the directory
    try:
        configured = replace(cfg, roots=[tmp_path], source_path=config_file)
        client = TestClient(create_app(configured))
        result = client.post(
            "/api/settings", json={"vlm_model": "gemma4:e4b"},
            headers={"X-Tracepaper-Request": "1"}).json()

        assert result["ok"] is False
        assert "Could not write" in result["problems"][0]
        assert str(config_file) in result["problems"][0]
    finally:
        locked.chmod(0o700)

"""REST API, MCP tools and Tier 2 evidence (FR-12, requirements §3)."""

from __future__ import annotations

import json
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

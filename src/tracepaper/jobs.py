"""Triggering the background units from the app (scan, enrich, backup).

The same split as `updates`: this module reads systemd state and asks it to
start a unit. It never does the work itself. Scanning walks a read-only NAS
mount and writes to the index; running that inside a web worker would block a
request for hours and hold a SQLite connection open the whole time.

Each unit is oneshot, so `systemctl start` would block until it finished --
hence `--no-block` everywhere. The UI polls `status()` to follow progress.

Permission comes from the same polkit rule shape as updates: one action, one
unit, one verb, one user. A unit missing from POLKIT_UNITS cannot be started
by the app no matter what the caller asks for.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

TIMEOUT_SECONDS = 10

# The units the app may trigger, keyed by the name the API accepts. This is an
# allowlist, not a lookup: a name absent here is refused before it reaches
# systemd, so a bad request can never turn into an arbitrary unit start.
UNITS = {
    "scan": "tracepaper-scan.service",
    "enrich": "tracepaper-enrich.service",
    "backup": "tracepaper-backup.service",
}

LABELS = {
    "scan": "Scan for new and changed files",
    "enrich": "Enrich photos and documents",
    "backup": "Back up corrections and notes",
}


@dataclass
class JobStatus:
    name: str
    unit: str
    label: str
    running: bool = False
    can_start: bool = False
    # systemd's own words: active/inactive/failed, and the result of the last
    # run. Passed through rather than reinterpreted, so the UI can say "failed"
    # without this module having to model every systemd state.
    state: str = "unknown"
    result: str = ""
    last_run: str = ""
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "unit": self.unit,
            "label": self.label,
            "running": self.running,
            "can_start": self.can_start,
            "state": self.state,
            "result": self.result,
            "last_run": self.last_run,
            "detail": self.detail,
        }


@dataclass
class JobsStatus:
    jobs: list[JobStatus] = field(default_factory=list)
    available: bool = False
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "available": self.available,
            "detail": self.detail,
            "jobs": [job.as_dict() for job in self.jobs],
        }


def _run(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True,
                            timeout=TIMEOUT_SECONDS, check=True)
    return result.stdout.strip()


def _show(unit: str) -> dict[str, str]:
    """`systemctl show` for one unit, as a dict.

    `show` is used rather than `is-active` because it answers every question in
    a single call and, unlike `status`, exits 0 for an inactive or failed unit
    -- so a failed unit reports its failure instead of raising.
    """
    try:
        out = _run(["systemctl", "show", unit, "--no-pager",
                    "--property=ActiveState,SubState,Result,"
                    "ExecMainStartTimestamp,LoadState"])
    except (subprocess.SubprocessError, OSError):
        return {}
    values: dict[str, str] = {}
    for line in out.splitlines():
        key, _, value = line.partition("=")
        if key:
            values[key] = value
    return values


def _can_start(unit: str) -> bool:
    """Whether polkit will actually let this user start the unit.

    `--dry-run` asks systemd to authorise and plan the job without running it,
    so this is a real permission check. A button that appears and then fails
    with an authentication error is worse than one that never appears.
    """
    try:
        _run(["systemctl", "start", "--dry-run", "--no-block", unit])
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def status() -> JobsStatus:
    """Live state of every triggerable unit.

    Off a systemd host -- a laptop checkout, a test -- nothing is loaded and
    `available` stays false, so the UI omits the controls rather than showing
    buttons that cannot work.
    """
    result = JobsStatus()
    for name, unit in UNITS.items():
        values = _show(unit)
        if not values or values.get("LoadState") not in ("loaded", None):
            continue
        active = values.get("ActiveState", "unknown")
        started = values.get("ExecMainStartTimestamp", "") or ""
        job = JobStatus(
            name=name,
            unit=unit,
            label=LABELS.get(name, name),
            # A oneshot unit is "activating" for its whole run: ExecStart has
            # not returned yet. Treating only "active" as running would report
            # a multi-hour scan as idle.
            running=active in ("active", "activating", "reloading"),
            state=active,
            result=values.get("Result", ""),
            last_run=started,
        )
        job.can_start = (not job.running) and _can_start(unit)
        result.jobs.append(job)

    result.available = bool(result.jobs)
    if not result.available:
        result.detail = ("No tracepaper units are loaded here, so jobs cannot "
                         "be started from the UI. This is normal outside the "
                         "container.")
    return result


def start(name: str) -> tuple[bool, str]:
    """Ask systemd to run one job. Returns (started, message)."""
    unit = UNITS.get(name)
    if unit is None:
        return False, f"unknown job {name!r}."

    values = _show(unit)
    if not values or values.get("LoadState") not in ("loaded", None):
        return False, (f"{unit} is not installed here, so it cannot be started "
                       "from the UI.")
    if values.get("ActiveState") in ("active", "activating", "reloading"):
        # Starting a running oneshot is a no-op in systemd, so reporting it as
        # started would be a lie the UI then shows as a fresh run.
        return False, f"{LABELS.get(name, name)} is already running."
    if not _can_start(unit):
        return False, (f"this user is not permitted to start {unit}. Run "
                       f"`systemctl start {unit}` in the container, or "
                       "reinstall the polkit rule.")
    try:
        # --no-block: these are oneshot units, so start would otherwise wait
        # for the whole job -- hours, for a cold scan -- and the HTTP response
        # would never be sent.
        _run(["systemctl", "start", "--no-block", unit])
    except (subprocess.SubprocessError, OSError) as exc:
        return False, f"could not start {unit}: {exc}"
    return True, f"{LABELS.get(name, name)} started."

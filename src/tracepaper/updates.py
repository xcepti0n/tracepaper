"""Checking for updates, and asking systemd to apply one.

The division of labour is the whole point: this module only ever *reads* git
state and *asks systemd* to run the update. It never pulls, installs or
restarts. Those need root, and the service deliberately does not have it --
tracepaper.service runs as an unprivileged user with an empty capability
bounding set. The privileged half lives in tracepaper-update.service, a
separate unit that root owns.

So the worst anyone reaching this endpoint can do is make the machine install
the code already published at the configured remote. That is a real capability
and not nothing, but it is far smaller than "run arbitrary commands as root",
which is what putting the update logic here would have meant.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Every one of these shells out to git or systemd. A hung network call must not
# hold an HTTP worker open indefinitely.
TIMEOUT_SECONDS = 30

UPDATE_UNIT = "tracepaper-update.service"
APP_DIR = Path("/opt/tracepaper")


@dataclass
class Commit:
    sha: str
    subject: str

    def as_dict(self) -> dict:
        return {"sha": self.sha, "short": self.sha[:7], "subject": self.subject}


@dataclass
class UpdateStatus:
    supported: bool = False
    can_apply: bool = False
    current: Commit | None = None
    behind: int = 0
    commits: list[Commit] = field(default_factory=list)
    branch: str = ""
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "supported": self.supported,
            "can_apply": self.can_apply,
            "current": self.current.as_dict() if self.current else None,
            "behind": self.behind,
            "commits": [c.as_dict() for c in self.commits],
            "branch": self.branch,
            "reason": self.reason,
        }


def app_dir() -> Path:
    """Read per call rather than captured at import, so tests can point it
    elsewhere without a module-reload trick."""
    return APP_DIR


def _run(args: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(
        args, cwd=str(cwd) if cwd else None, capture_output=True,
        text=True, timeout=TIMEOUT_SECONDS, check=True)
    return result.stdout.strip()


def _git(args: list[str], cwd: Path) -> str:
    return _run(["git", *args], cwd=cwd)


def can_apply() -> bool:
    """Whether this process can actually trigger an update.

    Two things must be true and both are worth checking. The unit has to exist,
    and polkit has to permit *this user* to start it -- the app runs as
    `tracepaper`, and `systemctl start` is privileged. Checking only for the
    file would light up a button that then fails with an authentication error,
    which is worse than not offering it at all.

    `--dry-run` asks systemd to authorise and plan the job without running it,
    so this is a real permission check rather than a guess.
    """
    installed = any(
        Path(d, UPDATE_UNIT).exists()
        for d in ("/etc/systemd/system", "/lib/systemd/system",
                  "/usr/lib/systemd/system"))
    if not installed:
        return False
    try:
        _run(["systemctl", "start", "--dry-run", "--no-block", UPDATE_UNIT])
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def check_local() -> UpdateStatus:
    """Local git state only -- no network, no fetch.

    The settings page renders this on load, and a page load must not block on
    `git fetch`: it is slow on a good connection and hangs on a bad one.
    Comparing against the remote is what `check()` does, when the user asks.
    """
    status = UpdateStatus()
    directory = app_dir()

    if not (directory / ".git").exists():
        status.reason = (
            f"{directory} is not a git checkout — this install was copied in "
            "rather than cloned, so there is no remote to update from.")
        return status

    status.supported = True
    try:
        status.current = Commit(_git(["rev-parse", "HEAD"], directory),
                                _git(["log", "-1", "--format=%s"], directory))
        status.branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], directory)
    except (subprocess.SubprocessError, OSError) as exc:
        status.supported = False
        status.reason = f"could not read local git state: {exc}"
        return status

    status.can_apply = can_apply()
    return status


def check() -> UpdateStatus:
    """Compare the checkout against its upstream.

    `git fetch` is a network call, so this is not something to poll. The UI
    calls it when the user asks.
    """
    status = UpdateStatus()
    directory = app_dir()

    if not (directory / ".git").exists():
        status.reason = (
            f"{directory} is not a git checkout — this install was copied in "
            "rather than cloned, so there is no remote to update from.")
        return status

    status.supported = True

    try:
        status.current = Commit(_git(["rev-parse", "HEAD"], directory),
                                _git(["log", "-1", "--format=%s"], directory))
        status.branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], directory)
    except (subprocess.SubprocessError, OSError) as exc:
        status.supported = False
        status.reason = f"could not read local git state: {exc}"
        return status

    # ls-remote, not fetch. A fetch WRITES -- FETCH_HEAD, new objects, a lock
    # file -- and the service cannot write to its own code: the tree is
    # root-owned precisely so a compromised service cannot rewrite what it runs
    # next. Fetching as this user fails with a generic transport error (255)
    # that hides the permission problem underneath.
    #
    # ls-remote asks the remote what it has and writes nothing, which is what a
    # read-only check should do anyway. The privileged half still fetches for
    # real, as root, inside tracepaper-update.service.
    try:
        listing = _git(["ls-remote", "origin",
                        f"refs/heads/{status.branch}"], directory)
    except (subprocess.SubprocessError, OSError) as exc:
        # Offline is not worth failing on -- report what is known locally.
        status.reason = f"could not reach the remote: {exc}"
        status.can_apply = can_apply()
        return status

    target = listing.split("\t")[0].strip() if listing else ""
    if not target:
        status.reason = f"the remote has no branch {status.branch}"
        status.can_apply = can_apply()
        return status

    if target != status.current.sha:
        # How far ahead the remote is, described from what is available locally.
        # The commits themselves may not have been fetched yet, in which case
        # only the count is known -- which is still the useful part.
        try:
            log = _git(["log", "--format=%H%x1f%s",
                        f"{status.current.sha}..{target}"], directory)
            for line in log.splitlines():
                sha, _, subject = line.partition("\x1f")
                status.commits.append(Commit(sha, subject))
            status.behind = len(status.commits)
        except (subprocess.SubprocessError, OSError):
            # The remote is ahead but those objects have never been fetched, so
            # their subjects cannot be read without writing to the repo -- which
            # is exactly what this check must not do.
            #
            # Reporting the count honestly beats inventing one: "an update is
            # waiting, applying it will show you what changed" is true and
            # useful, while a fabricated list would not be.
            status.behind = 1
            status.commits = []
            status.reason = (
                "An update is available. The details are not readable until it "
                "is applied, because checking must not write to the checkout.")

    status.can_apply = can_apply()
    return status


def apply() -> tuple[bool, str]:
    """Ask systemd to run the update. Returns (started, message).

    `--no-block` because the update restarts this very service: waiting for the
    job to finish would mean waiting for our own process to be killed, and the
    HTTP response would never be sent.
    """
    if not can_apply():
        return False, (
            f"{UPDATE_UNIT} is not installed or this user is not permitted to "
            "start it, so the server cannot apply updates itself. Run "
            "`systemctl start tracepaper-update` in the container.")
    try:
        _run(["systemctl", "start", "--no-block", UPDATE_UNIT])
    except (subprocess.SubprocessError, OSError) as exc:
        return False, f"could not start {UPDATE_UNIT}: {exc}"
    return True, (
        "Update started. The service restarts as part of it, so this page will "
        "be briefly unavailable. Follow it with "
        "`journalctl -u tracepaper-update -f`.")

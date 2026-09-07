"""Deployment artefacts, checked against the target's constraints.

These files cannot be exercised by running the app: a systemd unit, an fstab
line and a container's mount namespace are only exercised on the host. The
directives that break them are *valid systemd that works on bare metal*, so no
syntax check and no local run will ever find them -- which is precisely why
they need tests.

Every assertion here corresponds to a failure that actually happens on an
unprivileged Proxmox LXC.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
UNITS = sorted(DEPLOY.glob("*.service"))
TIMERS = sorted(DEPLOY.glob("*.timer"))
SCRIPTS = sorted(DEPLOY.glob("*.sh"))

# Every one of these needs to remount /proc, which an unprivileged container is
# not permitted to do. Present, they do not harden the service -- they stop it
# starting at all, with status=226/NAMESPACE and a restart loop.
NAMESPACE_DIRECTIVES = [
    "ProtectSystem", "PrivateTmp", "PrivateDevices", "ProtectHome",
    "ProtectProc", "ProtectKernelTunables", "ProtectKernelModules",
    "ProtectControlGroups", "PrivateUsers", "ReadWritePaths",
]


def _directives(unit: Path) -> list[tuple[int, str]]:
    """(line number, directive) for each assignment, ignoring comments."""
    found = []
    for number, line in enumerate(unit.read_text().splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        found.append((number, stripped.split("=", 1)[0].strip()))
    return found


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_no_mount_namespace_directives(unit: Path):
    """status=226/NAMESPACE. The single most likely way this deploy breaks."""
    offenders = [f"{unit.name}:{n} {d}"
                 for n, d in _directives(unit) if d in NAMESPACE_DIRECTIVES]
    assert not offenders, (
        "these need a mount namespace an unprivileged LXC cannot create, so the "
        "unit will not start at all:\n  " + "\n  ".join(offenders))


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_no_restrict_address_families(unit: Path):
    """Blocks AF_NETLINK, so interface enumeration fails after the port binds.

    The process reports its own success and then exits.
    """
    assert not [d for _, d in _directives(unit) if d == "RestrictAddressFamilies"], (
        f"{unit.name}: RestrictAddressFamilies also blocks AF_NETLINK. Bind "
        "address is the control that matters.")


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_start_limit_is_in_unit_section(unit: Path):
    """In [Service] these are silently ignored -- crash-loop protection off."""
    section = None
    for line in unit.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped
        elif stripped.startswith(("StartLimitBurst", "StartLimitIntervalSec")):
            assert section == "[Unit]", (
                f"{unit.name}: {stripped.split('=')[0]} is in {section}; systemd "
                "ignores it outside [Unit] and disables crash-loop protection "
                "with no error.")


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_execstart_binary_matches_installed_path(unit: Path):
    """203/EXEC is a wrong ExecStart path. It must match what the installer builds."""
    text = unit.read_text()
    for match in re.finditer(r"^ExecStart=(\S+)", text, re.MULTILINE):
        binary = match.group(1)
        assert binary.startswith("/opt/tracepaper/.venv/bin/"), (
            f"{unit.name}: ExecStart={binary} is not the path the installer "
            "creates; systemd would fail with 203/EXEC.")


@pytest.mark.parametrize("unit", UNITS + TIMERS, ids=lambda p: p.name)
def test_unit_is_referenced_by_the_installer(unit: Path):
    """A unit the installer never copies is a unit that does not exist on the host."""
    installer = (DEPLOY / "proxmox-install.sh").read_text()
    assert unit.name in installer, (
        f"{unit.name} exists in deploy/ but the installer never installs it.")


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_scripts_set_errexit_with_inherited_trap(script: Path):
    """Without -E an ERR trap is not inherited by functions, so a failure inside
    one exits silently and leaves a half-built container behind."""
    assert re.search(r"^set -Eeuo pipefail", script.read_text(), re.MULTILINE), (
        f"{script.name} must use `set -Eeuo pipefail`; the -E is what makes the "
        "cleanup trap fire for failures inside functions.")


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_no_bare_trailing_conditional(script: Path):
    """`[[ test ]] && cmd` evaluates to the test's status when the test is false.

    Under `set -e` that aborts the script, and with an ERR trap installed it
    destroys a container that installed perfectly. It is invisible in review and
    only fires on the branch where the test happens to be false -- so this bans
    the shape outright rather than trying to decide which uses are reachable.

    The safe forms are explicit: `|| true` swallows the status, and a body that
    exits or returns never falls through to it.
    """
    problems = []
    for number, line in enumerate(script.read_text().splitlines(), 1):
        stripped = line.strip()
        if not re.match(r"^\[\[.*\]\]\s*&&", stripped):
            continue
        if stripped.endswith("|| true"):
            continue
        # A body that exits or returns makes the status moot: control leaves.
        if re.search(r"\b(exit|return)\b[^;]*;?\s*\}?\s*$", stripped):
            continue
        problems.append(f"{script.name}:{number}: {stripped}")
    assert not problems, (
        "`[[ ]] && cmd` returns 1 when the test is false, which aborts the "
        "script under `set -e`. Use `if ... then ... fi`:\n  "
        + "\n  ".join(problems))


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_variables_adjacent_to_non_ascii_are_braced(script: Path):
    """bash reads a multi-byte character as part of the name and fails under -u."""
    problems = [f"{script.name}:{n}: {line.strip()}"
                for n, line in enumerate(script.read_text().splitlines(), 1)
                if re.search(r"\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7F\s]", line)]
    assert not problems, "brace these:\n  " + "\n  ".join(problems)


def test_installer_uses_bash_ec_for_remote_blocks():
    """`bash -c` returns only the last command's status, so a failed clone
    followed by a successful rm reports success."""
    text = (DEPLOY / "proxmox-install.sh").read_text()
    assert "bash -ec" in text, "the pct exec helper must use `bash -ec`"
    assert "LC_ALL=C" in text, (
        "pct exec passes the host environment through; without LC_ALL=C every "
        "apt call emits perl locale warnings")


def test_installer_cleans_up_a_half_built_container():
    text = (DEPLOY / "proxmox-install.sh").read_text()
    assert "trap on_failure ERR" in text
    assert "KEEP_ON_FAIL" in text, "debugging a failure needs the container kept"
    assert "pct destroy" in text


def test_verification_can_actually_fail():
    """A health loop that falls through to success reports Done for a dead service."""
    text = (DEPLOY / "proxmox-install.sh").read_text()
    verify = text[text.index("verify() {"):]
    verify = verify[:verify.index("\n}")]
    assert "exit 1" in verify, "verify() must exit nonzero when the service never came up"
    assert "journalctl" in verify, "a failed verification must show the journal"
    assert "226" in verify, (
        "verify() should name status=226/NAMESPACE explicitly -- it is the "
        "failure this target produces and the message does not explain itself")


def test_prompts_read_from_the_terminal_not_stdin():
    """Under `curl | bash` stdin is the script itself; reading it eats the source."""
    text = (DEPLOY / "proxmox-install.sh").read_text()
    assert "</dev/tty" in text
    assert "-r /dev/tty" in text, (
        "a run with no terminal must fall back to defaults rather than hang")


def test_privileged_ports_are_rejected_up_front():
    """The unit drops every capability, so a port below 1024 cannot be bound.
    Failing at first start says nothing about the cause; failing at input does."""
    text = (DEPLOY / "proxmox-install.sh").read_text()
    assert "APP_PORT < 1024" in text, (
        "the installer should reject a privileged port with an explanation "
        "rather than letting the service fail to bind")


def test_unit_port_is_unprivileged():
    unit = (DEPLOY / "tracepaper.service").read_text()
    match = re.search(r"--port (\d+)", unit)
    assert match and int(match.group(1)) >= 1024, (
        "CapabilityBoundingSet= is empty, so the default port must be >= 1024")


def test_service_code_is_not_owned_by_the_service_user():
    """Service-writable code means a compromise can rewrite what it runs next."""
    text = (DEPLOY / "proxmox-install.sh").read_text()
    assert "chown -R root:root /opt/tracepaper" in text
    assert "chown -R tracepaper:tracepaper /opt/tracepaper" not in text, (
        "the code tree must stay root-owned")


def test_index_directory_is_writable_by_the_service():
    """SQLite writes -wal and -shm beside the database, so the directory itself
    has to be writable, not just the file."""
    text = (DEPLOY / "proxmox-install.sh").read_text()
    assert "chown -R tracepaper:tracepaper /var/lib/tracepaper" in text


def test_documents_mount_is_read_only(cfg_text=None):
    """NFR-7 enforced by the kernel rather than promised by the code."""
    installer = (DEPLOY / "proxmox-install.sh").read_text()
    assert "ro=1" in installer, "the documents bind mount must be read-only"
    fstab = (DEPLOY / "nas.fstab.example").read_text()
    docs_line = next(l for l in fstab.splitlines()
                     if l.startswith("nas.local:") and "documents" in l)
    assert re.search(r"\bro,", docs_line), f"documents export must be ro: {docs_line}"


def test_nfs_mounts_are_soft_and_nofail():
    """`hard` wedges a scan unkillably when the NAS goes away; without nofail an
    unreachable NAS drops the container to an emergency shell at boot."""
    for source in (DEPLOY / "nas.fstab.example", DEPLOY / "proxmox-install.sh"):
        text = source.read_text()
        assert "soft" in text and "nofail" in text, source.name


def test_index_is_never_placed_on_the_nas():
    """SQLite over NFS corrupts silently and weeks later (D-008)."""
    for source in list(DEPLOY.glob("*.toml.example")) + [DEPLOY / "proxmox-install.sh"]:
        text = source.read_text()
        for match in re.finditer(r'db_path\s*=\s*"([^"]+)"', text):
            path = match.group(1)
            assert not path.startswith("/mnt/"), (
                f"{source.name}: db_path={path} is on a mount; SQLite must live "
                "on local disk")

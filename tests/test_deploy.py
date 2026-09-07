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
        allowed = ("/opt/tracepaper/.venv/bin/", "/opt/tracepaper/deploy/")
        assert binary.startswith(allowed), (
            f"{unit.name}: ExecStart={binary} is not a path the installer "
            "creates; systemd would fail with 203/EXEC.")
        # A script ExecStart must actually be executable in the repo, or the
        # copied-in copy will not run either.
        if binary.startswith("/opt/tracepaper/deploy/"):
            local = DEPLOY / Path(binary).name
            assert local.exists(), f"{unit.name}: {binary} is not shipped"
            assert local.stat().st_mode & 0o111, f"{local.name} is not executable"


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


def test_documents_mount_is_read_only():
    """NFR-7 enforced by the kernel rather than promised by the code.

    Read-only at both levels: the host's NFS mount and the bind mount into the
    container.
    """
    attach = (DEPLOY / "add-nas.sh").read_text()
    assert "ro=1" in attach, "the documents bind mount must be read-only"
    assert re.search(r'"ro,\$\{?common', attach), (
        "the host-side documents mount must carry ro")
    fstab = (DEPLOY / "nas.fstab.example").read_text()
    docs_line = next(l for l in fstab.splitlines()
                     if l.startswith("nas.local:") and "documents" in l)
    assert re.search(r"\bro,", docs_line), f"documents export must be ro: {docs_line}"


def test_nfs_mounts_are_soft_and_nofail():
    """`hard` wedges a scan unkillably when the NAS goes away; without nofail an
    unreachable NAS drops the container to an emergency shell at boot."""
    for source in (DEPLOY / "nas.fstab.example", DEPLOY / "add-nas.sh"):
        text = source.read_text()
        assert "soft" in text and "nofail" in text, source.name


def test_installer_does_not_configure_storage():
    """Which folders get indexed is not an install-time decision: it changes,
    there can be several, and they can come from different shares. Keeping it
    out of the installer is what makes add-nas.sh repeatable."""
    installer = (DEPLOY / "proxmox-install.sh").read_text()
    assert "NAS_HOST" not in installer, (
        "storage belongs in add-nas.sh, not the installer")
    assert "add-nas.sh" in installer, (
        "the installer should point at add-nas.sh once it finishes")


def test_installer_writes_empty_roots():
    """Nothing is mounted at install time, so a configured root would point at
    a path that does not exist and make every scan fail confusingly."""
    installer = (DEPLOY / "proxmox-install.sh").read_text()
    assert "roots = []" in installer


def test_add_nas_is_rerunnable():
    """You attach one share, then another. Re-running must not stack duplicate
    fstab entries or fail on an existing mount."""
    attach = (DEPLOY / "add-nas.sh").read_text()
    assert "already has an entry" in attach, "fstab writes must be idempotent"
    assert "mountpoint -q" in attach, (
        "an already-mounted path must not be treated as a failure")


def test_index_is_never_placed_on_the_nas():
    """SQLite over NFS corrupts silently and weeks later (D-008)."""
    for source in list(DEPLOY.glob("*.toml.example")) + [DEPLOY / "proxmox-install.sh"]:
        text = source.read_text()
        for match in re.finditer(r'db_path\s*=\s*"([^"]+)"', text):
            path = match.group(1)
            assert not path.startswith("/mnt/"), (
                f"{source.name}: db_path={path} is on a mount; SQLite must live "
                "on local disk")


# --------------------------------------------------------------- updates ---

def test_update_unit_is_not_enabled_at_boot():
    """It has no [Install] section on purpose: enabling it would run an update
    at every boot, so a power cut could change the running version unattended."""
    unit = (DEPLOY / "tracepaper-update.service").read_text()
    assert "[Install]" not in unit, (
        "tracepaper-update.service must stay on-demand only")
    installer = (DEPLOY / "proxmox-install.sh").read_text()
    assert not re.search(r"systemctl enable[^\n]*tracepaper-update", installer)


def test_update_unit_runs_as_root_and_the_app_does_not():
    """The split is the security property: the app triggers, root applies."""
    update = (DEPLOY / "tracepaper-update.service").read_text()
    service = (DEPLOY / "tracepaper.service").read_text()
    assert "User=root" in update
    assert "User=tracepaper" in service
    assert "CapabilityBoundingSet=" in service


def test_update_unit_has_headroom_and_a_timeout():
    """pip needs more memory than the app's ceiling, and a hung update must not
    hold the service down forever."""
    unit = (DEPLOY / "tracepaper-update.service").read_text()
    assert re.search(r"MemoryMax=\d+G", unit)
    assert re.search(r"TimeoutStartSec=\d+", unit)


def test_polkit_rule_is_narrow():
    """One action, one unit, one verb, one user -- not blanket systemd access."""
    rule = (DEPLOY / "49-tracepaper-update.rules").read_text()
    assert '"tracepaper-update.service"' in rule
    assert '"start"' in rule
    assert 'subject.user === "tracepaper"' in rule
    assert "org.freedesktop.systemd1.manage-units" in rule


def test_installer_installs_the_polkit_rule_and_reloads_polkit():
    """polkit reads rules at start; without a reload the grant exists on disk
    but is not in effect until the next reboot."""
    installer = (DEPLOY / "proxmox-install.sh").read_text()
    assert "49-tracepaper-update.rules" in installer
    assert "/etc/polkit-1/rules.d" in installer
    assert "restart polkit" in installer


def test_update_script_installs_every_unit_it_ships():
    """The units live in /etc, so a git pull alone never updates them. A unit
    missing from this list is a fix that silently never reaches the host."""
    updater = (DEPLOY / "update.sh").read_text()
    for unit in sorted(p.name for p in DEPLOY.glob("tracepaper*.service")):
        assert unit in updater, f"update.sh does not reinstall {unit}"
    for timer in sorted(p.name for p in DEPLOY.glob("tracepaper*.timer")):
        assert timer in updater, f"update.sh does not reinstall {timer}"


# ------------------------------------------------------------------- TLS ---

def test_caddyfile_has_no_log_directive():
    """The Debian package runs Caddy under ProtectSystem=full, so /var/log is
    read-only in its namespace. A log directive there fails at config load and
    Caddy exits before binding -- which looks like a network problem."""
    caddyfile = (DEPLOY / "Caddyfile").read_text()
    assert not re.search(r"^\s*log\s*\{", caddyfile, re.MULTILINE), (
        "leave logging on journald")


def test_caddy_install_binds_the_app_inward():
    """While the app still listens on 0.0.0.0 the plain-HTTP port keeps working
    and quietly bypasses TLS. The plaintext path has to actually go away."""
    script = (DEPLOY / "caddy-install.sh").read_text()
    assert "--host 127.0.0.1" in script
    assert "systemctl daemon-reload" in script, (
        "the bind address lives in the unit, so a daemon-reload is required")


def test_caddy_install_verification_can_fail():
    script = (DEPLOY / "caddy-install.sh").read_text()
    assert "healthy=0" in script and "healthy" in script
    assert "journalctl -u caddy" in script
    assert "exit 1" in script


def test_caddy_install_validates_config_before_restarting():
    script = (DEPLOY / "caddy-install.sh").read_text()
    assert "caddy validate" in script


def test_tls_is_optional_and_off_by_default():
    """Enabling TLS closes the plain-HTTP port and needs a CA root trusted per
    device; defaulting it on would hand a new install a browser warning."""
    installer = (DEPLOY / "proxmox-install.sh").read_text()
    assert 'TLS_DOMAIN="${TLS_DOMAIN:-}"' in installer
    assert "configure_tls" in installer


def test_tls_failure_does_not_destroy_the_container():
    """By the time TLS runs the app is installed and serving. A TLS failure
    should leave a working HTTP install, not trip the ERR trap."""
    installer = (DEPLOY / "proxmox-install.sh").read_text()
    block = installer[installer.index("configure_tls() {"):]
    block = block[:block.index("\n}")]
    assert "still running over plain HTTP" in block, (
        "a TLS failure must be reported as non-fatal")


# ------------------------------------------------------- attaching storage ---

def test_add_nas_supports_a_subfolder_of_a_share():
    """NFS exports a whole share, but the documents are usually a subfolder.
    Mount the share, scan the subtree -- the rest stays visible but unread."""
    attach = (DEPLOY / "add-nas.sh").read_text()
    assert "DOCS_SUBDIR" in attach
    assert 'SCAN_ROOT="${DOCS_MOUNT%/}/${DOCS_SUBDIR#/}"' in attach
    assert 'roots = [\\"${SCAN_ROOT}\\"]' in attach, (
        "the config must point at the subfolder, not the mount root")


def test_add_nas_verifies_the_subfolder_exists():
    """A typo in DOCS_SUBDIR otherwise surfaces much later, as a scan that
    silently finds nothing."""
    attach = (DEPLOY / "add-nas.sh").read_text()
    assert "does not exist inside the mounted share" in attach


def test_add_nas_supports_smb_for_account_based_access():
    """NFS with sec=sys grants by client IP and never consults a user account.
    SMB is the option that honours a dedicated NAS user."""
    attach = (DEPLOY / "add-nas.sh").read_text()
    assert "PROTOCOL" in attach and "cifs-utils" in attach
    assert "SMB_CREDENTIALS" in attach


def test_smb_credentials_are_not_world_readable():
    """A credentials file anyone can read is a password anyone can read."""
    attach = (DEPLOY / "add-nas.sh").read_text()
    assert "chmod 600" in attach
    assert 'stat -c' in attach, "the script should check the mode it was given"


def test_documents_stay_read_only_under_both_protocols():
    attach = (DEPLOY / "add-nas.sh").read_text()
    # Host-side mount options, one per protocol branch.
    assert attach.count('"ro,${common}"') == 2, (
        "both the nfs and smb branches must mount documents ro")
    # And the bind mount into the container.
    assert "ro=1" in attach


def test_share_variables_are_named_for_what_they_do():
    """NAS_DOCS_EXPORT / NAS_BACKUP_EXPORT both said "EXPORT" and neither said
    which one is read and which is written. The old names still work so nobody
    mid-setup is broken."""
    attach = (DEPLOY / "add-nas.sh").read_text()
    assert "READ_SHARE" in attach and "WRITE_SHARE" in attach
    assert "${NAS_DOCS_EXPORT:-" in attach, "old name must still be honoured"
    assert "${NAS_BACKUP_EXPORT:-" in attach


def test_fstab_helper_reconciles_rather_than_skipping():
    """Skipping on a mount-point match made a first run with wrong values
    sticky: `mount <point>` reads the stale source out of fstab, so every later
    run mounted the wrong export while reporting the right one."""
    attach = (DEPLOY / "add-nas.sh").read_text()
    assert "does not match — replacing it" in attach
    assert "fstab.tracepaper-" in attach, "rewriting fstab must leave a backup"
    assert "umount" in attach, (
        "a stale mount keeps serving the old export until it is unmounted")


def test_fstab_lookup_tolerates_no_match():
    """grep exits 1 when it finds nothing, which under `set -eo pipefail`
    aborts the script — on a fresh host, where finding nothing is correct."""
    attach = (DEPLOY / "add-nas.sh").read_text()
    line = next(l for l in attach.splitlines()
                if l.strip().startswith("existing=$(grep"))
    assert "|| true" in line, (
        "the fstab lookup must tolerate no match; without it a fresh install "
        "aborts in the ERR trap")

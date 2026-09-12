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
        # A shell wrapper is legitimate when the command needs substitution
        # systemd cannot do (a dated filename, say) -- but the binary it runs
        # must still be one the installer creates, so check that too.
        if binary in ("/bin/sh", "/usr/bin/sh", "/bin/bash"):
            body = text[match.end():text.index("\n", match.end())]
            assert "/opt/tracepaper/.venv/bin/" in body, (
                f"{unit.name}: shell ExecStart must invoke the installed binary")
            continue
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
    fstab entries or fail on an existing mount -- but it must also not skip an
    entry whose values have changed, which is what made a bad first run stick."""
    attach = (DEPLOY / "add-nas.sh").read_text()
    assert "is already correct" in attach, "an identical entry is a no-op"
    assert "does not match — replacing it" in attach, (
        "a changed entry must be rewritten, not left alone")
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
    """One action, one verb, one user, and a fixed unit list -- not blanket
    systemd access. The units the app can trigger are exactly the ones it has
    buttons for; anything else must stay out of the grant."""
    rule = (DEPLOY / "49-tracepaper-update.rules").read_text()
    assert '"start"' in rule
    assert 'subject.user === "tracepaper"' in rule
    assert "org.freedesktop.systemd1.manage-units" in rule

    for unit in ("tracepaper-update.service", "tracepaper-scan.service",
                 "tracepaper-enrich.service", "tracepaper-backup.service"):
        assert f'"{unit}"' in rule, f"{unit} is not grantable"

    # The web service itself must never be startable/stoppable through polkit:
    # that would let a request reaching the port take the server down.
    assert '"tracepaper.service"' not in rule
    for verb in ("stop", "restart", "disable", "mask"):
        assert f'"{verb}"' not in rule, f"{verb} must not be granted"


def test_polkit_grants_exactly_the_units_the_app_can_trigger():
    """The allowlist in jobs.py and the polkit rule have to agree. If they
    drift, either a button appears that fails on authentication, or a unit is
    grantable that nothing needs -- both are silent until someone clicks."""
    from tracepaper import jobs, updates

    rule = (DEPLOY / "49-tracepaper-update.rules").read_text()
    granted = set(re.findall(r'"(tracepaper[\w-]*\.service)"', rule))
    expected = set(jobs.UNITS.values()) | {updates.UPDATE_UNIT}
    assert granted == expected


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


def test_smb_maps_files_to_the_service_account_not_root():
    """SMB has no uid negotiation: the client decides what files look like
    locally. Mapping to the container's root gives a readable tree but an
    unwritable backup share -- the service does not run as root."""
    attach = (DEPLOY / "add-nas.sh").read_text()
    assert "id -u tracepaper" in attach, (
        "the service uid must be read, not assumed")
    assert "uid=${host_uid}" in attach
    assert "uid=100000" not in attach, (
        "hardcoding the container root uid is the bug this replaced")


def test_smb_reads_the_id_map_offset_from_the_container():
    """100000 is the default offset, not a guarantee. A custom map would
    silently produce files the service cannot touch."""
    attach = (DEPLOY / "add-nas.sh").read_text()
    assert "lxc.idmap" in attach
    assert 'id_offset="${id_offset:-100000}"' in attach, (
        "fall back to the default only when the container declares no map")


def test_config_update_replaces_a_stale_root():
    """Matching `roots = []` alone meant a re-run kept the previous value: the
    script reported the path it intended while the config held the old one, and
    the scan then failed on a path nobody asked for."""
    attach = (DEPLOY / "add-nas.sh").read_text()
    assert "s#^roots = .*#roots =" in attach, (
        "the roots line must be replaced whatever its current value")
    assert "s#^roots = \\[\\]#" not in attach, (
        "matching only an empty list is the bug this replaced")


def test_config_update_is_verified_after_writing():
    """Printing the file and moving on let a failed edit slide past unnoticed."""
    attach = (DEPLOY / "add-nas.sh").read_text()
    assert "the config was not updated to" in attach


def test_scan_timeout_allows_a_cold_first_pass():
    """A cold scan of a large corpus runs for many hours. A timeout tuned to
    the hourly steady state would SIGTERM it partway through, every time, so it
    could never finish and never reach the cheap steady state."""
    unit = (DEPLOY / "tracepaper-scan.service").read_text()
    match = re.search(r"TimeoutStartSec=(\d+)", unit)
    assert match, "the scan unit must bound its runtime"
    assert int(match.group(1)) >= 43200, (
        "a first scan over tens of thousands of files needs more than a few "
        "hours; a hung mount is caught by soft/timeo on the mount instead")


def test_the_irreplaceable_layer_is_backed_up_on_a_timer():
    """Corrections, notes and merges are the only state that cannot be
    regenerated by re-scanning. Leaving that to a command someone remembers to
    run is how it is not there when the container is lost."""
    assert (DEPLOY / "tracepaper-backup.service").exists()
    assert (DEPLOY / "tracepaper-backup.timer").exists()
    installer = (DEPLOY / "proxmox-install.sh").read_text()
    assert "enable --now tracepaper-backup.timer" in installer


def test_backups_are_dated_and_pruned():
    """One overwritten file means a bad correction is unrecoverable the next
    night. They are a few KB of JSON, so keeping a month costs nothing."""
    unit = (DEPLOY / "tracepaper-backup.service").read_text()
    assert "date +" in unit, "each run must write its own dated file"
    assert "-mtime +30 -delete" in unit, "and old ones must be pruned"


def test_backup_exports_json_not_a_copy_of_the_database():
    """A SQLite file copied while the service holds it open can be torn
    mid-transaction, and would restore only into a schema-compatible build."""
    unit = (DEPLOY / "tracepaper-backup.service").read_text()
    # The directives only -- a comment explaining why this is not a database
    # copy legitimately mentions index.db.
    directives = [l for l in unit.splitlines()
                  if l.strip() and not l.strip().startswith("#")]
    exec_lines = [l for l in directives if l.startswith("ExecStart")]
    assert exec_lines, "the unit must run something"
    assert any(".json" in l for l in exec_lines)
    assert not any("index.db" in l for l in exec_lines), (
        "back up the export, never the live database file")


def test_the_service_user_can_read_git_state():
    """The code tree is root-owned on purpose, but git then refuses to read it
    as anyone else ("dubious ownership") -- and the UI reads git state to show
    the running version and whether an update is waiting. Marking the path safe
    grants reading only; the update still runs as root through its own unit."""
    installer = (DEPLOY / "proxmox-install.sh").read_text()
    assert "safe.directory /opt/tracepaper" in installer
    assert "--system" in installer, (
        "the service user has no home for a --global gitconfig")
    updater = (DEPLOY / "update.sh").read_text()
    assert "safe.directory" in updater, (
        "an install predating this must get the fix on its next update")


def test_pre_update_backup_goes_to_the_configured_share():
    """A pre-update backup on local disk is the copy that disappears with the
    container -- which is the case it exists for."""
    updater = (DEPLOY / "update.sh").read_text()
    assert "backup_dir" in updater, (
        "the pre-update backup should prefer the configured share")


def test_pre_update_backup_directory_is_writable_by_the_service_user():
    """The export runs as the service user; a root-owned mkdir made this step
    fail silently on every update."""
    updater = (DEPLOY / "update.sh").read_text()
    assert "chown tracepaper:tracepaper \"$BACKUP_BASE\"" in updater


def test_pre_update_backup_failure_is_visible():
    """Swallowing stderr hid the reason this step was failing."""
    updater = (DEPLOY / "update.sh").read_text()
    line = next(l for l in updater.splitlines()
                if "backup \"$BACKUP\"" in l)
    assert "2>&1" not in line, "the export's error must reach the operator"


def test_update_reinstalls_the_polkit_rule():
    """The polkit rule lives in /etc, so a git pull never updates it -- the
    same trap the unit files have, and worse: a stale rule does not break
    loudly, it just refuses to authorise units it has never heard of. Adding a
    job button without this leaves the new unit ungranted, and the start fails
    with a bare exit 1 that reads like a broken unit rather than a denial."""
    update = (DEPLOY / "update.sh").read_text()
    assert "49-tracepaper-update.rules" in update
    assert "/etc/polkit-1/rules.d" in update
    assert "restart polkit" in update, (
        "polkit reads rules at start; without a restart the grant is not live")


def test_enrich_does_not_wait_for_an_idle_machine():
    """--wait gave up after five minutes and exited reporting success, so a
    backlog silently never got embedded. Nice=19 plus idle IO expresses the
    same intent without ever refusing to run."""
    unit = (DEPLOY / "tracepaper-enrich.service").read_text()
    assert "enrich --wait" not in unit
    assert "Nice=19" in unit
    assert "IOSchedulingClass=idle" in unit


def test_enrich_has_a_writable_model_cache():
    """/opt/tracepaper is root-owned, and HuggingFace caches to the working
    directory by default -- the download failed with EACCES and was reported
    as 'skipped' rather than an error."""
    unit = (DEPLOY / "tracepaper-enrich.service").read_text()
    assert "HF_HOME=" in unit
    assert "/opt/tracepaper" not in unit.split("HF_HOME=")[1].split("\n")[0]


def test_enrich_leaves_cpu_for_the_web_service():
    """PyTorch saturates every visible core, and Nice only helps against
    something runnable -- a request blocked on SQLite is not."""
    unit = (DEPLOY / "tracepaper-enrich.service").read_text()
    assert "CPUQuota=" in unit
    assert "OMP_NUM_THREADS=" in unit


def test_web_service_shares_the_model_cache():
    """The service user cannot write /opt/tracepaper, which is where
    HuggingFace caches by default -- startup failed with EACCES on every boot
    and search silently fell back to keyword-only. It must point at the same
    writable cache the enrich unit fills, or each would download its own."""
    web = (DEPLOY / "tracepaper.service").read_text()
    enrich = (DEPLOY / "tracepaper-enrich.service").read_text()
    assert "HF_HOME=" in web, "the web service needs a writable model cache"

    def cache_dir(unit: str) -> str:
        return unit.split("HF_HOME=")[1].split("\n")[0].strip()

    assert cache_dir(web) == cache_dir(enrich), (
        "both units must share one cache; two paths means two downloads")
    assert "/opt/tracepaper" not in cache_dir(web)


def test_web_service_does_not_reach_the_network_for_the_model():
    """A HEAD request to huggingface.co before the port binds delays startup
    past the health check. The model is already on disk; load it from there."""
    web = (DEPLOY / "tracepaper.service").read_text()
    assert "HF_HUB_OFFLINE=1" in web

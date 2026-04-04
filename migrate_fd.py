"""
Author: Gururaj Mohan-Oracle
Date: 2026-04-03

Planned change only: list active IAAS announcements with type PLANNED_CHANGE, then for each
affected compute instance move it to another fault domain in the same availability domain and reboot.

Dry-run by default (prints only). Set EXECUTE_FD_MIGRATE=1 to call update_instance + SOFTRESET.

Announcements can stay PLANNED_CHANGE after work is done; Instance maintenance (Compute) is the
source of truth. By default we skip execute when there is no active instance maintenance event
(SCHEDULED/STARTED/PROCESSING) so nightly jobs do not repeat work. Override with
SKIP_FD_IF_NO_ACTIVE_MAINTENANCE=0. Execute is never run for cancelled-only maintenance (CANCELED
with no SCHEDULED/STARTED/PROCESSING); execute applies to non-cancelled active maintenance only
(cannot override the cancelled-only case).

Oracle may reject fault_domain updates on running instances; you may need STOP → update → START first.

Compute and Identity calls use the region embedded in the instance OCID when it differs from the
profile region (multi-region tenancies).

Limit scope to a compartment (optional): PLANNED_CHANGE listing and the initial maintenance list
use this scope — set OCI_COMPARTMENT_ID or OCI_COMPARTMENT_OCID to a compartment OCID,
or set compartmentId in ~/.oci/config for your OCI_CLI_PROFILE. If unset, the tenancy OCID is
used (broader scope).

  export OCI_COMPARTMENT_ID=ocid1.compartment.oc1...
  python3 migrate_fd.py
"""

import os
import re

import oci
from oci.core.models import UpdateInstanceDetails
from oci.exceptions import ConfigFileNotFound, InvalidConfig, ProfileNotFound
from oci.pagination import list_call_get_all_results

_DEFAULT_OCI_PROFILE = "GURU"
_config_path = os.environ.get("OCI_CONFIG_FILE", os.path.expanduser("~/.oci/config"))
_profile = os.environ.get("OCI_CLI_PROFILE", _DEFAULT_OCI_PROFILE)

_EXEC_ENV = "EXECUTE_FD_MIGRATE"
_REBOOT_ACTION_ENV = "FD_MIGRATE_REBOOT_ACTION"  # SOFTRESET (default) or RESET
_SKIP_IF_NO_ACTIVE_ENV = "SKIP_FD_IF_NO_ACTIVE_MAINTENANCE"

# Instance maintenance events that mean work is still in flight (Compute API).
_ACTIVE_MAINTENANCE_STATES = frozenset({"SCHEDULED", "STARTED", "PROCESSING"})
_CANCELED_MAINTENANCE_STATE = "CANCELED"

_compute_by_region = {}
_identity_by_region = {}

try:
    config = oci.config.from_file(_config_path, _profile)
    announcements_client = oci.announcements_service.AnnouncementClient(config)
except ConfigFileNotFound as e:
    raise SystemExit(
        f"OCI config not found at {_config_path}. See "
        "https://docs.oracle.com/en-us/iaas/Content/API/Concepts/sdkconfig.htm\n"
        f"{e}"
    ) from e
except ProfileNotFound as e:
    raise SystemExit(f"Profile [{_profile}] not in {_config_path}.\n{e}") from e
except InvalidConfig as e:
    raise SystemExit(f"Invalid OCI config: {e.errors}") from e


def _env_truthy(name):
    return os.environ.get(name, "").lower() in ("1", "true", "yes")


def _skip_fd_if_no_active_maintenance():
    """Default True: do not run FD migrate when Compute shows no active maintenance (avoids nightly redo)."""
    raw = os.environ.get(_SKIP_IF_NO_ACTIVE_ENV)
    if raw is None or str(raw).strip() == "":
        return True
    return _env_truthy(_SKIP_IF_NO_ACTIVE_ENV)


def _has_non_cancelled_active_maintenance(active_maint):
    """Non-cancelled maintenance = SCHEDULED / STARTED / PROCESSING (work still applies)."""
    return bool(active_maint)


def _maintenance_is_cancelled_only(maint_events, active_maint):
    """Cancelled maintenance: at least one CANCELED event and no active non-cancelled work — never execute."""
    if not maint_events or active_maint:
        return False
    return any(
        (getattr(e, "lifecycle_state", None) or "").upper() == _CANCELED_MAINTENANCE_STATE
        for e in maint_events
    )


def _list_maintenance_events_for_instance(instance_id, cc):
    cid = _compartment_scope()
    try:
        evs = list_call_get_all_results(
            cc.list_instance_maintenance_events,
            cid,
            instance_id=instance_id,
        ).data or []
    except oci.exceptions.ServiceError:
        evs = []
    if not evs:
        try:
            inst = cc.get_instance(instance_id).data
            if inst.compartment_id and inst.compartment_id != cid:
                evs = list_call_get_all_results(
                    cc.list_instance_maintenance_events,
                    inst.compartment_id,
                    instance_id=instance_id,
                ).data or []
        except oci.exceptions.ServiceError:
            pass
    return evs or []


def _compartment_scope():
    return (
        os.environ.get("OCI_COMPARTMENT_ID")
        or os.environ.get("OCI_COMPARTMENT_OCID")
        or config.get("compartmentId")
        or config["tenancy"]
    )


def _is_compute_instance_ocid(ocid):
    return bool(ocid) and ".instance." in ocid


def _region_from_instance_ocid(ocid):
    m = re.match(r"^ocid1\.instance\.oc1\.([a-z0-9-]+)\.", ocid or "")
    return m.group(1) if m else None


def _config_region():
    return (config.get("region") or "").strip() or None


def _compute_for_region(region_name):
    if region_name not in _compute_by_region:
        _compute_by_region[region_name] = oci.core.ComputeClient(
            config, region_name=region_name
        )
    return _compute_by_region[region_name]


def _identity_for_region(region_name):
    if region_name not in _identity_by_region:
        _identity_by_region[region_name] = oci.identity.IdentityClient(
            config, region_name=region_name
        )
    return _identity_by_region[region_name]


def _response_etag(resp):
    """Instance model has no .etag; OCI returns it on the HTTP response."""
    if not resp or not resp.headers:
        return None
    return resp.headers.get("etag") or resp.headers.get("ETag")


def _alternate_fault_domain(inst, region_name):
    """Pick another fault domain name in the same AD (or use TARGET_FAULT_DOMAIN env if valid)."""
    explicit = os.environ.get("TARGET_FAULT_DOMAIN", "").strip()
    id_client = _identity_for_region(region_name)
    fds = id_client.list_fault_domains(
        config["tenancy"], inst.availability_domain
    ).data or []
    names = {fd.name for fd in fds if fd.name}
    if explicit:
        if explicit not in names:
            raise ValueError(
                f"TARGET_FAULT_DOMAIN={explicit!r} is not in this AD. Valid: {sorted(names)}"
            )
        if explicit == inst.fault_domain:
            raise ValueError(
                f"TARGET_FAULT_DOMAIN equals current fault domain ({explicit}); pick another FD."
            )
        return explicit
    for fd in fds:
        if fd.name and fd.name != inst.fault_domain:
            return fd.name
    return None


def _reboot_action():
    raw = (os.environ.get(_REBOOT_ACTION_ENV) or "SOFTRESET").strip().upper()
    if raw not in ("SOFTRESET", "RESET"):
        raise SystemExit(
            f"{_REBOOT_ACTION_ENV} must be SOFTRESET or RESET, not {raw!r}"
        )
    return raw


def list_planned_change_announcements(compartment_id):
    """Only console-style 'Planned change' rows (API announcement_type=PLANNED_CHANGE)."""
    return list_call_get_all_results(
        announcements_client.list_announcements,
        compartment_id,
        lifecycle_state="ACTIVE",
        platform_type="IAAS",
        announcement_type="PLANNED_CHANGE",
    ).data or []


def change_fault_domain_and_reboot(instance_id, label, execute, reboot_action):
    region = _region_from_instance_ocid(instance_id) or _config_region()
    if not region:
        print(f"  Skip {label}: cannot determine API region (OCID or config).")
        return
    cc = _compute_for_region(region)
    cfg_r = _config_region()
    region_note = ""
    if cfg_r and region != cfg_r:
        region_note = (
            f"    (Compute API region {region}; profile region {cfg_r})\n"
        )

    try:
        resp0 = cc.get_instance(instance_id)
    except oci.exceptions.ServiceError as e:
        if e.status in (404, 403):
            print(
                f"  Skip {label}: get_instance failed in Compute region {region} "
                f"({e.status} {e.code}). OCI uses the same code for \"not found\" and \"not allowed\". "
                "Typical causes: instance terminated (stale announcement), IAM policy missing for "
                "this compartment/region, or region not enabled for the tenancy. "
                f"OCID: {instance_id}"
            )
            return
        raise
    inst = resp0.data
    etag = _response_etag(resp0)
    if not inst.availability_domain or not inst.fault_domain:
        print(f"  Skip {label}: missing availability_domain or fault_domain.")
        return

    try:
        target_fd = _alternate_fault_domain(inst, region)
    except ValueError as e:
        print(f"  Skip {label}: {e}")
        return

    if not target_fd:
        print(f"  Skip {label}: no alternate fault domain in AD {inst.availability_domain}.")
        return

    print(
        f"  {label}\n"
        f"{region_note}"
        f"    Current FD: {inst.fault_domain}  →  target: {target_fd}  |  reboot: {reboot_action}"
    )

    maint_events = _list_maintenance_events_for_instance(instance_id, cc)
    active_maint = [e for e in maint_events if e.lifecycle_state in _ACTIVE_MAINTENANCE_STATES]
    if maint_events:
        states = ", ".join(sorted({e.lifecycle_state for e in maint_events}))
        print(f"    Instance maintenance (Compute): {states}")
    else:
        print("    Instance maintenance (Compute): no events returned for this instance.")

    if not execute:
        print("    (dry-run; set EXECUTE_FD_MIGRATE=1 to apply)")
        if _maintenance_is_cancelled_only(maint_events, active_maint):
            print(
                "    Note: execute would be skipped — maintenance is CANCELED only (not non-cancelled work)."
            )
        elif _skip_fd_if_no_active_maintenance() and not _has_non_cancelled_active_maintenance(
            active_maint
        ):
            print(
                "    Note: with default guards, execute would be skipped — no SCHEDULED/STARTED/PROCESSING event."
            )
        return

    if _maintenance_is_cancelled_only(maint_events, active_maint):
        print(
            "    Skip execute: maintenance is CANCELED (no non-cancelled active work); "
            "will not run FD change or reboot "
            f"(not overridable with {_SKIP_IF_NO_ACTIVE_ENV}=0)."
        )
        return

    if _skip_fd_if_no_active_maintenance() and not _has_non_cancelled_active_maintenance(
        active_maint
    ):
        print(
            "    Skip execute: no active instance maintenance (SCHEDULED/STARTED/PROCESSING). "
            "Maintenance may already be finished while PLANNED_CHANGE announcement can stay open. "
            f"Set {_SKIP_IF_NO_ACTIVE_ENV}=0 to force FD change anyway."
        )
        return

    try:
        cc.update_instance(
            instance_id,
            UpdateInstanceDetails(fault_domain=target_fd),
            if_match=etag,
        )
        print("    update_instance(fault_domain) OK")
    except oci.exceptions.ServiceError as e:
        print(f"    update_instance failed: {e.status} — {e.message}")
        print(
            "    Hint: stop the instance, change fault domain, start, then re-run; "
            "or use OKE cordon/drain + replace node."
        )
        return

    try:
        resp2 = cc.get_instance(instance_id)
    except oci.exceptions.ServiceError as e:
        print(f"    get_instance (after update) failed: {e.status} — {e.message}")
        return
    etag2 = _response_etag(resp2)
    try:
        cc.instance_action(instance_id, reboot_action, if_match=etag2)
        print(f"    {reboot_action} OK")
    except oci.exceptions.ServiceError as e:
        print(f"    {reboot_action} failed: {e.status} — {e.message}")


def main():
    execute = _env_truthy(_EXEC_ENV)
    reboot_action = _reboot_action()
    compartment_id = _compartment_scope()

    print("=== PLANNED_CHANGE announcements only (IAAS, active) ===")
    print(f"Compartment: {compartment_id}")
    print(f"Execute FD change + reboot: {execute} (set {_EXEC_ENV}=1 to enable)")
    print(f"Reboot action: {reboot_action} ({_REBOOT_ACTION_ENV})")
    print(
        f"Skip if no active maintenance: {_skip_fd_if_no_active_maintenance()} "
        f"({_SKIP_IF_NO_ACTIVE_ENV}, default 1 — uses Instance maintenance, not Announcements)"
    )
    print(
        "Execute only for non-cancelled maintenance (SCHEDULED/STARTED/PROCESSING); "
        "cancelled-only (CANCELED) never runs execute.\n"
    )

    collection = list_planned_change_announcements(compartment_id)
    if not collection:
        print("No PLANNED_CHANGE announcements found.")
        return

    for ann in collection:
        print("--- PLANNED_CHANGE ---")
        print(f"Summary: {ann.summary}")
        print(f"Ticket: {ann.reference_ticket_number}")
        if ann.time_one_value:
            print(f"{ann.time_one_title or 'Time'}: {ann.time_one_value}")
        print(f"Announcement ID: {ann.id}")

        detail = announcements_client.get_announcement(ann.id).data
        resources = detail.affected_resources or []
        if not resources:
            print("  No affected resources in GetAnnouncement.\n")
            continue

        for ar in resources:
            rid = ar.resource_id
            label = ar.resource_name or rid
            if not _is_compute_instance_ocid(rid):
                print(f"  Skip non-instance resource: {label}")
                continue
            change_fault_domain_and_reboot(rid, label, execute, reboot_action)
        print()


if __name__ == "__main__":
    main()

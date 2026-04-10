"""
Author: Gururaj Mohan
Date: 2026-04-02

OCI announcements, compute maintenance, and fault-domain migration utilities.

Limit scope: set OCI_COMPARTMENT_ID or OCI_COMPARTMENT_OCID (or compartmentId in ~/.oci/config);
otherwise tenancy scope is used.
"""

import os

import oci
from oci.core.models import UpdateInstanceDetails
from oci.exceptions import ConfigFileNotFound, InvalidConfig, ProfileNotFound
from oci.pagination import list_call_get_all_results

# ~/.oci/config + OCI_CONFIG_FILE. Default profile is GURU unless OCI_CLI_PROFILE is set.
# Avoid profile name "DEFAULT" unless your file has an explicit [DEFAULT] section — Python's
# ConfigParser always has an empty DEFAULT section, so oci loads no keys if you only use named profiles.
_DEFAULT_OCI_PROFILE = "GURU"
_config_path = os.environ.get("OCI_CONFIG_FILE", os.path.expanduser("~/.oci/config"))
_profile = os.environ.get("OCI_CLI_PROFILE", _DEFAULT_OCI_PROFILE)

try:
    config = oci.config.from_file(_config_path, _profile)
    compute_client = oci.core.ComputeClient(config)
    identity_client = oci.identity.IdentityClient(config)
    announcements_client = oci.announcements_service.AnnouncementClient(config)
except ConfigFileNotFound as e:
    raise SystemExit(
        f"OCI config not found. Create {_config_path} (e.g. run `oci setup config`).\n"
        "Docs: https://docs.oracle.com/en-us/iaas/Content/API/Concepts/sdkconfig.htm\n"
        f"Details: {e}"
    ) from e
except ProfileNotFound as e:
    raise SystemExit(
        f"Profile [{_profile}] not in {_config_path}. "
        "Add it or set OCI_CLI_PROFILE to an existing profile name.\n"
        f"Details: {e}"
    ) from e
except InvalidConfig as e:
    raise SystemExit(
        f"OCI config at {_config_path} (profile [{_profile}]) is incomplete.\n"
        "Required: tenancy, user, fingerprint, region, key_file (path to API private key PEM).\n"
        "If you only use named profiles (e.g. [MYPROFILE]), set OCI_CLI_PROFILE=MYPROFILE — "
        "profile DEFAULT maps to Python's empty default section unless [DEFAULT] exists in the file.\n"
        f"Validation errors: {e.errors}"
    ) from e

# Compartment to list: OCI_COMPARTMENT_ID, profile key compartmentId, else tenancy (whole tenancy tree).
_ACTIVE_MAINTENANCE_STATES = frozenset({"SCHEDULED", "STARTED", "PROCESSING"})

# Console "Planned change" / maintenance banners use the Announcements API (not Compute instance events).
_PLANNED_ANNOUNCEMENT_TYPES = frozenset(
    {
        "PLANNED_CHANGE",
        "PLANNED_CHANGE_COMPLETE",
        "PLANNED_CHANGE_EXTENDED",
        "PLANNED_CHANGE_RESCHEDULED",
        "SCHEDULED_MAINTENANCE",
    }
)

# REBOOTMIGRATE for instances under PLANNED_CHANGE: on by default; set env to 0 to skip a run.
_EXEC_REBOOT_ENV = "EXECUTE_REBOOT_MIGRATE_FOR_PLANNED_CHANGE"
_DEFAULT_EXECUTE_REBOOT_FOR_PLANNED_CHANGE = True

# What to run for each affected instance on PLANNED_CHANGE (when execute is on):
#   REBOOTMIGRATE — move to new hardware (only when Compute shows pending maintenance).
#   SOFTRESET / RESET — guest reboot only; does not change fault domain or satisfy infra maintenance.
#   FD_SOFTRESET — update_instance to another fault domain in the same AD, then SOFTRESET (may require STOP first; see API error).
_PLANNED_INSTANCE_ACTION_ENV = "PLANNED_CHANGE_INSTANCE_ACTION"
_PLANNED_INSTANCE_ACTIONS = frozenset({"REBOOTMIGRATE", "SOFTRESET", "RESET", "FD_SOFTRESET"})


def _env_truthy(name):
    return os.environ.get(name, "").lower() in ("1", "true", "yes")


def _should_execute_reboot_for_planned_change():
    raw = os.environ.get(_EXEC_REBOOT_ENV)
    if raw is None or str(raw).strip() == "":
        return _DEFAULT_EXECUTE_REBOOT_FOR_PLANNED_CHANGE
    return _env_truthy(_EXEC_REBOOT_ENV)


def _planned_change_instance_action():
    raw = (os.environ.get(_PLANNED_INSTANCE_ACTION_ENV) or "REBOOTMIGRATE").strip().upper()
    if raw not in _PLANNED_INSTANCE_ACTIONS:
        raise SystemExit(
            f"Invalid {_PLANNED_INSTANCE_ACTION_ENV}={raw!r}. "
            f"Use one of: {', '.join(sorted(_PLANNED_INSTANCE_ACTIONS))}"
        )
    return raw


def _alternate_fault_domain_in_ad(inst):
    """Return a fault domain name in the same AD that differs from the instance's current FD."""
    fds = identity_client.list_fault_domains(
        config["tenancy"], inst.availability_domain
    ).data or []
    for fd in fds:
        if fd.name and fd.name != inst.fault_domain:
            return fd.name
    return None


def _is_compute_instance_ocid(ocid):
    return bool(ocid) and ".instance." in ocid


def _response_etag(resp):
    if not resp or not resp.headers:
        return None
    return resp.headers.get("etag") or resp.headers.get("ETag")


def _list_maintenance_events_for_instance(instance_id, compartment_id):
    """List instance maintenance events for one VM (may be empty until Oracle attaches maintenance)."""
    try:
        return list_call_get_all_results(
            compute_client.list_instance_maintenance_events,
            compartment_id,
            instance_id=instance_id,
        ).data or []
    except oci.exceptions.ServiceError:
        return []


def _pending_maintenance_events_for_instance(instance_id):
    """
    REBOOTMIGRATE is only accepted when Compute already shows pending maintenance for the instance
    (announcement alone is not enough).
    """
    cid = _compartment_scope()
    evs = _list_maintenance_events_for_instance(instance_id, cid)
    if not evs:
        try:
            inst = compute_client.get_instance(instance_id).data
            if inst.compartment_id and inst.compartment_id != cid:
                evs = _list_maintenance_events_for_instance(instance_id, inst.compartment_id)
        except oci.exceptions.ServiceError:
            pass
    return [e for e in evs if e.lifecycle_state in _ACTIVE_MAINTENANCE_STATES]


def _compartment_scope():
    return (
        os.environ.get("OCI_COMPARTMENT_ID")
        or os.environ.get("OCI_COMPARTMENT_OCID")
        or config.get("compartmentId")
        or config["tenancy"]
    )


def check_instance_maintenance_events(compartment_id):
    show_all_states = os.environ.get("MAINTENANCE_ALL_STATES", "").lower() in (
        "1",
        "true",
        "yes",
    )

    print(f"=== Compute instance maintenance events (per-VM reboot / migration) ===")
    print(f"Compartment: {compartment_id}\n")

    # No lifecycle_state filter — SCHEDULED-only misses STARTED/PROCESSING. Paginate (default page size can hide rows).
    all_events = list_call_get_all_results(
        compute_client.list_instance_maintenance_events,
        compartment_id,
    ).data

    if not all_events:
        print("No instance maintenance events returned by the API for this compartment.")
        print(
            "That is normal if only a tenancy-level PLANNED_CHANGE exists so far — "
            "per-VM rows appear later in Compute. Check service announcements below.\n"
        )
        return

    if show_all_states:
        events = list(all_events)
    else:
        events = [e for e in all_events if e.lifecycle_state in _ACTIVE_MAINTENANCE_STATES]

    if not events:
        states = sorted({e.lifecycle_state for e in all_events})
        print(
            f"No events in active states {_ACTIVE_MAINTENANCE_STATES}. "
            f"Found {len(all_events)} event(s) in other state(s): {states}. "
            "Set MAINTENANCE_ALL_STATES=1 to print them."
        )
        return

    for event in events:
        instance_id = event.instance_id
        try:
            instance = compute_client.get_instance(instance_id).data
            instance_label = f"{instance.display_name} ({instance_id})"
        except oci.exceptions.ServiceError:
            instance_label = f"(get_instance failed) {instance_id}"

        print(f"--- Maintenance Detected ---")
        print(f"State: {event.lifecycle_state}")
        print(f"Instance: {instance_label}")
        print(f"Window start: {event.time_window_start}")
        print(f"Instance action: {event.instance_action}")
        
        # 2. Optional: proactive reboot migration (never runs until you uncomment — script is report-only).
        # PLANNED_CHANGE announcements do not reboot VMs; this loop only runs when Compute returns
        # instance maintenance events. Otherwise migrate manually: Console → instance → Reboot migration,
        # or: oci compute instance action --instance-id <ocid> --action REBOOTMIGRATE
        # WARNING: downtime (often ~5–10 minutes).
        #
        # print(f"Triggering Reboot Migration for {instance.display_name}...")
        # compute_client.instance_action(instance_id, "REBOOTMIGRATE")
        # print("Migration triggered successfully.")


def check_service_announcements(compartment_id):
    """Lists tenancy announcements; 'Planned change' in the console is usually PLANNED_CHANGE here."""
    print(f"\n=== Service announcements (Console: Planned change, scheduled maintenance, etc.) ===")
    print(f"Compartment: {compartment_id}\n")

    execute_reboot = _should_execute_reboot_for_planned_change()
    planned_action = (
        _planned_change_instance_action() if execute_reboot else "REBOOTMIGRATE"
    )
    if execute_reboot:
        print(
            f"*** PLANNED_CHANGE actions enabled: {planned_action} "
            f"(set {_PLANNED_INSTANCE_ACTION_ENV} to change; {_EXEC_REBOOT_ENV}=0 to skip). ***\n"
        )
    else:
        print(f"*** PLANNED_CHANGE actions skipped ({_EXEC_REBOOT_ENV}=0). ***\n")

    show_all = os.environ.get("ANNOUNCEMENTS_ALL", "").lower() in ("1", "true", "yes")

    try:
        resp = list_call_get_all_results(
            announcements_client.list_announcements,
            compartment_id,
            lifecycle_state="ACTIVE",
            platform_type="IAAS",
        )
    except oci.exceptions.ServiceError as e:
        print(f"Announcements API error: {e.status} — {e.message}")
        return

    all_items = resp.data or []
    if not all_items:
        print("No active IAAS announcements for this tenancy.")
        return

    items = all_items if show_all else [a for a in all_items if a.announcement_type in _PLANNED_ANNOUNCEMENT_TYPES]

    if not items:
        other_types = sorted({a.announcement_type for a in all_items})
        print(
            f"No PLANNED_* / SCHEDULED_MAINTENANCE announcements in the active list. "
            f"Other announcement_type values present: {other_types}\n"
            "Set ANNOUNCEMENTS_ALL=1 to print every active IAAS announcement."
        )
        return

    for ann in items:
        print("--- Announcement ---")
        print(f"Type: {ann.announcement_type}")
        print(f"Summary: {ann.summary}")
        print(f"Ticket: {ann.reference_ticket_number}")
        if ann.time_one_value:
            print(f"{ann.time_one_title or 'Time'}: {ann.time_one_value}")
        if ann.time_two_value:
            print(f"{ann.time_two_title or 'Time'}: {ann.time_two_value}")
        if ann.affected_regions:
            print(f"Regions: {', '.join(ann.affected_regions)}")
        if ann.services:
            print(f"Services: {', '.join(ann.services)}")
        print(f"ID: {ann.id}")
        _handle_announcement_detail(ann, execute_reboot, planned_action)
        print()


def _handle_announcement_detail(ann, execute_reboot, planned_action):
    """GetAnnouncement once: print affected resources; optional PLANNED_CHANGE instance action."""
    try:
        detail = announcements_client.get_announcement(ann.id).data
    except oci.exceptions.ServiceError as e:
        print(f"  get_announcement failed: {e.status} — {e.message}")
        return

    resources = detail.affected_resources or []
    if resources:
        print("Affected resources:")
        for ar in resources:
            name = ar.resource_name or "(no name)"
            print(f"  - {name}  {ar.resource_id}")
    else:
        print("Affected resources: (none in API response)")

    if ann.announcement_type != "PLANNED_CHANGE":
        return
    if not execute_reboot:
        print(
            f"  (PLANNED_CHANGE actions skipped — {_EXEC_REBOOT_ENV}=0 or "
            f"_DEFAULT_EXECUTE_REBOOT_FOR_PLANNED_CHANGE=False.)"
        )
        return
    if not resources:
        print(f"  {planned_action} skipped: no affected resources.")
        return

    for ar in resources:
        rid = ar.resource_id
        label = ar.resource_name or rid
        if not _is_compute_instance_ocid(rid):
            print(f"  Action skipped (not a compute instance OCID): {label}")
            continue

        _run_planned_change_instance_action(rid, label, planned_action)


def _run_planned_change_instance_action(instance_id, label, planned_action):
    if planned_action == "REBOOTMIGRATE":
        pending = _pending_maintenance_events_for_instance(instance_id)
        if not pending:
            print(
                f"  REBOOTMIGRATE skipped for {label}: Compute has no active instance maintenance event yet."
            )
            print(
                "  Oracle only allows REBOOTMIGRATE after pending maintenance appears for this VM "
                "(PLANNED_CHANGE can arrive first). Re-run later or watch the instance maintenance section above."
            )
            return

        states = ", ".join(sorted({e.lifecycle_state for e in pending}))
        print(f"  Pending maintenance in Compute ({states}) — eligible for REBOOTMIGRATE.")
        try:
            print(f"  Calling REBOOTMIGRATE for {label} ...")
            compute_client.instance_action(instance_id, "REBOOTMIGRATE")
            print(f"  REBOOTMIGRATE accepted for {instance_id}")
        except oci.exceptions.ServiceError as e:
            print(f"  REBOOTMIGRATE failed for {instance_id}: {e.status} — {e.message}")
        return

    if planned_action in ("SOFTRESET", "RESET"):
        try:
            print(
                f"  Calling {planned_action} for {label} (guest power cycle; does not migrate hardware by itself)."
            )
            compute_client.instance_action(instance_id, planned_action)
            print(f"  {planned_action} accepted for {instance_id}")
        except oci.exceptions.ServiceError as e:
            print(f"  {planned_action} failed for {instance_id}: {e.status} — {e.message}")
        return

    if planned_action == "FD_SOFTRESET":
        try:
            resp0 = compute_client.get_instance(instance_id)
            inst = resp0.data
            etag = _response_etag(resp0)
        except oci.exceptions.ServiceError as e:
            print(f"  get_instance failed for {instance_id}: {e.status} — {e.message}")
            return

        if not inst.availability_domain or not inst.fault_domain:
            print("  FD_SOFTRESET skipped: instance missing availability_domain or fault_domain.")
            return

        target_fd = _alternate_fault_domain_in_ad(inst)
        if not target_fd:
            print("  FD_SOFTRESET skipped: no other fault domain in this availability domain.")
            return

        print(
            f"  Fault domain change: {inst.fault_domain} → {target_fd} (same AD), then SOFTRESET."
        )
        try:
            compute_client.update_instance(
                instance_id,
                UpdateInstanceDetails(fault_domain=target_fd),
                if_match=etag,
            )
            print("  update_instance (fault_domain) succeeded.")
        except oci.exceptions.ServiceError as e:
            print(f"  update_instance (fault_domain) failed: {e.status} — {e.message}")
            print(
                "  Oracle often requires the VM to be STOPped before changing fault domain; "
                "or the shape may not support in-place FD moves. Use Console/CLI to STOP, "
                "change FD, START, or use OKE node replacement if this is a worker node."
            )
            return

        try:
            print(f"  Calling SOFTRESET for {label} ...")
            resp1 = compute_client.get_instance(instance_id)
            etag1 = _response_etag(resp1)
            compute_client.instance_action(instance_id, "SOFTRESET", if_match=etag1)
            print(f"  SOFTRESET accepted for {instance_id}")
        except oci.exceptions.ServiceError as e:
            print(f"  SOFTRESET failed for {instance_id}: {e.status} — {e.message}")


def main():
    compartment_id = _compartment_scope()
    check_instance_maintenance_events(compartment_id)
    check_service_announcements(compartment_id)


if __name__ == "__main__":
    main()

"""
Author: Gururaj Mohan
Date: 2026-04-02

Query OCI Compute instance maintenance events. The Console "Status" column maps to the API field
lifecycle_state (e.g. SCHEDULED, PROCESSING, CANCELED, SUCCEEDED). There is no separate "status" field.
Also prints maintenance_category, maintenance_reason, and related fields.

Uses the same config as migrate_fd.py (~/.oci/config, OCI_CLI_PROFILE).

Limit scope to a compartment (optional): list_instance_maintenance_events uses the same scope
resolution — OCI_COMPARTMENT_ID or OCI_COMPARTMENT_OCID, else compartmentId in the profile, else
tenancy OCID.

  export OCI_COMPARTMENT_ID=ocid1.compartment.oc1...
  python check_instance_maintenance.py

Examples:
  # All events in compartment scope (default: tenancy if env unset) — paginated
  python check_instance_maintenance.py

  # Filter to one instance
  INSTANCE_OCID=ocid1.instance... python check_instance_maintenance.py

  # MATCH_ALL: filter on WANT_* fields. Use ANY (or *) to skip that dimension.
  # Defaults: lifecycle=ANY, category=MANDATORY, reason=FIRMWARE_UPDATE (so CANCELED still shows
  # if category/reason match). For legacy "only PROCESSING" use WANT_LIFECYCLE_STATE=PROCESSING.
  MATCH_ALL=1 WANT_LIFECYCLE_STATE=PROCESSING python check_instance_maintenance.py

  # Get one event by OCID (from list output)
  INSTANCE_MAINTENANCE_EVENT_OCID=ocid1.instance... python check_instance_maintenance.py

  # Only canceled maintenance (Console label "Canceled"; API lifecycle_state=CANCELED)
  MATCH_CANCELLED=1 INSTANCE_OCID=ocid1.instance... python check_instance_maintenance.py

  # Or any lifecycle filter server-side: SCHEDULED, STARTED, PROCESSING, SUCCEEDED, FAILED, CANCELED
  LIFECYCLE_STATE_FILTER=CANCELED python check_instance_maintenance.py
"""

import os
import sys

import oci
from oci.exceptions import ConfigFileNotFound, InvalidConfig, ProfileNotFound
from oci.pagination import list_call_get_all_results

_DEFAULT_OCI_PROFILE = "GURU"
_config_path = os.environ.get("OCI_CONFIG_FILE", os.path.expanduser("~/.oci/config"))
_profile = os.environ.get("OCI_CLI_PROFILE", _DEFAULT_OCI_PROFILE)

try:
    config = oci.config.from_file(_config_path, _profile)
    compute_client = oci.core.ComputeClient(config)
except ConfigFileNotFound as e:
    raise SystemExit(f"OCI config not found: {_config_path}\n{e}") from e
except ProfileNotFound as e:
    raise SystemExit(f"Profile [{_profile}] not in {_config_path}\n{e}") from e
except InvalidConfig as e:
    raise SystemExit(f"Invalid OCI config: {e.errors}") from e


def _compartment_scope():
    return (
        os.environ.get("OCI_COMPARTMENT_ID")
        or os.environ.get("OCI_COMPARTMENT_OCID")
        or config.get("compartmentId")
        or config["tenancy"]
    )


def _env_truthy(name):
    return os.environ.get(name, "").lower() in ("1", "true", "yes")


_ALLOWED_LIFECYCLE = frozenset(
    {"SCHEDULED", "STARTED", "PROCESSING", "SUCCEEDED", "FAILED", "CANCELED"}
)


def _list_kwargs(instance_id):
    """instance_id + optional lifecycle_state (MATCH_CANCELLED or LIFECYCLE_STATE_FILTER)."""
    kwargs = {}
    if instance_id:
        kwargs["instance_id"] = instance_id

    ls_filter = os.environ.get("LIFECYCLE_STATE_FILTER", "").strip().upper()
    if _env_truthy("MATCH_CANCELLED"):
        if ls_filter and ls_filter != "CANCELED":
            raise SystemExit(
                "MATCH_CANCELLED=1 conflicts with LIFECYCLE_STATE_FILTER "
                f"(use only one; expected CANCELED, got {ls_filter!r})."
            )
        ls_filter = "CANCELED"
    if ls_filter:
        if ls_filter not in _ALLOWED_LIFECYCLE:
            raise SystemExit(
                f"LIFECYCLE_STATE_FILTER must be one of: {', '.join(sorted(_ALLOWED_LIFECYCLE))}"
            )
        kwargs["lifecycle_state"] = ls_filter
    return kwargs


def _parse_match_all_filters(match_all):
    """When MATCH_ALL: lifecycle defaults to ANY (unset); category/reason default MANDATORY/FIRMWARE_UPDATE."""
    if not match_all:
        return None, None, None

    def _field(name, default_if_unset):
        if name not in os.environ:
            return default_if_unset
        raw = os.environ.get(name, "").strip().upper()
        if raw in ("", "*", "ANY"):
            return None
        return raw

    want_state = _field("WANT_LIFECYCLE_STATE", None)
    want_cat = _field("WANT_MAINTENANCE_CATEGORY", "MANDATORY")
    want_reason = _field("WANT_MAINTENANCE_REASON", "FIRMWARE_UPDATE")
    return want_state, want_cat, want_reason


def _event_matches_match_all(ev, want_state, want_cat, want_reason):
    if want_state and (getattr(ev, "lifecycle_state", None) or "").upper() != want_state:
        return False
    if want_cat and (getattr(ev, "maintenance_category", None) or "").upper() != want_cat:
        return False
    if want_reason and (getattr(ev, "maintenance_reason", None) or "").upper() != want_reason:
        return False
    return True


def _print_match_all_mismatch_help(items, want_state, want_cat, want_reason):
    print("No events matched your MATCH_ALL filters. Values returned by the API:")
    for i, ev in enumerate(items, 1):
        ls = (getattr(ev, "lifecycle_state", None) or "").upper()
        cat = (getattr(ev, "maintenance_category", None) or "").upper()
        reason = (getattr(ev, "maintenance_reason", None) or "").upper()
        eid = getattr(ev, "id", None)
        print(f"  [{i}] id={eid}")
        print(
            f"       lifecycle_state={ls}, maintenance_category={cat}, "
            f"maintenance_reason={reason}"
        )
    print(
        "\nHint: set WANT_LIFECYCLE_STATE=PROCESSING for the old default, "
        "or WANT_MAINTENANCE_CATEGORY=ANY / WANT_MAINTENANCE_REASON=ANY to widen the filter."
    )


def main():
    event_id = os.environ.get("INSTANCE_MAINTENANCE_EVENT_OCID", "").strip()

    if event_id:
        try:
            ev = compute_client.get_instance_maintenance_event(event_id).data
        except oci.exceptions.ServiceError as e:
            print(f"get_instance_maintenance_event failed: {e.status} — {e.message}")
            sys.exit(1)
        _print_event("Single event", ev)
        return

    compartment_id = _compartment_scope()
    instance_id = os.environ.get("INSTANCE_OCID", "").strip()

    if _env_truthy("MATCH_ALL") and _env_truthy("MATCH_CANCELLED"):
        raise SystemExit("Use MATCH_ALL=1 or MATCH_CANCELLED=1, not both.")

    kwargs = _list_kwargs(instance_id)

    try:
        items = list_call_get_all_results(
            compute_client.list_instance_maintenance_events,
            compartment_id,
            **kwargs,
        ).data or []
    except oci.exceptions.ServiceError as e:
        print(f"list_instance_maintenance_events failed: {e.status} — {e.message}")
        sys.exit(1)

    # If filtering by instance but tenancy-scoped list is empty, retry with the instance's compartment.
    if instance_id and not items:
        try:
            inst = compute_client.get_instance(instance_id).data
            cid_inst = inst.compartment_id
            if cid_inst and cid_inst != compartment_id:
                items = list_call_get_all_results(
                    compute_client.list_instance_maintenance_events,
                    cid_inst,
                    **kwargs,
                ).data or []
                if items:
                    print(
                        f"(Listed using instance compartment instead of scope: {cid_inst})\n"
                    )
        except oci.exceptions.ServiceError as e:
            print(f"get_instance (retry path) failed: {e.status} — {e.message}")

    match_all = _env_truthy("MATCH_ALL")
    match_cancelled = _env_truthy("MATCH_CANCELLED")
    want_state, want_cat, want_reason = _parse_match_all_filters(match_all)

    print(f"Region (config): {config.get('region')}")
    print(f"Compartment: {compartment_id}")
    if instance_id:
        print(f"Instance filter: {instance_id}")
    if match_cancelled or kwargs.get("lifecycle_state") == "CANCELED":
        print("Lifecycle filter: CANCELED (Console shows 'Canceled')")
    if match_all:
        print(
            "MATCH_ALL filters (ANY = do not filter that field): "
            f"lifecycle_state={want_state or 'ANY'}, "
            f"maintenance_category={want_cat or 'ANY'}, "
            f"maintenance_reason={want_reason or 'ANY'}"
        )
    print(f"Found {len(items)} event(s).\n")

    if len(items) == 0:
        print(
            "Note: Zero events is common. Compute only shows rows when an InstanceMaintenanceEvent\n"
            "exists for your scope. You may only have a PLANNED_CHANGE announcement (no per-VM row yet),\n"
            "maintenance may have completed, or you need INSTANCE_OCID=<instance-ocid> for that VM.\n"
            "Confirm config region matches the instance's home region.\n"
        )

    shown = 0
    for ev in items:
        if match_all and not _event_matches_match_all(ev, want_state, want_cat, want_reason):
            continue
        shown += 1
        _print_event(f"Event {shown}", ev)

    if match_all:
        if shown == 0 and items:
            _print_match_all_mismatch_help(items, want_state, want_cat, want_reason)
            sys.exit(1)
        if shown == 0:
            print(
                "No events from API for this scope, or nothing matched your MATCH_ALL filters."
            )
            sys.exit(1)
        print(f"\nOK: {shown} matching event(s).")
        sys.exit(0)

    if match_cancelled:
        if shown == 0:
            print("No CANCELED instance maintenance events for this scope (or not canceled yet).")
            sys.exit(1)
        print(f"\nOK: {shown} CANCELED event(s) — maintenance was canceled for those instance(s).")
        sys.exit(0)


def _print_event(title, ev):
    # InstanceMaintenanceEvent vs Summary — attribute names align in SDK.
    # Console "Status" column = API lifecycle_state (no separate "status" field).
    eid = getattr(ev, "id", None)
    print(f"--- {title} ---")
    print(f"  id:                    {eid}")
    print(f"  display_name:          {getattr(ev, 'display_name', None)}")
    print(f"  status:                {getattr(ev, 'lifecycle_state', None)}")
    print(f"    (API field lifecycle_state; Console Status — e.g. SCHEDULED, PROCESSING, CANCELED, SUCCEEDED)")
    print(f"  maintenance_category:  {getattr(ev, 'maintenance_category', None)}")
    print(f"  maintenance_reason:    {getattr(ev, 'maintenance_reason', None)}")
    print(f"  instance_id:           {getattr(ev, 'instance_id', None)}")
    print(f"  instance_action:       {getattr(ev, 'instance_action', None)}")
    print(f"  time_window_start:     {getattr(ev, 'time_window_start', None)}")
    print(f"  time_started:          {getattr(ev, 'time_started', None)}")
    print(f"  time_finished:         {getattr(ev, 'time_finished', None)}")
    print()


if __name__ == "__main__":
    main()

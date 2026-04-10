"""
Author: Gururaj Mohan-Oracle
Date: 2026-04-04

List OCI Compute instance maintenance events, evaluate maintenance window / start times, and send
email notifications with instance details.

Uses ~/.oci/config and OCI_CLI_PROFILE (same as migrate_fd.py).

Typical use: run nightly or hourly via cron. Set NOTIFY_DRY_RUN=1 to print messages without publishing.

Delivery — OCI Notifications only (configure an email subscription on the topic in the Console):

  OCI_NOTIFICATION_TOPIC_OCID — OCID of an ONS topic; messages are published with title + body.
    Optional: a default topic is set in this script; export this variable to override.
  OCI_NOTIFICATION_REGION — optional; defaults to region in topic OCID or OCI config `region`.

  IAM: publishing requires permission to use the topic (e.g. ONS topic publish in compartment).

Environment (behavior):
  NOTIFY_LEAD_HOURS=48     — remind when time_window_start is within this many hours (future window).
  NOTIFY_ON_SCHEDULED=1    — send reminder for SCHEDULED events in the lead window (default on).
  NOTIFY_ON_ACTIVE=1       — email when lifecycle is STARTED or PROCESSING (default on).
  NOTIFY_STATE_FILE=path   — JSON file to dedupe notifications (default: ~/.oci/maintenance_notify_state.json).
  NOTIFY_DEDUPE=1          — skip repeat emails for same event+phase (default on). Set 0 to always email.
  NOTIFY_DRY_RUN=1         — print message only, do not publish to the topic.

  NOTIFY_SEND_TEST=1       — publish a short test message to the topic (skips maintenance listing).
    NOTIFY_DRY_RUN=1 only prints the message — it does **not** publish, so you will not get email.

  NOTIFY_TENANCY_SCOPE — controls **which OCID** is passed to list_instance_maintenance_events:
    • **On** (default if unset): use **tenancy root** from the OCI profile (`tenancy` in ~/.oci/config).
    • **Off** (0 / false / no): use **compartment scope** — env `OCI_COMPARTMENT_ID` / `OCI_COMPARTMENT_OCID`,
      else profile `compartment_id` (or `compartmentId`), else tenancy OCID.
      (`OCI_COMPARTMENT_ID` is an **environment** name; it is not a key inside `~/.oci/config`.)
  NOTIFY_REGIONS — comma-separated region names to query (e.g. us-ashburn-1,us-chicago-1).
    **Instance maintenance listing is regional:** the home region alone often returns no rows even
    at tenancy scope if VMs live in other regions. Default is a single region from config `region`.
  INSTANCE_OCID=...        — only events for this instance (optional).

  LIFECYCLE_STATE_FILTER     — optional server-side filter (same as check_instance_maintenance.py).
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import oci
from oci.exceptions import ConfigFileNotFound, InvalidConfig, ProfileNotFound, ServiceError
from oci.pagination import list_call_get_all_results

_DEFAULT_OCI_PROFILE = "Default"
_config_path = os.environ.get("OCI_CONFIG_FILE", os.path.expanduser("~/.oci/config"))
_profile = os.environ.get("OCI_CLI_PROFILE", _DEFAULT_OCI_PROFILE)

_ACTIVE_LIFECYCLE = frozenset({"STARTED", "PROCESSING"})
_SCHEDULED = "SCHEDULED"

_compute_by_region: dict[str, oci.core.ComputeClient] = {}

try:
    config = oci.config.from_file(_config_path, _profile)
except ConfigFileNotFound as e:
    raise SystemExit(f"OCI config not found: {_config_path}\n{e}") from e
except ProfileNotFound as e:
    raise SystemExit(f"Profile [{_profile}] not in {_config_path}.\n{e}") from e
except InvalidConfig as e:
    raise SystemExit(f"Invalid OCI config: {e.errors}") from e

# Default Notifications topic OCID (US Ashburn / iad). Override per environment:
#   export OCI_NOTIFICATION_TOPIC_OCID=ocid1.onstopic...
_DEFAULT_NOTIFICATION_TOPIC_OCID = (
    ""
)


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).lower() in ("1", "true", "yes")


def _compartment_scope() -> str:
    """Resolve scope OCID: env vars first, then profile (compartment_id or compartmentId), else tenancy."""
    return (
        os.environ.get("OCI_COMPARTMENT_ID")
        or os.environ.get("OCI_COMPARTMENT_OCID")
        or config.get("compartment_id")
        or config.get("compartmentId")
        or config["tenancy"]
    )


def _tenancy_list_scope_enabled() -> bool:
    """True → list at tenancy root (`config['tenancy']`). False → `_compartment_scope()` chain."""
    raw = os.environ.get("NOTIFY_TENANCY_SCOPE")
    if raw is None or str(raw).strip() == "":
        return True
    if str(raw).lower() in ("0", "false", "no"):
        return False
    return str(raw).lower() in ("1", "true", "yes")


def _list_maintenance_scope_ocid() -> tuple[bool, str]:
    """Return (use_tenancy_root, compartment_id_for_api).

    When use_tenancy_root is True, the API scope is the profile tenancy OCID (full tree in that region).
    When False, the scope follows OCI_COMPARTMENT_* / profile compartmentId / tenancy fallback.
    """
    if _tenancy_list_scope_enabled():
        return (True, config["tenancy"])
    return (False, _compartment_scope())


def _region_from_instance_ocid(ocid: str | None) -> str | None:
    m = re.match(r"^ocid1\.instance\.oc1\.([a-z0-9-]+)\.", ocid or "")
    return m.group(1) if m else None


def _region_from_topic_ocid(ocid: str | None) -> str | None:
    m = re.match(r"^ocid1\.onstopic\.oc1\.([a-z0-9-]+)\.", ocid or "")
    return m.group(1) if m else None


def _config_region() -> str | None:
    return (config.get("region") or "").strip() or None


def _compute_for_region(region_name: str) -> oci.core.ComputeClient:
    if region_name not in _compute_by_region:
        _compute_by_region[region_name] = oci.core.ComputeClient(
            config, region_name=region_name
        )
    return _compute_by_region[region_name]


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _list_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    iid = os.environ.get("INSTANCE_OCID", "").strip()
    if iid:
        kwargs["instance_id"] = iid
    ls = os.environ.get("LIFECYCLE_STATE_FILTER", "").strip().upper()
    allowed = frozenset(
        {"SCHEDULED", "STARTED", "PROCESSING", "SUCCEEDED", "FAILED", "CANCELED"}
    )
    if ls:
        if ls not in allowed:
            raise SystemExit(
                f"LIFECYCLE_STATE_FILTER must be one of: {', '.join(sorted(allowed))}"
            )
        kwargs["lifecycle_state"] = ls
    return kwargs


def _regions_to_query() -> list[str]:
    """Regions to call list_instance_maintenance_events against (API is per-region)."""
    raw = os.environ.get("NOTIFY_REGIONS", os.environ.get("OCI_REGIONS", "")).strip()
    if raw:
        return [r.strip() for r in raw.split(",") if r.strip()]
    home = _config_region()
    if not home:
        raise SystemExit("OCI config profile must set 'region', or set NOTIFY_REGIONS explicitly.")
    return [home]


def _list_events(cid: str, kwargs: dict[str, Any], regions: list[str]) -> tuple[list, list[str]]:
    """Return (merged events, regions queried without API error). Dedupe by event id."""
    seen: set[str] = set()
    merged: list = []
    queried: list[str] = []
    for reg in regions:
        cc = _compute_for_region(reg)
        try:
            batch = (
                list_call_get_all_results(
                    cc.list_instance_maintenance_events,
                    cid,
                    **kwargs,
                ).data
                or []
            )
        except oci.exceptions.ServiceError as e:
            print(
                f"list_instance_maintenance_events ({reg}) failed: {e.status} — {e.message}",
                file=sys.stderr,
            )
            continue
        queried.append(reg)
        for ev in batch:
            eid = getattr(ev, "id", None) or ""
            if eid and eid in seen:
                continue
            if eid:
                seen.add(eid)
            merged.append(ev)
    return merged, queried


def _get_instance_safe(instance_id: str) -> Any | None:
    region = _region_from_instance_ocid(instance_id) or _config_region()
    if not region:
        return None
    try:
        return _compute_for_region(region).get_instance(instance_id).data
    except oci.exceptions.ServiceError:
        return None


def _default_state_file() -> str:
    return os.environ.get(
        "NOTIFY_STATE_FILE",
        os.path.join(os.path.expanduser("~/.oci"), "maintenance_notify_state.json"),
    )


def _load_state(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def _state_key(event_id: str, phase: str) -> str:
    return f"{event_id}|{phase}"


def _format_event_block(ev: Any, inst: Any | None) -> str:
    lines = [
        f"  Maintenance event ID: {getattr(ev, 'id', None)}",
        f"  Display name:           {getattr(ev, 'display_name', None)}",
        f"  lifecycle_state:        {getattr(ev, 'lifecycle_state', None)}",
        f"  maintenance_category:   {getattr(ev, 'maintenance_category', None)}",
        f"  maintenance_reason:     {getattr(ev, 'maintenance_reason', None)}",
        f"  time_created:           {getattr(ev, 'time_created', None)}",
        f"  time_window_start:      {getattr(ev, 'time_window_start', None)}",
        f"  time_started:           {getattr(ev, 'time_started', None)}",
        f"  time_finished:          {getattr(ev, 'time_finished', None)}",
        f"  instance_id:            {getattr(ev, 'instance_id', None)}",
    ]
    if inst:
        lines.extend(
            [
                f"  instance display_name:  {getattr(inst, 'display_name', None)}",
                f"  instance compartment:   {getattr(inst, 'compartment_id', None)}",
                f"  availability_domain:    {getattr(inst, 'availability_domain', None)}",
                f"  fault_domain:           {getattr(inst, 'fault_domain', None)}",
                f"  region (from OCID):     {_region_from_instance_ocid(getattr(ev, 'instance_id', '') or '')}",
            ]
        )
    else:
        lines.append("  (instance details unavailable — check IAM or regional endpoint.)")
    return "\n".join(lines)


def _ons_topic_ocid() -> str:
    return (
        os.environ.get("OCI_NOTIFICATION_TOPIC_OCID", "").strip()
        or os.environ.get("ONS_TOPIC_OCID", "").strip()
        or _DEFAULT_NOTIFICATION_TOPIC_OCID
    )


def _send_ons(topic_id: str, title: str, body: str) -> None:
    """Publish to an OCI Notifications topic (email is via topic subscription in Console)."""
    region = (
        os.environ.get("OCI_NOTIFICATION_REGION", "").strip()
        or _region_from_topic_ocid(topic_id)
        or _config_region()
    )
    if not region:
        raise SystemExit(
            "Set OCI_NOTIFICATION_REGION or ensure OCI_NOTIFICATION_TOPIC_OCID includes the region "
            "segment, or set region in ~/.oci/config."
        )
    # 64 KB limit for publish payload (title + body); trim if needed.
    max_body = 60000
    if len(body) > max_body:
        body = body[:max_body] + "\n\n[truncated for ONS size limit]"

    dp = oci.ons.NotificationDataPlaneClient(config, region_name=region)
    details = oci.ons.models.MessageDetails(title=title[:256], body=body)
    dp.publish_message(topic_id, details)


def _notify_send(subject: str, body: str, dry: bool) -> None:
    """Publish to the OCI Notifications topic (unless dry-run)."""
    if dry:
        return
    topic = _ons_topic_ocid()
    if not topic:
        raise SystemExit(
            "Set OCI_NOTIFICATION_TOPIC_OCID (or rely on the default in this script). "
            "Use NOTIFY_DRY_RUN=1 to test without publishing."
        )
    _send_ons(topic, subject, body)


def _validate_delivery_config(dry: bool) -> None:
    if dry:
        return
    if not _ons_topic_ocid():
        raise SystemExit(
            "No Notifications topic OCID: set OCI_NOTIFICATION_TOPIC_OCID or define "
            "_DEFAULT_NOTIFICATION_TOPIC_OCID in the script. Use NOTIFY_DRY_RUN=1 to test without publishing."
        )


def _test_message_payload() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    subject = "[OCI maintenance notify] test message"
    body = "\n".join(
        [
            "This is a test publish from notify_instance_maintenance.py.",
            f"Time (UTC): {now.isoformat()}",
            f"OCI_CLI_PROFILE / config profile: {_profile}",
            "",
            "If you receive this by email, your topic subscription is working.",
        ]
    )
    return subject, body


def main() -> None:
    lead_h = float(os.environ.get("NOTIFY_LEAD_HOURS", "48"))
    notify_scheduled = _env_truthy("NOTIFY_ON_SCHEDULED", True)
    notify_active = _env_truthy("NOTIFY_ON_ACTIVE", True)
    dedupe = _env_truthy("NOTIFY_DEDUPE", True)
    dry = _env_truthy("NOTIFY_DRY_RUN", False)

    if _env_truthy("NOTIFY_SEND_TEST"):
        _validate_delivery_config(dry)
        topic = _ons_topic_ocid()
        subj, body = _test_message_payload()
        region_used = (
            os.environ.get("OCI_NOTIFICATION_REGION", "").strip()
            or _region_from_topic_ocid(topic)
            or _config_region()
            or "?"
        )
        print(
            f"NOTIFY_SEND_TEST: topic {topic[:60]}…  DRY_RUN={dry}\n"
            f"Publish region (ONS): {region_used}\n"
            f"Subject: {subj}\n"
        )
        if dry:
            print(body)
            print(
                "\nNOTIFY_DRY_RUN is on — nothing was published to OCI Notifications, "
                "so email subscriptions will not fire.\n"
                "To actually test email: unset NOTIFY_DRY_RUN, then run again with NOTIFY_SEND_TEST=1.\n"
            )
        else:
            try:
                _notify_send(subj, body, dry=False)
            except ServiceError as e:
                raise SystemExit(
                    f"OCI Notifications publish failed ({e.status}): {e.message}\n"
                    "Check: IAM policy allows this user to PUBLISH_MESSAGE on the topic; "
                    "OCI_NOTIFICATION_REGION matches the topic's home region if in doubt."
                ) from e
            print(
                "Publish returned OK. If no email: confirm your Email subscription is on **this same** "
                "topic OCID (export OCI_NOTIFICATION_TOPIC_OCID=… from the topic in Console), "
                "subscription status is **Active** (confirm the subscription email), and check spam.\n"
            )
        print("Done. (test path — no maintenance listing)")
        return

    if not notify_scheduled and not notify_active:
        raise SystemExit("Enable at least one of NOTIFY_ON_SCHEDULED or NOTIFY_ON_ACTIVE.")

    _validate_delivery_config(dry)

    use_tenancy_list, cid = _list_maintenance_scope_ocid()
    kwargs = _list_kwargs()
    print(f"Region (config): {config.get('region')}")
    if use_tenancy_list:
        print(f"List scope: tenancy (root) — {cid}")
    else:
        print(f"List scope: compartment — {cid}")
        if cid == config["tenancy"] and not (
            os.environ.get("OCI_COMPARTMENT_ID") or os.environ.get("OCI_COMPARTMENT_OCID")
        ):
            print(
                "  (No OCI_COMPARTMENT_ID / OCI_COMPARTMENT_OCID set; profile compartmentId also unset — "
                "using tenancy OCID as compartment_id.)"
            )
    print(f"Filters: {kwargs}")
    topic = _ons_topic_ocid()
    delivery = f"OCI Notifications topic {topic[:60]}…" if topic else "(no topic — use NOTIFY_DRY_RUN or set OCID)"
    print(
        f"NOTIFY_LEAD_HOURS={lead_h}, NOTIFY_ON_SCHEDULED={notify_scheduled}, "
        f"NOTIFY_ON_ACTIVE={notify_active}, DEDUPE={dedupe}, DRY_RUN={dry}"
    )
    print(f"Delivery: {delivery}\n")

    regions_plan = _regions_to_query()
    print(f"Compute regions to query: {', '.join(regions_plan)}")

    events, regions_queried = _list_events(cid, kwargs, regions_plan)
    if regions_queried:
        print(f"Regions queried successfully: {', '.join(regions_queried)} — {len(events)} event(s) total.\n")
    if not events:
        print(
            "No instance maintenance events returned for this scope and region(s).\n"
            "If you expect events, list maintenance is **regional**: add all regions where you run VMs, e.g.\n"
            "  export NOTIFY_REGIONS=us-ashburn-1,us-chicago-1,us-phoenix-1\n"
            "Or there may be no matching maintenance in the NOTIFY_LEAD_HOURS / lifecycle filters.\n"
            "To test email/topic delivery without maintenance: NOTIFY_SEND_TEST=1"
        )
        return

    now = datetime.now(timezone.utc)
    lead_delta = timedelta(hours=lead_h)
    state_path = _default_state_file()
    state = _load_state(state_path) if dedupe else {}

    notifications_sent = 0
    for ev in events:
        eid = getattr(ev, "id", None) or ""
        ls = (getattr(ev, "lifecycle_state", None) or "").upper()
        tws = _parse_dt(getattr(ev, "time_window_start", None))
        tcr = _parse_dt(getattr(ev, "time_created", None))

        phases: list[str] = []

        if notify_active and ls in _ACTIVE_LIFECYCLE:
            phases.append("active")

        if notify_scheduled and ls == _SCHEDULED and tws is not None:
            if now < tws <= now + lead_delta:
                phases.append("upcoming")

        if not phases:
            continue

        for phase in phases:
            key = _state_key(eid, phase)
            if dedupe and state.get(key):
                continue

            inst = _get_instance_safe(getattr(ev, "instance_id", "") or "")
            subject = (
                f"[OCI maintenance] {ls} — {getattr(ev, 'display_name', eid or 'event')}"
            )
            body_lines = [
                "OCI Compute — instance maintenance notification",
                "",
                f"Generated (UTC): {now.isoformat()}",
                f"Notification phase: {phase}",
                "  (upcoming = SCHEDULED and maintenance window starts within NOTIFY_LEAD_HOURS;",
                "   active = STARTED or PROCESSING)",
                "",
                _format_event_block(ev, inst),
                "",
                f"Summary: time_created={tcr!s}, time_window_start={tws!s}",
            ]
            body = "\n".join(body_lines)

            if dry:
                print("=" * 60)
                print(f"Subject: {subject}\n")
                print(body)
                print("=" * 60)
            else:
                _notify_send(subject, body, dry=False)

            if dedupe:
                state[key] = {"notified_at": now.isoformat(), "lifecycle": ls}
                _save_state(state_path, state)
            notifications_sent += 1

    print(f"\nDone. Notifications sent or printed: {notifications_sent}")


if __name__ == "__main__":
    main()

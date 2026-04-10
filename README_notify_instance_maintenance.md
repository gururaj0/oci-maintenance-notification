# `notify_instance_maintenance.py` — OCI Compute maintenance alerts (OCI Notifications)

Lists **OCI Compute instance maintenance events** (`list_instance_maintenance_events`), filters by schedule and lifecycle, and **publishes** messages to an **OCI Notifications** topic. Subscribers (for example **Email**) receive the alert according to how the topic is configured in the Console.

**Author:** Gururaj Mohan-Oracle · **Date:** 2026-04-04

## What it does

- **Upcoming:** `SCHEDULED` events whose `time_window_start` falls within **`NOTIFY_LEAD_HOURS`** (default 48h ahead).
- **Active:** `STARTED` or `PROCESSING` (when **`NOTIFY_ON_ACTIVE`** is on).
- Includes instance details when **`get_instance`** succeeds (regional Compute client).
- **Dedupes** repeated sends per event + phase using a JSON state file (optional).

It does **not** use SMTP; delivery is **OCI Notifications only**.

## Requirements

- Python 3.x  
- [`oci`](https://docs.oracle.com/en-us/iaas/tools/python/latest/) Python SDK  
- Valid **`~/.oci/config`** (or **`OCI_CONFIG_FILE`**) with profile **`OCI_CLI_PROFILE`** (script default profile is `ORASENATDPLTINTEGRATION03` if unset).  
- An **OCI Notifications** topic and IAM permission to **publish** to it (for example `ONS_TOPIC_PUBLISH` or equivalent policy on that topic / compartment).  
- For email: an **Email** subscription on **that same topic**, confirmed (**Active**).

## Quick start

```bash
pip install oci
export OCI_CLI_PROFILE=your_profile
# Set your topic (recommended — script may ship with a default topic OCID for override/testing)
export OCI_NOTIFICATION_TOPIC_OCID=ocid1.onstopic.oc1...

python3 notify_instance_maintenance.py
```

Use **`NOTIFY_DRY_RUN=1`** first to print what would be sent without publishing.

## Test topic + email without maintenance events

Publishing is skipped when **`NOTIFY_DRY_RUN=1`** — you will **not** get email.

```bash
# Real publish to ONS (triggers email if subscription is on this topic)
unset NOTIFY_DRY_RUN
export NOTIFY_SEND_TEST=1
python3 notify_instance_maintenance.py
```

**`NOTIFY_SEND_TEST=1`** skips maintenance listing and sends a short canned message. Use this to verify IAM, region, and email subscription.

## OCI Notifications

| Item | Notes |
|------|--------|
| **`OCI_NOTIFICATION_TOPIC_OCID`** | Topic OCID. Optional if the script defines a default; **always set this to your topic** in production so publishes match your Console subscriptions. |
| **`OCI_NOTIFICATION_REGION`** | Home region of the topic if not inferable from the OCID or config `region`. |
| **`ONS_TOPIC_OCID`** | Alternate env name accepted for the topic OCID. |

Email is **not** sent by this script directly; it arrives only if the topic has an **Email** subscription and the address is **confirmed**.

## Listing scope and tenancy

The **tenancy OCID** comes from **`tenancy=`** in your **`~/.oci/config`** profile (loaded by the OCI SDK).

| `NOTIFY_TENANCY_SCOPE` | Scope passed to the API |
|------------------------|-------------------------|
| **On** (default if unset) | **`config['tenancy']`** — tenancy root |
| **Off** (`0` / `false` / `no`) | **`OCI_COMPARTMENT_ID`** or **`OCI_COMPARTMENT_OCID`** (environment), then profile **`compartment_id`** or **`compartmentId`**, else tenancy OCID |

**Note:** `OCI_COMPARTMENT_ID` is an **environment** variable name. Inside the config file the usual key is **`compartment_id=`**, not `OCI_COMPARTMENT_ID`.

## Regions (important)

**Instance maintenance listing is per Compute region.** If you only query the profile’s home region, you may see **no events** while VMs exist in other regions.

```bash
export NOTIFY_REGIONS=us-ashburn-1,us-chicago-1,us-phoenix-1
python3 notify_instance_maintenance.py
```

You can also set **`OCI_REGIONS`** as an alias for the same comma-separated list (`NOTIFY_REGIONS` is checked first).

## Environment variables (behavior)

| Variable | Default | Purpose |
|----------|---------|---------|
| **`NOTIFY_LEAD_HOURS`** | `48` | For `SCHEDULED`: notify if `time_window_start` is within this many hours (and still in the future). |
| **`NOTIFY_ON_SCHEDULED`** | on | Remind for qualifying `SCHEDULED` events. |
| **`NOTIFY_ON_ACTIVE`** | on | Notify for `STARTED` / `PROCESSING`. |
| **`NOTIFY_DEDUPE`** | on | Skip repeats using state file; set `0` to always notify. |
| **`NOTIFY_STATE_FILE`** | `~/.oci/maintenance_notify_state.json` | Dedupe state path. |
| **`NOTIFY_DRY_RUN`** | off | Print only; **no** publish to Notifications. |
| **`NOTIFY_SEND_TEST`** | off | One test publish; skips maintenance listing. |
| **`INSTANCE_OCID`** | — | Limit listing to one instance. |
| **`LIFECYCLE_STATE_FILTER`** | — | Optional: `SCHEDULED`, `STARTED`, … (server-side filter). |

## Scheduling (example)

```bash
0 * * * * cd /path/to/repo && OCI_CLI_PROFILE=your_profile /usr/bin/python3 notify_instance_maintenance.py >> /var/log/notify_maintenance.log 2>&1
```

## Troubleshooting

- **No email after a successful publish:** Topic OCID must **match** the topic where you created the subscription; confirm subscription **Active**; check spam; **`NOTIFY_DRY_RUN`** must be **off** for real sends.  
- **403 / publish errors:** IAM policy must allow the config **user** (or dynamic group) to publish to that topic.  
- **Wrong region:** Set **`OCI_NOTIFICATION_REGION`** to the topic’s region.  
- **Empty maintenance list:** Expand **`NOTIFY_REGIONS`**; confirm **`NOTIFY_TENANCY_SCOPE`** and compartment envs match where instances live.

## Related scripts in this repo

- **`migrate_fd.py`** — planned change + fault-domain migration (separate workflow).  
- **`check_instance_maintenance.py`** — maintenance listing / diagnostics without notifications.

For the latest behavior and defaults, see the module docstring at the top of **`notify_instance_maintenance.py`**.

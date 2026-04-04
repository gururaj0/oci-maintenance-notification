# Solution: Planned-change announcements + instance maintenance–driven fault-domain migration

## Problem this addresses

Oracle Cloud **Infrastructure** can schedule **host / platform maintenance** that affects your VMs. The **Service Health** experience surfaces **PLANNED_CHANGE** announcements that list affected resources. Those announcements can remain **ACTIVE** even after maintenance for a specific instance is **finished**, **skipped**, or **canceled** in **Compute**. Relying only on announcement timing or visibility can cause **stale** signals and unnecessary or repeated automation.

Separately, when maintenance requires moving workloads off a physical host, one operational response is to move the VM to another **fault domain** within the same **availability domain** (so the instance is not tied to the same fault-isolated rack group) and **reboot** so the change takes effect—consistent with maintenance workflows that involve **fault domain** updates plus **instance action** (e.g. soft reset).

## What the script does (high level)

The **`migrate_fd.py`** utility (and variant **`migrate_fd_404.py`**) automates a **discover → validate → optionally act** pipeline:

1. **Announcements (discovery scope)**  
   Lists **active**, **IAAS**, **`PLANNED_CHANGE`** announcements for a configurable **compartment scope** (environment variable, config profile, or tenancy). For each announcement, it reads **affected resources** and focuses on **compute instance** OCIDs.

2. **Per instance (regional Compute + Identity)**  
   For each instance, it uses the **region embedded in the instance OCID** when it differs from the profile region so **Compute** and **Identity** APIs target the correct regional endpoints (multi-region tenancies).

3. **Instance maintenance (source of truth)**  
   It queries **OCI Compute instance maintenance events** for that instance. **Console “Status”** aligns with API **`lifecycle_state`** (e.g. `SCHEDULED`, `STARTED`, `PROCESSING`, `CANCELED`, `SUCCEEDED`).

4. **Fault-domain change + reboot (when allowed)**  
   If execution is enabled, it picks an **alternate fault domain** in the same availability domain (or **`TARGET_FAULT_DOMAIN`** if set and valid), calls **`update_instance`** with the new **`fault_domain`**, then **`instance_action`** with **`SOFTRESET`** or **`RESET`** (configurable).

## Why Compute maintenance is the gate (not the announcement alone)

- **PLANNED_CHANGE** tells you *that* Oracle scheduled something and *which* resources were associated when the announcement was built or refreshed.  
- **Instance maintenance** in **Compute** tells you the **current lifecycle** of maintenance *for that VM*.

The script therefore:

- **Skips execute** when maintenance is **canceled-only** (`CANCELED` with no `SCHEDULED` / `STARTED` / `PROCESSING`)—**not overridable** by `SKIP_FD_IF_NO_ACTIVE_MAINTENANCE=0`.  
- **By default** skips execute when there is **no active** maintenance (`SKIP_FD_IF_NO_ACTIVE_MAINTENANCE` defaults to on), so **nightly** jobs do not repeat work after maintenance is done while an announcement can still show **PLANNED_CHANGE**.  
- **Allows** execute when there is **active** maintenance in **`SCHEDULED`**, **`STARTED`**, or **`PROCESSING`** (non-canceled work in flight), subject to **`EXECUTE_FD_MIGRATE=1`**.

This reduces **stale-announcement** noise and aligns actions with **Oracle’s per-instance maintenance lifecycle**.

## Nightly execution

Running the script **every night** (e.g. **cron**, **scheduler**, or **CI**) is appropriate because:

- New **PLANNED_CHANGE** rows can appear at any time.  
- **Instance maintenance** state transitions over time; a **nightly** pass picks up VMs that newly need action.  
- Default **“skip if no active maintenance”** avoids **redoing** fault-domain work when maintenance is already complete but the announcement is still open.

**Typical patterns:**

| Mode | Purpose |
|------|--------|
| **Dry-run** (default) | `python3 migrate_fd.py` — reports what would happen; **no** API changes. |
| **Execute** | `EXECUTE_FD_MIGRATE=1` — performs **fault-domain update** and **reboot** when gates pass. |

Optional: **`OCI_COMPARTMENT_ID`** / **`OCI_COMPARTMENT_OCID`** (or **`compartmentId`** in **`~/.oci/config`**) to **limit scope** to a compartment instead of the whole tenancy.

## How this “solves” host maintenance in practice

Host maintenance is addressed by Oracle’s schedule and your operational policy; this script **automates the customer-side remediation pattern** of **moving the instance to another fault domain** in the same AD (reducing exposure to the same fault-isolated grouping) and **rebooting** so the placement change applies—**only when** Compute shows **active, non-canceled** maintenance (and optional skip overrides), so you are not driving the change from **stale** announcement state alone.

**Note:** Oracle may reject **`fault_domain`** updates on **running** instances in some cases; you may need **STOP → update → START** or cluster-specific procedures (e.g. **OKE** node cordon/drain). The script prints hints on **`update_instance`** failures.

## Key environment variables (summary)

| Variable | Role |
|----------|------|
| **`EXECUTE_FD_MIGRATE`** | `1` / `true` / `yes` to perform **update_instance** + **instance_action**; otherwise dry-run. |
| **`SKIP_FD_IF_NO_ACTIVE_MAINTENANCE`** | Default **on**: skip execute if no `SCHEDULED`/`STARTED`/`PROCESSING`. Set to `0` to allow force (still **never** for canceled-only). |
| **`FD_MIGRATE_REBOOT_ACTION`** | **`SOFTRESET`** (default) or **`RESET`**. |
| **`OCI_COMPARTMENT_ID`** or **`OCI_COMPARTMENT_OCID`** | Limit listing scope to a compartment OCID. |
| **`OCI_CLI_PROFILE`** | OCI config profile (default in code may vary by file). |

---

*Author note: see module docstrings in **`migrate_fd.py`** for authoritative behavior and examples.*

# `migrate_fd.py` — OCI planned change & fault-domain migration

Python utility that lists **active** **IAAS** **`PLANNED_CHANGE`** announcements, then for each affected **compute instance** evaluates **OCI Compute instance maintenance** and optionally moves the VM to another **fault domain** in the same availability domain and **reboots** (`SOFTRESET` / `RESET`).

**Compute maintenance** is the source of truth (announcements can stay open after work is done or canceled). See the script’s module docstring for full behavior.

## Requirements

- Python 3.x  
- [`oci`](https://docs.oracle.com/en-us/iaas/tools/python/latest/) Python SDK  
- Valid **`~/.oci/config`**; set **`OCI_CLI_PROFILE`** to your profile.

## Quick start

```bash
pip install oci
export OCI_CLI_PROFILE=your_profile
python3 migrate_fd.py          # dry-run (no API changes)
```

## Execute (real FD change + reboot)

```bash
EXECUTE_FD_MIGRATE=1 python3 migrate_fd.py
```

## Common environment variables

| Variable | Purpose |
|----------|--------|
| **`EXECUTE_FD_MIGRATE`** | `1` / `true` / `yes` to run `update_instance` + reboot; otherwise dry-run. |
| **`SKIP_FD_IF_NO_ACTIVE_MAINTENANCE`** | Default on: skip execute if no `SCHEDULED`/`STARTED`/`PROCESSING` maintenance. Set to `0` to force (never when maintenance is canceled-only). |
| **`FD_MIGRATE_REBOOT_ACTION`** | `SOFTRESET` (default) or `RESET`. |
| **`OCI_COMPARTMENT_ID`** or **`OCI_COMPARTMENT_OCID`** | Limit announcement / maintenance listing scope to a compartment. |
| **`OCI_CLI_PROFILE`** | Config profile name. |

Full details, edge cases, and multi-region behavior are documented in **`migrate_fd.py`** at the top of the file.

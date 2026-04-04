# Oracle Cloud — planned change & instance maintenance utilities

Python helpers for **PLANNED_CHANGE** announcements, **Compute instance maintenance** lifecycle checks, and optional **fault-domain migration** with reboot.

## Contents

| File | Purpose |
|------|--------|
| **`migrate_fd.py`** | Main flow: list IAAS `PLANNED_CHANGE` announcements → per instance, validate maintenance → optional FD change + `SOFTRESET`/`RESET`. |
| **`migrate_fd_404.py`** | Same logic + regional handling / filters (see its docstring). |
| **`check_instance_maintenance.py`** | Query and print instance maintenance events (`lifecycle_state`, category, reason). |
| **`migrate.py`** | Broader migration / announcement utilities. |
| **`backup_code.sh`** | Local backup of selected project files. |
| **`SOLUTION.md`** | Solution overview (stale announcements vs Compute, nightly runs, env vars). |

## Requirements

- Python 3.x  
- [`oci`](https://docs.oracle.com/en-us/iaas/tools/python/latest/) Python SDK  
- Valid **`~/.oci/config`** and API key; use **`OCI_CLI_PROFILE`** to select a profile.

## Quick start

```bash
pip install oci
export OCI_CLI_PROFILE=your_profile
python3 migrate_fd.py   # dry-run by default
```

See **`SOLUTION.md`** and each script’s module docstring for environment variables (`EXECUTE_FD_MIGRATE`, `OCI_COMPARTMENT_ID`, etc.).

## License

Use and modify per your organization’s policy; no license file is included by default.

#!/usr/bin/env bash
# Local backup of your scripts (not OCI VM backups). Run from the project directory:
#   chmod +x backup_code.sh && ./backup_code.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="${ROOT}/backups/code-backup-${STAMP}"

mkdir -p "${DEST}"

shopt -s nullglob
PY=( "${ROOT}"/*.py )
if (( ${#PY[@]} )); then
  cp "${PY[@]}" "${DEST}/"
fi
shopt -u nullglob

for dir in yaml ingress; do
  if [[ -d "${ROOT}/${dir}" ]]; then
    cp -R "${ROOT}/${dir}" "${DEST}/"
  fi
done

echo "Code backup created: ${DEST}"
echo "Contents:"
ls -la "${DEST}"

#!/usr/bin/env sh
# Give an auditor its own database and upload dir.
set -e
SLICE="$1"
cd "$(dirname "$0")/.."
rm -f "data/audit-$SLICE.db"
DATABASE_URL="sqlite:///$PWD/data/audit-$SLICE.db" .venv/bin/python seed.py >/dev/null
echo "sqlite:///$PWD/data/audit-$SLICE.db"

#!/usr/bin/env bash
# Restore one Coil backup into a directory.
#
#   ops/restore.sh <archive.tar.gz> <target-dir>
#
# Restores practice.db, uploads/, pdf/ and .env. It refuses to write into a directory
# that already holds a database, because the one time you run this is the one time you
# cannot afford to overwrite the wrong firm. Move the old data aside first.
set -euo pipefail

ARCHIVE="${1:-}"
TARGET="${2:-}"
[ -f "$ARCHIVE" ] || { echo "usage: restore.sh <archive.tar.gz> <target-dir>"; exit 2; }
[ -n "$TARGET" ] || { echo "usage: restore.sh <archive.tar.gz> <target-dir>"; exit 2; }

if [ -f "$TARGET/data/practice.db" ]; then
  echo "refusing: $TARGET/data/practice.db already exists. Move it aside first." >&2
  exit 1
fi

# The archive already carries its own leading data/ (see app/cli.py backup, which adds
# every member as data/...). Extracting into $TARGET/data therefore produced
# $TARGET/data/data/practice.db, the app found nothing where it looks, created an empty
# database, and the firm concluded its backup was worthless. On the one day this script
# is ever run. Extract at the install root, exactly as `python -m app.cli backup` says to.
mkdir -p "$TARGET"
tar -xzf "$ARCHIVE" -C "$TARGET"

# Never report success without looking. A restore that quietly half-worked is worse than
# one that failed, because nobody goes back to check.
if [ ! -f "$TARGET/data/practice.db" ]; then
  echo "FAILED: no database at $TARGET/data/practice.db after extracting $ARCHIVE" >&2
  echo "The archive may be from a different version. Its contents:" >&2
  tar -tzf "$ARCHIVE" | head -10 >&2
  exit 1
fi
if command -v sqlite3 >/dev/null 2>&1; then
  CHECK=$(sqlite3 "$TARGET/data/practice.db" "PRAGMA integrity_check;" 2>&1 | head -1)
  [ "$CHECK" = "ok" ] || { echo "FAILED: restored database is not intact ($CHECK)" >&2; exit 1; }
fi

echo "restored into $TARGET"
echo "  database: $(du -h "$TARGET/data/practice.db" | cut -f1)"
[ -d "$TARGET/data/uploads" ] && echo "  uploads:  $(find "$TARGET/data/uploads" -type f | wc -l | tr -d ' ') file(s)"
echo
echo "The backup does NOT contain .env, because the backup runs inside the container and"
echo ".env lives beside it on the host. Copy the original .env into $TARGET yourself."
echo "Without the original SECRET_KEY every session, magic link and signed token is void."
echo
echo "Next: put the app code in $TARGET, restore .env, then docker compose up -d"

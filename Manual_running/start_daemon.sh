#!/bin/zsh

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

python3 "$ROOT_DIR/postgres/contrib/pg_auto_index/pg_auto_indexer.py" \
  --log-file "$ROOT_DIR/pg-custom/data/pg_auto_index.csv" \
  --database test \
  --user "$USER" \
  --host localhost \
  --port 5544

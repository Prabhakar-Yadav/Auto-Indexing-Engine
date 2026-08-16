#!/bin/zsh

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PG_BIN="$ROOT_DIR/pg-custom/bin"
PG_DATA="$ROOT_DIR/pg-custom/data"
PG_LOG="$ROOT_DIR/pg-custom/server.log"

if ! "$PG_BIN/pg_ctl" -D "$PG_DATA" status >/dev/null 2>&1; then
  "$PG_BIN/pg_ctl" -D "$PG_DATA" -l "$PG_LOG" start >/dev/null
fi

"$PG_BIN/psql" -p 5544 -d test <<'SQL'
DROP TABLE IF EXISTS demo_orders;

CREATE TABLE demo_orders (
    order_id bigint PRIMARY KEY,
    customer_id integer NOT NULL,
    region_id integer NOT NULL,
    status text NOT NULL,
    order_date date NOT NULL,
    amount numeric(12,2) NOT NULL,
    notes text
);

INSERT INTO demo_orders
SELECT
    gs AS order_id,
    (gs % 10000) + 1 AS customer_id,
    (gs % 50) + 1 AS region_id,
    CASE
        WHEN gs % 5 = 0 THEN 'PAID'
        WHEN gs % 5 = 1 THEN 'OPEN'
        WHEN gs % 5 = 2 THEN 'FAILED'
        WHEN gs % 5 = 3 THEN 'REFUNDED'
        ELSE 'PENDING'
    END AS status,
    DATE '2024-01-01' + ((gs % 720)::int) AS order_date,
    ((gs % 100000) / 10.0)::numeric(12,2) AS amount,
    'demo row ' || gs AS notes
FROM generate_series(1, 250000) AS gs;

ANALYZE demo_orders;
SQL



exec "$PG_BIN/psql" -p 5544 -d test

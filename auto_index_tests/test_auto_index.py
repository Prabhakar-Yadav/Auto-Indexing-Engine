from __future__ import annotations

import argparse
import csv
import os
import signal
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

import psycopg2
from psycopg2 import sql


ROOT = Path(__file__).resolve().parents[1]
PG_CUSTOM = ROOT / "pg-custom"
PG_BIN = PG_CUSTOM / "bin"
PG_DATA = PG_CUSTOM / "data"
PG_LOG = PG_CUSTOM / "server.log"
AUTO_INDEXER = ROOT / "postgres" / "contrib" / "pg_auto_index" / "pg_auto_indexer.py"
DEFAULT_WORKLOAD_CSV = Path(__file__).resolve().parent / "test_queries1.csv"
CSV_LOG = PG_DATA / "pg_auto_index.csv"


@dataclass
class WorkloadQuery:
    label: str
    repeats: int
    query: str


def banner(title: str) -> None:
    print("\n" + "=" * 88)
    print(title)
    print("=" * 88)


def step(message: str) -> None:
    print(f"\n[demo] {message}")


def run_cmd(command: List[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=check)


def pg_ctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run_cmd([str(PG_BIN / "pg_ctl"), "-D", str(PG_DATA), *args], check=check)


def ensure_server_running() -> bool:
    status = pg_ctl("status", check=False)
    if status.returncode == 0:
        return False

    started = pg_ctl("-l", str(PG_LOG), "start", check=True)
    return True


def connect(database: str, port: str) -> psycopg2.extensions.connection:
    conn = psycopg2.connect(dbname=database, user=os.getenv("USER"), host="localhost", port=port)
    conn.autocommit = True
    return conn


def control_connect(database: str, port: str) -> psycopg2.extensions.connection:
    conn = connect(database, port)
    with conn.cursor() as cur:
        cur.execute("SET pg_auto_index.enabled = off")
    return conn


def ensure_database(database: str, port: str) -> None:
    try:
        conn = control_connect(database, port)
        conn.close()
        return
    except psycopg2.OperationalError:
        pass

    run_cmd([str(PG_BIN / "createdb"), "-p", port, database])


def load_workload(path: Path) -> List[WorkloadQuery]:
    with path.open(newline="") as handle:
        return [
            WorkloadQuery(row["label"], int(row["repeats"]), row["query"])
            for row in csv.DictReader(handle)
        ]


def cleanup(conn: psycopg2.extensions.connection) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND indexname LIKE 'pgidx_demo_orders_%'
            """
        )
        for (index_name,) in cur.fetchall():
            cur.execute(sql.SQL("DROP INDEX IF EXISTS {}").format(sql.Identifier(index_name)))
        cur.execute("DROP TABLE IF EXISTS demo_orders")


def setup_demo_table(conn: psycopg2.extensions.connection, rows: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE demo_orders (
                order_id bigint PRIMARY KEY,
                customer_id integer NOT NULL,
                region_id integer NOT NULL,
                status text NOT NULL,
                order_date date NOT NULL,
                amount numeric(12,2) NOT NULL,
                notes text
            )
            """
        )
        cur.execute(
            """
            INSERT INTO demo_orders
            SELECT
                gs AS order_id,
                (gs %% 10000) + 1 AS customer_id,
                (gs %% 50) + 1 AS region_id,
                CASE
                    WHEN gs %% 5 = 0 THEN 'PAID'
                    WHEN gs %% 5 = 1 THEN 'OPEN'
                    WHEN gs %% 5 = 2 THEN 'FAILED'
                    WHEN gs %% 5 = 3 THEN 'REFUNDED'
                    ELSE 'PENDING'
                END AS status,
                DATE '2024-01-01' + ((gs %% 720)::int) AS order_date,
                ((gs %% 100000) / 10.0)::numeric(12,2) AS amount,
                'demo row ' || gs AS notes
            FROM generate_series(1, %s) AS gs
            """,
            (rows,),
        )
        cur.execute("ANALYZE demo_orders")


def ensure_extensions(conn: psycopg2.extensions.connection) -> None:
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_auto_index")
        cur.execute("CREATE EXTENSION IF NOT EXISTS hypopg")


def truncate_auto_index_log() -> None:
    CSV_LOG.parent.mkdir(parents=True, exist_ok=True)
    CSV_LOG.write_text("", encoding="utf-8")


def explain(conn: psycopg2.extensions.connection, query: str) -> List[str]:
    with conn.cursor() as cur:
        cur.execute("EXPLAIN " + query)
        return [row[0] for row in cur.fetchall()]


def timed_execution(conn: psycopg2.extensions.connection, query: str, loops: int) -> float:
    start = time.perf_counter()
    with conn.cursor() as cur:
        for _ in range(loops):
            cur.execute(query)
            cur.fetchall()
    return time.perf_counter() - start


def timing_loops_for_workload(workload: List[WorkloadQuery], override: int | None) -> int:
    if override is not None:
        return override
    if not workload:
        return 1
    return max(sum(item.repeats for item in workload), 1)


def print_plan(title: str, plan: Iterable[str]) -> None:
    print(f"\n{title}")
    for line in plan:
        print("  " + line)


def list_demo_indexes(conn: psycopg2.extensions.connection) -> List[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND tablename = 'demo_orders'
              AND indexname LIKE 'pgidx_demo_orders_%'
            ORDER BY indexname
            """
        )
        return [f"{name}: {definition}" for name, definition in cur.fetchall()]


def wait_for_indexes(conn: psycopg2.extensions.connection, timeout_seconds: int) -> List[str]:
    step(f"Waiting up to {timeout_seconds} seconds for pg_auto_indexer.py to create an index.")
    if timeout_seconds <= 0:
        return []
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        indexes = list_demo_indexes(conn)
        if indexes:
            return indexes
        time.sleep(1)
    return []


def start_indexer(args: argparse.Namespace) -> subprocess.Popen[str]:
    step("Starting pg_auto_indexer.py in the background.")
    command = [
        sys.executable,
        str(AUTO_INDEXER),
        "--log-file",
        str(CSV_LOG),
        "--database",
        args.database,
        "--user",
        os.getenv("USER", "postgres"),
        "--host",
        "localhost",
        "--port",
        args.port,
        "--from-end",
        "--poll-seconds",
        "0.2",
        "--decay-seconds",
        "10000",
        "--drop-decay-seconds",
        "10000",
        "--drop-threshold",
        "0.000001",
        "--index-cost-factor",
        str(args.index_cost_factor),
        "--max-candidates",
        "80",
    ]
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    return subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )


def stop_indexer(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def drain_indexer_output(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.stdout is None:
        return
    output = process.stdout.read()
    if output:
        lines = [line for line in output.splitlines() if line.strip()]
        if lines:
            print("\n[pg_auto_indexer.py output]")
            for line in lines:
                print(line)


def run_workload(conn: psycopg2.extensions.connection, workload: List[WorkloadQuery]) -> None:
    step("Running workload from CSV. These SELECTs are logged by pg_auto_index.")
    with conn.cursor() as cur:
        cur.execute("SET pg_auto_index.enabled = on")
        for item in workload:
            print(f"  {item.label}: {item.repeats} executions")
            for _ in range(item.repeats):
                cur.execute(item.query)
                cur.fetchall()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a repeatable pg_auto_index demonstration.")
    parser.add_argument(
        "workload_csv",
        nargs="?",
        default=str(DEFAULT_WORKLOAD_CSV),
        help="CSV workload file to run. Relative paths are resolved from auto_index_tests/.",
    )
    parser.add_argument("--database", default="test")
    parser.add_argument("--port", default="5544")
    parser.add_argument("--rows", type=int, default=250000)
    parser.add_argument(
        "--timing-loops",
        type=int,
        default=None,
        help="Override the before/after timing loop count. Defaults to the sum of repeats across the CSV workload.",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=10,
        help="Maximum time to wait for an auto-created index before exiting.",
    )
    parser.add_argument(
        "--index-cost-factor",
        type=float,
        default=0.1,
        help="Lower values make indexes appear sooner during the demo.",
    )
    parser.add_argument(
        "--keep-objects",
        action="store_true",
        help="Do not drop demo table/indexes at the end; useful while debugging.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workload_path = Path(args.workload_csv)
    if not workload_path.is_absolute():
        workload_path = Path(__file__).resolve().parent / workload_path
    indexer: subprocess.Popen[str] | None = None
    control_conn = None
    workload_conn = None
    started_server = False

    banner("pg_auto_index repeatable end-to-end test")
    print(
        textwrap.dedent(
            f"""
            Database:        {args.database}
            Port:            {args.port}
            Demo rows:       {args.rows:,}
            Workload CSV:    {workload_path}
            """
        ).strip()
    )

    try:
        started_server = ensure_server_running()
        ensure_database(args.database, args.port)

        control_conn = control_connect(args.database, args.port)
        ensure_extensions(control_conn)
        cleanup(control_conn)
        setup_demo_table(control_conn, args.rows)
        truncate_auto_index_log()

        workload = load_workload(workload_path)
        target_query = workload[0].query
        timing_loops = timing_loops_for_workload(workload, args.timing_loops)

        print_plan("Plan before auto-indexing:", explain(control_conn, target_query))
        before = timed_execution(control_conn, target_query, timing_loops)
        print(f"\nTiming before indexes: {timing_loops} executions in {before:.3f}s")

        indexer = start_indexer(args)
        time.sleep(1)

        workload_conn = connect(args.database, args.port)
        run_workload(workload_conn, workload)

        indexes = wait_for_indexes(control_conn, args.wait_seconds)
        if not indexes:
            print("\nNo pg_auto_index-created indexes appeared before timeout.")
            print("Try increasing --rows, --wait-seconds, or lowering --index-cost-factor.")
            return 2

        time.sleep(2)
        indexes = list_demo_indexes(control_conn)
        print("\nIndexes created by pg_auto_indexer.py:")
        for index in indexes:
            print("  " + index)

        with control_conn.cursor() as cur:
            cur.execute("ANALYZE demo_orders")

        print_plan("Plan after auto-indexing:", explain(control_conn, target_query))
        after = timed_execution(control_conn, target_query, timing_loops)
        print(f"\nTiming after indexes:  {timing_loops} executions in {after:.3f}s")

        final_indexes = list_demo_indexes(control_conn)
        if final_indexes != indexes:
            print("\nFinal index list after measurement:")
            for index in final_indexes:
                print("  " + index)

        if after > 0:
            print(f"Observed speedup:      {before / after:.2f}x")

        step("Showing last few workload log rows.")
        for line in CSV_LOG.read_text(encoding="utf-8").splitlines()[-8:]:
            print("  " + line)

        return 0
    finally:
        stop_indexer(indexer)
        drain_indexer_output(indexer)
        if workload_conn is not None:
            workload_conn.close()
        if control_conn is not None:
            if not args.keep_objects:
                cleanup(control_conn)
            control_conn.close()
        if started_server:
            pg_ctl("stop", check=False)


if __name__ == "__main__":
    raise SystemExit(main())

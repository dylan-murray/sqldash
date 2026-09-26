"""Seed a Snowflake account with the sqldash demo dataset.

Usage:
    SNOWFLAKE_ACCOUNT=xy12345 SNOWFLAKE_USER=you@acme.com \
        uv run python scripts/seed_snowflake.py [--warehouse WH] [--auth externalbrowser|pat|password]

Auth: externalbrowser (default, opens a browser), pat (reads SNOWFLAKE_PAT),
or password (reads SNOWFLAKE_PASSWORD). Creates database SQLDASH_DEMO with an
ORDERS table (~6k rows over 120 days) and prints ready-to-paste source blocks.
"""

import argparse
import os
import sys

import snowflake.connector

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sqldash.scaffold import generate_orders  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--warehouse", default=os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"))
    parser.add_argument("--auth", default="externalbrowser", choices=["externalbrowser", "pat", "password"])
    parser.add_argument("--database", default="SQLDASH_DEMO")
    args = parser.parse_args()

    account = os.environ.get("SNOWFLAKE_ACCOUNT")
    user = os.environ.get("SNOWFLAKE_USER")
    if not account or not user:
        sys.exit("set SNOWFLAKE_ACCOUNT and SNOWFLAKE_USER")

    kwargs = {"account": account, "user": user, "warehouse": args.warehouse}
    if args.auth == "externalbrowser":
        kwargs["authenticator"] = "externalbrowser"
    elif args.auth == "pat":
        token = os.environ.get("SNOWFLAKE_PAT") or sys.exit("set SNOWFLAKE_PAT")
        kwargs.update(authenticator="PROGRAMMATIC_ACCESS_TOKEN", token=token)
    else:
        password = os.environ.get("SNOWFLAKE_PASSWORD") or sys.exit("set SNOWFLAKE_PASSWORD")
        kwargs["password"] = password

    rows = generate_orders()
    print(f"connecting to {account} as {user} ({args.auth}) ...")
    conn = snowflake.connector.connect(**kwargs)
    try:
        cur = conn.cursor()
        # semgrep: DDL cannot bind an identifier; the name is the operator's own flag
        # nosemgrep: formatted-sql-query, sqlalchemy-execute-raw-query
        cur.execute(f"CREATE DATABASE IF NOT EXISTS {args.database}")
        # semgrep: DDL cannot bind an identifier; the name is the operator's own flag
        # nosemgrep: formatted-sql-query, sqlalchemy-execute-raw-query
        cur.execute(f"USE DATABASE {args.database}")
        cur.execute("USE SCHEMA PUBLIC")
        cur.execute(
            "CREATE OR REPLACE TABLE ORDERS "
            "(ORDER_DATE DATE, REGION STRING, CATEGORY STRING, AMOUNT NUMBER(10,2))"
        )
        cur.executemany("INSERT INTO ORDERS VALUES (%s, %s, %s, %s)", rows)
        cur.execute("SELECT COUNT(*), ROUND(SUM(AMOUNT), 2) FROM ORDERS")
        count, total = cur.fetchone()
        print(f"seeded {count} rows, total revenue {total}")
    finally:
        conn.close()

    print(f"""
paste into a dashboard or metrics.yaml:

source:
  type: snowflake
  account: {account}
  warehouse: {args.warehouse}
  database: {args.database}
  schema: PUBLIC
  authentication: {args.auth}
  username: "${{env:SNOWFLAKE_USER}}"{'''
  token: "${env:SNOWFLAKE_PAT}"''' if args.auth == "pat" else ""}{'''
  password: "${env:SNOWFLAKE_PASSWORD}"''' if args.auth == "password" else ""}
""")


if __name__ == "__main__":
    main()

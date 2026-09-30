"""LISTEN 叫醒为主，另有短间隔兜底认领，避免通知丢失。"""
import threading
import time
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

from db import DSN, SCHEMA, connect
from rules import judge


def claim_id(conn, scan_id: int | None) -> bool:
    with conn.transaction():
        if scan_id is not None:
            row = conn.execute(
                """SELECT id, fill_factor FROM iv_scans
                   WHERE id = %s AND status = 'pending'
                   FOR UPDATE SKIP LOCKED""",
                (scan_id,),
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT id, fill_factor FROM iv_scans
                   WHERE status = 'pending' ORDER BY id
                   FOR UPDATE SKIP LOCKED LIMIT 1"""
            ).fetchone()
        if row is None:
            return False
        verdict, reason = judge(float(row["fill_factor"]))
        conn.execute(
            """UPDATE iv_scans SET status='done', verdict=%s, reason=%s, processed_at=%s
               WHERE id=%s""",
            (verdict, reason, datetime.now(timezone.utc), row["id"]),
        )
    return True


def drain(conn) -> bool:
    any_row = False
    while claim_id(conn, None):
        any_row = True
    return any_row


def poll_loop():
    while True:
        try:
            with connect() as conn:
                conn.execute(SCHEMA)
                drain(conn)
                conn.commit()
        except Exception as exc:
            print(f"poll error: {exc}", flush=True)
        time.sleep(0.6)


def listen_loop():
    while True:
        try:
            with psycopg.connect(DSN, row_factory=dict_row, autocommit=True) as conn:
                conn.execute("LISTEN iv_scan_new")
                for note in conn.notifies():
                    with connect() as work:
                        if note is not None:
                            claim_id(work, int(note.payload))
                        drain(work)
                        work.commit()
        except Exception as exc:
            print(f"listen error: {exc}", flush=True)
            time.sleep(1.5)


def main():
    print("pv iv-scan notify worker started", flush=True)
    threading.Thread(target=listen_loop, name="iv-listen", daemon=True).start()
    poll_loop()


if __name__ == "__main__":
    main()

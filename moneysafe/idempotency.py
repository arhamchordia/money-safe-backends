"""Part 3: safe to retry.

A timeout means "maybe". The client cannot know whether its payout went
through, so it retries. Without protection, every retry is a second payout.

The fix: the client sends one Idempotency-Key per intended action, the SAME
key on every retry. The server stores the key and the result in the same
transaction as the work, and replays the saved result for any repeat.

Two designs exist for failures, and real providers differ:
  - Stripe saves the first response for a key whether it succeeded or failed,
    so a retry gets the same failure back.
  - Many systems save only successes, so a client may retry a failed request
    with the same key once the cause is fixed.
`run_once` does the second by default; pass `store_failures` for the first.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from psycopg.types.json import Jsonb

from .db import Connection, transaction


class IdempotencyConflict(Exception):
    """The same key was reused for a different request."""


class StoredFailure(Exception):
    """A failure saved under this key, raised again on every replay."""

    def __init__(self, response: dict[str, Any], replayed: bool):
        super().__init__(response.get("message", "stored failure"))
        self.response = response
        self.replayed = replayed


def fingerprint(request: dict[str, Any]) -> str:
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def run_once(conn: Connection, key: str, request: dict[str, Any],
             work: Callable[[Connection], dict[str, Any]],
             store_failures: tuple[type[Exception], ...] = ()) -> tuple[dict[str, Any], bool]:
    """Run `work` at most once per key. Returns (response, replayed).

    - First call: inserts the key, runs the work, saves the response, commits all together.
    - Repeat with the same request: returns the saved response, does no work.
    - Repeat with a different request: raises IdempotencyConflict.
    - Two calls at the same moment: the second INSERT waits on the unique key until
      the first commits, then finds the saved response.
    - Work raises an exception listed in `store_failures`: the work is rolled
      back, the failure is saved under the key, and StoredFailure is raised now
      and on every later replay.
    - Work raises anything else: everything rolls back, including the key.
    """
    request_hash = fingerprint(request)
    failure: dict[str, Any] | None = None

    with transaction(conn):
        inserted = conn.execute(
            "INSERT INTO idempotency_keys (key, request_hash) VALUES (%s, %s) "
            "ON CONFLICT (key) DO NOTHING",
            (key, request_hash),
        ).rowcount
        if not inserted:
            row = conn.execute(
                "SELECT request_hash, response FROM idempotency_keys WHERE key = %s", (key,)
            ).fetchone()
            if row["request_hash"] != request_hash:
                raise IdempotencyConflict(f"key {key!r} was used for a different request")
            saved = row["response"]
            if saved.get("ok") is False:
                raise StoredFailure(saved, replayed=True)
            return saved, True

        try:
            with transaction(conn):  # a savepoint: lets us undo the work but keep the key
                response = work(conn)
        except store_failures as exc:
            failure = {"ok": False, "error": type(exc).__name__, "message": str(exc)}
            conn.execute("UPDATE idempotency_keys SET response = %s WHERE key = %s",
                         (Jsonb(failure), key))
        else:
            conn.execute("UPDATE idempotency_keys SET response = %s WHERE key = %s",
                         (Jsonb(response), key))

    if failure is not None:
        raise StoredFailure(failure, replayed=False)
    return response, False

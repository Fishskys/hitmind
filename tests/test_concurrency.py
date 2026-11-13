"""Concurrency: multiple Engine connections to the same DB.

WAL + BEGIN IMMEDIATE serialize writers; these tests assert no corruption and
that the write gate still collapses duplicates under contention.
"""

import threading
import uuid

from hitmind.core.engine import Engine


def test_concurrent_distinct_writes(tmp_path):
    db = str(tmp_path / "conc.db")
    n_threads, per = 8, 15
    errors = []

    def worker(tid):
        try:
            e = Engine(db, agent_id=f"w{tid}")
            for i in range(per):
                # random content so the write gate never collapses them as duplicates
                body = f"{uuid.uuid4().hex} {uuid.uuid4().hex}"
                e.remember(body, "fact", title=f"t{tid}-{i}")
            e.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    ts = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert not errors, errors
    e = Engine(db, agent_id="check")
    total = e.list(limit=500)["data"]["total"]
    e.close()
    assert total == n_threads * per


def test_concurrent_duplicate_collapses(tmp_path):
    db = str(tmp_path / "dup.db")
    n_threads = 8
    errors = []

    def worker():
        try:
            e = Engine(db, agent_id="w")
            e.remember("the one and only shared fact about quantum widgets", "fact", title="shared")
            e.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    ts = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert not errors, errors
    e = Engine(db, agent_id="check")
    total = e.list(limit=500)["data"]["total"]
    e.close()
    # dedup within the write lock should leave exactly one active row
    assert total == 1

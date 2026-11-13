"""HttpEmbedder failure guard: bounded attempt + consecutive-failure circuit breaker."""

from hitmind.core.embedder import HttpEmbedder


def test_trips_after_consecutive_failures_and_reset_recovers_probe():
    # unreachable port -> every attempt fails fast
    emb = HttpEmbedder("http://127.0.0.1:9/v1", "m", "", timeout=0.2, max_consecutive_failures=3)
    assert emb.ok is True
    for _ in range(3):
        assert emb.embed_query("x") == []
    assert emb.ok is False                     # tripped after 3 consecutive failures
    # while tripped: short-circuits, no attempt, still empty
    assert emb.embed_query("x") == [] and emb.ok is False
    # runner re-probes once per pass
    emb.reset()
    assert emb.ok is True
    # a single new failure does NOT immediately re-trip (need `max` in a row)
    assert emb.embed_query("x") == [] and emb.ok is True

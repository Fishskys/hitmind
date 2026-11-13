"""Tiny stdlib-only float32 vector helpers (no numpy).

HitMind's vector needs are only: pack/unpack a float32 blob and compute cosine.
Doing it with ``array`` + ``math`` keeps ``core`` pure-stdlib, so it imports under
the Hermes gateway Python (3.14.9) without any compiled dependency.
"""

from __future__ import annotations

import math
from array import array


def pack_vec(vec) -> bytes:
    """list[float] -> little-endian float32 bytes."""
    return array("f", (float(x) for x in vec)).tobytes()


def unpack_vec(blob) -> list[float]:
    """float32 bytes -> list[float] (empty list if invalid)."""
    if not blob:
        return []
    try:
        a = array("f")
        a.frombytes(blob)
        return a.tolist()
    except Exception:
        return []


def l2norm(vec) -> float:
    return math.sqrt(sum(x * x for x in vec))


def cosine_lists(a, b) -> float:
    """Cosine between two equal-length float sequences; 0.0 if degenerate."""
    if not a or not b or len(a) != len(b):
        return 0.0
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)

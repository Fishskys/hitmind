"""CJK-aware tokenizer.

Outputs tokens that we join with spaces into a FTS5 ``search_text`` column using
the unicode61 tokenizer. Rationale: FTS5's default tokenizer treats CJK badly and
trigram can't match <3 char Chinese words, so we emit uni/bi/tri-gram tokens for
CJK runs ourselves while keeping latin words and entity-like strings intact.
"""

from __future__ import annotations

import re
import unicodedata

_CJK = r"\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff"
_CJK_RUN = re.compile(f"[{_CJK}]+")
_LATIN_WORD = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")
# Entity-like tokens kept whole (not n-grammed): dates, numbers, urls, emails.
_ENTITY = re.compile(
    r"(https?://[^\s]+|[\w.+-]+@[\w-]+\.[\w.-]+|"
    r"\d{4}-\d{2}-\d{2}|\d{1,2}:\d{2}(?::\d{2})?|\d+(?:\.\d+)?)"
)


def _cjk_ngrams(run: str) -> list[str]:
    out: list[str] = []
    n = len(run)
    for size in (1, 2, 3):
        for i in range(n - size + 1):
            out.append(run[i : i + size])
    # Keep the full run when short so exact phrase matching still works.
    if n <= 4 and n not in (1,):
        out.append(run)
    return out


def tokenize(text: str) -> list[str]:
    if not text:
        return []
    text = unicodedata.normalize("NFKC", text).lower()
    tokens: list[str] = []

    def _consume(segment: str) -> None:
        for m in _ENTITY.finditer(segment):
            tokens.append(m.group(0))
        # strip entities, then handle cjk/latin runs
        stripped = _ENTITY.sub(" ", segment)
        for run in _CJK_RUN.findall(stripped):
            tokens.extend(_cjk_ngrams(run))
        for w in _LATIN_WORD.findall(stripped):
            tokens.append(w)

    _consume(text)

    # stable de-dup
    seen: set[str] = set()
    result: list[str] = []
    for t in tokens:
        if t and t not in seen:
            seen.add(t)
            result.append(t)
    return result


def search_text(text: str) -> str:
    """Space-joined token string for the FTS5 index."""
    return " ".join(tokenize(text))

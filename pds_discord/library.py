"""The library's own public files: the search index that `/record`, `/glossary` and `/search` read,
and the approved news feed behind `/news`.

Both are plain JSON on the library site, so this module only fetches and caches. Nothing here
imports discord.py, and the ranking is a plain function so it can be tested on a handful of made-up
entries (`tests/test_library.py`).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

import httpx

from .api import USER_AGENT

log = logging.getLogger("pds_discord.library")

CACHE_SECONDS = 3600.0
WORD_RE = re.compile(r"[a-z0-9][a-z0-9'+-]*")

# What the library calls each kind of record, for a line under a search hit.
KINDS = {
    "paper": "paper", "preprint": "preprint", "gray": "gray literature", "glossary": "glossary term",
    "powers-comment": "Dr Powers' comment", "powers-post": "Dr Powers' post", "gene": "gene",
    "gene-claim": "gene claim", "timeline": "timeline entry", "medication": "medication",
    "dataset": "dataset", "mechanism": "mechanism", "intervention": "intervention", "video": "video",
    "regulatory": "regulatory record", "question": "open question", "conference": "conference",
    "news": "news item", "community": "community account", "icd": "ICD code",
}


def words(text: str) -> list[str]:
    return WORD_RE.findall((text or "").lower())


@dataclass
class Entry:
    """One record in the library, as the search index describes it."""
    id: str = ""
    href: str = ""
    kind: str = ""
    title: str = ""
    number: int | None = None
    collection: str | None = None
    source_line: str | None = None
    authors: str | None = None
    year: int | str | None = None
    date_label: str | None = None
    snippet: str = ""
    keywords: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    flags: dict = field(default_factory=dict)
    full_text: bool = False

    @classmethod
    def from_payload(cls, raw: dict) -> "Entry":
        return cls(
            id=str(raw.get("id") or ""), href=str(raw.get("href") or ""), kind=str(raw.get("kind") or ""),
            title=str(raw.get("title") or ""), number=raw.get("number"), collection=raw.get("collection"),
            source_line=raw.get("sourceLine"), authors=raw.get("authors"), year=raw.get("year"),
            date_label=raw.get("dateLabel"), snippet=str(raw.get("snippet") or ""),
            keywords=[str(k) for k in raw.get("keywords") or []],
            topics=[str(t) for t in raw.get("topics") or []],
            flags=raw.get("flags") if isinstance(raw.get("flags"), dict) else {},
            full_text=bool(raw.get("fullText")),
        )

    @property
    def kind_label(self) -> str:
        return KINDS.get(self.kind, self.kind.replace("-", " ") or "record")

    @property
    def byline(self) -> str:
        """A one-line description: who, where and when."""
        bits = [b for b in (self.authors, self.source_line) if b]
        when = self.date_label or (str(self.year) if self.year else None)
        if when and not any(when in b for b in bits):
            bits.append(when)
        return " · ".join(bits)

    @property
    def warnings(self) -> list[str]:
        """The record's own flags, in the words the library uses for its badges."""
        out = []
        if self.flags.get("retraction"):
            out.append("Retracted")
        if self.flags.get("unverifiable"):
            out.append("Unverifiable")
        if self.flags.get("tentative"):
            out.append("Metadata tentative")
        if self.flags.get("powers"):
            out.append("Dr Powers' material")
        return out

    def url(self, origin: str) -> str:
        return f"{origin.rstrip('/')}{self.href}"


def rank(entries: list[Entry], query: str, limit: int = 5) -> list[Entry]:
    """The entries that best match a few words. An ID or a catalog number wins outright, then a
    title match, then keywords, then the summary."""
    q = query.strip()
    if not q:
        return []
    exact = q.upper().strip("#")
    terms = words(q)
    if not terms:
        return []
    scored: list[tuple[float, int, Entry]] = []
    for i, e in enumerate(entries):
        score = 0.0
        if e.id.upper() == exact:
            score += 1000
        if exact.isdigit() and e.number == int(exact):
            score += 900
        title, keys, snip = e.title.lower(), " ".join(e.keywords).lower(), e.snippet.lower()
        if title == q.lower():
            score += 200  # the term itself, not a longer term that contains it
        elif q.lower() in title:
            score += 60
        for t in terms:
            if t in words(title):
                score += 12
            elif t in title:
                score += 6
            if t in keys:
                score += 5
            if t in snip:
                score += 2
        if score and e.full_text:
            score += 1  # a record with its full text behind it answers more fully
        if score:
            scored.append((score, -i, e))
    # Same score: the shorter title is the more exact match, then whichever the library lists first.
    scored.sort(key=lambda s: (s[0], -len(s[2].title), s[1]), reverse=True)
    return [e for _, _, e in scored[:limit]]


class Library:
    """The library's public JSON, fetched once an hour."""

    def __init__(self, origin: str) -> None:
        self.origin = origin.rstrip("/")
        self._entries: list[Entry] = []
        self._news: list[dict] = []
        self._fetched: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._client = httpx.AsyncClient(timeout=30.0, headers={"User-Agent": USER_AGENT},
                                         follow_redirects=True)

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str) -> object:
        r = await self._client.get(f"{self.origin}{path}", headers={"Accept": "application/json"})
        r.raise_for_status()
        return r.json()

    async def _refresh(self, what: str) -> None:
        """Fetch one of the two files if the copy in hand is old. A failure keeps the old copy:
        an hour-stale index answers better than an error does."""
        async with self._lock:
            if time.monotonic() - self._fetched.get(what, 0.0) < CACHE_SECONDS:
                return
            try:
                if what == "entries":
                    payload = await self._get("/research/search-index.json")
                    rows = payload.get("entries") if isinstance(payload, dict) else None
                    self._entries = [Entry.from_payload(e) for e in rows or []]
                    log.info("library index: %d records (version %s)", len(self._entries),
                             payload.get("version") if isinstance(payload, dict) else "?")
                else:
                    payload = await self._get("/api/news")
                    items = payload.get("items") if isinstance(payload, dict) else None
                    self._news = sorted((i for i in items or [] if i.get("title") and i.get("url")),
                                        key=lambda i: (i.get("date") or "", i.get("id") or ""), reverse=True)
                    log.info("library news: %d approved items", len(self._news))
                self._fetched[what] = time.monotonic()
            except (httpx.HTTPError, ValueError, AttributeError) as exc:
                log.warning("could not refresh the library's %s (%s)", what, exc)
                if not self._fetched.get(what):
                    self._fetched[what] = time.monotonic() - CACHE_SECONDS + 60  # try again in a minute

    async def entries(self) -> list[Entry]:
        await self._refresh("entries")
        return self._entries

    async def news(self, limit: int = 5) -> list[dict]:
        await self._refresh("news")
        return self._news[:limit]

    async def search(self, query: str, limit: int = 5, kinds: set[str] | None = None) -> list[Entry]:
        entries = await self.entries()
        if kinds:
            entries = [e for e in entries if e.kind in kinds]
        return rank(entries, query, limit)

    async def record(self, needle: str) -> Entry | None:
        """One record by ID, catalog number or title."""
        hits = await self.search(needle, limit=1)
        return hits[0] if hits else None

    async def glossary(self, term: str) -> Entry | None:
        hits = await self.search(term, limit=1, kinds={"glossary"})
        return hits[0] if hits else None

    async def suggest(self, partial: str, limit: int = 25, kinds: set[str] | None = None) -> list[Entry]:
        """Autocomplete: what someone has typed so far, or the first records when they have typed
        nothing yet."""
        entries = await self.entries()
        if kinds:
            entries = [e for e in entries if e.kind in kinds]
        if not partial.strip():
            return entries[:limit]
        return rank(entries, partial, limit)

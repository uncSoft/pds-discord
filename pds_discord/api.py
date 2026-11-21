"""Ask the research library's chat server and read the answer it streams back.

Nothing in here imports discord.py. The server answers `POST /api/chat` with Server-Sent Events
(the contract is in the library's `bot/HANDOFF.md`), so the parsing is done by a small feed that
takes plain lines, and every event the server can send is driven from a recorded stream in
`tests/test_api.py`, the way PandaBot's parser is tested without a Discord connection.

The library's server holds the guard that declines advice questions, the fixed care-first reply for
someone at risk, Dr Powers' attribution and the source flags. This module passes all of it through
untouched; it never writes an answer of its own.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import AsyncIterator, Iterable, Iterator

import httpx

log = logging.getLogger("pds_discord.api")

MAX_QUESTION = 1000   # the server's own ceiling: a longer question comes back as a 422
MIN_QUESTION = 2
MAX_TURNS = 6         # how many earlier turns it will accept as history
USER_AGENT = "pds-discord (+https://github.com/uncSoft/pds-discord)"

CITATION_RE = re.compile(r"\[(\d{1,2})\]")


class AskError(Exception):
    """The question could not be answered. `message` is written for a reader to see."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def flag_word(flag: str) -> str:
    """One word for a source's flag, the same words the chat page puts on its badges."""
    if "RETRACTED" in flag:
        return "Retracted"
    if "UNVERIFIABLE" in flag:
        return "Unverifiable"
    if "tentative" in flag.lower():
        return "Tentative"
    if "Powers" in flag:
        return "Powers' theory"
    return "Quality note"


@dataclass
class Source:
    """One numbered source under an answer, as the server describes it."""
    n: int
    label: str = ""
    title: str | None = None
    year: str | int | None = None
    url: str | None = None
    source_url: str | None = None
    flags: list[str] = field(default_factory=list)
    powers_voice: bool = False
    also: str | None = None
    source: str | None = None
    item_id: str | None = None
    publisher: str | None = None
    date: str | None = None
    licence: str | None = None
    credit: str | None = None

    @classmethod
    def from_payload(cls, raw: dict) -> "Source":
        known = {f for f in cls.__dataclass_fields__ if f != "n"}
        flags = raw.get("flags") or []
        if isinstance(flags, dict):  # the search index spells flags as an object; the chat as a list
            flags = [f"{k}: {v}" for k, v in flags.items()]
        return cls(n=int(raw.get("n") or 0),
                   **{k: v for k, v in raw.items() if k in known and k != "flags"},
                   flags=[str(f) for f in flags])

    @property
    def chip(self) -> str:
        """The short name to show, preferring the record's own ID."""
        if self.source == "news":
            return f"{self.publisher or 'News'}{f' · {self.date[:7]}' if self.date else ''}"
        if self.source == "textbook":
            return self.label.removeprefix("Textbook: ").split(",")[0]
        return self.item_id or self.label or "Source"

    @property
    def badges(self) -> list[str]:
        """Short warnings to print beside the source, in the chat page's wording."""
        out = [flag_word(f) for f in self.flags]
        if self.powers_voice and not any("Powers" in f for f in self.flags):
            out.append("Powers' commentary")
        if self.source == "textbook":
            out.append("Textbook background")
        if self.source == "news":
            out.append("News, not a reviewed record")
        seen: dict[str, None] = {}
        for word in out:
            seen.setdefault(word, None)
        return list(seen)


@dataclass
class Answer:
    """An answer as it arrives. The same object is handed back as it fills up."""
    question: str
    text: str = ""
    sources: list[Source] = field(default_factory=list)
    decline: bool = False
    support: bool = False
    background: str | None = None
    background_sources: list[Source] = field(default_factory=list)
    further: dict | None = None
    error: str | None = None
    done: bool = False

    @property
    def cited(self) -> list[Source]:
        """Only the sources the answer actually cites, in the order it cites them, one per URL."""
        out: list[Source] = []
        seen: set[str] = set()
        for num in dict.fromkeys(int(m) for m in CITATION_RE.findall(self.text)):
            if 1 <= num <= len(self.sources):
                src = self.sources[num - 1]
                key = src.url or f"n{num}"
                if key not in seen:
                    seen.add(key)
                    out.append(src)
        return out

    def as_history(self) -> dict:
        """This answer as a history turn. Source numbers restart every turn, so `[3]` becomes
        `(#3 PFS-001)`, which is what the chat page sends back too."""
        def label(m: re.Match) -> str:
            num = int(m.group(1))
            src = self.sources[num - 1] if 1 <= num <= len(self.sources) else None
            return f"(#{num} {src.item_id})" if src and src.item_id else f"(#{num})"
        return {"role": "assistant", "content": CITATION_RE.sub(label, self.text)[:4000]}


class Feed:
    """Server-Sent Events, one line at a time, out as (event, data) pairs."""

    def __init__(self) -> None:
        self.event: str | None = None
        self.data: list[str] = []

    def push(self, line: str) -> list[tuple[str, object]]:
        line = line.rstrip("\r")
        if line.startswith("event:"):
            self.event = line[6:].strip()
            return []
        if line.startswith("data:"):
            self.data.append(line[5:].lstrip())
            return []
        if line:  # a comment or a field this client has no use for
            return []
        event, raw = self.event, "\n".join(self.data)
        self.event, self.data = None, []
        if not event:
            return []
        try:
            return [(event, json.loads(raw) if raw else None)]
        except json.JSONDecodeError:
            log.warning("ignoring a %s event that was not JSON", event)
            return []


def feed_lines(lines: Iterable[str]) -> Iterator[tuple[str, object]]:
    """Every event in a whole stream. Used by the tests on recorded streams."""
    feed = Feed()
    for line in lines:
        yield from feed.push(line)
    yield from feed.push("")  # flush a stream that ended without its blank line


def apply(answer: Answer, event: str, data: object) -> bool:
    """Fold one event into the answer. True when there is something new worth showing."""
    if event == "meta" and isinstance(data, dict):
        answer.decline = bool(data.get("decline"))
        answer.support = bool(data.get("support"))
        answer.sources = [Source.from_payload(s) for s in data.get("sources") or []]
        return False
    if event == "token" and isinstance(data, str):
        answer.text += data
        return True
    if event == "background" and isinstance(data, dict):
        answer.background = data.get("text") or None
        answer.background_sources = [Source.from_payload(s) for s in data.get("sources") or []]
        return True
    if event == "further" and isinstance(data, dict):
        answer.further = data
        return True
    if event == "error":
        answer.error = str(data) if data else "Something went wrong on the server."
        return True
    if event == "done":
        answer.done = True
        return True
    return False


def user_hash(key: str, user_id: int) -> str:
    """A stable, opaque name for one reader, so the server can keep a rate limit per person
    without being told who that person is."""
    return hashlib.sha256(f"{key}:{user_id}".encode()).hexdigest()[:32]


class Chat:
    """A client for one chat server."""

    def __init__(self, base_url: str, client_key: str = "", *, read_timeout: float = 180.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.client_key = client_key
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=read_timeout, write=10.0, pool=10.0),
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self._client.aclose()

    def _headers(self, user_id: int | None) -> dict[str, str]:
        """The client key, when there is one, lets the server hold its rate limit per reader
        instead of per IP address: every question from Discord arrives from one address."""
        if not self.client_key:
            return {}
        head = {"X-PDS-Client": self.client_key}
        if user_id is not None:
            head["X-PDS-Client-User"] = user_hash(self.client_key, user_id)
        return head

    async def health(self) -> dict:
        r = await self._client.get(f"{self.base_url}/api/health", timeout=10.0)
        r.raise_for_status()
        return r.json()

    async def ask(self, question: str, history: list[dict] | None = None,
                  user_id: int | None = None) -> AsyncIterator[Answer]:
        """Ask one question, handing back the answer each time it grows."""
        question = " ".join(question.split())[:MAX_QUESTION]
        if len(question) < MIN_QUESTION:
            raise AskError("That question is too short for me to look anything up.")
        answer = Answer(question=question)
        payload = {"question": question, "history": (history or [])[-MAX_TURNS:]}
        try:
            async with self._client.stream("POST", f"{self.base_url}/api/chat", json=payload,
                                           headers=self._headers(user_id)) as resp:
                if resp.status_code != 200:
                    raise AskError(await _error_message(resp), resp.status_code)
                feed = Feed()
                async for line in resp.aiter_lines():
                    for event, data in feed.push(line):
                        if apply(answer, event, data):
                            yield answer
                        if answer.done:
                            return
        except httpx.HTTPError as exc:
            log.warning("the chat server could not be reached: %s", exc)
            raise AskError("I couldn't reach the library's assistant just now. Please try again shortly.") from exc
        if not answer.done:  # the stream stopped early; whatever arrived still stands
            answer.done = True
            yield answer


async def _error_message(resp: httpx.Response) -> str:
    """The server's own words for a 429, 503 or 422, which are written for a reader."""
    fallbacks = {
        429: "The library's assistant is limiting how fast questions come in. Please wait a minute.",
        503: "The assistant is busy with other questions. Please try again shortly.",
        422: "I couldn't send that question on. Try rephrasing it, or make it shorter.",
    }
    try:
        detail = (json.loads(await resp.aread()) or {}).get("error")
    except (json.JSONDecodeError, ValueError, httpx.HTTPError):
        detail = None
    return str(detail or fallbacks.get(resp.status_code, "The library's assistant could not answer that just now."))

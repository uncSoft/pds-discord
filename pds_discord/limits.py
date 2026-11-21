"""How many questions one reader and one server may ask, and the short conversation each reader has
going.

The library's assistant answers two questions at a time and keeps a rate limit of its own, which it
holds per IP address. Every question from Discord arrives from this one process, so without the
client key (see README, "The server side") the whole of Discord shares a single reader's allowance.
The budgets below are set to fit inside it, so the bot is safe to run before that change lands: busy
servers wait rather than spending the allowance the library's own readers need.

Nothing here is written down. These are health questions, the assistant runs with its question log
switched off, and keeping a copy on this side would undo that. Everything in this module lives in
memory and is forgotten when the process restarts.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque

Window = tuple[float, int]  # (seconds, how many questions are allowed in them)

# Without the client key, all of Discord looks to the assistant like one reader, so everything here
# has to fit inside one reader's allowance of 6 a minute and 40 an hour.
USER_WINDOWS: tuple[Window, ...] = ((60, 2), (3600, 12))
GUILD_WINDOWS: tuple[Window, ...] = ((60, 4), (3600, 30))
GLOBAL_WINDOWS: tuple[Window, ...] = ((60, 5), (3600, 35))

# With the key, the assistant counts per reader and holds this client as a whole to 20 a minute and
# 200 an hour. A reader still gets less here than the assistant would give them, so someone asking
# too fast hears it from the bot rather than as a 429.
KEYED_USER_WINDOWS: tuple[Window, ...] = ((60, 4), (3600, 25))
KEYED_GUILD_WINDOWS: tuple[Window, ...] = ((60, 10), (3600, 120))
KEYED_GLOBAL_WINDOWS: tuple[Window, ...] = ((60, 18), (3600, 180))
TURNS = 6          # messages of history the assistant accepts: three exchanges
THREAD_TTL = 600.0  # a follow-up ten minutes later is a new question


def human(seconds: float) -> str:
    """"in 40 seconds", "in 3 minutes": what to tell someone who has to wait."""
    seconds = max(1.0, seconds)
    if seconds < 90:
        return f"in {round(seconds)} seconds"
    minutes = round(seconds / 60)
    return f"in {minutes} minute{'s' if minutes != 1 else ''}"


class Budget:
    """A sliding-window allowance, kept per key."""

    def __init__(self, windows: tuple[Window, ...]) -> None:
        self.windows = windows
        self.longest = max(w for w, _ in windows)
        self._hits: dict[object, deque[float]] = defaultdict(deque)

    def _prune(self, key: object, now: float) -> deque[float]:
        hits = self._hits[key]
        while hits and now - hits[0] > self.longest:
            hits.popleft()
        if not hits:
            self._hits.pop(key, None)
        return hits

    def wait(self, key: object, now: float | None = None) -> float:
        """Seconds until this key may ask again; 0.0 when it may ask now."""
        now = time.monotonic() if now is None else now
        hits = self._prune(key, now)
        waits = [hits[-allowed] + seconds - now
                 for seconds, allowed in self.windows if len(hits) >= allowed]
        return max([w for w in waits if w > 0], default=0.0)

    def spend(self, key: object, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._prune(key, now)
        self._hits[key].append(now)

    def give_back(self, key: object) -> None:
        """Undo the most recent question against this key."""
        if hits := self._hits.get(key):
            hits.pop()
            if not hits:
                self._hits.pop(key, None)

    def take(self, key: object, now: float | None = None) -> float:
        """Spend one question, or say how long the wait is. 0.0 means it was spent."""
        now = time.monotonic() if now is None else now
        if left := self.wait(key, now):
            return left
        self.spend(key, now)
        return 0.0


class Budgets:
    """The reader's, the server's and the whole bot's allowances together."""

    def __init__(self, user: tuple[Window, ...] = USER_WINDOWS,
                 guild: tuple[Window, ...] = GUILD_WINDOWS,
                 overall: tuple[Window, ...] = GLOBAL_WINDOWS) -> None:
        self.user, self.guild, self.overall = Budget(user), Budget(guild), Budget(overall)

    @classmethod
    def for_client(cls, keyed: bool) -> "Budgets":
        """The wider allowances once the assistant knows this client and counts per reader."""
        if not keyed:
            return cls()
        return cls(KEYED_USER_WINDOWS, KEYED_GUILD_WINDOWS, KEYED_GLOBAL_WINDOWS)

    def take(self, user_id: int, guild_id: int | None) -> str:
        """Empty when the question may go ahead, else what to tell the person asking."""
        now = time.monotonic()
        if left := self.user.wait(user_id, now):
            return f"You've asked a few in a row. Try again {human(left)}."
        if guild_id is not None and (left := self.guild.wait(guild_id, now)):
            return f"This server has asked a lot just now. Try again {human(left)}."
        if left := self.overall.wait("all", now):
            return ("The library's assistant is answering as fast as it can for everyone. "
                    f"Try again {human(left)}.")
        self.user.spend(user_id, now)
        if guild_id is not None:
            self.guild.spend(guild_id, now)
        self.overall.spend("all", now)
        return ""

    def refund(self, user_id: int, guild_id: int | None) -> None:
        """Give a question back when it never reached the assistant."""
        for budget, key in ((self.user, user_id), (self.guild, guild_id), (self.overall, "all")):
            if key is not None:
                budget.give_back(key)


class Gate:
    """At most this many questions in flight, so the bot never holds both of the assistant's
    answering slots while someone on the library's own site is waiting."""

    def __init__(self, at_once: int = 1, patience: float = 25.0) -> None:
        self._slots = asyncio.Semaphore(at_once)
        self.patience = patience

    async def __aenter__(self) -> "Gate":
        try:
            await asyncio.wait_for(self._slots.acquire(), self.patience)
        except asyncio.TimeoutError as exc:
            raise TimeoutError("busy") from exc
        return self

    async def __aexit__(self, *_) -> None:
        self._slots.release()


class Threads:
    """The last few messages of each reader's conversation, per channel, so a follow-up question
    ("and in women?") still makes sense. `turns` is the assistant's own ceiling on history."""

    def __init__(self, turns: int = TURNS, ttl: float = THREAD_TTL) -> None:
        self.turns, self.ttl = turns, ttl
        self._talk: dict[object, tuple[float, deque[dict]]] = {}

    def _live(self, key: object, now: float) -> deque[dict]:
        seen, turns = self._talk.get(key, (0.0, deque(maxlen=self.turns)))
        if now - seen > self.ttl:
            turns = deque(maxlen=self.turns)
        self._talk[key] = (now, turns)
        return turns

    def history(self, key: object) -> list[dict]:
        return list(self._live(key, time.monotonic()))

    def remember(self, key: object, question: str, answer_turn: dict) -> None:
        turns = self._live(key, time.monotonic())
        turns.append({"role": "user", "content": question[:4000]})
        turns.append(answer_turn)

    def clear(self, key: object) -> None:
        self._talk.pop(key, None)

    def sweep(self) -> None:
        """Drop conversations nobody came back to."""
        now = time.monotonic()
        for key in [k for k, (seen, _) in self._talk.items() if now - seen > self.ttl]:
            self._talk.pop(key, None)

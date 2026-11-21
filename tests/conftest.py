"""Recorded answers from the library's assistant, replayed into an Answer the way the live stream
would fill one. Capture a new one with `curl -N` against /api/chat and drop it in tests/data."""
from pathlib import Path

import pytest

from pds_discord.api import Answer, apply, feed_lines

DATA = Path(__file__).parent / "data"


def replay(name: str, question: str = "does finasteride cause lasting effects?") -> Answer:
    answer = Answer(question=question)
    for event, data in feed_lines((DATA / name).read_text().splitlines()):
        apply(answer, event, data)
    return answer


@pytest.fixture
def answered() -> Answer:
    return replay("answer.sse")


@pytest.fixture
def declined() -> Answer:
    return replay("decline.sse", "what dose should I take?")


@pytest.fixture
def supported() -> Answer:
    return replay("support.sse", "I can't go on like this")

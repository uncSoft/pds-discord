#!/usr/bin/env python
"""Ask the assistant from the terminal, with no Discord in the way.

    .venv/bin/python tools/ask.py "does finasteride cause lasting effects?"
    .venv/bin/python tools/ask.py --health

Useful for seeing what the bot will be handed, and for capturing a stream into tests/data.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pds_discord.api import AskError, Chat  # noqa: E402
from pds_discord.render import source_lines  # noqa: E402


async def main() -> int:
    args = [a for a in sys.argv[1:] if a]
    chat = Chat(os.getenv("PDS_CHAT_URL", "https://ask.postdrugsyndrome.com"),
                os.getenv("PDS_CLIENT_KEY", ""))
    try:
        if not args or args[0] == "--health":
            print(await chat.health())
            return 0
        shown = 0
        reply = None
        async for reply in chat.ask(" ".join(args)):
            print(reply.text[shown:], end="", flush=True)
            shown = len(reply.text)
        print()
        if reply and reply.background:
            print(f"\nBackground: {reply.background}")
        if reply and reply.cited:
            print("\nSources")
            for line in source_lines(reply.cited):
                print(" ", line)
        if reply and reply.further:
            print(f"\nFurther reading: {len(reply.further.get('papers') or [])} papers, "
                  f"{reply.further.get('in_library')} already in the library")
        if reply and (reply.decline or reply.support):
            print(f"\n[{'declined' if reply.decline else 'care-first reply'}]")
    except AskError as exc:
        print(f"could not ask: {exc.message}", file=sys.stderr)
        return 1
    finally:
        await chat.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

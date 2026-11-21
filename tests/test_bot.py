"""The answering loop, driven with stand-in Discord objects: no token, no gateway, no network."""
import asyncio
import os

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test.token.value")

from pds_discord import render                      # noqa: E402
from pds_discord.api import Answer, AskError        # noqa: E402
from pds_discord.bot import Config, LibraryBot      # noqa: E402
from pds_discord.limits import Budgets, Gate        # noqa: E402

from conftest import replay                         # noqa: E402


class FakeUser:
    def __init__(self, id: int) -> None:
        self.id = id


class FakeInteraction:
    """Only what bot.answer touches: who asked, where, and the reply it edits."""

    def __init__(self, user_id: int = 7, guild_id: int | None = 11, channel_id: int = 22) -> None:
        self.user = FakeUser(user_id)
        self.guild_id, self.channel_id = guild_id, channel_id
        self.edits: list[dict] = []

    async def edit_original_response(self, **kwargs) -> None:
        self.edits.append(kwargs)

    @property
    def shown(self) -> list:
        return self.edits[-1]["embeds"] if self.edits else []


def a_bot(**chat) -> LibraryBot:
    bot = LibraryBot(Config())
    for name, value in chat.items():
        setattr(bot.chat, name, value)
    return bot


def streaming(*answers):
    """A stand-in for Chat.ask: hands back each answer in turn."""
    async def ask(question, history=None, user_id=None):
        ask.asked = (question, list(history or []), user_id)
        for answer in answers:
            yield answer
    ask.asked = None
    return ask


def run(bot: LibraryBot, interaction: FakeInteraction, question: str = "does it persist?") -> None:
    asyncio.run(bot.answer(interaction, question))


def test_a_finished_answer_is_shown_with_its_sources_and_buttons(answered):
    bot, who = a_bot(ask=streaming(answered)), FakeInteraction()
    run(bot, who)
    titles = [e.title for e in who.shown]
    assert titles[:3] == [answered.question, "Background", "Sources"]
    assert who.edits[-1]["view"] is not None  # the link buttons arrive with the final edit


def test_the_question_and_an_empty_history_reach_the_assistant(answered):
    ask = streaming(answered)
    bot, who = a_bot(ask=ask), FakeInteraction()
    run(bot, who, "does it persist?")
    assert ask.asked == ("does it persist?", [], 7)


def test_a_follow_up_carries_the_last_exchange(answered):
    bot = a_bot(ask=streaming(answered))
    run(bot, FakeInteraction(), "first question?")
    follow = streaming(replay("answer.sse"))
    bot.chat.ask = follow
    run(bot, FakeInteraction(), "and in women?")
    question, history, _ = follow.asked
    assert question == "and in women?"
    assert [t["role"] for t in history] == ["user", "assistant"]
    assert history[0]["content"] == "first question?"
    assert "(#1 PFS-001)" in history[1]["content"]


def test_a_declined_question_is_not_remembered(declined):
    bot = a_bot(ask=streaming(declined))
    run(bot, FakeInteraction(), "what should I take?")
    assert bot.threads.history((22, 7)) == []


def test_the_care_first_reply_is_shown_alone_and_not_remembered(supported):
    bot, who = a_bot(ask=streaming(supported)), FakeInteraction()
    run(bot, who, "I can't go on like this")
    assert len(who.shown) == 1 and who.shown[0].colour.value == render.CARE
    assert bot.threads.history((22, 7)) == []
    assert who.edits[-1]["view"] is not None  # the crisis-lines button


def test_a_reader_who_asks_too_fast_is_told_to_wait(answered):
    bot = a_bot(ask=streaming(answered))
    bot.budgets = Budgets(user=((60, 1),), guild=((60, 9),), overall=((60, 9),))
    run(bot, FakeInteraction())
    who = FakeInteraction()
    run(bot, who)
    assert len(who.edits) == 1
    assert "asked a few in a row" in who.shown[0].description


def test_a_busy_bot_asks_the_reader_to_come_back(answered):
    class Full:
        async def __aenter__(self):
            raise TimeoutError("busy")

        async def __aexit__(self, *_):
            return None

    bot, who = a_bot(ask=streaming(answered)), FakeInteraction()
    bot.gate = Full()
    run(bot, who)
    assert "still working through other questions" in who.shown[0].description
    assert bot.budgets.user.wait(7) == 0.0  # the question was given back


def test_an_assistant_that_cannot_answer_says_so_in_its_own_words():
    async def ask(question, history=None, user_id=None):
        raise AskError("Please wait a minute.")
        yield  # pragma: no cover

    bot, who = a_bot(ask=ask), FakeInteraction()
    run(bot, who)
    assert who.shown[0].description == "Please wait a minute."
    assert who.shown[0].colour.value == render.WARN
    assert bot.budgets.user.wait(7) == 0.0


def test_a_stream_that_ends_with_nothing_leaves_no_history():
    async def ask(question, history=None, user_id=None):
        if False:  # pragma: no cover
            yield
    bot, who = a_bot(ask=ask), FakeInteraction()
    run(bot, who)
    assert bot.threads.history((22, 7)) == []


def test_an_unexpected_failure_is_not_shown_to_the_reader_as_itself():
    async def ask(question, history=None, user_id=None):
        raise RuntimeError("a bug")
        yield  # pragma: no cover

    bot, who = a_bot(ask=ask), FakeInteraction()
    run(bot, who)
    assert "Something went wrong on my side" in who.shown[0].description
    assert "a bug" not in who.shown[0].description


def test_a_question_in_a_direct_message_works_without_a_server(answered):
    bot, who = a_bot(ask=streaming(answered)), FakeInteraction(guild_id=None)
    run(bot, who)
    assert who.shown[0].title == answered.question


def test_the_invite_asks_for_three_permissions_and_no_more():
    from pds_discord.bot import INVITE_PERMISSIONS
    assert INVITE_PERMISSIONS == 1024 | 2048 | 16384  # view channels, send messages, embed links


def test_the_client_key_is_sent_as_a_header_with_a_hashed_reader():
    bot = a_bot()
    assert bot.chat._headers(7) == {}  # no key configured, so nothing is sent
    bot.chat.client_key = "shhh"
    head = bot.chat._headers(7)
    assert head["X-PDS-Client"] == "shhh"
    assert head["X-PDS-Client-User"] and "7" != head["X-PDS-Client-User"]
    assert "X-PDS-Client-User" not in bot.chat._headers(None)


def test_one_question_in_flight_by_default():
    assert Config().at_once == 1
    assert isinstance(a_bot().gate, Gate)


def test_a_test_server_can_be_named_so_its_commands_appear_at_once():
    os.environ.pop("PDS_GUILD_ID", None)
    assert Config().guild_id == 0
    os.environ["PDS_GUILD_ID"] = "1566613383325818990"
    try:
        assert Config().guild_id == 1566613383325818990
    finally:
        os.environ.pop("PDS_GUILD_ID", None)


def test_every_command_is_open_to_everyone():
    """Nothing here is gated on a permission: a command Discord will not let someone send is not
    something this bot asked for."""
    bot = a_bot()
    for command in bot.tree.get_commands():
        assert command.default_permissions is None, command.name
        assert getattr(command, "guild_only", False) is False, command.name

"""A Discord bot for the post-drug syndromes research library.

It carries no corpus and no model. Every question goes to the library's own assistant at
`ask.postdrugsyndrome.com`, which holds the guard that declines requests for medical advice, the
fixed care-first reply for someone at risk, Dr Powers' attribution and the flags on each source.
This process turns what comes back into Discord embeds and keeps the asking rate civil. One copy of
the rules, one place to fix them.

Slash commands and the message context menu arrive as interactions, so the bot needs no privileged
intent: it never reads what anyone types in a channel.
"""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import time

import discord
from discord import app_commands
from dotenv import load_dotenv

from . import render
from .api import MAX_QUESTION, AskError, Chat
from .library import Library
from .limits import Budgets, Gate, Threads

log = logging.getLogger("pds_discord")

EDIT_EVERY = 1.75          # seconds between edits while an answer streams, to stay inside Discord's limits
SOURCE_URL = "https://github.com/uncSoft/pds-discord"  # AGPL: readers of the hosted bot can get its source
# View Channels, Send Messages, Embed Links. It needs nothing else, and never reads message content.
INVITE_PERMISSIONS = 19456
CARE_BUTTON = "Support and crisis lines"


class Config:
    def __init__(self) -> None:
        self.token = os.environ["DISCORD_TOKEN"]
        # The library's assistant, and the library site its records live on.
        self.chat_url = os.getenv("PDS_CHAT_URL", "https://ask.postdrugsyndrome.com").rstrip("/")
        self.site = os.getenv("PDS_SITE_ORIGIN", "https://postdrugsyndrome.com").rstrip("/")
        # Shared with the assistant, so it can hold its rate limit per reader rather than per
        # address. Without it the bot still works, inside one reader's allowance for all of Discord.
        self.client_key = os.getenv("PDS_CLIENT_KEY", "").strip()
        self.support_url = os.getenv("PDS_SUPPORT_URL", "").strip() or f"{self.site}/research/support"
        self.invite_url = os.getenv("PDS_INVITE_URL", "").strip()
        # A test server's ID. Global commands can take a while to reach a client; commands synced to
        # one server are there at once, which is what you want while trying the bot out.
        self.guild_id = int(os.getenv("PDS_GUILD_ID") or 0)
        # How many questions this bot will have in flight. Without the key the assistant answers
        # two at a time in all, so one leaves a slot for readers on the library's own site; with the
        # key it answers three, so the bot may hold two and still leave one.
        self.at_once = max(1, int(os.getenv("PDS_ASK_AT_ONCE") or (2 if self.client_key else 1)))


class LibraryBot(discord.Client):
    def __init__(self, config: Config) -> None:
        # No privileged intents: guilds alone, for the server list in the log.
        super().__init__(intents=discord.Intents(guilds=True),
                         allowed_mentions=discord.AllowedMentions.none())
        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.chat = Chat(config.chat_url, config.client_key)
        self.library = Library(config.site)
        # Wider once the key is set, because the assistant then holds a limit per reader itself.
        self.budgets = Budgets.for_client(bool(config.client_key))
        self.gate = Gate(config.at_once)
        self.threads = Threads()
        register_commands(self)

    async def setup_hook(self) -> None:
        # Global, so the commands work in every server the bot joins and the App Directory sees
        # them. What comes back is what Discord accepted, which is the only way to know from here
        # that a command people cannot find was ever registered.
        synced = await self.tree.sync()
        log.info("registered %d commands: %s", len(synced), ", ".join(sorted(c.name for c in synced)))
        if self.config.guild_id:
            here = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=here)
            now = await self.tree.sync(guild=here)
            log.info("and %d in server %s, which take effect at once: %s", len(now),
                     self.config.guild_id, ", ".join(sorted(c.name for c in now)))
        try:
            health = await self.chat.health()
            log.info("the assistant is %s (%s passages, model %s)",
                     "answering" if health.get("ok") else "not answering just now",
                     health.get("chunks"), health.get("model"))
        except Exception as exc:  # the bot still starts; questions will say so
            log.warning("could not reach the assistant at %s (%s)", self.config.chat_url, exc)

    async def on_ready(self) -> None:
        log.info("connected as %s, in %d servers", self.user, len(self.guilds))

    async def on_app_command_completion(self, interaction: discord.Interaction, command) -> None:
        # The command and where, never what was asked: these are health questions.
        log.info("/%s in server %s", command.qualified_name, interaction.guild_id)

    async def close(self) -> None:
        await self.chat.close()
        await self.library.close()
        await super().close()

    def invite(self) -> str:
        if self.config.invite_url:
            return self.config.invite_url
        if not self.user:
            return ""
        return (f"https://discord.com/oauth2/authorize?client_id={self.user.id}"
                f"&scope=bot+applications.commands&permissions={INVITE_PERMISSIONS}")

    def answer_view(self, support: bool = False) -> discord.ui.View | None:
        if support:
            return render.link_view((CARE_BUTTON, "💬", self.config.support_url))
        return render.link_view(("Ask the library", "🔎", f"{self.config.chat_url}/"),
                                ("The library", "📚", f"{self.config.site}/research"))

    async def answer(self, interaction: discord.Interaction, question: str) -> None:
        """Put one question to the assistant and keep the reply up to date while it streams.
        The interaction is already deferred."""
        key = (interaction.channel_id, interaction.user.id)
        log.info("asking in server %s (%d characters, %d turns of history)",
                 interaction.guild_id, len(question), len(self.threads.history(key)))
        if wait := self.budgets.take(interaction.user.id, interaction.guild_id):
            log.info("not asked: the reader or the server is over its allowance")
            await interaction.edit_original_response(
                embeds=[discord.Embed(colour=render.MUTED, description=wait)])
            return
        last, shown, reply = 0.0, False, None
        try:
            async with self.gate:
                async for reply in self.chat.ask(question, self.threads.history(key),
                                                 interaction.user.id):
                    now = time.monotonic()
                    if not reply.done and now - last < EDIT_EVERY:
                        continue
                    last, shown = now, True
                    await interaction.edit_original_response(
                        embeds=render.answer_embeds(reply, chat_url=f"{self.config.chat_url}/",
                                                    streaming=not reply.done),
                        view=self.answer_view(reply.support) if reply.done else discord.utils.MISSING)
            if reply is None:
                log.warning("the assistant closed the connection without answering")
                return
            log.info("answered: %d characters, %d sources, %s", len(reply.text), len(reply.cited),
                     "care-first reply" if reply.support else "declined" if reply.decline
                     else f"error: {reply.error}" if reply.error else "ok")
            if reply.text and not (reply.decline or reply.support or reply.error):
                self.threads.remember(key, question, reply.as_history())
            elif reply.support or reply.decline:
                self.threads.clear(key)  # neither belongs in a follow-up's history
        except TimeoutError:
            log.info("not asked: this bot already has its questions in flight")
            self.budgets.refund(interaction.user.id, interaction.guild_id)
            await interaction.edit_original_response(embeds=[discord.Embed(
                colour=render.MUTED,
                description="I'm still working through other questions. Please ask again shortly.")])
        except AskError as exc:
            log.warning("the assistant would not answer: %s", exc.message)
            self.budgets.refund(interaction.user.id, interaction.guild_id)
            await interaction.edit_original_response(
                embeds=[discord.Embed(colour=render.WARN, description=exc.message)])
        except discord.HTTPException as exc:
            log.warning("could not update the reply: %s", exc)
        except Exception:
            log.exception("a question could not be answered")
            if not shown:
                await interaction.edit_original_response(embeds=[discord.Embed(
                    colour=render.WARN,
                    description="Something went wrong on my side. Please try again shortly.")])


def register_commands(bot: LibraryBot) -> None:
    tree = bot.tree
    site = bot.config.site

    @tree.error
    async def failed(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        """Anything that got past the handlers: say so in the log, and to whoever is waiting, rather
        than leaving Discord's "thinking" to spin until it times out."""
        log.exception("/%s failed", getattr(interaction.command, "name", "?"), exc_info=error)
        said = discord.Embed(colour=render.WARN,
                             description="Something went wrong on my side. Please try again shortly.")
        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(embeds=[said])
            else:
                await interaction.response.send_message(embed=said, ephemeral=True)
        except discord.HTTPException:
            pass

    async def records(interaction: discord.Interaction, partial: str) -> list[app_commands.Choice[str]]:
        hits = await bot.library.suggest(partial, limit=25)
        return [app_commands.Choice(name=f"{e.id} · {e.title}"[:100], value=e.id or e.href[:100])
                for e in hits]

    async def terms(interaction: discord.Interaction, partial: str) -> list[app_commands.Choice[str]]:
        hits = await bot.library.suggest(partial, limit=25, kinds={"glossary"})
        return [app_commands.Choice(name=e.title[:100], value=(e.id or e.title)[:100]) for e in hits]

    @tree.command(name="ask", description="Ask the research library a question about PFS, PSSD or PRSD")
    @app_commands.describe(question="A research question. The library doesn't give medical advice.",
                           private="Show the answer only to you")
    async def ask(interaction: discord.Interaction, question: str, private: bool = False) -> None:
        if len(question.strip()) > MAX_QUESTION:
            await interaction.response.send_message(
                f"That question is longer than the assistant takes ({MAX_QUESTION} characters). "
                "Could you shorten it?", ephemeral=True)
            return
        # Discord shows its own "thinking" state after a defer, so nothing is edited in until the
        # first tokens arrive: an edit sent in the same breath as the defer can come back 404.
        await interaction.response.defer(thinking=True, ephemeral=private)
        await bot.answer(interaction, question)

    @tree.context_menu(name="Ask the library")
    async def ask_message(interaction: discord.Interaction, message: discord.Message) -> None:
        """Someone asked a question in chat: put their own words to the assistant. The message
        arrives inside the interaction, so this needs no message-content intent."""
        question = " ".join((message.content or "").split())
        if len(question) < 8:
            await interaction.response.send_message(
                "There's no question in that message for me to look up.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        await bot.answer(interaction, question[:MAX_QUESTION])

    @tree.command(name="search", description="Find records in the library by title, keyword or ID")
    @app_commands.describe(words="Words, a record ID like PFS-001, or a catalog number like 214")
    async def search(interaction: discord.Interaction, words: str) -> None:
        await interaction.response.defer(thinking=True)
        hits = await bot.library.search(words, limit=5)
        await interaction.edit_original_response(
            embeds=[render.search_embed(words, hits, site)],
            view=render.link_view(("Search the library", "🔎", f"{site}/research/search"),
                                  ("Ask the assistant", "💬", f"{bot.config.chat_url}/")))

    @tree.command(name="record", description="Show one record in the library")
    @app_commands.describe(record="Start typing a title or an ID")
    @app_commands.autocomplete(record=records)
    async def record(interaction: discord.Interaction, record: str) -> None:
        await interaction.response.defer(thinking=True)
        found = await bot.library.record(record)
        if not found:
            await interaction.edit_original_response(embeds=[discord.Embed(
                colour=render.MUTED, description=f"I can't find **{record[:80]}** in the library.")])
            return
        await interaction.edit_original_response(
            embeds=[render.record_embed(found, site)],
            view=render.link_view(("Open in the library", "📚", found.url(site))))

    @tree.command(name="glossary", description="What a term means in the library")
    @app_commands.describe(term="Start typing a term")
    @app_commands.autocomplete(term=terms)
    async def glossary(interaction: discord.Interaction, term: str) -> None:
        await interaction.response.defer(thinking=True)
        found = await bot.library.glossary(term)
        if not found:
            await interaction.edit_original_response(embeds=[discord.Embed(
                colour=render.MUTED,
                description=f"**{term[:80]}** isn't in the library's glossary. `/ask` will try the papers.")])
            return
        await interaction.edit_original_response(
            embeds=[render.record_embed(found, site)],
            view=render.link_view(("Open the glossary", "📖", f"{site}/research/glossary")))

    @tree.command(name="news", description="The library's most recent approved news")
    async def news(interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        items = await bot.library.news(limit=5)
        await interaction.edit_original_response(
            embeds=[render.news_embed(items, site)],
            view=render.link_view(("The library", "📚", f"{site}/research")))

    @tree.command(name="support", description="Support, and crisis lines if you need them now")
    async def support(interaction: discord.Interaction) -> None:
        embed = discord.Embed(colour=render.CARE, description=(
            "If you are in crisis, please reach someone now. The library keeps the lines for "
            f"several countries on its [support page]({bot.config.support_url}).\n\n"
            "This bot answers research questions. It cannot give medical advice, and it is not "
            "a substitute for care from a clinician."))
        await interaction.response.send_message(
            embed=embed, view=render.link_view((CARE_BUTTON, "💬", bot.config.support_url)))

    @tree.command(name="forget", description="Forget the conversation we were having")
    async def forget(interaction: discord.Interaction) -> None:
        bot.threads.clear((interaction.channel_id, interaction.user.id))
        await interaction.response.send_message(
            "Forgotten. Your next question starts fresh.\n"
            "Nothing was written down: the few turns I keep for follow-up questions are held in "
            "memory for ten minutes and never stored.", ephemeral=True)

    @tree.command(name="about", description="What this bot answers from")
    async def about(interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="Post-drug syndromes research library",
            url=f"{site}/research", colour=render.ACCENT,
            description=(
                "A curated library of the research on post-finasteride syndrome, post-SSRI sexual "
                "dysfunction and post-retinoid sexual dysfunction, with an assistant that answers "
                "from its reviewed records and cites each one.\n\n"
                "`/ask` puts your question to that assistant. `/search`, `/record` and `/glossary` "
                "read the library directly."))
        embed.add_field(name="What it won't do", value=(
            "No medical advice, no dosing, no diagnosis. Dr Powers' views are attributed to him, "
            "retracted and unverifiable sources are labelled, and every answer carries its sources."),
            inline=False)
        embed.add_field(name="Your questions", value=(
            "Not logged, here or by the assistant. A few recent turns stay in memory for follow-up "
            "questions, and `/forget` drops them."), inline=False)
        embed.add_field(name="Licence", value=(
            f"The library is CC BY 4.0 and citable by DOI. This bot is AGPL-3.0: [source]({SOURCE_URL})."),
            inline=False)
        await interaction.response.send_message(embed=embed, view=render.link_view(
            ("The library", "📚", f"{site}/research"),
            ("Add to your server", "➕", bot.invite()),
            ("Source", "🧩", SOURCE_URL)))


async def sweep_forever(bot: LibraryBot) -> None:
    while True:
        await asyncio.sleep(300)
        bot.threads.sweep()


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.info("python %s, discord.py %s", platform.python_version(), discord.__version__)
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise SystemExit("DISCORD_TOKEN is empty. Paste your bot token into .env (see the README).")
    if token.startswith("http") or token.count(".") != 2:
        raise SystemExit("DISCORD_TOKEN doesn't look like a bot token (three chunks separated by dots, "
                         "not a link). Developer Portal > your app > Bot > Reset Token, then paste it in.")
    bot = LibraryBot(Config())

    async def run() -> None:
        async with bot:
            bot.loop.create_task(sweep_forever(bot))
            await bot.start(token)

    try:
        asyncio.run(run())
    except discord.LoginFailure:
        raise SystemExit("Discord rejected the token. Reset it in the Developer Portal and paste the new one.")
    except KeyboardInterrupt:
        pass

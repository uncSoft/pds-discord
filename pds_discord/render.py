"""Turn an answer from the library's assistant into Discord embeds.

Discord caps an embed description at 4,096 characters and everything in one message at 6,000, so an
answer, its background note, its sources and its further reading each get a budget below and the
answer links out to the assistant when it does not fit. The shaping of the text is done by plain
functions so the awkward parts (citations, cutting on a boundary, source badges) are tested without
a Discord connection.
"""
from __future__ import annotations

import re

import discord

from .api import Answer, Source

ACCENT = 0x2457A6        # the assistant's own accent, so an answer here looks like one there
MUTED = 0x6B7280         # a declined question
CARE = 0x5F7A8A          # the care-first reply: calm, not alarming
WARN = 0x9A3412

MESSAGE_LIMIT = 5800     # Discord adds up every embed in a message and rejects the lot at 6,000
ANSWER_LIMIT = 2800      # of those, for the answer itself
BACKGROUND_LIMIT = 1000
SOURCES_LIMIT = 1600
FURTHER_LIMIT = 500
TITLE_LIMIT = 250

ADVICE_NOTE = "Research only, not medical advice"
CITATION_RE = re.compile(r"\[(B?\d{1,2})\]")
SENTENCE_END = re.compile(r"[.!?]\s")


def shorten(text: str, limit: int) -> tuple[str, bool]:
    """Cut text to fit, on a sentence boundary where there is one nearby, else on a word.
    True in second place when something was cut."""
    text = text.strip()
    if len(text) <= limit:
        return text, False
    window = text[:limit]
    ends = [m.end() for m in SENTENCE_END.finditer(window)]
    cut = ends[-1] if ends and ends[-1] > limit * 0.6 else window.rfind(" ")
    return window[:cut if cut > 0 else limit].rstrip(" ,;:"), True


def with_links(text: str, sources: list[Source]) -> str:
    """Make each citation a link to what it points at. A source is found by the number it was given
    ([1] in an answer, [B1] in a background note) and, failing that, by its place in the list."""
    by_number = {str(s.n): s for s in sources}
    for place, src in enumerate(sources, 1):
        by_number.setdefault(str(place), src)

    def one(m: re.Match) -> str:
        src = by_number.get(m.group(1))
        return f"[[{m.group(1)}]]({src.url})" if src and src.url else m.group(0)
    return CITATION_RE.sub(one, text)


def source_lines(sources: list[Source]) -> list[str]:
    """One line per source: its ID as a link, any warnings, then the title."""
    lines = []
    for s in sources:
        name = f"[{s.chip}]({s.url})" if s.url else s.chip
        badges = "".join(f" `{b}`" for b in s.badges)
        title = s.title or ""
        if s.year and title:
            title += f" ({s.year})"
        if s.also:
            title += f" · same paper as {s.also}"
        lines.append(f"**{s.n}.** {name}{badges}{f' — {title}' if title else ''}")
    return lines


def fit_lines(lines: list[str], limit: int) -> str:
    """As many whole lines as the budget allows, saying how many were left out."""
    out: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > limit - 30:
            left = len(lines) - len(out)
            out.append(f"*and {left} more, on the answer's page*" if left else "")
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(p for p in out if p)


def further_text(further: dict | None) -> str:
    """PubMed's suggestions, which are further reading rather than part of the answer."""
    if not further:
        return ""
    papers = further.get("papers") or []
    search_url = further.get("search_url")
    if not papers:
        if further.get("in_library"):
            return "PubMed's closest matches are already in the library."
        return ""
    lines = []
    for p in papers[:3]:
        bits = [b for b in (p.get("journal"), str(p.get("year") or "")) if b]
        kind = " `review`" if p.get("review") else " `preprint`" if p.get("preprint") else ""
        title, _ = shorten(str(p.get("title") or "untitled"), 110)
        lines.append(f"· [{title}]({p.get('url')}){kind}{f' — {", ".join(bits)}' if bits else ''}")
    if search_url:
        lines.append(f"[More on PubMed]({search_url})")
    return fit_lines(lines, FURTHER_LIMIT)


def link_view(*buttons: tuple[str, str, str]) -> discord.ui.View | None:
    """A row of link buttons, from (label, emoji, url) triples. Empty urls are skipped."""
    rows = [(label, emoji, url) for label, emoji, url in buttons if url]
    if not rows:
        return None
    view = discord.ui.View()
    for label, emoji, url in rows:
        view.add_item(discord.ui.Button(label=label, emoji=emoji or None, url=url))
    return view


def fit_message(parts: list[tuple[int, discord.Embed]], limit: int = MESSAGE_LIMIT) -> list[discord.Embed]:
    """Keep a message inside Discord's ceiling.

    Discord counts the characters of every embed in a message together and refuses the whole
    message at 6,000, which leaves the reader looking at whatever was there before: for a streamed
    answer, the half-written one that still says it is answering. So shed what matters least until
    it fits, in the order given by each part's rank, and trim the answer's tail as a last resort.
    The answer and its sources are never dropped.
    """
    parts = list(parts)
    while len(parts) > 1 and sum(len(e) for _, e in parts) > limit:
        loosest = max(parts, key=lambda p: p[0])
        if loosest[0] <= 1:  # the answer and its sources stay
            break
        parts.remove(loosest)
    embeds = [e for _, e in parts]
    if (over := sum(len(e) for e in embeds) - limit) > 0 and embeds[0].description:
        keep = max(200, len(embeds[0].description) - over - 40)
        embeds[0].description = shorten(embeds[0].description, keep)[0] + " ..."
    return embeds


def answer_embeds(answer: Answer, *, chat_url: str, streaming: bool = False) -> list[discord.Embed]:
    """The finished answer, or what has arrived so far while it streams."""
    if answer.support:
        return [care_embed(answer)]
    if answer.error and not answer.text:
        return [discord.Embed(colour=WARN, description=answer.error)]

    title = shorten(answer.question, TITLE_LIMIT)[0]
    if answer.decline:
        return [discord.Embed(title=title, colour=MUTED, description=answer.text.strip())
                .set_footer(text=f"{ADVICE_NOTE} · the library answers research questions")]

    body, cut = shorten(answer.text, ANSWER_LIMIT)
    text = with_links(body, answer.sources)
    if cut:
        text += f"\n\n*This answer is longer than Discord allows. [Read the rest]({chat_url}).*"
    elif streaming:
        text += " ..."
    main = discord.Embed(title=title, colour=ACCENT, description=text or "...")

    if streaming:
        main.set_footer(text="Answering...")
        return [main]

    # Ranked by what a reader can least afford to lose, for when the message will not fit.
    parts: list[tuple[int, discord.Embed]] = [(0, main)]
    if answer.background:
        # Kept visibly apart from the answer, the way the library's own page keeps it: a background
        # note comes from the FDA drug labels or from general knowledge, never from the library.
        note, _ = shorten(answer.background, BACKGROUND_LIMIT)
        bg = discord.Embed(colour=MUTED, description=with_links(note, answer.background_sources),
                           title=("Background, from FDA drug labels" if answer.background_cited
                                  else "General background, not from the library"))
        if lines := source_lines(answer.background_sources):
            bg.add_field(name="From", value=fit_lines(lines, 400), inline=False)
        parts.append((2, bg))
    if cited := answer.cited:
        parts.append((1, discord.Embed(title="Sources", colour=ACCENT,
                                       description=fit_lines(source_lines(cited), SOURCES_LIMIT))))
    if further := further_text(answer.further):
        parts.append((3, discord.Embed(title="Further reading, from PubMed", colour=MUTED,
                                       description=further)))
    if answer.error:
        parts.append((1, discord.Embed(colour=WARN, description=answer.error)))

    embeds = fit_message(parts)
    embeds[-1].set_footer(text=f"{ADVICE_NOTE} · answered from the library's reviewed records")
    return embeds


def care_embed(answer: Answer) -> discord.Embed:
    """The assistant's fixed care-first reply, word for word. No sources, no advice note: it is
    written for someone who is struggling, and nothing here rewrites it."""
    return discord.Embed(colour=CARE, description=answer.text.strip())


def record_embed(entry, origin: str) -> discord.Embed:
    """One library record."""
    e = discord.Embed(title=shorten(entry.title or entry.id, TITLE_LIMIT)[0], url=entry.url(origin),
                      colour=WARN if entry.warnings else ACCENT,
                      description=shorten(entry.snippet, 1200)[0] or None)
    if entry.byline:
        e.add_field(name=entry.kind_label.capitalize(), value=shorten(entry.byline, 300)[0], inline=False)
    if entry.warnings:
        e.add_field(name="Read with care", value=" · ".join(entry.warnings), inline=False)
    bits = [entry.id] + ([f"#{entry.number}"] if entry.number else [])
    if entry.full_text:
        bits.append("full text held by the research group")
    e.set_footer(text=" · ".join(bits))
    return e


def search_embed(query: str, hits: list, origin: str) -> discord.Embed:
    """What the library has on a few words."""
    if not hits:
        return discord.Embed(colour=MUTED, description=(
            f"Nothing in the library matched **{shorten(query, 80)[0]}**. "
            "`/ask` puts the question to the assistant instead, which reads inside the papers."))
    lines = []
    for h in hits:
        warn = "".join(f" `{w}`" for w in h.warnings)
        line = f"**[{h.title or h.id}]({h.url(origin)})**{warn}\n{h.kind_label}"
        if h.byline:
            line += f" · {shorten(h.byline, 120)[0]}"
        lines.append(line)
    return discord.Embed(title=f"In the library: {shorten(query, 180)[0]}", colour=ACCENT,
                         description=fit_lines(lines, 3500))


def news_embed(items: list[dict], origin: str) -> discord.Embed:
    if not items:
        return discord.Embed(colour=MUTED, description="No news items have been approved yet.")
    lines = []
    for it in items:
        day = (it.get("date") or "")[:10]
        who = it.get("publisher") or ""
        lines.append(f"**[{shorten(str(it.get('title')), 140)[0]}]({it.get('url')})**\n"
                     f"{' · '.join(b for b in (who, day) if b)}\n"
                     f"{shorten(str(it.get('summary') or ''), 220)[0]}")
    e = discord.Embed(title="Approved news", colour=ACCENT, description=fit_lines(lines, 3500))
    e.set_footer(text="Approved for the library's news feed, not reviewed records")
    return e

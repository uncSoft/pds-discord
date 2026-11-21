from pds_discord import render
from pds_discord.api import Answer, Source
from pds_discord.library import Entry

CHAT = "https://ask.postdrugsyndrome.com/"
SITE = "https://postdrugsyndrome.com"


def test_short_text_is_left_alone():
    assert render.shorten("Two sentences. Both short.", 100) == ("Two sentences. Both short.", False)


def test_long_text_is_cut_on_a_sentence_boundary():
    text = "First sentence is here. " * 20
    out, cut = render.shorten(text, 100)
    assert cut and out.endswith("here.") and len(out) <= 100


def test_text_with_no_sentence_boundary_is_cut_on_a_word():
    out, cut = render.shorten("word " * 100, 40)
    assert cut and " " in out and not out.endswith(" ") and len(out) <= 40


def test_citations_become_links_to_the_record():
    text = render.with_links("persists [1], retracted [2], uncited [9]",
                             [Source(n=1, url="https://a"), Source(n=2, url=None)])
    assert "[[1]](https://a)" in text
    assert "[2]" in text and "[[2]]" not in text  # no url, so no link
    assert "[9]" in text                          # out of range, left as written


def test_a_source_line_carries_its_id_warnings_and_title(answered):
    lines = render.source_lines(answered.cited)
    assert lines[0].startswith("**1.** [PFS-001](https://postdrugsyndrome.com/research/entries/PFS-001/)")
    assert "(2020)" in lines[0]
    assert "`Retracted`" in lines[1]
    assert "`Powers' commentary`" in lines[2] and "same paper as PFS-012" in lines[2]


def test_lines_that_do_not_fit_are_counted_not_cut_in_half():
    lines = [f"**{n}.** a source line of some length" for n in range(1, 11)]
    out = render.fit_lines(lines, 160)
    assert "more, on the answer's page" in out
    assert all(line in out for line in out.splitlines()[:-1])


def test_further_reading_lists_papers_and_the_search(answered):
    out = render.further_text(answered.further)
    assert "pubmed.ncbi.nlm.nih.gov/39000001" in out and "More on PubMed" in out


def test_further_reading_says_when_pubmeds_matches_are_already_held():
    assert render.further_text({"papers": [], "in_library": 3}) == \
        "PubMed's closest matches are already in the library."
    assert render.further_text({"papers": [], "in_library": 0}) == ""
    assert render.further_text(None) == ""


def test_an_answer_becomes_answer_background_sources_and_further_reading(answered):
    embeds = render.answer_embeds(answered, chat_url=CHAT)
    assert [e.title for e in embeds] == [answered.question, "Background", "Sources",
                                         "Further reading, from PubMed"]
    assert "[[1]](https://postdrugsyndrome.com/research/entries/PFS-001/)" in embeds[0].description
    assert "EPI-015" not in embeds[2].description  # never cited, so never listed
    assert render.ADVICE_NOTE in embeds[-1].footer.text


def test_a_whole_message_stays_inside_discords_limit(answered):
    answered.text = "A long answer. " * 400
    answered.background = "Background. " * 200
    embeds = render.answer_embeds(answered, chat_url=CHAT)
    assert sum(len(e) for e in embeds) <= 6000
    assert all(len(e.description) <= 4096 for e in embeds)


def test_an_answer_too_long_for_discord_links_to_the_assistant(answered):
    answered.text = "Still going. " * 400
    embeds = render.answer_embeds(answered, chat_url=CHAT)
    assert f"[Read the rest]({CHAT})" in embeds[0].description


def test_while_it_streams_only_the_answer_shows(answered):
    embeds = render.answer_embeds(answered, chat_url=CHAT, streaming=True)
    assert len(embeds) == 1 and embeds[0].footer.text == "Answering..."
    assert embeds[0].description.endswith("...")


def test_a_declined_question_shows_the_assistants_own_words(declined):
    embeds = render.answer_embeds(declined, chat_url=CHAT)
    assert len(embeds) == 1 and embeds[0].colour.value == render.MUTED
    assert embeds[0].description == declined.text.strip()
    assert render.ADVICE_NOTE in embeds[0].footer.text


def test_the_care_first_reply_is_passed_through_untouched(supported):
    embeds = render.answer_embeds(supported, chat_url=CHAT)
    assert len(embeds) == 1
    assert embeds[0].description == supported.text.strip()
    assert embeds[0].colour.value == render.CARE
    assert embeds[0].footer.text is None  # no advice note under a crisis reply
    assert "988" in embeds[0].description


def test_a_server_error_with_no_answer_is_all_that_shows():
    embeds = render.answer_embeds(Answer(question="q", error="Something went wrong.", done=True),
                                  chat_url=CHAT)
    assert len(embeds) == 1 and embeds[0].description == "Something went wrong."


def test_a_record_card_shows_its_byline_and_warnings():
    entry = Entry.from_payload({
        "id": "DISC-024", "number": 214, "href": "/research/entries/DISC-024", "kind": "gray",
        "title": "A withdrawn case series", "snippet": "Withdrawn by the journal.",
        "authors": "Nobody", "year": 2019, "flags": {"retraction": "withdrawn in 2021"}})
    embed = render.record_embed(entry, SITE)
    assert embed.url == f"{SITE}/research/entries/DISC-024"
    assert embed.colour.value == render.WARN
    assert any("Retracted" in f.value for f in embed.fields)
    assert "#214" in embed.footer.text


def test_a_search_with_no_hits_points_at_ask():
    embed = render.search_embed("badminton", [], SITE)
    assert "`/ask`" in embed.description


def test_a_search_lists_each_hit_with_a_link():
    entry = Entry.from_payload({"id": "PFS-001", "href": "/research/entries/PFS-001", "kind": "paper",
                                "title": "Post-finasteride syndrome", "year": 2020})
    embed = render.search_embed("finasteride", [entry], SITE)
    assert f"({SITE}/research/entries/PFS-001)" in embed.description and "paper" in embed.description


def test_news_says_it_is_news_rather_than_a_reviewed_record():
    embed = render.news_embed([{"title": "A conference is announced", "url": "https://example.org/x",
                               "date": "2026-11-04", "publisher": "Example", "summary": "Short."}], SITE)
    assert "not reviewed records" in embed.footer.text
    assert "A conference is announced" in embed.description
    assert render.news_embed([], SITE).description.startswith("No news items")


def test_link_buttons_skip_the_ones_with_no_url():
    view = render.link_view(("One", "", "https://a"), ("Two", "", ""))
    assert view is not None and len(view.children) == 1
    assert render.link_view(("None", "", "")) is None

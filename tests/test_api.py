from pds_discord.api import (MAX_QUESTION, Answer, AskError, Feed, Source, apply, feed_lines,
                             flag_word, user_hash)

from conftest import replay


def test_a_whole_recorded_stream_fills_the_answer(answered):
    assert answered.done and not answered.decline and not answered.support
    assert answered.text.startswith("Several reviews describe")
    assert "[3]" in answered.text
    assert len(answered.sources) == 4
    assert answered.background.startswith("Finasteride's label lists")
    assert answered.background_sources[0].source == "label"
    assert answered.further["papers"][0]["pmid"] == "39000001"
    assert answered.error is None


def test_only_cited_sources_are_listed(answered):
    assert [s.n for s in answered.cited] == [1, 2, 3]  # the fourth is never cited


def test_cited_sources_keep_the_order_they_are_cited_in():
    answer = Answer(question="q", text="first [2] then [1] and [2] again",
                    sources=[Source(n=1, url="https://a"), Source(n=2, url="https://b")])
    assert [s.n for s in answer.cited] == [2, 1]


def test_a_citation_out_of_range_is_ignored():
    answer = Answer(question="q", text="see [9]", sources=[Source(n=1, url="https://a")])
    assert answer.cited == []


def test_history_turns_citations_into_labels(answered):
    turn = answered.as_history()
    assert turn["role"] == "assistant"
    assert "(#1 PFS-001)" in turn["content"] and "[1]" not in turn["content"]


def test_declined_and_support_streams_are_marked(declined, supported):
    assert declined.decline and declined.sources == []
    assert "can't say what anyone should take" in declined.text
    assert supported.support and "988" in supported.text


def test_badges_follow_the_chat_page_wording(answered):
    retracted, powers = answered.sources[1], answered.sources[2]
    assert retracted.badges == ["Retracted"]
    assert powers.badges == ["Powers' commentary"]
    assert answered.background_sources[1].badges == ["Textbook background"]


def test_flag_words():
    assert flag_word("RETRACTED. Withdrawn in 2021.") == "Retracted"
    assert flag_word("UNVERIFIABLE: the source link is dead") == "Unverifiable"
    assert flag_word("Metadata tentative: not fully verified") == "Tentative"
    assert flag_word("Evidence tier: Powers' unpublished theorizing") == "Powers' theory"
    assert flag_word("Quality note: small sample") == "Quality note"


def test_chip_prefers_the_record_id_and_names_news_and_textbooks():
    assert Source(n=1, label="PFS-001 paper", item_id="PFS-001").chip == "PFS-001"
    assert Source(n=1, label="Textbook: Basic Neurochemistry, 8th ed",
                  source="textbook").chip == "Basic Neurochemistry"
    assert Source(n=1, label="News", source="news", publisher="Reuters",
                  date="2026-11-04").chip == "Reuters · 2026-11"


def test_flags_given_as_an_object_still_read():
    assert Source.from_payload({"n": 1, "flags": {"retraction": "withdrawn"}}).flags == \
        ["retraction: withdrawn"]


def test_the_feed_ignores_what_it_cannot_use():
    feed = Feed()
    assert feed.push(": keep-alive comment") == []
    assert feed.push("event: token") == []
    assert feed.push("data: not json") == []
    assert feed.push("") == []  # dropped rather than raised


def test_the_feed_joins_a_payload_split_over_two_data_lines():
    events = list(feed_lines(["event: meta", "data: {", 'data: "decline": true}', ""]))
    assert events == [("meta", {"decline": True})]


def test_a_stream_that_ends_without_its_blank_line_still_reports():
    assert list(feed_lines(["event: done", "data: {}"])) == [("done", {})]


def test_apply_reports_only_what_is_worth_showing():
    answer = Answer(question="q")
    assert apply(answer, "meta", {"decline": False, "sources": []}) is False
    assert apply(answer, "token", "hello") is True
    assert apply(answer, "nonsense", {"x": 1}) is False


def test_an_error_event_is_kept_as_text():
    answer = replay("answer.sse")
    apply(answer, "error", "Something went wrong on the server.")
    assert answer.error == "Something went wrong on the server."


def test_the_reader_is_hashed_not_named():
    key, who = "s3cret", 123456789012345678
    assert user_hash(key, who) == user_hash(key, who)
    assert str(who) not in user_hash(key, who)
    assert user_hash(key, who) != user_hash("other", who)


def test_ask_error_carries_a_message_for_a_reader():
    exc = AskError("Please wait a minute.", 429)
    assert exc.message == "Please wait a minute." and exc.status == 429


def test_the_question_ceiling_matches_the_server():
    assert MAX_QUESTION == 1000


def test_a_background_source_is_numbered_apart_from_the_librarys_own(answered):
    """The server numbers a background note's sources B1, B2 because they are FDA drug labels.
    Coercing that to an integer raised, which killed the whole answer mid-stream."""
    assert [s.n for s in answered.background_sources] == ["B1", "B2"]
    assert answered.background_cited is True
    assert answered.text and answered.done           # the answer survived the background event
    assert [s.n for s in answered.cited] == [1, 2, 3]  # and [B1] is not mistaken for one of these


def test_an_fda_label_is_named_without_its_prefix(answered):
    assert answered.background_sources[0].chip == "Finasteride"


def test_a_source_number_that_is_neither_raises_nothing():
    assert Source.from_payload({"n": None}).n == ""
    assert Source.from_payload({"n": "7"}).n == 7
    assert Source.from_payload({"n": "B12"}).n == "B12"

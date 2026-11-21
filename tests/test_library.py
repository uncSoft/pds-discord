from pds_discord.library import Entry, rank, words

SITE = "https://postdrugsyndrome.com"

PAPER = {
    "id": "PFS-001", "number": 1, "href": "/research/entries/PFS-001", "kind": "paper",
    "collection": "core", "title": "Post-finasteride syndrome: a surmountable challenge for clinicians",
    "sourceLine": "Fertility and Sterility · 113(1):21-50", "authors": "Traish, Abdulmaged M.",
    "year": 2020, "dateLabel": "2020", "snippet": "Systematic evaluation of 250+ articles concludes PFS is real.",
    "keywords": ["finasteride", "5-alpha-reductase-inhibitor"], "topics": ["PFS"], "fullText": True,
}
TERM = {"id": "GL-allopregnanolone", "number": 86, "href": "/research/glossary/allopregnanolone",
        "kind": "glossary", "title": "Allopregnanolone", "snippet": "A neurosteroid made from progesterone.",
        "keywords": ["neurosteroid", "GABA"]}
RETRACTED = {"id": "DISC-024", "number": 214, "href": "/research/entries/DISC-024", "kind": "gray",
             "title": "A withdrawn case series", "snippet": "Withdrawn by the journal.",
             "flags": {"retraction": "withdrawn in 2021"}}
POWERS = {"id": "POW-012", "href": "/research/entries/POW-012", "kind": "powers-comment",
          "title": "Comment on allopregnanolone", "dateLabel": "April 2023",
          "flags": {"powers": "author"}, "snippet": "Clinical theorizing, not published evidence."}

ENTRIES = [Entry.from_payload(e) for e in (PAPER, TERM, RETRACTED, POWERS)]


def test_an_entry_reads_the_index_fields_it_shows():
    e = Entry.from_payload(PAPER)
    assert e.id == "PFS-001" and e.kind_label == "paper" and e.full_text
    assert e.url(SITE) == "https://postdrugsyndrome.com/research/entries/PFS-001"
    assert e.byline == "Traish, Abdulmaged M. · Fertility and Sterility · 113(1):21-50 · 2020"


def test_a_byline_does_not_repeat_the_year_already_in_the_source_line():
    e = Entry.from_payload({**PAPER, "sourceLine": "Fertility and Sterility, 2020"})
    assert e.byline.count("2020") == 1


def test_flags_become_the_librarys_own_warning_words():
    assert Entry.from_payload(RETRACTED).warnings == ["Retracted"]
    assert Entry.from_payload(POWERS).warnings == ["Dr Powers' material"]
    assert Entry.from_payload(PAPER).warnings == []


def test_a_record_id_wins_outright():
    assert [e.id for e in rank(ENTRIES, "PFS-001")] == ["PFS-001"]
    assert [e.id for e in rank(ENTRIES, "disc-024")] == ["DISC-024"]


def test_a_catalog_number_finds_its_record():
    assert [e.id for e in rank(ENTRIES, "#214")] == ["DISC-024"]


def test_a_title_match_beats_a_keyword_match():
    hits = rank(ENTRIES, "allopregnanolone", limit=4)
    assert hits[0].id == "GL-allopregnanolone"
    assert "POW-012" in [h.id for h in hits]


def test_keywords_and_summaries_are_searched_too():
    assert [e.id for e in rank(ENTRIES, "neurosteroid")] == ["GL-allopregnanolone"]
    assert [e.id for e in rank(ENTRIES, "systematic evaluation")] == ["PFS-001"]


def test_nothing_matching_finds_nothing():
    assert rank(ENTRIES, "badminton") == []
    assert rank(ENTRIES, "   ") == []


def test_a_limit_is_kept():
    assert len(rank(ENTRIES, "a", limit=2)) <= 2


def test_words_keeps_hyphenated_terms_whole():
    assert words("5-alpha-reductase Inhibitors") == ["5-alpha-reductase", "inhibitors"]


def test_the_term_itself_beats_a_longer_term_that_contains_it():
    short = Entry.from_payload({"id": "GL-androsterone", "href": "/research/glossary/androsterone",
                                "kind": "glossary", "title": "Androsterone"})
    longer = Entry.from_payload({"id": "GL-androsterone-glucuronide", "kind": "glossary",
                                 "href": "/research/glossary/androsterone-glucuronide",
                                 "title": "Androsterone glucuronide"})
    assert [e.id for e in rank([longer, short], "androsterone", limit=2)][0] == "GL-androsterone"

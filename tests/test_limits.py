from pds_discord.limits import (KEYED_USER_WINDOWS, USER_WINDOWS, Budget, Budgets,
                                Threads, human)


def test_a_budget_allows_its_window_then_says_how_long_to_wait():
    b = Budget(((60, 2),))
    assert b.take("reader", now=0.0) == 0.0
    assert b.take("reader", now=1.0) == 0.0
    assert b.take("reader", now=2.0) == 58.0  # the first of the two ages out at 60


def test_a_budget_frees_up_as_the_window_slides():
    b = Budget(((60, 2),))
    b.take("reader", now=0.0)
    b.take("reader", now=1.0)
    assert b.take("reader", now=61.0) == 0.0


def test_two_windows_both_hold():
    b = Budget(((60, 2), (3600, 3)))
    for t in (0.0, 1.0, 120.0):
        assert b.take("reader", now=t) == 0.0
    assert b.take("reader", now=240.0) > 0  # inside the minute, but the hour is spent


def test_keys_do_not_share_an_allowance():
    b = Budget(((60, 1),))
    assert b.take("one", now=0.0) == 0.0
    assert b.take("two", now=0.0) == 0.0


def test_a_question_given_back_can_be_asked_again():
    b = Budget(((60, 1),))
    b.take("reader", now=0.0)
    b.give_back("reader")
    assert b.take("reader", now=1.0) == 0.0


def test_the_reader_is_told_which_allowance_ran_out():
    budgets = Budgets(user=((60, 1),), guild=((60, 5),), overall=((60, 5),))
    assert budgets.take(1, 10) == ""
    assert "You've asked a few in a row" in budgets.take(1, 10)
    assert budgets.take(2, 10) == ""  # a different reader in the same server is fine


def test_a_busy_server_waits_before_everyone_else_does():
    budgets = Budgets(user=((60, 9),), guild=((60, 1),), overall=((60, 9),))
    assert budgets.take(1, 10) == ""
    assert "This server has asked a lot" in budgets.take(2, 10)


def test_the_whole_bot_has_a_ceiling_of_its_own():
    budgets = Budgets(user=((60, 9),), guild=((60, 9),), overall=((60, 1),))
    assert budgets.take(1, 10) == ""
    assert "as fast as it can for everyone" in budgets.take(2, 11)


def test_a_refund_restores_all_three_allowances():
    budgets = Budgets(user=((60, 1),), guild=((60, 1),), overall=((60, 1),))
    assert budgets.take(1, 10) == ""
    budgets.refund(1, 10)
    assert budgets.take(1, 10) == ""


def test_a_question_asked_in_a_direct_message_has_no_server():
    budgets = Budgets(user=((60, 2),), guild=((60, 1),), overall=((60, 2),))
    assert budgets.take(1, None) == ""
    assert budgets.take(1, None) == ""


def test_waiting_is_said_in_seconds_then_minutes():
    assert human(40) == "in 40 seconds"
    assert human(120) == "in 2 minutes"
    assert human(61) == "in 61 seconds"
    assert human(0) == "in 1 seconds"


def test_a_conversation_holds_the_last_few_messages():
    threads = Threads(turns=4)
    key = (1, 2)
    assert threads.history(key) == []
    threads.remember(key, "first?", {"role": "assistant", "content": "one"})
    threads.remember(key, "second?", {"role": "assistant", "content": "two"})
    assert [t["content"] for t in threads.history(key)] == ["first?", "one", "second?", "two"]
    assert [t["role"] for t in threads.history(key)] == ["user", "assistant", "user", "assistant"]
    threads.remember(key, "third?", {"role": "assistant", "content": "three"})
    assert [t["content"] for t in threads.history(key)] == ["second?", "two", "third?", "three"]


def test_history_never_exceeds_what_the_assistant_accepts():
    threads = Threads()
    for i in range(10):
        threads.remember((1, 2), f"q{i}?", {"role": "assistant", "content": f"a{i}"})
    assert len(threads.history((1, 2))) == 6


def test_conversations_are_separate_per_reader_and_channel():
    threads = Threads()
    threads.remember((1, 2), "mine?", {"role": "assistant", "content": "a"})
    assert threads.history((1, 3)) == []
    assert threads.history((9, 2)) == []


def test_forgetting_and_sweeping_drop_a_conversation():
    threads = Threads(ttl=0.0)
    threads.remember((1, 2), "q?", {"role": "assistant", "content": "a"})
    threads.clear((1, 2))
    assert threads.history((1, 2)) == []
    threads.remember((3, 4), "q?", {"role": "assistant", "content": "a"})
    threads.sweep()
    assert threads._talk == {}


def test_the_allowances_widen_once_the_assistant_knows_this_client():
    unkeyed, keyed = Budgets.for_client(False), Budgets.for_client(True)
    assert unkeyed.user.windows == USER_WINDOWS and keyed.user.windows == KEYED_USER_WINDOWS
    for budget in ("user", "guild", "overall"):
        before = dict(getattr(unkeyed, budget).windows)
        after = dict(getattr(keyed, budget).windows)
        assert all(after[w] > before[w] for w in before), budget


def test_the_keyed_allowances_stay_inside_what_the_assistant_gives_this_client():
    keyed = Budgets.for_client(True)
    assert dict(keyed.user.windows) == {60: 4, 3600: 25}       # the assistant allows 6 and 40 a reader
    assert dict(keyed.overall.windows) == {60: 18, 3600: 180}  # and 20 and 200 to the client as a whole

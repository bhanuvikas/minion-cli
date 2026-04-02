"""Tests for minion/memory/triggers.py — ExtractionTrigger implementations.

No API calls. Pure unit tests.
"""

import pytest

from minion.memory.triggers import (
    AlwaysTrigger,
    EveryNTurnsTrigger,
    ExtractionTrigger,
    ManualOnlyTrigger,
    SubstantialContentTrigger,
)

_SHORT = "OK, done."
_LONG = " ".join(["word"] * 60)  # 60 words, above default threshold


# ─── ABC contract ─────────────────────────────────────────────────────────────

class TestTriggerInterface:
    def test_all_triggers_implement_abc(self):
        for cls in (SubstantialContentTrigger, EveryNTurnsTrigger, AlwaysTrigger, ManualOnlyTrigger):
            assert issubclass(cls, ExtractionTrigger)


# ─── SubstantialContentTrigger ────────────────────────────────────────────────

class TestSubstantialContentTrigger:
    def test_fires_above_threshold(self):
        t = SubstantialContentTrigger(min_words=10)
        assert t.should_extract("q", " ".join(["w"] * 11)) is True

    def test_does_not_fire_below_threshold(self):
        t = SubstantialContentTrigger(min_words=50)
        assert t.should_extract("q", _SHORT) is False

    def test_fires_at_exactly_threshold(self):
        t = SubstantialContentTrigger(min_words=5)
        assert t.should_extract("q", " ".join(["w"] * 5)) is True

    def test_does_not_fire_one_below_threshold(self):
        t = SubstantialContentTrigger(min_words=5)
        assert t.should_extract("q", " ".join(["w"] * 4)) is False

    def test_default_threshold_is_50(self):
        t = SubstantialContentTrigger()
        assert t.min_words == 50

    def test_custom_threshold(self):
        t = SubstantialContentTrigger(min_words=3)
        assert t.should_extract("q", "one two three") is True
        assert t.should_extract("q", "one two") is False


# ─── AlwaysTrigger ────────────────────────────────────────────────────────────

class TestAlwaysTrigger:
    def test_always_fires_on_short_response(self):
        assert AlwaysTrigger().should_extract("q", _SHORT) is True

    def test_always_fires_on_long_response(self):
        assert AlwaysTrigger().should_extract("q", _LONG) is True

    def test_always_fires_on_empty_response(self):
        assert AlwaysTrigger().should_extract("q", "") is True


# ─── ManualOnlyTrigger ────────────────────────────────────────────────────────

class TestManualOnlyTrigger:
    def test_never_fires_on_long_response(self):
        assert ManualOnlyTrigger().should_extract("q", _LONG) is False

    def test_never_fires_on_short_response(self):
        assert ManualOnlyTrigger().should_extract("q", _SHORT) is False


# ─── EveryNTurnsTrigger ───────────────────────────────────────────────────────

class TestEveryNTurnsTrigger:
    def test_fires_on_nth_turn(self):
        t = EveryNTurnsTrigger(n=3)
        assert t.should_extract("q", "r") is False  # turn 1
        assert t.should_extract("q", "r") is False  # turn 2
        assert t.should_extract("q", "r") is True   # turn 3

    def test_does_not_fire_before_nth_turn(self):
        t = EveryNTurnsTrigger(n=5)
        for _ in range(4):
            assert t.should_extract("q", "r") is False

    def test_fires_again_at_2n_turns(self):
        t = EveryNTurnsTrigger(n=2)
        t.should_extract("q", "r")  # turn 1
        t.should_extract("q", "r")  # turn 2 — fires
        t.should_extract("q", "r")  # turn 3
        result = t.should_extract("q", "r")  # turn 4 — fires again
        assert result is True

    def test_n_equals_1_fires_every_turn(self):
        t = EveryNTurnsTrigger(n=1)
        for _ in range(5):
            assert t.should_extract("q", "r") is True

    def test_stateful_separate_instances(self):
        t1 = EveryNTurnsTrigger(n=3)
        t2 = EveryNTurnsTrigger(n=3)
        t1.should_extract("q", "r")  # t1 turn 1
        t1.should_extract("q", "r")  # t1 turn 2
        # t2 is untouched — should not fire on first call
        assert t2.should_extract("q", "r") is False

    def test_invalid_n_raises(self):
        with pytest.raises(ValueError):
            EveryNTurnsTrigger(n=0)

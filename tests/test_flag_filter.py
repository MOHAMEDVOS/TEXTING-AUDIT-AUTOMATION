"""Tests for _filter_flags and _load_invalid_flag_patterns in ai.scorer."""
import sqlite3
import pytest
from ai.scorer import _filter_flags, _load_invalid_flag_patterns


# ── _filter_flags ─────────────────────────────────────────────────────────────

def test_filter_flags_empty_patterns_returns_all():
    flags = ["Flag A", "Flag B"]
    assert _filter_flags(flags, set()) == ["Flag A", "Flag B"]


def test_filter_flags_empty_flags_returns_empty():
    patterns = {"some pattern"}
    assert _filter_flags([], patterns) == []


def test_filter_flags_exact_match_suppressed():
    flags = ["Ignored explicit opt-out and continued sending messages"]
    patterns = {"ignored explicit opt-out and continued sending messages"}
    assert _filter_flags(flags, patterns) == []


def test_filter_flags_pattern_is_substring_of_flag_suppressed():
    flags = ["Wrong label: assigned 'Verified, Not Interested' but should be 'Not Interested'"]
    patterns = {"wrong label: assigned 'verified, not int...' but should be 'not interested'"}
    assert _filter_flags(flags, patterns) == []


def test_filter_flags_flag_is_substring_of_pattern_suppressed():
    flags = ["Continued messaging after clear disinterest"]
    patterns = {"continued messaging after clear disinterest and kept pushing the script"}
    assert _filter_flags(flags, patterns) == []


def test_filter_flags_unrelated_flag_kept():
    flags = ["Lead said stop texting. Agent sent another message."]
    patterns = {"ignored explicit opt-out and continued sending messages"}
    assert _filter_flags(flags, patterns) == ["Lead said stop texting. Agent sent another message."]


def test_filter_flags_mixed_keeps_only_clean():
    flags = [
        "Ignored explicit opt-out and continued sending messages",
        "Agent asked price before checking condition.",
    ]
    patterns = {"ignored explicit opt-out and continued sending messages"}
    result = _filter_flags(flags, patterns)
    assert result == ["Agent asked price before checking condition."]


def test_filter_flags_case_insensitive():
    flags = ["IGNORED EXPLICIT OPT-OUT AND CONTINUED SENDING MESSAGES"]
    patterns = {"ignored explicit opt-out and continued sending messages"}
    assert _filter_flags(flags, patterns) == []


def test_filter_flags_truncation_wildcard_suppressed():
    # Neither f-in-p nor p-in-f holds (pattern has '...' gap that breaks substring).
    # Only the segment logic can suppress this — isolates the '...' branch.
    flags = ["Wrong label: assigned 'Verified, Not Interested' but should be 'Not Interested'"]
    patterns = {"wrong label: assigned 'verified...not interested'"}
    assert _filter_flags(flags, patterns) == []


def test_filter_flags_truncation_wildcard_not_suppressed_when_segments_missing():
    # Segments from '...' split don't both appear in the flag — should NOT suppress.
    flags = ["Agent asked price before checking condition."]
    patterns = {"wrong label: assigned 'verified...not interested'"}
    assert _filter_flags(flags, patterns) == ["Agent asked price before checking condition."]


def test_filter_flags_short_pattern_does_not_over_suppress():
    # Short patterns (< 15 chars) only match exactly — guards against accidental broad suppression.
    flags = ["Wrong label: missing rebuttal sequence"]
    patterns = {"wrong label"}  # too short to be a reliable fuzzy match
    assert _filter_flags(flags, patterns) == ["Wrong label: missing rebuttal sequence"]


def test_filter_flags_short_pattern_exact_match_suppressed():
    # A short pattern still suppresses when it matches exactly.
    flags = ["wrong label"]
    patterns = {"wrong label"}
    assert _filter_flags(flags, patterns) == []


# ── _load_invalid_flag_patterns ───────────────────────────────────────────────

def test_load_invalid_flag_patterns_returns_set(tmp_path):
    db = tmp_path / "test.db"
    con = sqlite3.connect(str(db))
    con.execute("""
        CREATE TABLE flag_feedback (
            id INTEGER PRIMARY KEY,
            red_flag TEXT NOT NULL
        )
    """)
    con.execute("INSERT INTO flag_feedback (red_flag) VALUES (?)", ("Ignored explicit opt-out",))
    con.execute("INSERT INTO flag_feedback (red_flag) VALUES (?)", ("Stated specific dollar amount as FIRM OFFER",))
    con.commit()
    con.close()

    patterns = _load_invalid_flag_patterns(str(db))

    assert "ignored explicit opt-out" in patterns
    assert "stated specific dollar amount as firm offer" in patterns
    assert len(patterns) == 2


def test_load_invalid_flag_patterns_lowercases(tmp_path):
    db = tmp_path / "test.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE flag_feedback (id INTEGER PRIMARY KEY, red_flag TEXT NOT NULL)")
    con.execute("INSERT INTO flag_feedback (red_flag) VALUES (?)", ("UPPER CASE FLAG",))
    con.commit()
    con.close()

    patterns = _load_invalid_flag_patterns(str(db))
    assert "upper case flag" in patterns


def test_load_invalid_flag_patterns_empty_table(tmp_path):
    db = tmp_path / "test.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE flag_feedback (id INTEGER PRIMARY KEY, red_flag TEXT NOT NULL)")
    con.commit()
    con.close()

    patterns = _load_invalid_flag_patterns(str(db))
    assert patterns == set()


def test_load_invalid_flag_patterns_missing_db_returns_empty(tmp_path):
    db = tmp_path / "nonexistent.db"
    patterns = _load_invalid_flag_patterns(str(db))
    assert patterns == set()


def test_load_invalid_flag_patterns_skips_null_entries(tmp_path):
    db = tmp_path / "test.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE flag_feedback (id INTEGER PRIMARY KEY, red_flag TEXT)")
    con.execute("INSERT INTO flag_feedback (red_flag) VALUES (?)", (None,))
    con.execute("INSERT INTO flag_feedback (red_flag) VALUES (?)", ("valid flag",))
    con.commit()
    con.close()

    patterns = _load_invalid_flag_patterns(str(db))
    assert patterns == {"valid flag"}

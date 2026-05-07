from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_red_flag_remap_gave_up_first_no_variants():
    import ai.analyzer as analyzer_mod

    out = analyzer_mod._normalize_red_flags(
        ["Gave Up After First 'No'", "gave up after first no with zero rebuttal"]
    )
    assert out == ["Gave up after first no with zero rebuttal."]


def test_red_flag_remap_revealed_6_month_variants():
    import ai.analyzer as analyzer_mod

    out = analyzer_mod._normalize_red_flags(
        ["Revealed or promised 6+ month timeline", "revealed 6 month timeline"]
    )
    assert out == ["Revealed or promised 6+ month timeline."]


def test_red_flag_remap_wrong_number_kept_selling():
    import ai.analyzer as analyzer_mod

    out = analyzer_mod._normalize_red_flags(["Wrong number but kept selling"])
    assert out == ["Continued original pitch after wrong number."]


def test_red_flag_remap_continued_after_opt_out():
    import ai.analyzer as analyzer_mod

    out = analyzer_mod._normalize_red_flags(["continued after unsubscribe request"])
    assert out == ["Continued texting after explicit opt-out."]


def test_red_flag_unknown_is_dropped():
    import ai.analyzer as analyzer_mod

    out = analyzer_mod._normalize_red_flags(["Continued After Explicit Disinterest"])
    assert out == []


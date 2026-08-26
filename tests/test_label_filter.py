"""Label-filter normalisation — deep review F37.

This logic sat duplicated in dashboard/app.py and scraper/api_bot.py, on opposite
sides of the dashboard->subprocess boundary where the two must agree.
"""
import pytest

from config.settings import is_all_labels_filter_value, normalize_label_filter
from scraper.api_bot import normalize_label_filter as api_bot_normalize
import dashboard.app as app


@pytest.mark.parametrize("value", ["all", "All Labels", "all label", "all lable", "all lables", "ALL  LABELS"])
def test_all_labels_sentinels_recognised(value):
    assert is_all_labels_filter_value(value) is True


@pytest.mark.parametrize("value", ["Hot Lead", "New Lead", "Extra", ""])
def test_real_labels_are_not_sentinels(value):
    assert is_all_labels_filter_value(value) is False


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, ""),
        ("", ""),
        ("All Labels", ""),
        ("all lables", ""),
        ("Hot Lead", "Hot Lead"),
        ("Hot Lead,Warm", "Hot Lead,Warm"),
        ("Hot Lead, all labels", "Hot Lead"),
        ("  Hot Lead ,  Warm  ", "Hot Lead,Warm"),
    ],
)
def test_normalisation(raw, expected):
    assert normalize_label_filter(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "All Labels", "Hot Lead", "Hot Lead, all labels", "Hot Lead,Warm"])
def test_all_three_call_sites_agree(raw):
    """The dashboard and the scraper must never diverge on this."""
    canonical = normalize_label_filter(raw)
    assert app._normalize_label_filter(raw) == canonical
    # api_bot maps "" -> None to signal "no filter"; otherwise identical.
    assert (api_bot_normalize(raw) or "") == canonical

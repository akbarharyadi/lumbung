"""Receipt parsing. The Indonesian thousands separator is the dangerous part."""

from __future__ import annotations

import pytest

from lumbung.ocr import Receipt, _json_from, _number, _valid_date


# ------------------------------------------------------------ amount parsing
@pytest.mark.parametrize(
    "raw,expected",
    [
        (150000, 150_000.0),
        (150000.0, 150_000.0),
        ("150.000", 150_000.0),        # Indonesian thousands, NOT 150.0
        ("1.234.567", 1_234_567.0),
        ("Rp 1.234.567", 1_234_567.0),
        ("Rp150.000", 150_000.0),
        ("1.234,50", 1_234.5),         # dot thousands, comma decimal
        ("89000", 89_000.0),
    ],
)
def test_indonesian_amounts_parse_correctly(raw, expected):
    """'150.000' read as 150.0 would log a thousand-times-too-small expense and
    never look obviously wrong."""
    assert _number(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", [None, "", "abc", 0, -5, "Rp 0"])
def test_unusable_amounts_return_none(raw):
    assert _number(raw) is None


# ---------------------------------------------------------------- json recovery
def test_plain_json_parses():
    assert _json_from('{"amount": 1000}') == {"amount": 1000}


def test_fenced_json_parses():
    assert _json_from('```json\n{"amount": 1000}\n```') == {"amount": 1000}


def test_json_embedded_in_prose_is_recovered():
    assert _json_from('Here you go:\n{"amount": 1000}\nHope that helps') == {"amount": 1000}


def test_unparseable_returns_none_rather_than_guessing():
    assert _json_from("I cannot read this receipt") is None
    assert _json_from("") is None


# --------------------------------------------------------------------- dates
def test_dates():
    assert _valid_date("2026-08-23") == "2026-08-23"
    assert _valid_date("23/08/2026") is None
    assert _valid_date(None) is None


# ------------------------------------------------------------------- receipt
def test_receipt_without_an_amount_is_not_usable():
    r = Receipt(None, "Indomaret", None, "food", "cash", [], 0.9)
    assert not r.usable and r.needs_review


def test_low_confidence_flags_review_even_with_an_amount():
    """A confident-looking number from an unsure model is the failure mode that
    silently corrupts the data."""
    assert Receipt(150_000, "X", None, "food", "cash", [], 0.4).needs_review
    assert not Receipt(150_000, "X", None, "food", "cash", [], 0.95).needs_review

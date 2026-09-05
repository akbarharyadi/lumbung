"""Tests for editing holdings and for parsing rupiah figures.

The number parser earned its own tests the hard way: a corrupted escape turned
the word boundary into a literal backspace, so the pattern could never match and
"7.401" parsed as 7.401 rupiah instead of 7,401. An average price wrong by 1000x
does not raise -- it just quietly reports a nonsensical P&L days later.
"""

from __future__ import annotations

import shutil

import pytest

import lumbung.web.settings as st
from lumbung.web.settings import SettingsError, _number


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A real holdings.yaml copy, so comment round-tripping is exercised."""
    from lumbung.config import PROJECT_ROOT

    dst = tmp_path / "holdings.yaml"
    shutil.copy(PROJECT_ROOT / "config" / "holdings.yaml", dst)
    monkeypatch.setattr(st, "_holdings_path", lambda: dst)
    return dst


# -- number parsing ----------------------------------------------------------
@pytest.mark.parametrize("raw,want", [
    ("7401", 7401.0),
    ("7.401", 7401.0),          # Indonesian thousands separator
    ("7,401", 7401.0),          # English
    ("17.000.000", 17_000_000.0),
    ("  6450  ", 6450.0),
    ("6450.5", 6450.5),         # a real decimal must survive
])
def test_number_accepts_both_separator_styles(raw, want):
    assert _number(raw) == pytest.approx(want)


def test_number_does_not_eat_a_genuine_decimal():
    """Without the word boundary this returns 12345 and nobody notices."""
    assert _number("1.2345") == pytest.approx(1.2345)


def test_number_strips_a_trailing_percent_sign():
    """Rates are typed the way they are read: "6.5%"."""
    assert _number("6.5%") == pytest.approx(6.5)
    assert _number("4%") == pytest.approx(4.0)


def test_number_rejects_prose_with_a_readable_message():
    """Not a float() traceback -- a sentence a person can act on."""
    with pytest.raises(SettingsError, match="could not read 'saldo' as a number"):
        _number("saldo")


# -- holdings ----------------------------------------------------------------
def test_add_a_holding(cfg):
    r = st.write_holding({"ticker": "BBRI", "lots": "20", "avg_price": "4520"})
    assert r["action"] == "added"
    assert any(h["ticker"] == "BBRI.JK" for h in st.read_holdings())


# -- wishes -------------------------------------------------------------------
def test_add_update_and_remove_a_wish(cfg):
    r = st.write_wish("Oven", amount="730rb",
                      note="Sharp EO-28LP, dual heater")
    assert r["action"] == "added"
    ws = st.read_wishes()
    assert any(w["name"] == "Oven" and w["amount_idr"] == 730_000
               and "dual heater" in w["note"] for w in ws)

    r = st.write_wish("Oven", amount="800rb")
    assert r["action"] == "updated"
    assert any(w["name"] == "Oven" and w["amount_idr"] == 800_000
               for w in st.read_wishes())

    r = st.write_wish("Oven", remove=True)
    assert r["action"] == "removed"
    assert not any(w["name"] == "Oven" for w in st.read_wishes())


def test_wish_never_matches_an_obligation(cfg):
    """A wish update must never touch a binding commitment (networth.py:
    obligations bind the safety net; wishes never do)."""
    import io

    with open(cfg, encoding="utf-8") as fh:
        doc = st._yaml.load(fh) or {}
    doc.setdefault("commitments", []).append(
        {"name": "Oven", "amount_idr": 1_000_000, "kind": "obligation"})
    buf = io.StringIO()
    st._yaml.dump(doc, buf)
    cfg.write_text(buf.getvalue(), encoding="utf-8")

    st.write_wish("Oven", amount="730rb")
    fresh = st._yaml.load(open(cfg, encoding="utf-8"))  # noqa: SIM115
    ovens = [c for c in fresh.get("commitments", [])
             if str(c.get("name")) == "Oven"]
    assert len(ovens) == 2                    # the obligation AND the new wish
    kinds = {c.get("kind") for c in ovens}
    assert kinds == {"obligation", "wish"}


def test_wish_amount_must_be_positive(cfg):
    with pytest.raises(SettingsError):
        st.write_wish("Oven", amount="0")


def test_ticker_suffix_is_added_for_you(cfg):
    """People type what Stockbit shows; the .JK is our implementation detail."""
    st.write_holding({"ticker": "bbri", "lots": "1", "avg_price": "4520"})
    assert any(h["ticker"] == "BBRI.JK" for h in st.read_holdings())


def test_updating_replaces_rather_than_duplicates(cfg):
    st.write_holding({"ticker": "BBRI", "lots": "20", "avg_price": "4520"})
    r = st.write_holding({"ticker": "BBRI", "lots": "30", "avg_price": "4400"})
    assert r["action"] == "updated"
    rows = [h for h in st.read_holdings() if h["ticker"] == "BBRI.JK"]
    assert len(rows) == 1
    assert rows[0]["lots"] == 30


def test_zero_lots_removes(cfg):
    st.write_holding({"ticker": "BBRI", "lots": "20", "avg_price": "4520"})
    r = st.write_holding({"ticker": "BBRI", "lots": "0", "avg_price": "0"})
    assert r["action"] == "removed"
    assert not any(h["ticker"] == "BBRI.JK" for h in st.read_holdings())


def test_removing_something_you_do_not_own_is_an_error(cfg):
    with pytest.raises(SettingsError, match="not in your holdings"):
        st.write_holding({"ticker": "GOTO", "lots": "0", "avg_price": "0"})


def test_comments_survive_a_write(cfg):
    """holdings.yaml carries the reasoning; a writer that strips it makes the
    file worse every time it is used."""
    st.write_holding({"ticker": "BBRI", "lots": "20", "avg_price": "4520"})
    text = cfg.read_text(encoding="utf-8")
    assert "Bank Central Asia" in text
    assert "1 lot = 100 shares" in text


def test_exit_levels_are_stored(cfg):
    st.write_holding({
        "ticker": "BBRI", "lots": "20", "avg_price": "4520",
        "take_profit": "5200", "cut_loss": "4100",
    })
    row = [h for h in st.read_holdings() if h["ticker"] == "BBRI.JK"][0]
    assert row["take_profit"] == 5200
    assert row["cut_loss"] == 4100


# -- refusals ----------------------------------------------------------------
def test_take_profit_below_average_is_rejected(cfg):
    with pytest.raises(SettingsError, match="above your average"):
        st.write_holding({"ticker": "BBRI", "lots": "20", "avg_price": "4520",
                          "take_profit": "4000"})


def test_cut_loss_above_average_is_rejected(cfg):
    with pytest.raises(SettingsError, match="below your average"):
        st.write_holding({"ticker": "BBRI", "lots": "20", "avg_price": "4520",
                          "cut_loss": "5000"})


def test_levels_the_wrong_way_round_are_rejected(cfg):
    with pytest.raises(SettingsError, match="below take profit"):
        st.write_holding({"ticker": "BBRI", "lots": "20", "avg_price": "4520",
                          "take_profit": "4600", "cut_loss": "4700"})


def test_blank_and_nonsense_tickers_are_rejected(cfg):
    for bad in ("", "   ", "BB RI!"):
        with pytest.raises(SettingsError):
            st.write_holding({"ticker": bad, "lots": "1", "avg_price": "1"})


def test_negative_lots_are_rejected(cfg):
    with pytest.raises(SettingsError, match="negative"):
        st.write_holding({"ticker": "BBRI", "lots": "-5", "avg_price": "4520"})


def test_zero_average_price_with_lots_is_rejected(cfg):
    with pytest.raises(SettingsError, match="above zero"):
        st.write_holding({"ticker": "BBRI", "lots": "20", "avg_price": "0"})


# -- cash ---------------------------------------------------------------------
def test_income_raises_cash_and_spending_lowers_it(cfg):
    start = st.add_to_cash(0)
    assert st.add_to_cash(5_000_000) == pytest.approx(start + 5_000_000)
    assert st.add_to_cash(-1_000_000) == pytest.approx(start + 4_000_000)


def test_cash_never_goes_negative(cfg):
    """A negative balance is not a fact about the world -- it means money moved
    through an account this file does not track."""
    st.add_to_cash(-999_999_999)
    assert st.add_to_cash(0) == 0.0


# -- shorthand amounts -------------------------------------------------------
@pytest.mark.parametrize("raw,want", [
    ("5jt", 5_000_000.0),
    ("500rb", 500_000.0),
    ("1.5jt", 1_500_000.0),
    ("1,5jt", 1_500_000.0),      # comma as decimal, Indonesian style
    ("15JT", 15_000_000.0),
])
def test_number_understands_jt_and_rb(raw, want):
    """These are how the amounts are actually typed, so they have to work."""
    assert _number(raw) == pytest.approx(want)


# -- other assets ------------------------------------------------------------
def test_add_and_remove_an_asset(cfg):
    r = st.write_asset("BSI Deposito", kind="savings", value="15jt", rate="6.5")
    assert r["action"] == "added"
    row = [a for a in st.read_assets() if a["name"] == "BSI Deposito"][0]
    assert row["value_idr"] == 15_000_000
    assert row["rate"] == pytest.approx(0.065)

    assert st.write_asset("BSI Deposito", value=0)["action"] == "removed"
    assert not [a for a in st.read_assets() if a["name"] == "BSI Deposito"]


def test_partial_name_matches(cfg):
    """"SR025" should find "SR025 (Sukuk Ritel)" without typing the whole thing."""
    r = st.write_asset("SR025", value="20jt")
    assert r["action"] == "updated"
    assert "SR025" in r["name"]


def test_ambiguous_name_is_refused_rather_than_guessed(cfg):
    st.write_asset("Test Bank A", kind="savings", value="1jt")
    st.write_asset("Test Bank B", kind="savings", value="1jt")
    with pytest.raises(SettingsError, match="more than one"):
        st.write_asset("Test Bank", value="2jt")


def test_rename_keeps_the_value(cfg):
    before = [a for a in st.read_assets() if "SR025" in a["name"]][0]
    st.rename_asset("SR025", "ORI027")
    after = [a for a in st.read_assets() if a["name"] == "ORI027"][0]
    assert after["value_idr"] == before["value_idr"]
    assert after["kind"] == before["kind"]


def test_unknown_kind_is_rejected(cfg):
    with pytest.raises(SettingsError, match="kind must be"):
        st.write_asset("Something", kind="property", value="1jt")


# -- selling moves value into cash -------------------------------------------
def test_selling_everything_closes_the_position_and_credits_cash(cfg):
    cash_before = st.add_to_cash(0)
    held = [a for a in st.read_assets() if "SR025" in a["name"]][0]["value_idr"]

    r = st.sell_asset("SR025")
    assert r["sold"] == pytest.approx(held)
    assert r["remaining"] == 0
    assert not [a for a in st.read_assets() if "SR025" in a["name"]]
    assert st.add_to_cash(0) == pytest.approx(cash_before + held)


def test_selling_part_leaves_the_rest(cfg):
    cash_before = st.add_to_cash(0)
    r = st.sell_asset("SR025", "5jt")
    assert r["remaining"] == pytest.approx(11_000_000)
    assert st.add_to_cash(0) == pytest.approx(cash_before + 5_000_000)


def test_cannot_sell_more_than_you_hold(cfg):
    with pytest.raises(SettingsError, match="cannot sell"):
        st.sell_asset("SR025", "999jt")


def test_selling_something_you_do_not_own_is_an_error(cfg):
    with pytest.raises(SettingsError, match="not in your assets"):
        st.sell_asset("Bitcoin ETF")


def test_revaluing_is_not_the_same_as_selling(cfg):
    """Gold falling in price and gold being sold give the same new number, and
    only one of them puts money in your account."""
    cash_before = st.add_to_cash(0)
    st.write_asset("Gold", value="25jt")          # price fell
    assert st.add_to_cash(0) == pytest.approx(cash_before), "revaluation must not credit cash"

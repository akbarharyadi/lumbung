"""Editing holdings.yaml without eating it.

The file is 116 lines of comments explaining why each target is what it is.
`deposit` used to round-trip the whole thing through yaml.safe_dump, which
drops every one of them -- it had simply never been run.
"""
import textwrap

import pytest
import yaml

from lumbung.holdings_io import adjust, bucket_balance, edit

SAMPLE = textwrap.dedent("""\
    # Everything you own and earn. This comment must survive.
    cash_idr: 9_000_000

    # Non-stock assets. `kind` picks the allocation bucket.
    other_assets:
      # Gold pays nothing; it only moves in price.
      - name: "Gold (Pegadaian)"
        kind: gold
        value_idr: 32_766_000
      - name: "Superbank"
        kind: savings
        value_idr: 10_000_000

    # Emergency fund target, in months of spending.
    emergency_fund_months: 6
    """)


@pytest.fixture()
def cfg(tmp_path):
    p = tmp_path / "holdings.yaml"
    p.write_text(SAMPLE, encoding="utf-8")
    return p


def _comments(p):
    return sum(1 for line in p.read_text(encoding="utf-8").splitlines()
               if line.strip().startswith("#"))


def test_comments_survive_an_edit(cfg):
    before = _comments(cfg)
    assert before == 4
    edit(cfg, lambda doc: adjust(doc, "cash", -1_000_000))
    assert _comments(cfg) == before, "a command that eats your notes is worse than one that does nothing"


def test_cash_moves_and_the_file_still_parses(cfg):
    edit(cfg, lambda doc: adjust(doc, "cash", -1_001_750))
    assert yaml.safe_load(cfg.read_text(encoding="utf-8"))["cash_idr"] == 7_998_250


def test_other_assets_are_found_by_kind(cfg):
    edit(cfg, lambda doc: adjust(doc, "savings", -2_000_000))
    d = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    sav = next(o for o in d["other_assets"] if o["kind"] == "savings")
    assert sav["value_idr"] == 8_000_000
    gold = next(o for o in d["other_assets"] if o["kind"] == "gold")
    assert gold["value_idr"] == 32_766_000, "the other bucket must not move"


def test_untouched_keys_are_untouched(cfg):
    edit(cfg, lambda doc: adjust(doc, "cash", -1))
    d = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert d["emergency_fund_months"] == 6
    assert len(d["other_assets"]) == 2


def test_a_move_below_zero_is_refused(cfg):
    """Clamping to zero would be a lie that looks like an answer.

    Naming the wrong bucket is the likely cause, and silently flooring the
    balance hides it behind a number that reads fine.
    """
    with pytest.raises(ValueError, match="would take it to"):
        edit(cfg, lambda doc: adjust(doc, "cash", -99_000_000))


def test_a_refused_move_leaves_the_file_alone(cfg):
    original = cfg.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        edit(cfg, lambda doc: adjust(doc, "cash", -99_000_000))
    assert cfg.read_text(encoding="utf-8") == original


def test_unknown_bucket_is_refused(cfg):
    with pytest.raises(ValueError, match="unknown bucket"):
        edit(cfg, lambda doc: adjust(doc, "shares", -1_000))


def test_a_missing_bucket_is_created(cfg):
    edit(cfg, lambda doc: adjust(doc, "bonds", 5_000_000))
    d = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert bucket_balance(d, "bonds") == 5_000_000


def test_bucket_balance_reads_without_writing(cfg):
    original = cfg.read_text(encoding="utf-8")
    d = yaml.safe_load(original)
    assert bucket_balance(d, "cash") == 9_000_000
    assert bucket_balance(d, "gold") == 32_766_000
    assert bucket_balance(d, "crypto") == 0, "absent means zero, not an error"
    assert cfg.read_text(encoding="utf-8") == original

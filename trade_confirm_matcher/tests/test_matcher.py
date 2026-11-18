from datetime import date

import pandas as pd
import pytest

from tcm.generate import generate
from tcm.matcher import compare, match, normalise_confirmations, resolve_counterparty
from tcm.report import build, validate

AS_OF = date(2026, 11, 18)


def booking(**kw):
    b = dict(trade_id="TRD-1", trade_date=date(2026, 11, 10), counterparty_code="ATLAS-SG",
             counterparty="Atlas Petroleum Trading Pte Ltd", product="BRENT", index="ICE Brent 1st Line",
             our_side="PAY_FIXED", fixed_price=75.123, quantity_per_period=50_000, unit="BBL", currency="USD",
             period_start=date(2027, 1, 1), period_end=date(2027, 3, 31), frequency="MONTHLY", booked_by="x")
    b.update(kw)
    return b


def confirmation(**kw):
    c = dict(conf_id="CNF-1", received_date=date(2026, 11, 10), sender_name="ATLAS PETROLEUM TRADING PTE. LTD.",
             our_trade_ref="TRD-1", trade_date=date(2026, 11, 10), cpty_side="RECEIVE_FIXED",
             floating_index="ICE Brent 1st Line", fixed_price=75.12, quantity=50_000, quantity_basis="PER_PERIOD",
             unit="bbl", currency="USD", period_start=date(2027, 1, 1), period_end=date(2027, 3, 31))
    c.update(kw)
    return c


def run_pair(b, c):
    return {f.field: f for f in compare(pd.Series(b), normalise_confirmations(pd.DataFrame([c])).iloc[0])}


def test_alias_resolves_to_same_entity():
    assert resolve_counterparty("Atlas Petroleum Trading Pte Limited")[0] == "ATLAS-SG"
    assert resolve_counterparty("ATLAS PETROLEUM TRADING (UK) LIMITED")[0] == "ATLAS-UK"
    assert resolve_counterparty("Totally Unknown Corp")[0] is None


def test_rounding_and_noise_are_not_breaks():
    r = run_pair(booking(), confirmation(quantity=150_000, quantity_basis="TOTAL"))
    assert all(f.status != "BREAK" for f in r.values())
    assert r["PRICE"].status == "TOLERANCE"
    assert r["QUANTITY"].status == "EXACT"


def test_price_outside_tolerance_and_transposition():
    r = run_pair(booking(fixed_price=75.12), confirmation(fixed_price=75.21))
    assert r["PRICE"].status == "BREAK"
    assert "transposition" in r["PRICE"].likely_cause


def test_quantity_fat_finger():
    r = run_pair(booking(), confirmation(quantity=500_000))
    assert r["QUANTITY"].status == "BREAK" and "factor 10" in r["QUANTITY"].likely_cause


def test_direction_is_mirrored():
    assert run_pair(booking(), confirmation())["DIRECTION"].status == "EXACT"
    assert run_pair(booking(), confirmation(cpty_side="PAY_FIXED"))["DIRECTION"].status == "BREAK"


def test_sibling_entity_counterparty_break():
    r = run_pair(booking(), confirmation(sender_name="Atlas Petroleum Trading (UK) Ltd"))
    assert r["COUNTERPARTY"].status == "BREAK"
    assert "same group" in r["COUNTERPARTY"].likely_cause


def test_economic_match_without_reference_and_duplicates():
    books = pd.DataFrame([booking(), booking(trade_id="TRD-2", fixed_price=80.0, our_side="RECEIVE_FIXED")])
    confs = pd.DataFrame([confirmation(our_trade_ref=None, fixed_price=75.5),   # price break, no ref
                          confirmation(conf_id="CNF-1")])                        # duplicate of the same conf_id
    m, _ = match(books, confs.iloc[[0]])
    assert m.pairs[0][:3] == ("TRD-1", "CNF-1", "ECONOMIC")
    m2, _ = match(books, pd.concat([confs.iloc[[0]], confs.iloc[[0]]]))
    assert len(m2.duplicates) == 1


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_end_to_end_recall(seed):
    bk, cf, truth = generate(400, 0.3, AS_OF, seed)
    m, cn = match(bk, cf)
    res, exc = build(bk, cn, m, AS_OF)
    v = validate(res, exc, truth).set_index("break_type")
    assert v.loc["(clean trade)", "recall_pct"] == 100.0
    assert (v["recall_pct"].dropna() >= 85).all()

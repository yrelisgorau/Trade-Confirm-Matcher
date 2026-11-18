"""Match counterparty confirmations to bookings, apply tolerance rules and classify breaks.

Pipeline
  1. normalise   - resolve the sender's name to a counterparty code, mirror the
                   counterparty's side to our perspective, convert TOTAL quantity
                   to quantity per period, upper-case units/indices.
  2. de-duplicate - the same confirmation reference received twice is flagged once.
  3. match pass 1 - by our trade reference quoted on the confirmation.
  4. match pass 2 - by economics, for confirmations with no (or an unknown) reference:
                   score each candidate booking on 7 key fields and accept the best
                   unique pair scoring >= ECONOMIC_MATCH_MIN_SCORE.
  5. compare     - field-by-field with tolerances; each difference becomes a break
                   with a type (what differs) and a likely cause (why it probably differs).
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

import pandas as pd

from . import config as C

BDAY = pd.offsets.BDay()
_CP = {c.code: c for c in C.COUNTERPARTIES}
_TICK_BY_INDEX = {p["index"].upper(): p["tick"] for p in C.PRODUCTS.values()}


# --------------------------------------------------------------------------- names
def _norm_name(s: str) -> str:
    s = s.upper().replace("&", " AND ")
    s = re.sub(r"[.,]", "", s)
    s = re.sub(r"[^A-Z0-9 ()]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_ALIAS_LOOKUP = {}
for _c in C.COUNTERPARTIES:
    for _n in (_c.legal_name, *_c.aliases):
        _ALIAS_LOOKUP[_norm_name(_n)] = _c.code


def resolve_counterparty(name: str) -> tuple[str | None, str]:
    """Return (counterparty code, how it was resolved)."""
    key = _norm_name(name)
    if key in _ALIAS_LOOKUP:
        return _ALIAS_LOOKUP[key], "alias table"
    close = difflib.get_close_matches(key, _ALIAS_LOOKUP, n=1, cutoff=0.9)
    if close:
        return _ALIAS_LOOKUP[close[0]], "fuzzy"
    return None, "unresolved"


# --------------------------------------------------------------------------- normalise
def _months(start, end) -> int:
    return (end.year - start.year) * 12 + end.month - start.month + 1


def normalise_confirmations(confs: pd.DataFrame) -> pd.DataFrame:
    df = confs.copy()
    resolved = df["sender_name"].map(resolve_counterparty)
    df["counterparty_code"] = resolved.map(lambda r: r[0])
    df["name_resolution"] = resolved.map(lambda r: r[1])
    df["our_side_implied"] = df["cpty_side"].map({"PAY_FIXED": "RECEIVE_FIXED", "RECEIVE_FIXED": "PAY_FIXED"})
    df["n_months"] = [_months(s, e) for s, e in zip(df["period_start"], df["period_end"])]
    df["quantity_per_period"] = df["quantity"].where(df["quantity_basis"] == "PER_PERIOD",
                                                     df["quantity"] / df["n_months"])
    df["unit"] = df["unit"].str.upper()
    df["index_norm"] = df["floating_index"].str.upper().str.strip()
    return df


# --------------------------------------------------------------------------- comparison
@dataclass
class FieldResult:
    field: str
    status: str            # EXACT | TOLERANCE | BREAK
    booked: object
    confirmed: object
    difference: object = None
    likely_cause: str = ""


def _price_check(b, c) -> FieldResult:
    tick = _TICK_BY_INDEX.get(b["index"].upper(), 0.01)
    diff = c["fixed_price"] - b["fixed_price"]
    ticks = abs(diff) / tick
    if abs(diff) < 1e-12:
        return FieldResult("PRICE", "EXACT", b["fixed_price"], c["fixed_price"], 0.0)
    if ticks <= C.PRICE_TOLERANCE_TICKS + 1e-9:
        return FieldResult("PRICE", "TOLERANCE", b["fixed_price"], c["fixed_price"], round(diff, 6),
                           f"rounding ({ticks:.2f} tick)")
    cause = "price differs by %.0f ticks" % ticks
    db, dc = re.sub(r"\D", "", f"{b['fixed_price']:.6f}").rstrip("0"), re.sub(r"\D", "", f"{c['fixed_price']:.6f}").rstrip("0")
    if sorted(db) == sorted(dc) and db != dc:
        cause = "digit transposition (fat finger)"
    return FieldResult("PRICE", "BREAK", b["fixed_price"], c["fixed_price"], round(diff, 6), cause)


def _qty_check(b, c) -> FieldResult:
    qb, qc = float(b["quantity_per_period"]), float(c["quantity_per_period"])
    if qb == qc:
        return FieldResult("QUANTITY", "EXACT", qb, qc, 0.0)
    if abs(qc - qb) <= C.QUANTITY_REL_TOLERANCE * max(abs(qb), 1):
        return FieldResult("QUANTITY", "TOLERANCE", qb, qc, qc - qb, "rounding")
    ratio = qc / qb if qb else float("inf")
    if any(abs(ratio - r) < 1e-9 for r in (10, 0.1, 100, 0.01, 1000, 0.001)):
        cause = f"quantity out by factor {ratio:g} (fat finger / missing zero)"
    elif c.get("quantity_basis") == "TOTAL" and abs(c["quantity"] - qb) < 1e-9:
        cause = "per-period vs total quantity confusion"
    else:
        cause = "different volume agreed or booked"
    return FieldResult("QUANTITY", "BREAK", qb, qc, qc - qb, cause)


def compare(b: pd.Series, c: pd.Series) -> list[FieldResult]:
    out: list[FieldResult] = []

    # counterparty (legal entity)
    if c["counterparty_code"] == b["counterparty_code"]:
        out.append(FieldResult("COUNTERPARTY", "EXACT", b["counterparty"], c["sender_name"]))
    else:
        cc = _CP.get(c["counterparty_code"])
        if cc is None:
            cause = "sender name not in counterparty alias table"
        elif cc.group == _CP[b["counterparty_code"]].group:
            cause = "same group, different legal entity booked"
        else:
            cause = "different counterparty group"
        out.append(FieldResult("COUNTERPARTY", "BREAK", b["counterparty"],
                               cc.legal_name if cc else c["sender_name"], None, cause))

    # trade date
    if b["trade_date"] == c["trade_date"]:
        out.append(FieldResult("TRADE_DATE", "EXACT", b["trade_date"], c["trade_date"]))
    else:
        n = len(pd.bdate_range(min(b["trade_date"], c["trade_date"]), max(b["trade_date"], c["trade_date"]))) - 1
        sign = "+" if c["trade_date"] > b["trade_date"] else "-"
        out.append(FieldResult("TRADE_DATE", "BREAK", b["trade_date"], c["trade_date"], f"{sign}{n} bd",
                               f"trade date off by {n} business day(s) (booked late / timezone cut-off)"))

    # direction
    if b["our_side"] == c["our_side_implied"]:
        out.append(FieldResult("DIRECTION", "EXACT", b["our_side"], c["our_side_implied"]))
    else:
        out.append(FieldResult("DIRECTION", "BREAK", b["our_side"], c["our_side_implied"], None,
                               "buy/sell reversed - both parties claim the same side"))

    # floating index
    if b["index"].upper() == c["index_norm"]:
        out.append(FieldResult("INDEX", "EXACT", b["index"], c["floating_index"]))
    else:
        out.append(FieldResult("INDEX", "BREAK", b["index"], c["floating_index"], None,
                               "different floating price source / benchmark"))

    # calculation period
    if b["period_start"] == c["period_start"] and b["period_end"] == c["period_end"]:
        out.append(FieldResult("PERIOD", "EXACT", f"{b['period_start']}..{b['period_end']}",
                               f"{c['period_start']}..{c['period_end']}"))
    else:
        shift = _months(b["period_start"], c["period_start"]) - 1
        cause = (f"strip shifted by {shift:+d} month(s)" if _months(b["period_start"], b["period_end"]) == c["n_months"]
                 else "different tenor / number of months")
        out.append(FieldResult("PERIOD", "BREAK", f"{b['period_start']}..{b['period_end']}",
                               f"{c['period_start']}..{c['period_end']}", f"{shift:+d}m", cause))

    out.append(_qty_check(b, c))
    out.append(_price_check(b, c))

    for f, bk, ck in (("CURRENCY", "currency", "currency"), ("UNIT", "unit", "unit")):
        same = str(b[bk]).upper() == str(c[ck]).upper()
        out.append(FieldResult(f, "EXACT" if same else "BREAK", b[bk], c[ck], None,
                               "" if same else f"{f.lower()} mismatch"))
    return out


def economic_score(b: pd.Series, c: pd.Series) -> int:
    tick = _TICK_BY_INDEX.get(b["index"].upper(), 0.01)
    checks = [
        b["counterparty_code"] == c["counterparty_code"],
        b["trade_date"] == c["trade_date"],
        b["index"].upper() == c["index_norm"],
        b["our_side"] == c["our_side_implied"],
        b["period_start"] == c["period_start"] and b["period_end"] == c["period_end"],
        abs(b["quantity_per_period"] - c["quantity_per_period"]) <= C.QUANTITY_REL_TOLERANCE * b["quantity_per_period"],
        abs(b["fixed_price"] - c["fixed_price"]) <= C.PRICE_TOLERANCE_TICKS * tick + 1e-9,
    ]
    return sum(checks)


# --------------------------------------------------------------------------- matching
@dataclass
class MatchOutput:
    pairs: list = field(default_factory=list)          # (trade_id, conf_id, method, score)
    duplicates: list = field(default_factory=list)     # conf rows
    unmatched_confs: list = field(default_factory=list)
    unmatched_trades: list = field(default_factory=list)


def match(bookings: pd.DataFrame, confs_raw: pd.DataFrame) -> tuple[MatchOutput, pd.DataFrame]:
    confs = normalise_confirmations(confs_raw).sort_values(["received_date", "conf_id"]).reset_index(drop=True)
    out = MatchOutput()

    dup_mask = confs.duplicated("conf_id", keep="first")
    out.duplicates = confs[dup_mask].to_dict("records")
    confs = confs[~dup_mask].set_index("conf_id", drop=False)
    books = bookings.set_index("trade_id", drop=False)

    matched_trades: set = set()
    pending: list[str] = []

    # pass 1: by reference
    for cid, c in confs.iterrows():
        ref = c["our_trade_ref"]
        if isinstance(ref, str) and ref in books.index and ref not in matched_trades:
            out.pairs.append((ref, cid, "REFERENCE", None))
            matched_trades.add(ref)
        else:
            pending.append(cid)

    # pass 2: by economics (greedy on best score, unique best only)
    open_trades = books[~books.index.isin(matched_trades)]
    candidates = []
    for cid in pending:
        c = confs.loc[cid]
        # blocking keys: a confirmation can only pair with a trade in the same unit and currency
        pool = open_trades[(open_trades["unit"] == c["unit"]) & (open_trades["currency"] == c["currency"])]
        scores = sorted(((economic_score(b, c), tid) for tid, b in pool.iterrows()), reverse=True)
        if not scores or scores[0][0] < C.ECONOMIC_MATCH_MIN_SCORE:
            continue
        if len(scores) > 1 and scores[1][0] == scores[0][0]:
            continue  # ambiguous - leave for a human
        candidates.append((scores[0][0], cid, scores[0][1]))
    used_confs: set = set()
    for score, cid, tid in sorted(candidates, reverse=True):
        if tid in matched_trades or cid in used_confs:
            continue
        out.pairs.append((tid, cid, "ECONOMIC", score))
        matched_trades.add(tid)
        used_confs.add(cid)

    out.unmatched_confs = [cid for cid in pending if cid not in used_confs]
    out.unmatched_trades = [t for t in books.index if t not in matched_trades]
    return out, confs

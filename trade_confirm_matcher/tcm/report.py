"""Build the per-trade match results, the exceptions report, match-rate summaries,
a validation against the injected ground truth, and an HTML report."""
from __future__ import annotations

import html
from datetime import date

import pandas as pd

from . import config as C
from .matcher import MatchOutput, compare

BDAY = pd.offsets.BDay()

ACTIONS = {
    "PRICE": "Check fixed price against deal ticket / voice log. If our booking is wrong, request front-office amendment; otherwise dispute the confirmation.",
    "QUANTITY": "Check volume against deal ticket. Amend booking or dispute confirmation; check quantity basis (per month vs total).",
    "TRADE_DATE": "Verify execution timestamp; check for late booking or timezone cut-off.",
    "PERIOD": "Confirm contract months / calculation periods with the trader.",
    "COUNTERPARTY": "Confirm legal entity with trader and client onboarding; rebook against correct entity (credit line and ISDA differ by entity).",
    "DIRECTION": "Escalate immediately to trader and desk head - risk position is reversed.",
    "INDEX": "Confirm floating price source with trader; benchmark mismatch changes settlement amounts.",
    "CURRENCY": "Confirm settlement currency with trader.",
    "UNIT": "Confirm quantity unit; check conversion.",
    "MISSING_CONFIRMATION": "Chase counterparty for confirmation.",
    "UNKNOWN_TRADE": "Query the desk for an unbooked trade; if not ours, reject the confirmation.",
    "DUPLICATE_CONFIRMATION": "Suppress duplicate; no economic action needed.",
}
ALWAYS_HIGH = {"DIRECTION", "COUNTERPARTY", "UNKNOWN_TRADE"}


def _months(s, e):
    return (e.year - s.year) * 12 + e.month - s.month + 1


def _usd_notional(b) -> float:
    return b["fixed_price"] * b["quantity_per_period"] * _months(b["period_start"], b["period_end"]) * C.FX_TO_USD[b["currency"]]


def _impact(break_type: str, b, fr) -> float | None:
    """Cash difference over the life of the trade, for breaks where that is measurable."""
    n = _months(b["period_start"], b["period_end"])
    fx = C.FX_TO_USD[b["currency"]]
    if break_type == "PRICE":
        return abs(fr.difference) * b["quantity_per_period"] * n * fx
    if break_type == "QUANTITY":
        return abs(fr.difference) * n * b["fixed_price"] * fx
    return None


ALWAYS_MEDIUM = {"INDEX", "PERIOD", "TRADE_DATE", "CURRENCY", "UNIT"}


def _priority(break_type: str, impact: float | None, overdue: bool = False) -> str:
    """HIGH: wrong risk position, wrong legal entity, unbooked trade, overdue confirmation,
    or a price/quantity difference worth more than HIGH_IMPACT_USD."""
    if break_type in ALWAYS_HIGH or overdue:
        return "HIGH"
    if break_type in ALWAYS_MEDIUM:
        return "MEDIUM"
    if impact is None:
        return "LOW"
    if impact >= C.HIGH_IMPACT_USD:
        return "HIGH"
    return "MEDIUM" if impact >= C.MEDIUM_IMPACT_USD else "LOW"


def build(bookings: pd.DataFrame, confs: pd.DataFrame, m: MatchOutput, as_of: date):
    books = bookings.set_index("trade_id", drop=False)
    results, exceptions = [], []

    def add_exc(**kw):
        kw["suggested_action"] = ACTIONS.get(kw["break_type"], "")
        exceptions.append(kw)

    for tid, cid, method, score in m.pairs:
        b, c = books.loc[tid], confs.loc[cid]
        frs = compare(b, c)
        brks = [f for f in frs if f.status == "BREAK"]
        tols = [f for f in frs if f.status == "TOLERANCE"]
        status = "BREAK" if brks else ("MATCHED_WITHIN_TOLERANCE" if tols else "MATCHED_EXACT")
        results.append(dict(trade_id=tid, conf_id=cid, counterparty=b["counterparty"], product=b["product"],
                            status=status, match_method=method, economic_score=score,
                            break_types="|".join(sorted(f.field for f in brks)),
                            tolerance_fields="|".join(f.field for f in tols),
                            name_resolution=c["name_resolution"]))
        for f in brks:
            imp = _impact(f.field, b, f)
            add_exc(notional_usd=round(_usd_notional(b)), category="FIELD_BREAK", break_type=f.field, likely_cause=f.likely_cause, trade_id=tid,
                    conf_id=cid, counterparty=b["counterparty"], product=b["product"],
                    booked_value=f.booked, confirmed_value=f.confirmed, difference=f.difference,
                    impact_usd=None if imp is None else round(imp, 2), priority=_priority(f.field, imp), match_method=method,
                    age_bdays=None)

    for tid in m.unmatched_trades:
        b = books.loc[tid]
        age = len(pd.bdate_range(b["trade_date"], as_of)) - 1
        overdue = age > C.CONFIRM_SLA_BDAYS
        results.append(dict(trade_id=tid, conf_id=None, counterparty=b["counterparty"], product=b["product"],
                            status="UNCONFIRMED", match_method=None, economic_score=None,
                            break_types="MISSING_CONFIRMATION", tolerance_fields="", name_resolution=None))
        add_exc(notional_usd=round(_usd_notional(b)), category="MISSING_CONFIRMATION", break_type="MISSING_CONFIRMATION",
                likely_cause=f"no confirmation after {age} business day(s)" + (" - overdue" if overdue else ""),
                trade_id=tid, conf_id=None, counterparty=b["counterparty"], product=b["product"],
                booked_value=None, confirmed_value=None, difference=None,
                impact_usd=None, priority=_priority("MISSING_CONFIRMATION", None, overdue),
                match_method=None, age_bdays=age)

    for cid in m.unmatched_confs:
        c = confs.loc[cid]
        add_exc(notional_usd=None, category="UNKNOWN_TRADE", break_type="UNKNOWN_TRADE",
                likely_cause="confirmation with no matching booking" + (
                    f" (quotes unknown ref {c['our_trade_ref']})" if isinstance(c["our_trade_ref"], str) else ""),
                trade_id=None, conf_id=cid, counterparty=c["sender_name"], product=c["floating_index"],
                booked_value=None, confirmed_value=f"{c['cpty_side']} {c['quantity']:,} {c['unit']} @ {c['fixed_price']}",
                difference=None, impact_usd=None, priority="HIGH", match_method=None,
                age_bdays=len(pd.bdate_range(c["received_date"], as_of)) - 1)

    for d in m.duplicates:
        add_exc(notional_usd=None, category="DUPLICATE_CONFIRMATION", break_type="DUPLICATE_CONFIRMATION",
                likely_cause="same confirmation reference received more than once",
                trade_id=d["our_trade_ref"], conf_id=d["conf_id"], counterparty=d["sender_name"],
                product=d["floating_index"], booked_value=None, confirmed_value=None, difference=None,
                impact_usd=None, priority="LOW", match_method=None, age_bdays=None)

    res = pd.DataFrame(results).sort_values("trade_id").reset_index(drop=True)
    exc = pd.DataFrame(exceptions)
    prio_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    exc = exc.assign(_p=exc["priority"].map(prio_order)).sort_values(
        ["_p", "impact_usd", "notional_usd"], ascending=[True, False, False], na_position="last").drop(columns="_p").reset_index(drop=True)
    exc.insert(0, "exception_id", [f"EXC-{i:05d}" for i in range(1, len(exc) + 1)])
    return res, exc


# --------------------------------------------------------------------------- summaries
def _rates(g: pd.DataFrame) -> pd.Series:
    n = len(g)
    s = g["status"].value_counts()
    clean = s.get("MATCHED_EXACT", 0) + s.get("MATCHED_WITHIN_TOLERANCE", 0)
    received = n - s.get("UNCONFIRMED", 0)
    return pd.Series(dtype=object, data={
        "trades": n,
        "confirmations_received": received,
        "matched_exact": s.get("MATCHED_EXACT", 0),
        "matched_within_tolerance": s.get("MATCHED_WITHIN_TOLERANCE", 0),
        "breaks": s.get("BREAK", 0),
        "unconfirmed": s.get("UNCONFIRMED", 0),
        "receipt_rate_%": round(100 * received / n, 1),
        "match_rate_%": round(100 * clean / n, 1),
        "match_rate_of_received_%": round(100 * clean / received, 1) if received else 0.0,
    })


def summarise(res: pd.DataFrame, exc: pd.DataFrame, confs_total: int) -> dict:
    overall = _rates(res)
    by_cpty = res.groupby("counterparty").apply(_rates, include_groups=False).sort_values("match_rate_%")
    by_product = res.groupby("product").apply(_rates, include_groups=False).sort_values("match_rate_%")
    by_type = (exc.groupby(["category", "break_type"])
               .agg(count=("exception_id", "size"), impact_usd=("impact_usd", lambda x: x.sum(min_count=1)),
             notional_usd=("notional_usd", lambda x: x.sum(min_count=1)),
                    high_priority=("priority", lambda s: (s == "HIGH").sum()))
               .sort_values("count", ascending=False).reset_index())
    by_cause = (exc[exc["category"] == "FIELD_BREAK"]
                .assign(cause=lambda d: d["likely_cause"].str.replace(r"\d+(\.\d+)?", "N", regex=True))
                .groupby(["break_type", "cause"]).size().rename("count").reset_index()
                .sort_values(["break_type", "count"], ascending=[True, False]))
    methods = res["match_method"].value_counts().to_dict()
    return dict(overall=overall, by_counterparty=by_cpty, by_product=by_product,
                by_type=by_type, by_cause=by_cause, methods=methods, confirmations_total=confs_total)


def validate(res: pd.DataFrame, exc: pd.DataFrame, truth: pd.DataFrame) -> pd.DataFrame:
    """Per break type: how many were injected, how many detected, precision and recall.
    Field breaks and missing confirmations are scored per trade, unknown and duplicate
    confirmations per confirmation."""
    inj = set()
    for r in truth.itertuples():
        for t in str(r.injected).split("|"):
            if t == "NONE":
                continue
            key = r.conf_id if t in ("UNKNOWN_TRADE", "DUPLICATE_CONFIRMATION") else r.trade_id
            inj.add((t, key))
    det = set()
    for r in exc.itertuples():
        key = r.conf_id if r.break_type in ("UNKNOWN_TRADE", "DUPLICATE_CONFIRMATION") else r.trade_id
        det.add((r.break_type, key))
    rows = []
    for t in sorted({t for t, _ in inj | det}):
        i = {k for tt, k in inj if tt == t}
        d = {k for tt, k in det if tt == t}
        tp = len(i & d)
        rows.append(dict(break_type=t, injected=len(i), detected=len(d), true_positive=tp,
                         precision_pct=round(100 * tp / len(d), 1) if d else None,
                         recall_pct=round(100 * tp / len(i), 1) if i else None))
    # trades with no injected break that were reported clean
    clean_truth = set(truth.loc[truth["injected"] == "NONE", "trade_id"])
    reported_clean = set(res.loc[res["status"].str.startswith("MATCHED"), "trade_id"])
    tp = len(clean_truth & reported_clean)
    rows.append(dict(break_type="(clean trade)", injected=len(clean_truth), detected=len(reported_clean),
                     true_positive=tp, precision_pct=round(100 * tp / len(reported_clean), 1),
                     recall_pct=round(100 * tp / len(clean_truth), 1)))
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- HTML
CSS = """
body{font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;margin:32px auto;max-width:1400px;color:#1d2433;padding:0 20px}
h1{font-size:24px;margin-bottom:4px}h2{font-size:17px;margin-top:36px;border-bottom:1px solid #d9dde5;padding-bottom:6px}
.sub{color:#5b6475;margin-top:0}.kpis{display:flex;gap:14px;flex-wrap:wrap;margin:22px 0}
.kpi{border:1px solid #d9dde5;border-radius:8px;padding:12px 16px;min-width:150px}
.kpi .v{font-size:24px;font-weight:600}.kpi .l{font-size:12px;color:#5b6475}
table{border-collapse:collapse;font-size:12.5px;width:100%;margin-top:8px}
th{background:#f3f5f8;text-align:left;font-weight:600}th,td{padding:5px 8px;border-bottom:1px solid #e8ebf0;vertical-align:top}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.HIGH{color:#b42318;font-weight:600}.MEDIUM{color:#b54708}.LOW{color:#5b6475}
.note{font-size:12.5px;color:#5b6475}.nw{white-space:nowrap}.wide{min-width:220px}
"""


def _table(df: pd.DataFrame, money=(), pct=()) -> str:
    cols = list(df.columns)
    h = "<table><tr>" + "".join(f"<th>{html.escape(str(c))}</th>" for c in cols) + "</tr>"
    for _, r in df.iterrows():
        h += "<tr>"
        for c in cols:
            v = r[c]
            cls = ""
            if v is None or (isinstance(v, float) and pd.isna(v)):
                s = ""
            elif c in money:
                s, cls = f"{v:,.0f}", "num"
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                s, cls = (f"{v:,.1f}" if c in pct else f"{v:,}" if float(v).is_integer() else f"{v:,.4g}"), "num"
            else:
                s = html.escape(str(v))
            if c == "priority":
                cls = str(v)
            if c not in ("likely_cause", "cause", "counterparty", "suggested_action", "booked_value", "confirmed_value"):
                cls += " nw"
            elif c == "likely_cause":
                cls += " wide"
            h += f'<td class="{cls}">{s}</td>'
        h += "</tr>"
    return h + "</table>"


def to_html(summary: dict, exc: pd.DataFrame, validation: pd.DataFrame, as_of: date, params: dict) -> str:
    o = summary["overall"]
    kpis = [
        ("Trades booked", f"{o['trades']:,}"),
        ("Confirmation messages", f"{summary['confirmations_total']:,}"),
        ("Match rate (all trades)", f"{o['match_rate_%']:.1f}%"),
        ("Match rate (confirmed trades)", f"{o['match_rate_of_received_%']:.1f}%"),
        ("Trades with breaks", f"{o['breaks']:,}"),
        ("Unconfirmed trades", f"{o['unconfirmed']:,}"),
        ("Open exceptions", f"{len(exc):,}"),
        ("High priority", f"{(exc['priority'] == 'HIGH').sum():,}"),
    ]
    k = "".join(f'<div class="kpi"><div class="v">{v}</div><div class="l">{l}</div></div>' for l, v in kpis)
    pct = {c for c in summary["by_counterparty"].columns if c.endswith("%")}
    m = summary["methods"]
    exc_cols = ["exception_id", "priority", "break_type", "likely_cause", "trade_id",
                "counterparty", "product", "booked_value", "confirmed_value", "difference", "impact_usd", "notional_usd",
                "age_bdays"]
    actions = pd.DataFrame([{"break_type": k, "suggested_action": v} for k, v in ACTIONS.items()])
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Confirmation matching - exceptions report</title>
<style>{CSS}</style></head><body>
<h1>Commodity swap confirmation matching - exceptions report</h1>
<p class="sub">As of {as_of:%d %b %Y} &middot; synthetic data (seed {params['seed']}, {params['n_trades']} trades, injected break rate {params['break_rate']:.0%})</p>
<div class="kpis">{k}</div>
<p class="note">Match rate = trades whose confirmation agrees on every field, exactly or within tolerance
(price within {C.PRICE_TOLERANCE_TICKS:g} tick; quantity compared per month; dates exact).
Matched by reference: {m.get('REFERENCE', 0)}; matched on economics (no usable reference): {m.get('ECONOMIC', 0)}.</p>

<h2>Exceptions by type</h2>{_table(summary['by_type'], money={'impact_usd', 'notional_usd'})}
<h2>Field breaks by likely cause</h2>{_table(summary['by_cause'])}
<h2>Match rates by counterparty</h2>{_table(summary['by_counterparty'].reset_index(), pct=pct)}
<h2>Match rates by product</h2>{_table(summary['by_product'].reset_index(), pct=pct)}
<h2>Suggested actions</h2>{_table(actions)}
<h2>Exceptions queue (priority, then impact)</h2>
<p class="note">impact_usd = cash difference over the life of the trade (price and quantity breaks only);
notional_usd = booked notional of the trade. All USD (EUR at {C.FX_TO_USD['EUR']}).
Priority: HIGH for direction, counterparty, unknown trades, overdue confirmations and price/quantity breaks
&ge; ${C.HIGH_IMPACT_USD:,}; MEDIUM for index, period, date, currency and unit breaks or price/quantity &ge; ${C.MEDIUM_IMPACT_USD:,}.</p>
{_table(exc[exc_cols], money={'impact_usd', 'notional_usd'})}
<p class="note">Full detail, including confirmation IDs and actions, is in exceptions_report.csv.</p>
<h2>Validation against injected breaks</h2>
<p class="note">The matcher never sees the ground truth; this table checks its output against what the generator injected.</p>
{_table(validation, pct={'precision_pct', 'recall_pct'})}
</body></html>"""

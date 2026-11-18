"""Synthetic commodity swap bookings, counterparty confirmations and injected breaks.

The generator writes three tables:
  bookings       - our internal booking of each fixed-for-floating swap
  confirmations  - what each counterparty sends back (their wording, their perspective)
  ground_truth   - which breaks were deliberately injected (never read by the matcher)

Confirmations also carry harmless noise that must NOT be reported as a break:
name aliases, prices rounded to tick, total rather than per-month quantity,
and a missing trade reference (forcing a match on economics).
"""
from __future__ import annotations

import random
from datetime import date

import pandas as pd

from . import config as C

BREAK_WEIGHTS = {
    "PRICE": 0.20, "QUANTITY": 0.15, "TRADE_DATE": 0.10, "PERIOD": 0.10,
    "COUNTERPARTY": 0.10, "DIRECTION": 0.08, "INDEX": 0.07,
    "MISSING_CONFIRMATION": 0.15, "MULTI": 0.05,
}
FIELD_BREAKS = ["PRICE", "QUANTITY", "TRADE_DATE", "PERIOD", "COUNTERPARTY", "DIRECTION", "INDEX"]

BDAY = pd.offsets.BDay()


def _decimals(tick: float) -> int:
    s = f"{tick:.10f}".rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0


def _to_tick(x: float, tick: float) -> float:
    return round(round(x / tick) * tick, _decimals(tick))


def _months_between(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + end.month - start.month + 1


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def _month_end(d: date) -> date:
    return (pd.Timestamp(d) + pd.offsets.MonthEnd(0)).date()


def _transpose_digits(price: float, decimals: int, rng: random.Random) -> float | None:
    """Swap two adjacent, different digits: the classic fat-finger price error."""
    s = f"{price:.{decimals}f}"
    idx = [i for i in range(len(s) - 1)
           if s[i].isdigit() and s[i + 1].isdigit() and s[i] != s[i + 1]]
    if not idx:
        return None
    i = rng.choice(idx)
    t = s[:i] + s[i + 1] + s[i] + s[i + 2:]
    return float(t)


def generate(n_trades: int = 500, break_rate: float = 0.25, as_of: date | None = None,
             seed: int = 42, unknown_rate: float = 0.02, duplicate_rate: float = 0.03):
    rng = random.Random(seed)
    as_of = as_of or date.today()
    cp_by_code = {c.code: c for c in C.COUNTERPARTIES}
    trade_window = pd.bdate_range(end=pd.Timestamp(as_of) - BDAY, periods=10)

    bookings, confs, truth = [], [], []

    for i in range(1, n_trades + 1):
        product = rng.choice(list(C.PRODUCTS))
        p = C.PRODUCTS[product]
        cp = rng.choice(C.COUNTERPARTIES)
        trade_date = rng.choice(trade_window).date()
        start = _add_months(trade_date, rng.randint(1, 6))
        tenor = rng.choice([1, 3, 3, 6, 12])
        end = _month_end(_add_months(start, tenor - 1))
        dec = _decimals(p["tick"])
        price = _to_tick(rng.uniform(*p["price"]), p["tick"])
        if rng.random() < 0.3:  # averaged fills: booked off-tick with an extra decimal
            price = round(price + rng.uniform(-0.45, 0.45) * p["tick"], dec + 1)
        qty = rng.choice(p["qty"])
        side = rng.choice(["PAY_FIXED", "RECEIVE_FIXED"])
        trade_id = f"TRD-{100000 + i}"

        bookings.append(dict(
            trade_id=trade_id, trade_date=trade_date, counterparty_code=cp.code,
            counterparty=cp.legal_name, product=product, index=p["index"], our_side=side,
            fixed_price=price, quantity_per_period=qty, unit=p["unit"], currency=p["ccy"],
            period_start=start, period_end=end, frequency="MONTHLY",
            booked_by=rng.choice(["jsmith", "aokafor", "lchen", "mrossi"]),
        ))

        # ------------------------------------------------ choose scenario
        injected: list[str] = []
        if rng.random() < break_rate:
            kind = rng.choices(list(BREAK_WEIGHTS), weights=BREAK_WEIGHTS.values())[0]
            if kind == "MULTI":
                injected = rng.sample(FIELD_BREAKS, 2)
            else:
                injected = [kind]
        if injected == ["MISSING_CONFIRMATION"]:
            truth.append(dict(trade_id=trade_id, conf_id=None, injected="MISSING_CONFIRMATION"))
            continue

        # ------------------------------------------------ build the confirmation
        sender = cp
        c_trade_date, c_start, c_end = trade_date, start, end
        c_price = _to_tick(price, p["tick"])             # benign: confirmed at tick precision
        c_qty = qty
        c_index = p["index"]
        cp_side = "RECEIVE_FIXED" if side == "PAY_FIXED" else "PAY_FIXED"  # mirror image

        for b in list(injected):
            if b == "PRICE":
                t = _transpose_digits(c_price, dec, rng) if rng.random() < 0.4 else None
                if t is None or abs(t - c_price) <= C.PRICE_TOLERANCE_TICKS * p["tick"]:
                    t = c_price + rng.choice([-1, 1]) * rng.randint(5, 200) * p["tick"]
                c_price = round(t, dec)
            elif b == "QUANTITY":
                if rng.random() < 0.4:
                    c_qty = qty * 10 if rng.random() < 0.5 else qty / 10   # fat finger
                else:
                    others = [q for q in p["qty"] if q != qty]
                    c_qty = rng.choice(others)
            elif b == "TRADE_DATE":
                c_trade_date = (pd.Timestamp(trade_date) + rng.choice([-1, 1]) * BDAY).date()
            elif b == "PERIOD":
                shift = rng.choice([-1, 1])
                c_start = _add_months(start, shift)
                c_end = _month_end(_add_months(c_start, tenor - 1))
            elif b == "COUNTERPARTY":
                siblings = [c for c in C.COUNTERPARTIES if c.group == cp.group and c.code != cp.code]
                pool = siblings or [c for c in C.COUNTERPARTIES if c.code != cp.code]
                sender = rng.choice(pool)
            elif b == "DIRECTION":
                cp_side = side  # counterparty claims the same side as us
            elif b == "INDEX":
                alts = [q for k, q in C.PRODUCTS.items()
                        if q["family"] == p["family"] and q["unit"] == p["unit"] and k != product]
                if alts:
                    c_index = rng.choice(alts)["index"]
                else:  # no plausible confusion for this product: use a price break instead
                    injected[injected.index("INDEX")] = "PRICE"
                    c_price = round(c_price + rng.randint(5, 200) * p["tick"], dec)

        # benign noise
        sender_name = rng.choice(sender.aliases) if rng.random() < 0.6 else sender.legal_name
        n_months = _months_between(c_start, c_end)
        if rng.random() < 0.3:
            qty_basis, qty_out = "TOTAL", c_qty * n_months
        else:
            qty_basis, qty_out = "PER_PERIOD", c_qty
        our_ref = trade_id if rng.random() < 0.85 else None
        conf_id = f"CNF-{sender.code}-{rng.randint(10_000_000, 99_999_999)}"
        received = min(pd.Timestamp(trade_date) + rng.randint(0, 2) * BDAY, pd.Timestamp(as_of)).date()

        conf = dict(
            conf_id=conf_id, received_date=received, sender_name=sender_name, our_trade_ref=our_ref,
            trade_date=c_trade_date, cpty_side=cp_side, floating_index=c_index,
            fixed_price=c_price, quantity=qty_out, quantity_basis=qty_basis,
            unit=p["unit"].lower() if rng.random() < 0.3 else p["unit"], currency=p["ccy"],
            period_start=c_start, period_end=c_end,
        )
        confs.append(conf)
        truth.append(dict(trade_id=trade_id, conf_id=conf_id,
                          injected="|".join(sorted(injected)) if injected else "NONE"))

        if rng.random() < duplicate_rate:          # counterparty re-sends the same confirmation
            dup = dict(conf, received_date=min(pd.Timestamp(received) + BDAY, pd.Timestamp(as_of)).date())
            confs.append(dup)
            truth.append(dict(trade_id=trade_id, conf_id=conf_id, injected="DUPLICATE_CONFIRMATION"))

    # confirmations for trades we never booked
    for _ in range(max(1, round(n_trades * unknown_rate))):
        product = rng.choice(list(C.PRODUCTS))
        p = C.PRODUCTS[product]
        cp = rng.choice(C.COUNTERPARTIES)
        td = rng.choice(trade_window).date()
        start = _add_months(td, rng.randint(1, 6))
        conf_id = f"CNF-{cp.code}-{rng.randint(10_000_000, 99_999_999)}"
        confs.append(dict(
            conf_id=conf_id, received_date=td, sender_name=cp.legal_name, our_trade_ref=None,
            trade_date=td, cpty_side=rng.choice(["PAY_FIXED", "RECEIVE_FIXED"]),
            floating_index=p["index"], fixed_price=_to_tick(rng.uniform(*p["price"]), p["tick"]),
            quantity=rng.choice(p["qty"]), quantity_basis="PER_PERIOD", unit=p["unit"],
            currency=p["ccy"], period_start=start, period_end=_month_end(_add_months(start, 2)),
        ))
        truth.append(dict(trade_id=None, conf_id=conf_id, injected="UNKNOWN_TRADE"))

    rng.shuffle(confs)  # confirmations arrive in no particular order
    return pd.DataFrame(bookings), pd.DataFrame(confs), pd.DataFrame(truth)

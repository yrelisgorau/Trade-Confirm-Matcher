# Commodity swap confirmation matcher

Affirms the economics of booked commodity swaps against incoming counterparty
confirmations, classifies every discrepancy by type and likely cause, and produces
a prioritised exceptions queue with match rates. It runs end to end on synthetic
data with known injected breaks, so the matcher's accuracy can be measured.

```
python run.py --as-of 2026-11-18                 # 500 trades, 25% break rate
python run.py --trades 3000 --break-rate 0.5 --seed 7 --out stress
python -m pytest -q
```
Needs Python 3.10+ and pandas.

## Layout
| File | Purpose |
|---|---|
| `tcm/config.py` | Reference data (fictional counterparties and aliases, products, ticks) and every tolerance rule |
| `tcm/generate.py` | Synthetic bookings and confirmations, harmless noise, injected breaks, ground truth |
| `tcm/matcher.py` | Normalisation, de-duplication, two-pass matching, field comparison and cause classification |
| `tcm/report.py` | Per-trade results, exceptions queue, match rates, validation, HTML report |
| `run.py` | Command line entry point |
| `tests/` | Unit tests for each rule plus end-to-end recall checks over several seeds |

## The data
**Bookings** are fixed-for-floating monthly swaps on ICE Brent, NYMEX WTI, Platts Dubai,
ICE Gasoil, Henry Hub, TTF (EUR) and LME Copper. Each one has a trade date, legal entity,
side (pay/receive fixed), fixed price, quantity per month, unit, currency and calculation period.

**Confirmations** are written from the counterparty's side, as a real one would be. They
include harmless differences that must not be reported as breaks:
- the counterparty's own spelling of its name (`ATLAS PETROLEUM TRADING PTE. LTD.`)
- the counterparty's side, which is the mirror of ours
- price rounded to the product tick when we booked an averaged price
- total quantity rather than quantity per month (30%)
- lower-case units
- no trade reference quoted (15%), so the confirmation has to be matched on its economics

**Injected breaks:** price (shifted by ticks, or digits transposed), quantity (out by a
factor of 10 or a different lot size), trade date (±1 business day), period (strip shifted
by a month), counterparty (often a sibling legal entity in the same group), direction,
floating index, two breaks at once, missing confirmation, duplicate confirmation, and a
confirmation for a trade we never booked.

## Matching
1. **Normalise.** The sender's name is resolved to a counterparty code through an alias
   table, with a fuzzy fallback. The counterparty's side is mirrored, total quantity is
   converted to quantity per month, and units and indices are upper-cased.
2. **De-duplicate** by confirmation reference.
3. **Pass 1, by reference:** our trade ID as quoted on the confirmation.
4. **Pass 2, by economics:** unit and currency are blocking keys. Each remaining booking is
   scored on 7 fields (entity, trade date, index, side, period, quantity, price) and the best
   *unique* candidate is accepted if it scores at least 5. Ties go to a person to resolve.
5. **Compare** each field and label it EXACT, TOLERANCE or BREAK.

| Field | Rule |
|---|---|
| Price | within 1 tick = tolerance match |
| Quantity | compared per month; relative tolerance 1e-6 |
| Trade date, period | exact |
| Counterparty | same legal entity (code), not just the same group |
| Side, index, currency, unit | exact after normalisation |

Each break also gets a **likely cause**: digit transposition, quantity out by a factor of 10,
per-month vs total confusion, a sibling legal entity in the same group, a strip shifted by
N months, a trade date off by N business days, and so on.

## Output (`output/`)
- `exceptions_report.html`: KPIs, exceptions by type and by cause, match rates by
  counterparty and product, the prioritised queue, suggested actions, and validation
- `exceptions_report.csv`: one row per exception, with the booked and confirmed values,
  difference, cash impact, notional, age and suggested action
- `match_results.csv`: one row per trade (MATCHED_EXACT / MATCHED_WITHIN_TOLERANCE / BREAK / UNCONFIRMED)
- `summary.json`, `validation.csv`, and the raw inputs plus ground truth

**Priority:** HIGH for a reversed side, wrong entity, unknown trade, a confirmation more than
2 business days overdue, or a price/quantity break worth at least $250k. MEDIUM for index,
period, date, currency and unit breaks, or a price/quantity break worth at least $25k. LOW otherwise.

## Results
Default run (500 trades, seed 42): match rate 79.2% of all trades and 81.6% of confirmed
trades. 397 confirmations matched by reference and 88 on economics. 129 exceptions, all
injected breaks found, no false alarms.

Stress run (3,000 trades, 50% break rate): every clean trade was still reported clean, and
recall was 97–100% for each field break type. The misses follow a pattern: a confirmation
with no reference *and* several breaks scores too low to pair. It then shows up as a missing
confirmation plus an unknown trade, which is the same pair of exceptions an operations team
would see and resolve by hand.

## Limitations
- Fixed-for-floating monthly swaps only; no options, basis swaps, or average-price conventions
- Calendars use weekends only, with no exchange holidays
- Impact uses a fixed EUR/USD rate and ignores discounting

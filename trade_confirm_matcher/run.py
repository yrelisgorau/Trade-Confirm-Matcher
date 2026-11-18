"""Generate synthetic data, match confirmations, and write the exceptions report.

    python run.py                          # 500 trades, 25% break rate, as of today
    python run.py --trades 2000 --break-rate 0.15 --seed 7 --as-of 2026-11-18
"""
import argparse
import json
from datetime import date
from pathlib import Path

from tcm.generate import generate
from tcm.matcher import match
from tcm.report import build, summarise, to_html, validate


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trades", type=int, default=500)
    ap.add_argument("--break-rate", type=float, default=0.25)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    ap.add_argument("--out", type=Path, default=Path("output"))
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)

    bookings, confs, truth = generate(a.trades, a.break_rate, a.as_of, a.seed)
    m, confs_norm = match(bookings, confs)
    results, exceptions = build(bookings, confs_norm, m, a.as_of)
    summary = summarise(results, exceptions, len(confs))
    validation = validate(results, exceptions, truth)

    bookings.to_csv(a.out / "bookings.csv", index=False)
    confs.to_csv(a.out / "confirmations.csv", index=False)
    truth.to_csv(a.out / "ground_truth.csv", index=False)
    results.to_csv(a.out / "match_results.csv", index=False)
    exceptions.to_csv(a.out / "exceptions_report.csv", index=False)
    validation.to_csv(a.out / "validation.csv", index=False)
    params = dict(seed=a.seed, n_trades=a.trades, break_rate=a.break_rate)
    (a.out / "exceptions_report.html").write_text(to_html(summary, exceptions, validation, a.as_of, params))
    o = summary["overall"]
    (a.out / "summary.json").write_text(json.dumps(
        {"as_of": str(a.as_of), **params, **{k: (float(v) if "%" in k else int(v)) for k, v in o.items()},
         "match_methods": summary["methods"], "open_exceptions": len(exceptions)}, indent=2))

    print(f"Trades {o['trades']}  | match rate {o['match_rate_%']}%  "
          f"(of confirmed {o['match_rate_of_received_%']}%)  | breaks {o['breaks']}  "
          f"| unconfirmed {o['unconfirmed']}  | exceptions {len(exceptions)}")
    print(validation.to_string(index=False))
    print(f"Written to {a.out.resolve()}")


if __name__ == "__main__":
    main()

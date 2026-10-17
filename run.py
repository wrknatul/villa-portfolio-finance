"""Usage: python3 run.py [--data data] [--out out]   (stdlib only, Python 3.10+)"""
import argparse
from collections import Counter
from pathlib import Path

from bse import pipeline

ROOT = Path(__file__).parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=ROOT / "data")
    ap.add_argument("--out", type=Path, default=ROOT / "out")
    args = ap.parse_args()

    result = pipeline.run(args.data, args.out / "finance.db", args.out)
    as_of = max(p["payment_date"] for p in result["payments"])
    pipeline.export_dashboard(result, ROOT / "bse" / "dashboard_template.html", args.out / "dashboard.html", as_of)

    stats = result["import_stats"]
    print(f"import run #{stats['run_id']}: inserted={stats['inserted']} updated={stats['updated']} "
          f"unchanged={stats['unchanged']} missing={stats['missing']}")
    by_action = Counter(i.action for i in result["issues"])
    print("data quality:", dict(by_action))
    for rule, n in sorted(Counter((i.rule, i.action) for i in result["issues"]).items()):
        print(f"  {rule[0]:38} {rule[1]:8} {n}")
    print(f"dashboard: {args.out / 'dashboard.html'}")


if __name__ == "__main__":
    main()

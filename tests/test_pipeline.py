"""Run: python3 -m unittest discover -s tests -v   (from the project root)"""
import csv
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from bse import pipeline

ROOT = Path(__file__).resolve().parent.parent
FILES = {
    "properties.csv": ["property_id", "property_name", "cluster", "owner_id", "base_currency"],
    "commission_rules.csv": ["property_id", "effective_from", "effective_to", "management_fee_rate", "fee_basis", "note"],
    "reservations.csv": ["reservation_id", "property_id", "check_in", "check_out", "status", "gross_booking_value", "currency",
                         "ota_fee", "refund_amount", "channel", "updated_at"],
    "payments.csv": ["payment_id", "reservation_id", "property_id", "payment_date", "amount", "currency", "status"],
    "expenses.csv": ["expense_id", "property_id", "expense_date", "category", "amount", "currency", "payment_status"],
}


def res(rid, ci, co, gross=1000, ota=150, status="confirmed", prop="V001", refund=0, cur="IDR", channel="OTA"):
    return [rid, prop, ci, co, status, gross, cur, ota, refund, channel, "2026-09-15"]


def write_data(folder: Path, **tables):
    base = {
        "properties.csv": [["V001", "Villa 01", "Ubud", "O1", "IDR"]],
        "commission_rules.csv": [["V001", "2026-09-01", "2026-12-31", "0.17", "gross_booking_value", ""]],
        "reservations.csv": [], "payments.csv": [], "expenses.csv": [],
    }
    base.update({k + ".csv": v for k, v in tables.items()})
    for name, header in FILES.items():
        with open(folder / name, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            w.writerows(base[name])


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.addCleanup(shutil.rmtree, self.tmp)

    def run_pipeline(self):
        return pipeline.run(self.data, self.tmp / "db.sqlite", self.tmp / "out")

    def rules(self, result):
        return {(i.rule, i.entity_id) for i in result["issues"]}


class LogicTests(Case):
    def test_mgmt_fee_and_balance(self):
        write_data(self.data, reservations=[res("R0001", "2026-09-01", "2026-09-03", gross=1_000_001, ota=150_000)],
                   payments=[["P0001", "R0001", "V001", "2026-09-03", 850_001, "IDR", "received"]],
                   expenses=[["E0001", "V001", "2026-09-02", "Cleaning", 50_000, "IDR", "paid"]])
        r = self.run_pipeline()
        fact = r["reservations"][0]
        self.assertEqual(fact["mgmt_fee"], 170_000)  # round(1_000_001 * 0.17) = 170000.17 -> 170000
        self.assertEqual(fact["expected_payout"], 850_001)
        self.assertEqual(r["payments"][0]["mgmt_fee_cash"], 170_000)
        self.assertEqual(r["issues"], [])

    def test_exact_duplicate_dropped_once(self):
        row = res("R0001", "2026-09-01", "2026-09-03")
        write_data(self.data, reservations=[row, row])
        r = self.run_pipeline()
        self.assertEqual(len(r["reservations"]), 1)
        self.assertIn(("DQ01_exact_duplicate", "R0001"), self.rules(r))

    def test_cancelled_has_no_revenue(self):
        write_data(self.data, reservations=[res("R0001", "2026-09-01", "2026-09-03", status="cancelled", refund=1000)])
        fact = self.run_pipeline()["reservations"][0]
        self.assertEqual((fact["included"], fact["gross"], fact["ota_fee"], fact["mgmt_fee"]), (0, 0, 0, 0))
        self.assertEqual(fact["excluded_gross"], 1000)

    def test_overlap_flagged_turnover_not(self):
        write_data(self.data, reservations=[
            res("R0001", "2026-09-01", "2026-09-05"), res("R0002", "2026-09-05", "2026-09-08"),  # turnover day: fine
            res("R0003", "2026-09-07", "2026-09-10")])                                           # overlaps R0002
        got = {i.entity_id for i in self.run_pipeline()["issues"] if i.rule == "DQ08_overlapping_stays"}
        self.assertEqual(got, {"R0002+R0003"})

    def test_bad_records_are_held_not_counted(self):
        write_data(self.data, reservations=[
            res("R0001", "2026-09-01", "2026-09-03", prop="V999"),
            res("R0002", "2026-09-10", "2026-09-12", cur="USD"),
            res("R0003", "2026-09-14", "2026-09-16", refund=5000),
            res("R0004", "2026-09-20", "2026-09-22", ota=-100),
            res("R0005", "2026-09-25", "2026-09-26")])
        r = self.run_pipeline()
        included = {f["reservation_id"] for f in r["reservations"] if f["included"]}
        self.assertEqual(included, {"R0005"})
        self.assertEqual({i.rule for i in r["issues"] if i.action == "hold"},
                         {"DQ03_unknown_property", "DQ05_currency_mismatch", "DQ06_refund_exceeds_gross", "DQ06_negative_ota_fee"})

    def test_payment_of_held_reservation_is_not_cash(self):
        write_data(self.data, reservations=[res("R0001", "2026-09-01", "2026-09-03", refund=5000)],
                   payments=[["P0001", "R0001", "V001", "2026-09-03", 850, "IDR", "received"]])
        pay = self.run_pipeline()["payments"][0]
        self.assertEqual((pay["included"], pay["amount"], pay["excluded_amount"]), (0, 0, 850))

    def test_orphan_payment_held(self):
        write_data(self.data, payments=[["P0001", "R0404", "V001", "2026-09-03", 850, "IDR", "received"]])
        self.assertIn(("DQ04_orphan_payment", "P0001"), self.rules(self.run_pipeline()))

    def test_overlapping_commission_periods_detected(self):
        write_data(self.data, commission_rules=[["V001", "2026-09-01", "2026-10-31", "0.17", "gross_booking_value", ""],
                                                ["V001", "2026-10-15", "2026-12-31", "0.20", "gross_booking_value", ""]])
        self.assertIn(("DQ14_rule_period_overlap", "V001"), self.rules(self.run_pipeline()))


class ReimportTests(Case):
    def test_reimport_is_idempotent_and_update_is_audited(self):
        write_data(self.data, reservations=[res("R0001", "2026-09-01", "2026-09-03")])
        first = self.run_pipeline()["import_stats"]
        second = self.run_pipeline()["import_stats"]
        self.assertEqual((first["inserted"], second["inserted"], second["updated"], second["unchanged"]), (3, 0, 0, 3))

        write_data(self.data, reservations=[res("R0001", "2026-09-01", "2026-09-03", gross=2000)])
        third = self.run_pipeline()["import_stats"]
        self.assertEqual((third["inserted"], third["updated"]), (0, 1))
        conn = sqlite3.connect(self.tmp / "db.sqlite")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM src_record WHERE entity='reservations'").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM fact_reservation").fetchone()[0], 1)
        changes = [c for (c,) in conn.execute("SELECT change FROM audit_log WHERE entity='reservations' ORDER BY id")]
        self.assertEqual(changes, ["insert", "update"])

    def test_row_removed_from_source_is_audited_not_deleted(self):
        write_data(self.data, reservations=[res("R0001", "2026-09-01", "2026-09-03")])
        self.run_pipeline()
        write_data(self.data, reservations=[])
        self.assertEqual(self.run_pipeline()["import_stats"]["missing"], 1)
        conn = sqlite3.connect(self.tmp / "db.sqlite")
        self.assertEqual(conn.execute("SELECT active FROM src_record WHERE key='R0001'").fetchone()[0], 0)


class RealDataTests(Case):
    """Independent reconciliation of headline numbers against the raw CSV."""

    def test_headline_numbers_match_raw_csv(self):
        r = pipeline.run(ROOT / "data", self.tmp / "db.sqlite", self.tmp / "out")
        with open(ROOT / "data" / "reservations.csv", encoding="utf-8-sig") as fh:
            raw = list(csv.DictReader(fh))
        seen, expect_gross, expect_ota = set(), 0, 0
        excluded = {"R0034", "R0061", "R0072", "R0102"}  # unknown property, USD, refund>gross, negative OTA fee
        for row in raw:
            rid = row["reservation_id"]
            if rid in seen or rid in excluded or row["status"] == "cancelled":
                continue
            seen.add(rid)
            expect_gross += int(row["gross_booking_value"])
            expect_ota += int(row["ota_fee"])
        facts = [f for f in r["reservations"] if f["included"]]
        self.assertEqual(sum(f["gross"] for f in facts), expect_gross)
        self.assertEqual(sum(f["ota_fee"] for f in facts), expect_ota)
        self.assertEqual(len(facts), len(seen))
        self.assertEqual(len([e for e in r["expenses"] if not e["included"]]), 1)  # V777
        self.assertEqual(len(r["payments"]), 75)  # P0075 duplicate dropped


if __name__ == "__main__":
    unittest.main()

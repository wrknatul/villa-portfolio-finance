"""Orchestration: ingest -> validate -> store -> calculate -> export."""
import csv
import json
from dataclasses import asdict
from pathlib import Path

from . import calc, ingest, quality, store

FACT_COLUMNS = {
    "fact_reservation": ["reservation_id", "property_id", "check_in", "check_out", "status", "channel", "included", "reason",
                         "gross", "refund", "ota_fee", "mgmt_rate", "mgmt_fee", "expected_payout", "excluded_gross", "flags", "paid"],
    "fact_payment": ["payment_id", "reservation_id", "property_id", "payment_date", "amount", "excluded_amount",
                     "included", "reason", "mgmt_fee_cash"],
    "fact_expense": ["expense_id", "property_id", "expense_date", "category", "amount", "excluded_amount", "paid", "included", "reason"],
    "dq_issue": ["rule", "entity", "entity_id", "action", "detail", "amount", "properties"],
}


def run(data_dir: Path, db_path: Path, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = ingest.read_all(data_dir)
    validated = quality.validate(raw)
    conn = store.connect(db_path)
    import_stats = store.import_rows(conn, validated.rows, source=str(data_dir))

    # calculations read what is in the database, not what was just parsed
    rows = {entity: store.load(conn, entity) for entity in ingest.ENTITIES}
    issues = list(validated.issues)
    reservations, payments, expenses = calc.build_facts(rows, validated.hold, issues)

    store.replace_derived(conn, "fact_reservation", reservations, FACT_COLUMNS["fact_reservation"])
    store.replace_derived(conn, "fact_payment", payments, FACT_COLUMNS["fact_payment"])
    store.replace_derived(conn, "fact_expense", expenses, FACT_COLUMNS["fact_expense"])
    store.replace_derived(conn, "dq_issue", [asdict(i) for i in issues], FACT_COLUMNS["dq_issue"])

    write_dq_report(out_dir / "dq_report.csv", issues)
    return dict(import_stats=import_stats, issues=issues, reservations=reservations, payments=payments,
                expenses=expenses, properties=rows["properties"], rows=rows)


def write_dq_report(path: Path, issues: list):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FACT_COLUMNS["dq_issue"])
        writer.writeheader()
        order = {"hold": 0, "exclude": 1, "flag": 2, "info": 3}
        for issue in sorted(issues, key=lambda i: (order[i.action], i.rule, i.entity_id)):
            writer.writerow(asdict(issue))


def export_dashboard(result: dict, template: Path, target: Path, as_of: str):
    payload = dict(
        as_of=as_of,
        properties=[{k: v for k, v in p.items() if not k.startswith("_")} for p in result["properties"]],
        reservations=result["reservations"], payments=result["payments"], expenses=result["expenses"],
        issues=[asdict(i) for i in result["issues"]],
    )
    html = template.read_text(encoding="utf-8").replace("/*__DATA__*/null", json.dumps(payload, ensure_ascii=False))
    target.write_text(html, encoding="utf-8")

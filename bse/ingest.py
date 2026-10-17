"""Stage 1: read CSV files into typed rows. No business logic here."""
import csv
from pathlib import Path

ENTITIES = {
    "properties": ("properties.csv", ("property_id",)),
    "commission_rules": ("commission_rules.csv", ("property_id", "effective_from")),
    "reservations": ("reservations.csv", ("reservation_id",)),
    "payments": ("payments.csv", ("payment_id",)),
    "expenses": ("expenses.csv", ("expense_id",)),
}

INT_FIELDS = {"gross_booking_value", "ota_fee", "refund_amount", "amount"}


def read_csv(path: Path) -> list[dict]:
    # utf-8-sig strips the BOM the source files carry
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    for line_no, row in enumerate(rows, start=2):
        row["_line"] = line_no
        row["_file"] = path.name
        for field in INT_FIELDS & row.keys():
            try:
                row[field] = int(row[field])
            except (TypeError, ValueError):
                row[field] = None  # surfaced by the quality stage
    return rows


def read_all(data_dir: Path) -> dict[str, list[dict]]:
    return {name: read_csv(data_dir / fname) for name, (fname, _) in ENTITIES.items()}


def key_of(entity: str, row: dict) -> str:
    return "|".join(str(row[k]) for k in ENTITIES[entity][1])

"""Stage 2: validation. Every finding becomes an Issue with an explicit action.

Actions:
  exclude - certain garbage (exact duplicate copy); never counted
  hold    - record is quarantined: excluded from totals until the source is fixed
  flag    - record stays in totals, but a human should look at it
  info    - fact worth knowing, no effect on numbers
"""
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from .ingest import ENTITIES, key_of

ID_FORMATS = {
    "reservation_id": r"R\d{4}",
    "payment_id": r"P\d{4}",
    "expense_id": r"E\d{4}",
    "property_id": r"V\d{3}",
}
ACTIVE_STATUSES = {"confirmed", "modified"}
KNOWN_STATUSES = ACTIVE_STATUSES | {"cancelled"}
MAX_PLAUSIBLE_OTA_RATE = 0.30
PAYMENT_TOLERANCE = 1  # IDR, rounding


@dataclass
class Issue:
    rule: str
    entity: str
    entity_id: str
    action: str
    detail: str
    amount: int = 0
    properties: str = ""


@dataclass
class Validated:
    rows: dict[str, list[dict]]            # deduplicated rows per entity
    issues: list[Issue] = field(default_factory=list)
    hold: dict[str, set] = field(default_factory=lambda: defaultdict(set))  # entity -> keys excluded from totals


def parse_date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _content(row: dict) -> tuple:
    return tuple(sorted((k, v) for k, v in row.items() if not k.startswith("_")))


def dedupe(entity: str, rows: list[dict], issues: list[Issue]) -> list[dict]:
    """Same key + same content -> drop the copy. Same key + different content -> keep latest, flag."""
    by_key: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_key[key_of(entity, row)].append(row)
    kept = []
    for key, group in by_key.items():
        first = group[0]
        if len(group) == 1:
            kept.append(first)
            continue
        distinct = {_content(r) for r in group}
        if len(distinct) == 1:
            issues.append(Issue("DQ01_exact_duplicate", entity, key, "exclude",
                                f"{len(group)} identical rows (lines {', '.join(str(r['_line']) for r in group)}); "
                                f"first kept, {len(group) - 1} copy dropped",
                                _amount(entity, first) * (len(group) - 1), first.get("property_id", "")))
            kept.append(first)
        else:
            winner = max(group, key=lambda r: (r.get("updated_at") or "", -r["_line"]))
            issues.append(Issue("DQ02_conflicting_duplicate", entity, key, "flag",
                                f"{len(group)} rows share the key but differ; kept line {winner['_line']} "
                                "(latest updated_at, else first)", 0, first.get("property_id", "")))
            kept.append(winner)
    return kept


def _amount(entity: str, row: dict) -> int:
    return row.get("gross_booking_value") or row.get("amount") or 0


def validate(raw: dict[str, list[dict]]) -> Validated:
    issues: list[Issue] = []
    rows = {name: dedupe(name, r, issues) for name, r in raw.items()}
    v = Validated(rows=rows, issues=issues)
    known_props = {p["property_id"] for p in rows["properties"]}
    base_currency = {p["property_id"]: p["base_currency"] for p in rows["properties"]}

    def hold(rule, entity, row, detail, amount=0):
        key = key_of(entity, row)
        v.hold[entity].add(key)
        issues.append(Issue(rule, entity, key, "hold", detail, amount, row.get("property_id", "")))

    def flag(rule, entity, row, detail, action="flag", amount=0):
        issues.append(Issue(rule, entity, key_of(entity, row), action, detail, amount, row.get("property_id", "")))

    _check_ids(rows, hold)
    _check_rules(rows, issues, known_props)
    for r in rows["reservations"]:
        _check_reservation(r, known_props, base_currency, hold, flag)
    reservations = {r["reservation_id"]: r for r in rows["reservations"]}
    for p in rows["payments"]:
        _check_payment(p, reservations, known_props, base_currency, hold, flag)
    for e in rows["expenses"]:
        _check_expense(e, known_props, base_currency, hold, flag)
    _check_overlaps(rows["reservations"], v)
    _check_unpaid(rows, v)
    return v


def _check_ids(rows, hold):
    for entity, id_field in (("reservations", "reservation_id"), ("payments", "payment_id"), ("expenses", "expense_id")):
        for r in rows[entity]:
            if not re.fullmatch(ID_FORMATS[id_field], r[id_field] or ""):
                hold("DQ04_bad_id_format", entity, r, f"{id_field}={r[id_field]!r} does not match {ID_FORMATS[id_field]}")


def _check_rules(rows, issues, known_props):
    by_prop = defaultdict(list)
    for rule in rows["commission_rules"]:
        by_prop[rule["property_id"]].append(rule)
        start, end = parse_date(rule["effective_from"]), parse_date(rule["effective_to"])
        if not start or not end or end < start:
            issues.append(Issue("DQ14_bad_rule_period", "commission_rules", key_of("commission_rules", rule), "flag",
                                "effective period unparsable or reversed", 0, rule["property_id"]))
        if rule["property_id"] not in known_props:
            issues.append(Issue("DQ03_unknown_property", "commission_rules", key_of("commission_rules", rule), "flag",
                                "rule refers to a property absent from properties.csv", 0, rule["property_id"]))
    for pid, rules in by_prop.items():
        rules.sort(key=lambda r: r["effective_from"])
        for a, b in zip(rules, rules[1:]):
            if b["effective_from"] <= a["effective_to"]:
                issues.append(Issue("DQ14_rule_period_overlap", "commission_rules", pid, "flag",
                                    f"fee periods overlap: {a['effective_from']}..{a['effective_to']} and "
                                    f"{b['effective_from']}..{b['effective_to']}", 0, pid))
    for pid in known_props - set(by_prop):
        issues.append(Issue("DQ14_no_commission_rule", "properties", pid, "flag", "property has no commission rule", 0, pid))


def _check_reservation(r, known_props, base_currency, hold, flag):
    gross, ota, refund = r["gross_booking_value"], r["ota_fee"], r["refund_amount"]
    if r["status"] not in KNOWN_STATUSES:
        hold("DQ06_unknown_status", "reservations", r, f"status={r['status']!r}", gross or 0)
    if r["property_id"] not in known_props:
        hold("DQ03_unknown_property", "reservations", r, f"property {r['property_id']} is not in properties.csv", gross or 0)
    elif r["currency"] != base_currency[r["property_id"]]:
        hold("DQ05_currency_mismatch", "reservations", r,
             f"currency {r['currency']} but {r['property_id']} is billed in {base_currency[r['property_id']]}; "
             "amount magnitude looks like IDR and no FX rate is provided", gross or 0)
    ci, co = parse_date(r["check_in"]), parse_date(r["check_out"])
    if not ci or not co or co <= ci:
        hold("DQ07_bad_dates", "reservations", r, f"check_in={r['check_in']!r} check_out={r['check_out']!r}", gross or 0)
    if gross is None or gross <= 0 or ota is None or refund is None:
        hold("DQ06_bad_amounts", "reservations", r, "gross/ota_fee/refund missing or non-positive", 0)
        return
    if r["status"] == "cancelled":
        if refund != gross:
            flag("DQ06_cancel_refund_mismatch", "reservations", r,
                 f"cancelled but refund {refund} != gross {gross}", amount=abs(gross - refund))
        return
    if ota < 0:
        hold("DQ06_negative_ota_fee", "reservations", r,
             f"ota_fee={ota} is negative (looks like a manual correction; payment is gross+100000)", gross)
    elif ota / gross > MAX_PLAUSIBLE_OTA_RATE:
        hold("DQ06_ota_fee_too_high", "reservations", r, f"ota_fee is {ota / gross:.0%} of gross", gross)
    if refund > gross:
        hold("DQ06_refund_exceeds_gross", "reservations", r,
             f"refund {refund} exceeds gross {gross} by {refund - gross}", gross)
    elif refund > 0:
        flag("DQ06_refund_on_active_booking", "reservations", r, f"partial refund {refund} on an active booking", amount=refund)
    if r["channel"] == "Direct" and ota > 0:
        flag("DQ09_direct_with_ota_fee", "reservations", r,
             f"Direct booking carries ota_fee={ota} ({ota / gross:.0%}); kept as a channel/payment cost, needs confirmation", amount=ota)
    if r["status"] == "modified":
        flag("DQ11_modified_no_history", "reservations", r,
             "status=modified but only the latest version is in the file; original terms unavailable", "info")


def _check_payment(p, reservations, known_props, base_currency, hold, flag):
    res = reservations.get(p["reservation_id"])
    if p["status"] != "received":
        flag("DQ10_payment_status", "payments", p, f"status={p['status']!r}, not counted as cash", "info")
    if p["amount"] is None or p["amount"] < 0:
        hold("DQ06_bad_amounts", "payments", p, f"amount={p['amount']!r}")
        return
    if res is None:
        hold("DQ04_orphan_payment", "payments", p, f"reservation {p['reservation_id']} not found", p["amount"])
        return
    if p["property_id"] != res["property_id"]:
        hold("DQ04_payment_property_mismatch", "payments", p,
             f"payment says {p['property_id']}, reservation says {res['property_id']}", p["amount"])
    if p["property_id"] not in known_props:
        hold("DQ03_unknown_property", "payments", p, f"property {p['property_id']} is not in properties.csv", p["amount"])
    elif p["currency"] != base_currency[p["property_id"]]:
        hold("DQ05_currency_mismatch", "payments", p, f"currency {p['currency']}", p["amount"])
    if p["amount"] == 0:
        flag("DQ10_zero_payment", "payments", p,
             f"zero-amount 'received' payment on a {res['status']} booking; ignored as cash", "info")
        return
    if res["status"] == "cancelled":
        flag("DQ10_payment_on_cancelled", "payments", p, "money received on a cancelled booking; refund status unknown", amount=p["amount"])
        return
    if res["gross_booking_value"] is not None and res["ota_fee"] is not None:
        expected = res["gross_booking_value"] - res["ota_fee"] - (res["refund_amount"] or 0)
        diff = p["amount"] - expected
        if abs(diff) > PAYMENT_TOLERANCE:
            flag("DQ10_payment_amount_mismatch", "payments", p,
                 f"received {p['amount']} vs expected gross-OTA fee-refund {expected} (diff {diff:+d})", amount=abs(diff))


def _check_expense(e, known_props, base_currency, hold, flag):
    if e["property_id"] not in known_props:
        hold("DQ03_unknown_property", "expenses", e, f"property {e['property_id']} is not in properties.csv", e["amount"] or 0)
    elif e["currency"] != base_currency[e["property_id"]]:
        hold("DQ05_currency_mismatch", "expenses", e, f"currency {e['currency']}", e["amount"] or 0)
    if e["amount"] is None or e["amount"] <= 0:
        hold("DQ06_bad_amounts", "expenses", e, f"amount={e['amount']!r}")
    if not parse_date(e["expense_date"]):
        hold("DQ07_bad_dates", "expenses", e, f"expense_date={e['expense_date']!r}", e["amount"] or 0)
    if e["payment_status"] != "paid":
        flag("DQ12_expense_not_paid", "expenses", e, f"payment_status={e['payment_status']}: accrued, not in cash", "info", e["amount"] or 0)


def _check_overlaps(reservations, v: Validated):
    """Two non-cancelled stays in the same villa whose nights intersect cannot both be real.
    Turnover (check_out == next check_in) is allowed."""
    by_prop = defaultdict(list)
    for r in reservations:
        ci, co = parse_date(r["check_in"]), parse_date(r["check_out"])
        if r["status"] != "cancelled" and ci and co and co > ci:
            by_prop[r["property_id"]].append((ci, co, r))
    for pid, stays in by_prop.items():
        stays.sort(key=lambda s: (s[0], s[1]))
        for i, (ci, co, r) in enumerate(stays):
            for ci2, co2, r2 in stays[i + 1:]:
                if ci2 >= co:
                    break
                same = (ci, co) == (ci2, co2)
                v.issues.append(Issue(
                    "DQ08_overlapping_stays", "reservations", f"{r['reservation_id']}+{r2['reservation_id']}", "flag",
                    f"{pid}: {ci}..{co} overlaps {ci2}..{co2}" + (" (identical dates)" if same else ""),
                    0, pid))


def _check_unpaid(rows, v: Validated):
    paid = {p["reservation_id"] for p in rows["payments"] if p["status"] == "received" and (p["amount"] or 0) > 0}
    for r in rows["reservations"]:
        if r["status"] in ACTIVE_STATUSES and r["reservation_id"] not in paid and r["reservation_id"] not in v.hold["reservations"]:
            v.issues.append(Issue("DQ13_no_payment_record", "reservations", r["reservation_id"], "info",
                                  f"active booking checked out {r['check_out']} has no received payment",
                                  (r["gross_booking_value"] or 0) - (r["ota_fee"] or 0), r["property_id"]))

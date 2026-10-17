"""Stage 4: financial calculations over validated rows; formulas are documented in README."""
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from .quality import Issue, parse_date


def money(value) -> int:
    return int(Decimal(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def rate_for(rules: list[dict], property_id: str, on: date):
    for rule in rules:
        if rule["property_id"] != property_id:
            continue
        start, end = parse_date(rule["effective_from"]), parse_date(rule["effective_to"])
        if start and end and start <= on <= end:
            return rule
    return None


def build_facts(rows: dict[str, list[dict]], hold: dict[str, set], issues: list[Issue]):
    """Returns (fact_reservations, fact_payments, fact_expenses). Held / cancelled rows stay in the facts
    with included=0 and a reason, so the dashboard can show what was left out and how much it was worth.
    May append issues (missing rule, payment of a held reservation)."""
    rules = rows["commission_rules"]
    flagged: dict[str, set] = {}
    for issue in issues:
        if issue.action == "flag":
            for key in issue.entity_id.split("+"):
                flagged.setdefault(key, set()).add(issue.rule.split("_")[0])

    reservations, by_id = [], {}
    for r in rows["reservations"]:
        rid = r["reservation_id"]
        reason, included = "ok", 1
        if rid in hold["reservations"]:
            reason, included = "held_by_data_quality", 0
        elif r["status"] == "cancelled":
            reason, included = "cancelled", 0
        rule = rate_for(rules, r["property_id"], parse_date(r["check_out"])) if included else None
        if included and rule is None:
            reason, included = "no_commission_rule", 0
            issues.append(Issue("DQ14_no_rule_for_stay", "reservations", rid, "hold",
                                f"no commission rule covers check_out {r['check_out']}", r["gross_booking_value"], r["property_id"]))
        gross = r["gross_booking_value"] or 0
        refund = (r["refund_amount"] or 0) if included else 0
        ota = (r["ota_fee"] or 0) if included else 0
        rate = Decimal(rule["management_fee_rate"]) if rule else Decimal(0)
        basis = {"gross_booking_value": gross, "net_of_refunds": gross - refund}.get(rule["fee_basis"] if rule else "", gross)
        fact = dict(
            reservation_id=rid, property_id=r["property_id"], check_in=r["check_in"], check_out=r["check_out"],
            status=r["status"], channel=r["channel"], included=included, reason=reason,
            gross=gross if included else 0, refund=refund, ota_fee=ota,
            mgmt_rate=float(rate), mgmt_fee=money(basis * rate) if included else 0,
            expected_payout=(gross - refund - ota) if included else 0,
            excluded_gross=0 if included else gross,
            flags=",".join(sorted(flagged.get(rid, ()))),
        )
        reservations.append(fact)
        by_id[rid] = fact

    payments = []
    paid_by_res: dict[str, int] = {}
    for p in rows["payments"]:
        res = by_id.get(p["reservation_id"])
        reason, included = "ok", 1
        if p["status"] != "received":
            reason, included = "not_received", 0
        elif (p["amount"] or 0) <= 0:
            reason, included = "zero_amount", 0
        elif p["payment_id"] in hold["payments"]:
            reason, included = "held_by_data_quality", 0
        elif res is None or not res["included"]:
            reason, included = "reservation_not_counted", 0
            if res is not None and res["reason"] == "held_by_data_quality":
                issues.append(Issue("DQ15_payment_of_held_reservation", "payments", p["payment_id"], "hold",
                                    f"{p['reservation_id']} is on hold, so its payment is left out of cash too",
                                    p["amount"], p["property_id"]))
        amount = p["amount"] or 0
        if included:
            paid_by_res[p["reservation_id"]] = paid_by_res.get(p["reservation_id"], 0) + amount
        payments.append(dict(payment_id=p["payment_id"], reservation_id=p["reservation_id"], property_id=p["property_id"],
                             payment_date=p["payment_date"], amount=amount if included else 0,
                             excluded_amount=0 if included else amount, included=included, reason=reason, mgmt_fee_cash=0))
    for pay in payments:
        res = by_id.get(pay["reservation_id"])
        if pay["included"] and res and res["expected_payout"] > 0:
            share = min(Decimal(1), Decimal(pay["amount"]) / res["expected_payout"])
            pay["mgmt_fee_cash"] = money(Decimal(res["mgmt_fee"]) * share)
    for fact in reservations:
        fact["paid"] = paid_by_res.get(fact["reservation_id"], 0)

    expenses = []
    for e in rows["expenses"]:
        included = 0 if e["expense_id"] in hold["expenses"] else 1
        expenses.append(dict(expense_id=e["expense_id"], property_id=e["property_id"], expense_date=e["expense_date"],
                             category=e["category"], amount=(e["amount"] or 0) if included else 0,
                             excluded_amount=0 if included else (e["amount"] or 0),
                             paid=1 if e["payment_status"] == "paid" else 0, included=included,
                             reason="ok" if included else "held_by_data_quality"))
    return reservations, payments, expenses

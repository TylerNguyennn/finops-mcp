"""The finops MCP server: read-only finance tools over Stripe and an invoice-intake bills register.

Run with `finops-mcp` (stdio). Configuration is environment only:
  FINOPS_DEMO=1              fictional demo data, no keys needed
  STRIPE_API_KEY             restricted read-only key (rk_live_... / rk_test_...)
  FINOPS_BILLS_DB            path to an invoice-intake register.sqlite (optional)
  FINOPS_DEFAULT_TERMS_DAYS  payment terms for bills with no printed due date (default 30)
"""
from __future__ import annotations

import functools
import os
import sys
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .sources import BillsSource, DemoBills, DemoStripe, StripeSource

# Same mapping as stripe-payout-reconcile, so the breakdown here matches the journal there.
ACCOUNTS = {
    "charge": "Sales",
    "refund": "Refunds",
    "refund_failure": "Refunds",
    "dispute": "Chargebacks",
    "dispute_reversal": "Chargebacks",
    "fee": "Stripe fees",
    "other_adjustment": "Stripe adjustments",
}
OPEN_DISPUTES = {"needs_response", "warning_needs_response", "under_review", "warning_under_review"}
AGING = [(0, "current"), (30, "1-30"), (60, "31-60"), (90, "61-90")]

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)

INSTRUCTIONS = """Read-only finance tools for a small business: Stripe receivables, payouts, disputes
and balance, plus supplier bills awaiting payment. Nothing here can move money or change a record.
Amounts are decimal strings in major units (12.50 means 12.50, never cents). Always state the
currency. When you give a cash forecast, repeat its assumptions to the user."""


def jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return f"{v:.2f}"
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    return v


def bucket(days_overdue: int) -> str:
    label = "90+"
    for limit, name in reversed(AGING):
        if days_overdue <= limit:
            label = name
    return label


def totals(rows: List[dict], key: str = "amount") -> Dict[str, Decimal]:
    out: Dict[str, Decimal] = defaultdict(Decimal)
    for r in rows:
        out[r["currency"]] += r[key]
    return dict(out)


def errors_to_model(fn):
    """Turn expected failures into a ToolError, whose text the model sees: an unknown id, a bad
    argument, or a Stripe error such as a restricted key missing a permission."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except KeyError as e:
            raise ToolError(str(e).strip("'")) from None
        except ValueError as e:
            raise ToolError(str(e)) from None
        except Exception as e:
            if type(e).__module__.startswith("stripe"):
                raise ToolError(f"Stripe: {getattr(e, 'user_message', None) or e}") from None
            raise
    return wrapper


def build(stripe, bills=None, today: Optional[Callable[[], date]] = None) -> MCPServer:
    today = today or date.today
    server = MCPServer("finops", instructions=INSTRUCTIONS)

    def tool(fn):
        return server.tool(annotations=READ_ONLY)(errors_to_model(fn))

    @tool
    def receivables(min_days_overdue: int = 0) -> dict:
        """Open customer invoices in Stripe with days overdue and an aging bucket.
        min_days_overdue=1 lists only overdue invoices; 0 lists everything still open."""
        now = today()
        rows = []
        for inv in stripe.open_invoices():
            overdue = (now - inv["due_date"]).days if inv["due_date"] else 0
            if max(overdue, 0) < min_days_overdue:
                continue
            rows.append(dict(inv, days_overdue=max(overdue, 0), aging=bucket(overdue)))
        rows.sort(key=lambda r: -r["days_overdue"])
        by_bucket: Dict[str, Dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
        for r in rows:
            by_bucket[r["aging"]][r["currency"]] += r["amount_due"]
        return jsonable({"as_of": now, "invoices": rows, "total_open": totals(rows, "amount_due"),
                         "by_aging_bucket": {k: dict(v) for k, v in by_bucket.items()}})

    @tool
    def invoice_detail(invoice_id: str) -> dict:
        """One Stripe invoice: customer, lines, totals, payment attempts and the hosted invoice link."""
        return jsonable(stripe.invoice(invoice_id))

    @tool
    def payouts(limit: int = 10) -> dict:
        """Recent Stripe payouts to the bank, newest first, with arrival date and status."""
        return jsonable({"payouts": stripe.payouts(max(1, min(limit, 100)))})

    @tool
    def payout_breakdown(payout_id: str) -> dict:
        """What one payout is made of: gross, fees and net per category (sales, refunds, chargebacks,
        fees), mapped to journal accounts, and whether the lines add up to the amount that hit the bank."""
        p = stripe.payout(payout_id)
        txns = stripe.payout_transactions(payout_id)
        groups: Dict[str, Dict[str, Any]] = {}
        unmapped = set()
        for t in txns:
            account = ACCOUNTS.get(t["category"])
            if account is None:
                unmapped.add(t["category"])
                account = "Stripe adjustments"
            g = groups.setdefault(t["category"], {"category": t["category"], "account": account, "count": 0,
                                                  "gross": Decimal(0), "fee": Decimal(0), "net": Decimal(0)})
            g["count"] += 1
            for k in ("gross", "fee", "net"):
                g[k] += t[k]
        net = sum((t["net"] for t in txns), Decimal(0))
        fees = sum((t["fee"] for t in txns), Decimal(0))
        return jsonable({
            "payout": p, "lines": len(txns), "by_category": list(groups.values()),
            "total_fees": fees, "net_of_lines": net, "difference": p["amount"] - net,
            "balanced": net == p["amount"],
            "unmapped_categories": sorted(unmapped),
            "transactions": txns,
        })

    @tool
    def disputes(open_only: bool = True) -> dict:
        """Stripe disputes (chargebacks) with the evidence deadline and days left to respond."""
        now = today()
        rows = []
        for d in stripe.disputes():
            if open_only and d["status"] not in OPEN_DISPUTES:
                continue
            left = (d["evidence_due"] - now).days if d["evidence_due"] else None
            rows.append(dict(d, days_to_respond=left))
        rows.sort(key=lambda r: (r["days_to_respond"] is None, r["days_to_respond"] or 0))
        return jsonable({"as_of": now, "disputes": rows, "total_at_stake": totals(rows)})

    @tool
    def payables(due_within_days: int = 14) -> dict:
        """Approved supplier bills not yet paid, due within the window (overdue ones included).
        Needs FINOPS_BILLS_DB (an invoice-intake register) or demo mode."""
        if bills is None:
            raise ValueError("no bills register configured: set FINOPS_BILLS_DB to an invoice-intake register.sqlite")
        now = today()
        horizon = now + timedelta(days=due_within_days)
        rows = [dict(b, overdue=b["due_date"] < now) for b in bills.unpaid() if b["due_date"] <= horizon]
        rows.sort(key=lambda b: b["due_date"])
        return jsonable({"as_of": now, "through": horizon, "bills": rows, "total_due": totals(rows)})

    @tool
    def balance() -> dict:
        """Stripe balance per currency: available (can be paid out now) and pending (still settling)."""
        return jsonable({"balance": stripe.balance()})

    @tool
    def cash_forecast(days: int = 13, currency: str = "USD", opening_bank_balance: str = "0",
                      pending_settles_in_days: int = 2, include_overdue_receivables: bool = False) -> dict:
        """Day-by-day cash forecast for one currency: bank balance you give + Stripe available today,
        Stripe pending and payouts in transit, customer invoices on their due date, approved bills on
        theirs. Returns each day with movement, the lowest point and the first day cash goes negative.
        The assumptions list in the result must be shown to the user with the numbers."""
        now = today()
        cur = currency.upper()
        end = now + timedelta(days=days)
        try:
            opening = Decimal(opening_bank_balance)
        except Exception:
            raise ValueError(f"opening_bank_balance must be a number, got {opening_bank_balance!r}") from None
        bal = stripe.balance().get(cur, {"available": Decimal(0), "pending": Decimal(0)})
        moves: List[dict] = []

        def add(day: date, amount: Decimal, what: str) -> None:
            moves.append({"date": max(day, now), "amount": amount, "what": what})

        if bal["pending"]:
            add(now + timedelta(days=pending_settles_in_days), bal["pending"], "Stripe pending balance settles")
        for p in stripe.payouts(100):
            if p["currency"] == cur and p["status"] in ("pending", "in_transit") and p["arrival_date"]:
                add(p["arrival_date"], p["amount"], f"Stripe payout {p['id']} lands")
        at_risk = []
        for inv in stripe.open_invoices():
            if inv["currency"] != cur:
                continue
            if inv["due_date"] and inv["due_date"] < now and not include_overdue_receivables:
                at_risk.append(inv)
                continue
            add(inv["due_date"] or now, inv["amount_due"], f"invoice {inv['number']} from {inv['customer']}")
        skipped_bills = 0
        if bills is not None:
            for b in bills.unpaid():
                if b["currency"] != cur:
                    skipped_bills += 1
                    continue
                add(b["due_date"], -b["amount"], f"bill {b['invoice_number']} to {b['supplier']}"
                    + ("" if b["due_date_printed"] else " (due date from default terms)"))

        start = opening + bal["available"]
        running = start
        timeline = []
        lowest = (start, now)
        first_negative = None
        for day in sorted({m["date"] for m in moves if m["date"] <= end}):
            todays = [m for m in moves if m["date"] == day]
            running += sum((m["amount"] for m in todays), Decimal(0))
            timeline.append({"date": day, "change": sum((m["amount"] for m in todays), Decimal(0)),
                             "balance": running, "items": [{"amount": m["amount"], "what": m["what"]} for m in todays]})
            if running < lowest[0]:
                lowest = (running, day)
            if running < 0 and first_negative is None:
                first_negative = day

        assumptions = [
            f"Opening cash = bank balance you gave ({opening:.2f}) + Stripe available ({bal['available']:.2f}).",
            f"Stripe pending balance is counted {pending_settles_in_days} days from today.",
            "Customers pay on the due date." + ("" if include_overdue_receivables else
                                                " Overdue invoices are left out and listed as at risk."),
            "Approved bills are paid on their due date; bills with no printed date use the default terms.",
            "Card sales you haven't made yet, payroll and other spend outside the bills register are not included.",
        ]
        if bills is None:
            assumptions.append("No bills register is configured, so no outgoing payments are included.")
        if skipped_bills:
            assumptions.append(f"{skipped_bills} bills in other currencies are not in this {cur} forecast.")
        return jsonable({
            "currency": cur, "from": now, "through": end, "opening": start, "closing": running,
            "lowest": {"balance": lowest[0], "date": lowest[1]}, "first_negative_date": first_negative,
            "timeline": timeline, "overdue_receivables_at_risk": at_risk, "assumptions": assumptions,
        })

    @server.prompt()
    def weekly_cash_review() -> str:
        """A Monday finance check-in: what's owed to us, what we owe, and whether cash holds for two weeks."""
        return ("Run my weekly cash review. Use receivables (overdue first), payables for the next 14 days, "
                "open disputes, and a 13-day cash_forecast in USD. Give me: 1) the lowest cash point and date, "
                "2) which overdue customers to chase, biggest first, with invoice numbers, 3) disputes with a "
                "deadline this week, 4) bills I could move if cash goes negative. Tables, short, no advice "
                "beyond the numbers, and list the forecast assumptions at the end.")

    return server


def from_env() -> MCPServer:
    if os.environ.get("FINOPS_DEMO") == "1":
        return build(DemoStripe(), DemoBills())
    key = os.environ.get("STRIPE_API_KEY")
    if not key:
        sys.exit("finops-mcp: set STRIPE_API_KEY (a restricted read-only key) or FINOPS_DEMO=1 for demo data")
    db = os.environ.get("FINOPS_BILLS_DB")
    return build(StripeSource(key), BillsSource(Path(db)) if db else None)


def main() -> None:
    from_env().run("stdio")

"""Read-only data sources: Stripe (receivables, payouts, disputes, balance) and the bills register (payables).

Everything is normalised to plain dicts with Decimal amounts in major units, so the
tools never see Stripe's minor units or SDK objects.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

# Currencies Stripe sends without minor units. https://docs.stripe.com/currencies#zero-decimal
ZERO_DECIMAL = {"bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga", "pyg", "rwf",
                "ugx", "vnd", "vuv", "xaf", "xof", "xpf"}

DEMO_DIR = Path(__file__).parent / "demo"


def major(amount: int, currency: str) -> Decimal:
    return Decimal(amount) if currency.lower() in ZERO_DECIMAL else Decimal(amount) / 100


def day(ts: Optional[int]) -> Optional[date]:
    return datetime.fromtimestamp(ts, tz=timezone.utc).date() if ts else None


class StripeSource:
    """Wraps stripe.StripeClient. Use a restricted key with read access to Invoices, Payouts,
    Balance, Balance transactions and Disputes; this code never calls a write endpoint."""

    def __init__(self, api_key: str):
        import stripe

        if api_key.startswith("sk_live_"):
            print("finops-mcp: you passed a full live secret key. A restricted read-only key (rk_live_...) "
                  "is enough and safer.", file=sys.stderr)
        self.client = stripe.StripeClient(api_key)

    def open_invoices(self) -> List[dict]:
        out = []
        for inv in self.client.v1.invoices.list(params={"status": "open", "limit": 100}).auto_paging_iter():
            out.append({
                "id": inv.id, "number": inv.number, "customer": inv.customer_name or inv.customer_email or inv.customer,
                "customer_email": inv.customer_email, "currency": inv.currency.upper(),
                "amount_due": major(inv.amount_remaining, inv.currency), "issued": day(inv.created),
                "due_date": day(inv.due_date), "url": inv.hosted_invoice_url,
            })
        return out

    def invoice(self, invoice_id: str) -> dict:
        inv = self.client.v1.invoices.retrieve(invoice_id)
        return {
            "id": inv.id, "number": inv.number, "status": inv.status,
            "customer": inv.customer_name or inv.customer_email, "customer_email": inv.customer_email,
            "currency": inv.currency.upper(), "total": major(inv.total, inv.currency),
            "amount_due": major(inv.amount_remaining, inv.currency), "issued": day(inv.created),
            "due_date": day(inv.due_date), "attempt_count": inv.attempt_count, "url": inv.hosted_invoice_url,
            "lines": [{"description": l.description, "quantity": l.quantity, "amount": major(l.amount, inv.currency)}
                      for l in inv.lines.data],
        }

    def payouts(self, limit: int) -> List[dict]:
        return [{
            "id": p.id, "amount": major(p.amount, p.currency), "currency": p.currency.upper(),
            "arrival_date": day(p.arrival_date), "status": p.status,
        } for p in self.client.v1.payouts.list(params={"limit": limit}).data]

    def payout(self, payout_id: str) -> dict:
        p = self.client.v1.payouts.retrieve(payout_id)
        return {"id": p.id, "amount": major(p.amount, p.currency), "currency": p.currency.upper(),
                "arrival_date": day(p.arrival_date), "status": p.status}

    def payout_transactions(self, payout_id: str) -> List[dict]:
        out = []
        for t in self.client.v1.balance_transactions.list(params={"payout": payout_id, "limit": 100}).auto_paging_iter():
            if t.type == "payout":
                continue  # the payout itself, not what it contains
            out.append({"category": t.reporting_category, "currency": t.currency.upper(),
                        "gross": major(t.amount, t.currency), "fee": major(t.fee, t.currency),
                        "net": major(t.net, t.currency), "description": t.description})
        return out

    def disputes(self) -> List[dict]:
        out = []
        for d in self.client.v1.disputes.list(params={"limit": 100}).auto_paging_iter():
            out.append({"id": d.id, "amount": major(d.amount, d.currency), "currency": d.currency.upper(),
                        "reason": d.reason, "status": d.status, "charge": d.charge,
                        "evidence_due": day(d.evidence_details.due_by) if d.evidence_details else None})
        return out

    def balance(self) -> Dict[str, Dict[str, Decimal]]:
        b = self.client.v1.balance.retrieve()
        out: Dict[str, Dict[str, Decimal]] = {}
        for kind in ("available", "pending"):
            for m in getattr(b, kind):
                out.setdefault(m.currency.upper(), {"available": Decimal(0), "pending": Decimal(0)})[kind] += major(m.amount, m.currency)
        return out


class DemoStripe:
    """The same shapes from demo/stripe.json. Dates are stored as offsets from today so the demo never ages."""

    def __init__(self, today: Optional[date] = None):
        self.today = today or date.today()
        self.data = json.loads((DEMO_DIR / "stripe.json").read_text())

    def _d(self, offset: Optional[int]) -> Optional[date]:
        return None if offset is None else self.today + timedelta(days=offset)

    def _invoice(self, i: dict) -> dict:
        return {
            "id": i["id"], "number": i["number"], "customer": i["customer"], "customer_email": i["customer_email"],
            "currency": i["currency"], "amount_due": Decimal(i["amount_due"]), "issued": self._d(i["issued"]),
            "due_date": self._d(i["due"]), "url": f"https://invoice.stripe.com/i/demo/{i['id']}",
        }

    def open_invoices(self) -> List[dict]:
        return [self._invoice(i) for i in self.data["invoices"]]

    def invoice(self, invoice_id: str) -> dict:
        i = next((i for i in self.data["invoices"] if i["id"] == invoice_id), None)
        if i is None:
            raise KeyError(f"no invoice {invoice_id}")
        return dict(self._invoice(i), status="open", total=Decimal(i["amount_due"]),
                    attempt_count=i["attempts"], lines=[dict(l, amount=Decimal(l["amount"])) for l in i["lines"]])

    def payouts(self, limit: int) -> List[dict]:
        return [{"id": p["id"], "amount": Decimal(p["amount"]), "currency": p["currency"],
                 "arrival_date": self._d(p["arrival"]), "status": p["status"]} for p in self.data["payouts"]][:limit]

    def payout(self, payout_id: str) -> dict:
        p = next((p for p in self.payouts(len(self.data["payouts"])) if p["id"] == payout_id), None)
        if p is None:
            raise KeyError(f"no payout {payout_id}")
        return p

    def payout_transactions(self, payout_id: str) -> List[dict]:
        p = next((p for p in self.data["payouts"] if p["id"] == payout_id), None)
        if p is None:
            raise KeyError(f"no payout {payout_id}")
        return [{"category": t["category"], "currency": p["currency"], "gross": Decimal(t["gross"]),
                 "fee": Decimal(t["fee"]), "net": Decimal(t["gross"]) - Decimal(t["fee"]),
                 "description": t["description"]} for t in p["transactions"]]

    def disputes(self) -> List[dict]:
        return [dict(d, amount=Decimal(d["amount"]), evidence_due=self._d(d["evidence_due"]))
                for d in self.data["disputes"]]

    def balance(self) -> Dict[str, Dict[str, Decimal]]:
        return {c: {k: Decimal(v) for k, v in b.items()} for c, b in self.data["balance"].items()}


class BillsSource:
    """Approved, unpaid bills from an invoice-intake register (opened read-only)."""

    def __init__(self, path: Path):
        self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self.db.row_factory = sqlite3.Row
        self.terms = int(os.environ.get("FINOPS_DEFAULT_TERMS_DAYS", "30"))

    def unpaid(self) -> List[dict]:
        rows = self.db.execute(
            "SELECT id, supplier, invoice_number, currency, total, issue_date, due_date FROM bills "
            "WHERE status IN ('approved', 'exported') AND paid_at IS NULL"
        ).fetchall()
        out = []
        for r in rows:
            due = date.fromisoformat(r["due_date"]) if r["due_date"] else \
                date.fromisoformat(r["issue_date"]) + timedelta(days=self.terms)
            out.append({"bill_id": r["id"], "supplier": r["supplier"], "invoice_number": r["invoice_number"],
                        "currency": r["currency"], "amount": Decimal(r["total"]), "due_date": due,
                        "due_date_printed": bool(r["due_date"])})
        return out


class DemoBills:
    def __init__(self, today: Optional[date] = None):
        self.today = today or date.today()
        self.data = json.loads((DEMO_DIR / "bills.json").read_text())

    def unpaid(self) -> List[dict]:
        out = []
        for b in self.data:
            row = {k: v for k, v in b.items() if k != "due"}
            out.append(dict(row, amount=Decimal(b["amount"]), due_date=self.today + timedelta(days=b["due"]),
                            due_date_printed=True))
        return out

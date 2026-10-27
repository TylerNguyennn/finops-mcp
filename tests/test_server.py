import asyncio
import json
import sqlite3
from datetime import date
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest
from mcp import Client

from finops_mcp.server import build
from finops_mcp.sources import BillsSource, DemoBills, DemoStripe, StripeSource, major

TODAY = date(2026, 10, 27)


def call(server, tool, args=None):
    async def go():
        async with Client(server) as c:
            return await c.call_tool(tool, args or {})
    r = asyncio.run(go())
    return r.is_error, (r.content[0].text if r.is_error else json.loads(r.content[0].text))


@pytest.fixture
def demo():
    return build(DemoStripe(TODAY), DemoBills(TODAY), today=lambda: TODAY)


def test_every_tool_is_read_only(demo):
    async def go():
        async with Client(demo) as c:
            return (await c.list_tools()).tools
    tools = asyncio.run(go())
    assert {t.name for t in tools} == {"receivables", "invoice_detail", "payouts", "payout_breakdown",
                                       "disputes", "payables", "balance", "cash_forecast"}
    assert all(t.annotations.read_only_hint and not t.annotations.destructive_hint for t in tools)


def test_receivables_aging(demo):
    _, r = call(demo, "receivables", {"min_days_overdue": 1})
    assert [i["number"] for i in r["invoices"]] == ["HB-1002", "HB-1001"]
    assert [i["aging"] for i in r["invoices"]] == ["1-30", "1-30"]
    assert r["total_open"] == {"USD": "4650.00"}
    _, everything = call(demo, "receivables")
    assert everything["by_aging_bucket"]["current"] == {"USD": "7180.00", "EUR": "2100.00"}


def test_payout_breakdown_balances_and_maps_accounts(demo):
    _, r = call(demo, "payout_breakdown", {"payout_id": "po_demo_3"})
    assert r["balanced"] and r["difference"] == "0.00"
    by = {g["category"]: g for g in r["by_category"]}
    assert by["charge"]["account"] == "Sales" and by["charge"]["net"] == "2912.10"
    assert by["dispute"]["account"] == "Chargebacks" and by["dispute"]["net"] == "-165.00"
    assert r["total_fees"] == "102.90"


def test_disputes_deadline(demo):
    _, r = call(demo, "disputes")
    assert r["disputes"][0]["days_to_respond"] == 4
    assert r["total_at_stake"] == {"USD": "150.00"}


def test_payables_window(demo):
    _, r = call(demo, "payables", {"due_within_days": 7})
    assert [b["invoice_number"] for b in r["bills"]] == ["RENT-NOV", "INV-2041"]
    _, r = call(demo, "payables", {"due_within_days": 14})
    assert r["total_due"] == {"USD": "12935.00", "GBP": "1135.20"}


def test_cash_forecast_finds_the_shortfall(demo):
    _, r = call(demo, "cash_forecast")
    assert r["opening"] == "4312.40"
    assert r["first_negative_date"] == "2026-11-04"
    assert r["lowest"] == {"balance": "-3037.50", "date": "2026-11-05"}
    assert r["closing"] == "3162.50"
    assert [i["number"] for i in r["overdue_receivables_at_risk"]] == ["HB-1001", "HB-1002"]
    assert any("other currencies" in a for a in r["assumptions"])
    # If the two overdue customers pay today, cash never goes negative.
    _, r = call(demo, "cash_forecast", {"include_overdue_receivables": True})
    assert r["first_negative_date"] is None


def test_cash_forecast_with_bank_balance(demo):
    _, r = call(demo, "cash_forecast", {"opening_bank_balance": "5000"})
    assert r["first_negative_date"] is None and r["lowest"]["balance"] == "1962.50"


def test_errors_reach_the_model(demo):
    err, text = call(demo, "invoice_detail", {"invoice_id": "in_missing"})
    assert err and "no invoice in_missing" in text
    err, text = call(demo, "cash_forecast", {"opening_bank_balance": "lots"})
    assert err and "must be a number" in text
    err, text = call(build(DemoStripe(TODAY), None, today=lambda: TODAY), "payables")
    assert err and "FINOPS_BILLS_DB" in text


def test_zero_decimal_currency():
    assert major(1250, "usd") == Decimal("12.50")
    assert major(125000, "VND") == Decimal("125000")


class FakeList:
    def __init__(self, items):
        self.data = items

    def auto_paging_iter(self):
        return iter(self.data)


def test_stripe_source_maps_sdk_objects():
    inv = NS(id="in_1", number="A-1", customer="cus_1", customer_name="Harbor", customer_email="ap@h.example",
             currency="jpy", amount_remaining=50000, total=50000, created=1_793_000_000, due_date=None,
             hosted_invoice_url="https://x", status="open", attempt_count=1,
             lines=NS(data=[NS(description="Plan", quantity=1, amount=50000)]))
    txns = [NS(type="payout", reporting_category="payout", currency="usd", amount=-1000, fee=0, net=-1000, description=""),
            NS(type="charge", reporting_category="charge", currency="usd", amount=1000, fee=59, net=941, description="c")]
    v1 = NS(
        invoices=NS(list=lambda params: FakeList([inv]), retrieve=lambda i: inv),
        balance_transactions=NS(list=lambda params: FakeList(txns)),
        balance=NS(retrieve=lambda: NS(available=[NS(currency="usd", amount=1234)], pending=[NS(currency="usd", amount=66)])),
    )
    src = StripeSource.__new__(StripeSource)
    src.client = NS(v1=v1)
    assert src.open_invoices()[0]["amount_due"] == Decimal("50000") and src.open_invoices()[0]["customer"] == "Harbor"
    assert src.invoice("in_1")["lines"][0]["amount"] == Decimal("50000")
    assert [t["net"] for t in src.payout_transactions("po_1")] == [Decimal("9.41")]
    assert src.balance() == {"USD": {"available": Decimal("12.34"), "pending": Decimal("0.66")}}


def test_bills_source_reads_invoice_intake_register(tmp_path, monkeypatch):
    db = tmp_path / "register.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE bills (id INTEGER PRIMARY KEY, supplier TEXT, invoice_number TEXT, currency TEXT, "
                "total TEXT, issue_date TEXT, due_date TEXT, status TEXT, paid_at TEXT)")
    con.executemany("INSERT INTO bills VALUES (?,?,?,?,?,?,?,?,?)", [
        (1, "Acme", "1", "USD", "100.00", "2026-10-01", "2026-10-31", "approved", None),
        (2, "Greenleaf", "2", "AUD", "475.20", "2026-10-05", None, "exported", None),
        (3, "Paid Co", "3", "USD", "50.00", "2026-10-01", "2026-10-15", "exported", "2026-10-14"),
        (4, "Pending Co", "4", "USD", "70.00", "2026-10-01", "2026-10-15", "needs_review", None),
    ])
    con.commit()
    monkeypatch.setenv("FINOPS_DEFAULT_TERMS_DAYS", "14")
    rows = {r["supplier"]: r for r in BillsSource(db).unpaid()}
    assert set(rows) == {"Acme", "Greenleaf"}
    assert rows["Greenleaf"]["due_date"] == date(2026, 10, 19) and not rows["Greenleaf"]["due_date_printed"]
    with pytest.raises(sqlite3.OperationalError):
        BillsSource(db).db.execute("DELETE FROM bills")

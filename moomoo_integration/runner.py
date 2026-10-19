"""Hard-coded moomoo SIMULATE execution. No real-account or unlock path."""
import argparse
import fcntl
import hashlib
import html
import json
import math
import sqlite3
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from moomoo import (OpenQuoteContext, OpenSecTradeContext, RET_OK, SecurityFirm,
                    TrdMarket, TrdEnv, TrdSide, OrderType, Currency, TimeInForce)
from .forecast import STATE, CAL, SYMBOLS, forecast_all, utcnow, atomic_json

BUDGET = 1000.0
MAX_ORDER = 500.0
MAX_ORDERS_DAY = 3
TERMINAL = {"FILLED_ALL", "CANCELLED_ALL", "CANCELLED_PART", "FAILED",
            "DISABLED", "DELETED", "SUBMIT_FAILED"}
NY = ZoneInfo("America/New_York")

def checked(result):
    ret, data = result
    if ret != RET_OK:
        raise RuntimeError(str(data)[:250])
    return data

def paper_account(accounts):
    rows = accounts[(accounts.trd_env.astype(str) == "SIMULATE") &
                    accounts.sim_acc_type.astype(str).isin(["STOCK", "STOCK_AND_OPTION"])]
    if len(rows) != 1:
        raise RuntimeError("Cannot uniquely verify US stock SIMULATE account")
    return int(rows.iloc[0].acc_id)

def in_window(target, now):
    stamp = pd.Timestamp(now)
    day = pd.Timestamp(target)
    return (CAL.is_session(day) and
            CAL.session_open(day) + pd.Timedelta(minutes=15) <= stamp <
            CAL.session_close(day) - pd.Timedelta(minutes=30))

def setup():
    STATE.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(STATE / "paper.sqlite3")
    db.row_factory = sqlite3.Row
    db.executescript("""
      CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
      CREATE TABLE IF NOT EXISTS positions(code TEXT PRIMARY KEY,qty REAL NOT NULL);
      CREATE TABLE IF NOT EXISTS cash(id INTEGER PRIMARY KEY,value REAL NOT NULL);
      INSERT OR IGNORE INTO cash VALUES(1,1000);
      CREATE TABLE IF NOT EXISTS orders(
        key TEXT PRIMARY KEY,day TEXT,code TEXT,side TEXT,qty REAL,price REAL,
        broker_id TEXT,status TEXT,dealt REAL DEFAULT 0,value REAL DEFAULT 0,
        fees REAL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS daily(day TEXT PRIMARY KEY,start_equity REAL);
    """)
    db.commit()
    return db

def bind_account(db, account):
    digest = hashlib.sha256(str(account).encode()).hexdigest()
    old = db.execute("SELECT value FROM meta WHERE key='account'").fetchone()
    if old and old[0] != digest:
        raise RuntimeError("Paper account changed; refusing to reuse ledger")
    db.execute("INSERT OR IGNORE INTO meta VALUES('account',?)", (digest,))
    db.commit()

def apply_fill(db, key, row):
    prior = db.execute("SELECT * FROM orders WHERE key=?", (key,)).fetchone()
    dealt = float(row["dealt_qty"])
    avg = float(row["dealt_avg_price"])
    if not math.isfinite(dealt) or dealt < prior["dealt"] or dealt > prior["qty"]:
        raise RuntimeError("Inconsistent cumulative fill quantity")
    if dealt > 0 and (not math.isfinite(avg) or avg <= 0):
        raise RuntimeError("Invalid fill price")
    value = dealt * avg if dealt else 0
    fees = value * .0015 + (1 if dealt else 0)
    delta = dealt - prior["dealt"]
    sign = 1 if prior["side"] == "BUY" else -1
    current = db.execute("SELECT qty FROM positions WHERE code=?", (prior["code"],)).fetchone()
    qty = (current[0] if current else 0) + sign * delta
    cash = db.execute("SELECT value FROM cash WHERE id=1").fetchone()[0]
    cash -= sign * (value - prior["value"]) + fees - prior["fees"]
    if qty < -1e-6 or cash < -1e-6:
        raise RuntimeError("Strategy cash/position inconsistent; halt for reconciliation")
    db.execute("INSERT OR REPLACE INTO positions VALUES(?,?)", (prior["code"], max(0, qty)))
    db.execute("UPDATE cash SET value=? WHERE id=1", (cash,))
    db.execute("UPDATE orders SET dealt=?,value=?,fees=?,status=? WHERE key=?",
               (dealt, value, fees, str(row["order_status"]), key))
    db.commit()

def reconcile(db, trade, account):
    for order in db.execute("SELECT * FROM orders").fetchall():
        if order["status"] in TERMINAL:
            continue
        if not order["broker_id"]:
            raise RuntimeError("Uncertain submission; manual reconciliation required. No retry.")
        rows = checked(trade.order_list_query(
            order_id=order["broker_id"], trd_env=TrdEnv.SIMULATE,
            acc_id=account, refresh_cache=True))
        if rows.empty:
            rows = checked(trade.history_order_list_query(
                code=order["code"], start=order["day"],
                end=utcnow().astimezone(NY).date().isoformat(),
                trd_env=TrdEnv.SIMULATE, acc_id=account))
            rows = rows[rows.order_id.astype(str) == order["broker_id"]]
        if len(rows) != 1:
            raise RuntimeError("Unable to reconcile paper order")
        apply_fill(db, order["key"], rows.iloc[0])
    if any(r["status"] not in TERMINAL for r in db.execute("SELECT * FROM orders")):
        raise RuntimeError("Paper order pending; defer new orders until reconciled")

def fresh_price(row, now):
    value = float(row["last_price"])
    at = pd.Timestamp(row["update_time"]).tz_localize(NY)
    age = (pd.Timestamp(now) - at).total_seconds()
    if not math.isfinite(value) or value <= 0 or not 0 <= age <= 120:
        raise RuntimeError("Stale/invalid quote")
    if bool(row.get("suspension", False)):
        raise RuntimeError("Suspended instrument")
    return value

def order_size(side, price, owned, cash, exposure):
    if not math.isfinite(price) or price <= 0:
        return 0
    if side == "SELL":
        return int(owned)
    # Start with at most one simulated share per instrument; never average up.
    if owned > 0 or price > MAX_ORDER:
        return 0
    return int(price * 1.0015 + 1 <= cash and exposure + price <= BUDGET)

def execute(result, q, db, submit=False):
    if not in_window(result["target_session"], utcnow()):
        return ["Outside regular-session execution window; forecast saved."]
    trade = OpenSecTradeContext(filter_trdmarket=TrdMarket.US,
        security_firm=SecurityFirm.FUTUSG, host="127.0.0.1", port=11111)
    notes = []
    try:
        account = paper_account(checked(trade.get_acc_list()))
        bind_account(db, account)
        reconcile(db, trade, account)
        positions = checked(trade.position_list_query(
            trd_env=TrdEnv.SIMULATE, acc_id=account, refresh_cache=True))
        actual = {str(r.code): float(r.qty) for _, r in positions.iterrows()}
        if any(not math.isfinite(v) or v < 0 for v in actual.values()):
            raise RuntimeError("Invalid or short paper-account positions")
        managed = {r["code"]: float(r["qty"]) for r in db.execute("SELECT * FROM positions")}
        if any(abs(actual.get(k, 0) - qty) > 1e-6 for k, qty in managed.items()):
            raise RuntimeError("Broker holdings differ from bot ledger; manual reconciliation required")
        funds = checked(trade.accinfo_query(trd_env=TrdEnv.SIMULATE, acc_id=account,
                                           refresh_cache=True, currency=Currency.USD))
        broker_cash = float(funds.iloc[0]["cash"])
        if not math.isfinite(broker_cash) or broker_cash < 0:
            raise RuntimeError("Invalid simulated cash")
        snapshots = checked(q.get_market_snapshot(["US." + s for s in SYMBOLS]))
        snapshots = {str(r.code): r for _, r in snapshots.iterrows()}
        prices = {code: fresh_price(row, utcnow()) for code, row in snapshots.items()}
        if any(qty and code not in prices for code, qty in managed.items()):
            raise RuntimeError("Missing held-position price")
        cash = db.execute("SELECT value FROM cash WHERE id=1").fetchone()[0]
        exposure = sum(qty * prices[code] for code, qty in managed.items() if qty)
        equity = cash + exposure
        day = result["target_session"]
        db.execute("INSERT OR IGNORE INTO daily VALUES(?,?)", (day, equity))
        db.commit()
        start_equity = db.execute("SELECT start_equity FROM daily WHERE day=?", (day,)).fetchone()[0]
        loss_block = equity < start_equity * .98
        for item in result["items"]:
            code, side = "US." + item["symbol"], item["action"]
            if item["status"] != "OK" or side not in ("BUY", "SELL"):
                continue
            if code not in prices:
                notes.append(code + ": missing quote"); continue
            owned = managed.get(code, 0)
            if code not in managed and actual.get(code, 0) != 0:
                notes.append(code + ": existing non-bot position left alone"); continue
            if side == "BUY" and loss_block:
                notes.append(code + ": daily 2% strategy loss gate"); continue
            key = day + ":" + code
            if db.execute("SELECT 1 FROM orders WHERE key=?", (key,)).fetchone():
                notes.append(code + ": already attempted this session"); continue
            if db.execute("SELECT COUNT(*) FROM orders WHERE day=?", (day,)).fetchone()[0] >= MAX_ORDERS_DAY:
                break
            last = prices[code]
            # A modest marketable limit bounds slippage; never use market orders.
            price = round(last * (1.001 if side == "BUY" else .999), 2)
            if abs(last / item["close"] - 1) > .05:
                notes.append(code + ": price moved >5% from input close"); continue
            qty = order_size(side, price, owned, min(cash, broker_cash), exposure)
            if qty <= 0:
                notes.append(code + ": no owned shares or size/cash limit"); continue
            notes.append(f"{'SUBMIT' if submit else 'PREVIEW'} SIMULATE {side} {qty} {code} limit {price}")
            if not submit:
                continue
            if (STATE / "STOP").exists():
                raise RuntimeError("STOP file present")
            if not in_window(day, utcnow()):
                raise RuntimeError("Execution window ended")
            # Persist intent BEFORE submitting. A crash/timeout never causes an automatic retry.
            db.execute("INSERT INTO orders(key,day,code,side,qty,price,status) VALUES(?,?,?,?,?,?,?)",
                       (key, day, code, side, qty, price, "SUBMITTING"))
            db.commit()
            ret, data = trade.place_order(price=price, qty=qty, code=code,
                trd_side=TrdSide.BUY if side == "BUY" else TrdSide.SELL,
                order_type=OrderType.NORMAL, trd_env=TrdEnv.SIMULATE, acc_id=account,
                time_in_force=TimeInForce.DAY, fill_outside_rth=False,
                remark="KRONOS_" + hashlib.sha256(key.encode()).hexdigest()[:16])
            if ret != RET_OK or data.empty:
                raise RuntimeError("Submission unconfirmed; blocked pending review: " + str(data)[:150])
            oid = str(data.iloc[0]["order_id"])
            db.execute("UPDATE orders SET broker_id=?,status='SUBMITTED' WHERE key=?", (oid, key))
            db.commit()
            # One broker submission per run. Reconcile before any subsequent order.
            break
        return notes or ["No eligible paper order."]
    finally:
        trade.close()

def report(result, notes, db):
    report = {"generated_utc": utcnow().isoformat(), "mode": "SIMULATE_ONLY",
              "forecasts": result, "execution": notes,
              "strategy_cash_after_estimated_fees": db.execute("SELECT value FROM cash WHERE id=1").fetchone()[0],
              "positions": [dict(r) for r in db.execute("SELECT * FROM positions")],
              "orders": [dict(r) for r in db.execute("SELECT * FROM orders")]}
    atomic_json(STATE / "latest.json", report)
    rows = []
    for item in result.get("items", []):
        ret = f"{item['return']*100:+.2f}%" if "return" in item else "—"
        rows.append("<tr><td>" + html.escape(item["symbol"]) + "</td><td>" +
                    html.escape(item["action"]) + "</td><td>" + ret + "</td><td>" +
                    html.escape(item["reason"]) + "</td></tr>")
    page = """<!doctype html><meta charset="utf-8"><title>Kronos paper trading</title>
    <style>body{font:16px system-ui;max-width:1100px;margin:45px auto;padding:20px;background:#101a29;color:#eaf1fa}
    table{width:100%;border-collapse:collapse}td,th{padding:12px;text-align:left;border-bottom:1px solid #344253}
    pre{white-space:pre-wrap}a{color:#91c7ff}</style>
    <h1>Kronos · moomoo paper trading</h1><p>SIMULATE only · $1,000 virtual strategy budget · one share per instrument · $500 order cap</p>
    <p>Experimental forecasts. Daily PB EMA entry filter. These returns are model outputs, not proven performance.</p>"""
    page += "<p>Updated " + html.escape(report["generated_utc"]) + " · Input session " + html.escape(result.get("last_session", "")) + "</p>"
    page += "<table><tr><th>Symbol</th><th>Signal</th><th>Forecast</th><th>Reason</th></tr>" + "".join(rows) + "</table>"
    page += "<h2>Execution</h2><pre>" + html.escape("\n".join(notes)) + "</pre>"
    page += "<h2>Bot orders</h2><pre>" + html.escape(json.dumps(report["orders"], indent=2)) + "</pre>"
    page += '<p><a href="../phase6/reports/shadow_dashboard.html">Existing shadow portfolios</a></p>'
    (STATE / "dashboard.html").write_text(page)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--paper", action="store_true", help="Submit exclusively SIMULATE orders")
    args = parser.parse_args()
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "run.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another Kronos run active"); return
        if (STATE / "STOP").exists():
            print("STOP file present; disabled"); return
        db = setup()
        q = None
        result = {}
        try:
            q = OpenQuoteContext(host="127.0.0.1", port=11111)
            result = forecast_all(q)
            notes = execute(result, q, db, submit=args.paper)
            report(result, notes, db)
            print("\n".join(notes))
        except Exception as exc:
            report(result, ["BLOCKED: " + str(exc)], db)
            raise
        finally:
            if q is not None: q.close()
            db.close()

if __name__ == "__main__":
    main()

# Kronos + moomoo paper trading

This adapter connects Kronos-small forecasts to **moomoo SIMULATE accounts only**.
There is no real-money mode, trade-unlock call, or configurable trading environment.

## Data and decisions

- Fetch 512 completed US daily OHLCV candles through local OpenD; require at least
  400 consecutive exchange sessions. Use the NYSE calendar for holidays and early closes.
- Use QFQ-adjusted OHLC and broker volume/turnover. Store the exact inputs and hash.
- Predict the next session's close with Kronos-small, averaging eight sampled paths.
  The sample average is not a calibrated probability or demonstrated trading edge.
- Cache one immutable forecast per input session; deterministic seed per symbol/session.
- BUY experiment: previous close above EMA200(high), forecast return above +1%.
- SELL experiment: previous close below EMA200(close), or forecast return below -1%.
- Otherwise HOLD; reject forecasts outside a 20% sanity bound.
- Daily PB EMA only; weekly/monthly confirmation is not implemented.
- Universe: AAPL, AXP, KO, GOOGL, GOOG, BAC, OXY, CVX, CB, SPY.
  This is the existing research universe, not a recommendation list.

## Paper execution limits

These are deliberately small experimental defaults, not validated investment parameters.

- $1,000 virtual strategy budget, independent of the paper account's total balance.
- At most one share per instrument; $500 per buy; no shorts or borrowing.
- At most three submitted orders per exchange session; one attempt per symbol/session.
- DAY limit orders, regular session only, starting 15 minutes after open and
  ending 30 minutes before close. At most one submission per process run.
- Quotes must be no older than two minutes; price moves over 5% from input close block orders.
- A 2% daily strategy loss blocks new buys; this is not a guaranteed loss cap.
- Cash and fills are reconciled with the broker; accounting estimates 15 bps + $1 per order.
  These are experiment costs, not a representation of actual moomoo fees.
- Existing non-bot holdings are left alone. Changing bot holdings manually halts execution.
- Persist order intent before submission. Timeouts/crashes with unknown status halt
  automatic retries; reconcile against broker order history before resuming.
- File locking prevents simultaneous runs; account fingerprint prevents ledger reuse
  with a different simulated account.

## Install and run

Python 3.12, a signed-in moomoo OpenD at 127.0.0.1:11111, US quote permission,
and one US stock simulated account are required. Model weights download from Hugging Face.

From repository root:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r moomoo_integration/requirements.txt
.venv/bin/python -m unittest moomoo_integration.test_integration -v
.venv/bin/python -m moomoo_integration.runner          # preview, no orders
.venv/bin/python -m moomoo_integration.runner --paper  # SIMULATE only
```

The runtime directory defaults to a sibling `phase7/` directory, or
`KRONOS_STATE_DIR` when set. It holds the SQLite ledger, forecasts, original inputs,
`latest.json`, and `dashboard.html`. Keep it private; it is not part of the repository.

The installed Mac job checks every 15 minutes. Forecasts run once per completed
session; execution only runs inside the window above. The Mac must be awake,
logged in, online, and running OpenD. launchd cannot guarantee execution while asleep.

To stop future runs immediately, create `phase7/STOP`. Remove it to resume.
Stopping does not cancel an already submitted DAY order: inspect/cancel it in
moomoo Paper Trading. Never delete the ledger to clear a blocked submission.

The previous phase2–phase6 research and local shadow portfolios remain independent.
This adapter adds broker-visible simulated execution, which those jobs did not provide.

## Validation and limitations

21 unit/integration tests cover real-account rejection, ambiguous accounts, account
changes, closed sessions/holidays, incomplete data, invalid OHLC, forecast limits,
sizing, stale quotes, partial-fill accounting, no unowned sells, duplicate prevention,
STOP, and uncertain-submission handling.

A successful prospective forecast or simulated fill is not evidence of profitability.
No Kronos out-of-sample strategy backtest, performance guarantee, real execution,
weekly/monthly PB EMA, or Congress/Berkshire allocation blending is implemented.

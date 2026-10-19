"""Kronos + moomoo daily forecasts. Completed exchange sessions only."""
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import exchange_calendars as xc
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
STATE = Path(os.environ.get("KRONOS_STATE_DIR", str(ROOT.parent / "phase7")))
SYMBOLS = ["AAPL", "AXP", "KO", "GOOGL", "GOOG", "BAC", "OXY", "CVX", "CB", "SPY"]
MODEL = "NeoQuasar/Kronos-small"
TOKENIZER = "NeoQuasar/Kronos-Tokenizer-base"
CAL = xc.get_calendar("XNYS")
COLS = ["open", "high", "low", "close", "volume", "amount"]

def utcnow():
    return datetime.now(timezone.utc)

def session_dates(now):
    stamp = pd.Timestamp(now)
    days = CAL.sessions_in_range((stamp - pd.Timedelta(days=15)).date(),
                                 (stamp + pd.Timedelta(days=15)).date())
    completed = [s for s in days if CAL.session_close(s) + pd.Timedelta(minutes=15) <= stamp]
    last = completed[-1]
    return str(last.date()), str(CAL.next_session(last).date())

def validate_bars(df, last_day):
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["time_key"])
    df = df[df.timestamp.dt.strftime("%Y-%m-%d") <= last_day].sort_values("timestamp")
    if df.timestamp.duplicated().any():
        raise ValueError("Duplicate bar timestamps")
    df = df.tail(512).reset_index(drop=True)
    if len(df) < 400 or df.timestamp.iloc[-1].date().isoformat() != last_day:
        raise ValueError("Incomplete/stale history: need 400+ bars ending " + last_day)
    df["amount"] = df["turnover"]
    a = df[COLS].to_numpy(dtype=float)
    if not np.isfinite(a).all() or (a[:, :4] <= 0).any() or (a[:, 4:] < 0).any():
        raise ValueError("Nonfinite or invalid OHLCV")
    if ((df.high < df[["open", "close", "low"]].max(axis=1)) |
        (df.low > df[["open", "close", "high"]].min(axis=1))).any():
        raise ValueError("Inconsistent OHLC")
    expected = CAL.sessions_in_range(df.timestamp.iloc[0].date(), last_day)
    if list(df.timestamp.dt.strftime("%Y-%m-%d")) != [str(s.date()) for s in expected]:
        raise ValueError("Missing or non-session bars")
    return df

def signal(close, upper, lower, forecast):
    values = [close, upper, lower, forecast]
    if not all(np.isfinite(v) and v > 0 for v in values) or lower > upper:
        raise ValueError("Invalid forecast/band")
    ret = forecast / close - 1
    if abs(ret) > .20:
        return "BLOCKED", "Forecast move exceeds 20% sanity bound"
    if close < lower or ret < -.01:
        return "SELL", "Below daily PB EMA or forecast below -1%"
    if close > upper and ret > .01:
        return "BUY", "Above daily PB EMA and forecast above +1%"
    return "HOLD", "No entry/exit threshold crossed"

def atomic_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(obj, indent=2, allow_nan=False) + "\n")
    temp.replace(path)

def forecast_all(q, symbols=SYMBOLS):
    from moomoo import RET_OK, KLType, AuType
    now = utcnow()
    last, target = session_dates(now)
    path = STATE / "forecasts" / (last + ".json")
    # One committed forecast per completed session; never resample to chase a signal.
    if path.exists():
        saved = json.loads(path.read_text())
        if saved["symbols"] != symbols:
            raise RuntimeError("Cached universe mismatch")
        return saved
    from model import Kronos, KronosTokenizer, KronosPredictor
    import torch
    torch.set_num_threads(4)
    tokenizer = KronosTokenizer.from_pretrained(TOKENIZER)
    model = Kronos.from_pretrained(MODEL)
    model.eval()
    tokenizer.eval()
    predictor = KronosPredictor(model, tokenizer, device="cpu", max_context=512)
    result = {"created_utc": now.isoformat(), "last_session": last,
              "target_session": target, "symbols": symbols, "model": MODEL,
              "tokenizer": TOKENIZER, "sample_count": 8,
              "price_adjustment": "QFQ", "items": [],
              "note": "Experimental forecasts, not calibrated probabilities; paper only."}
    for symbol in symbols:
        try:
            ret, raw, cursor = q.request_history_kline(
                "US." + symbol, start=(now.date() - timedelta(days=900)).isoformat(),
                end=last, ktype=KLType.K_DAY, autype=AuType.QFQ, max_count=1000)
            if ret != RET_OK or cursor:
                raise RuntimeError("History unavailable or incomplete: " + str(raw)[:150])
            df = validate_bars(raw, last)
            seed = int(hashlib.sha256((symbol + last).encode()).hexdigest()[:8], 16)
            torch.manual_seed(seed)
            np.random.seed(seed)
            y = pd.Series(pd.to_datetime([target]))
            with torch.inference_mode():
                pred = predictor.predict(df[COLS], df.timestamp, y, 1,
                                         sample_count=8, verbose=False)
            close = float(df.close.iloc[-1])
            upper = float(df.high.ewm(span=200, adjust=False).mean().iloc[-1])
            lower = float(df.close.ewm(span=200, adjust=False).mean().iloc[-1])
            predicted = float(pred.close.iloc[0])
            action, reason = signal(close, upper, lower, predicted)
            item = {"symbol": symbol, "status": "OK", "action": action, "reason": reason,
                    "close": close, "forecast_close": predicted, "return": predicted/close-1,
                    "ema200_high": upper, "ema200_close": lower, "seed": seed,
                    "input_sha256": hashlib.sha256(df[COLS].to_csv(index=False).encode()).hexdigest()}
            data_path = STATE / "inputs" / last / (symbol + ".csv")
            data_path.parent.mkdir(parents=True, exist_ok=True)
            df[["timestamp"] + COLS].to_csv(data_path, index=False)
        except Exception as exc:
            item = {"symbol": symbol, "status": "ERROR", "action": "BLOCKED", "reason": str(exc)}
        result["items"].append(item)
        print(symbol, item["action"], item.get("return", item["reason"]), flush=True)
    atomic_json(path, result)
    return result

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from moomoo import RET_OK, TrdEnv
from moomoo_integration import runner as r
from moomoo_integration.forecast import signal, session_dates, validate_bars, CAL

NOW = datetime(2026, 10, 19, 15, 0, tzinfo=timezone.utc)

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(r, "STATE", Path(self.tmp.name))
        self.patch.start()
        self.db = r.setup()
    def tearDown(self):
        self.db.close()
        self.patch.stop()
        self.tmp.cleanup()
    def accounts(self, env="SIMULATE"):
        return pd.DataFrame([dict(acc_id=99, trd_env=env, sim_acc_type="STOCK")])
    def test_real_rejected(self):
        with self.assertRaises(RuntimeError): r.paper_account(self.accounts("REAL"))
    def test_ambiguous_rejected(self):
        with self.assertRaises(RuntimeError):
            r.paper_account(pd.concat([self.accounts(), self.accounts()]))
    def test_simulate_selected(self):
        self.assertEqual(r.paper_account(pd.concat([self.accounts("REAL"), self.accounts()])),99)
    def test_account_change_blocked(self):
        r.bind_account(self.db, 1)
        with self.assertRaises(RuntimeError): r.bind_account(self.db, 2)
    def test_closed_market(self):
        self.assertFalse(r.in_window("2026-10-19", datetime(2026,10,19,10,tzinfo=timezone.utc)))
        self.assertTrue(r.in_window("2026-10-19",NOW))
    def test_holiday_excluded(self):
        self.assertFalse(r.in_window("2026-12-25", datetime(2026,12,25,15,tzinfo=timezone.utc)))
    def test_weekend_and_incomplete_bar(self):
        self.assertEqual(session_dates(NOW),("2026-10-16","2026-10-19"))
    def test_signal_thresholds(self):
        self.assertEqual(signal(110,105,100,112)[0],"BUY")
        self.assertEqual(signal(99,105,100,102)[0],"SELL")
        self.assertEqual(signal(102,105,100,104)[0],"HOLD")
        self.assertEqual(signal(110,105,100,150)[0],"BLOCKED")
    def test_nonfinite(self):
        with self.assertRaises(ValueError):signal(100,100,99,float("nan"))
    def test_size_limits(self):
        self.assertEqual(r.order_size("BUY",100,0,1000,0),1)
        for price,owned,cash,exp in [(501,0,1000,0),(100,1,1000,0),(100,0,99,0),(100,0,1000,950)]:
            self.assertEqual(r.order_size("BUY",price,owned,cash,exp),0)
        self.assertEqual(r.order_size("SELL",100,0,0,0),0)
    def test_stale_quote(self):
        with self.assertRaises(RuntimeError):
            r.fresh_price(dict(last_price=100,update_time="2026-10-19 10:00:00"),NOW)
    def test_valid_history_and_missing_bar(self):
        sessions=CAL.sessions_in_range("2024-10-01","2026-10-16")
        df=pd.DataFrame(dict(time_key=[str(s.date()) for s in sessions],
                    open=100.,high=101.,low=99.,close=100.,volume=1000.,turnover=100000.))
        self.assertGreater(len(validate_bars(df,"2026-10-16")),400)
        with self.assertRaises(ValueError):validate_bars(df.drop(df.index[-5]),"2026-10-16")
    def test_invalid_ohlc(self):
        sessions=CAL.sessions_in_range("2024-10-01","2026-10-16")
        df=pd.DataFrame(dict(time_key=[str(s.date()) for s in sessions],
                    open=100.,high=90.,low=99.,close=100.,volume=1000.,turnover=100000.))
        with self.assertRaises(ValueError):validate_bars(df,"2026-10-16")
    def insert(self, side="BUY",qty=2):
        self.db.execute("INSERT INTO orders(key,day,code,side,qty,price,broker_id,status) VALUES('k','2026-10-19','US.AAPL',?,?,100,'o','SUBMITTED')",(side,qty))
        self.db.commit()
    def test_partial_fill_idempotent(self):
        self.insert()
        row=dict(dealt_qty=1,dealt_avg_price=100,order_status="FILLED_PART")
        r.apply_fill(self.db,"k",row)
        r.apply_fill(self.db,"k",row)
        self.assertEqual(self.db.execute("SELECT qty FROM positions").fetchone()[0],1)
        self.assertAlmostEqual(self.db.execute("SELECT value FROM cash").fetchone()[0],898.85)
        r.apply_fill(self.db,"k",dict(dealt_qty=2,dealt_avg_price=101,order_status="FILLED_ALL"))
        self.assertEqual(self.db.execute("SELECT qty FROM positions").fetchone()[0],2)
        self.assertAlmostEqual(self.db.execute("SELECT value FROM cash").fetchone()[0],796.697)
    def test_overfill_rejected(self):
        self.insert()
        with self.assertRaises(RuntimeError):
            r.apply_fill(self.db,"k",dict(dealt_qty=3,dealt_avg_price=100,order_status="FILLED_ALL"))
    def test_unknown_submission_no_retry(self):
        self.insert()
        self.db.execute("UPDATE orders SET broker_id=NULL")
        self.db.commit()
        with self.assertRaises(RuntimeError):r.reconcile(self.db,MagicMock(),99)
    def test_no_sell_unowned(self):
        self.insert("SELL",1)
        with self.assertRaises(RuntimeError):
            r.apply_fill(self.db,"k",dict(dealt_qty=1,dealt_avg_price=100,order_status="FILLED_ALL"))
    def fixtures(self):
        trade=MagicMock()
        trade.get_acc_list.return_value=(RET_OK,self.accounts())
        trade.position_list_query.return_value=(RET_OK,pd.DataFrame(columns=["code","qty"]))
        trade.accinfo_query.return_value=(RET_OK,pd.DataFrame([dict(cash=1000000)]))
        trade.place_order.return_value=(RET_OK,pd.DataFrame([dict(order_id="123")]))
        q=MagicMock()
        q.get_market_snapshot.return_value=(RET_OK,pd.DataFrame([
            dict(code="US."+s,last_price=100,update_time="2026-10-19 11:00:00") for s in r.SYMBOLS]))
        result=dict(target_session="2026-10-19",items=[dict(symbol="AAPL",action="BUY",status="OK",close=100)])
        return trade,q,result
    def test_preview_no_order(self):
        trade,q,result=self.fixtures()
        with patch.object(r,"OpenSecTradeContext",return_value=trade),patch.object(r,"utcnow",return_value=NOW):
            r.execute(result,q,self.db,False)
        trade.place_order.assert_not_called()
    def test_submit_only_simulate_and_no_duplicate(self):
        trade,q,result=self.fixtures()
        with patch.object(r,"OpenSecTradeContext",return_value=trade),patch.object(r,"utcnow",return_value=NOW):
            r.execute(result,q,self.db,True)
            trade.order_list_query.return_value=(RET_OK,pd.DataFrame([dict(dealt_qty=1,dealt_avg_price=100,order_status="FILLED_ALL")]))
            trade.position_list_query.return_value=(RET_OK,pd.DataFrame([dict(code="US.AAPL",qty=1)]))
            r.execute(result,q,self.db,True)
        self.assertEqual(trade.place_order.call_count,1)
        for method in [trade.place_order,trade.position_list_query,trade.accinfo_query,trade.order_list_query]:
            for call in method.call_args_list:
                self.assertEqual(call.kwargs["trd_env"],TrdEnv.SIMULATE)
                self.assertEqual(call.kwargs["acc_id"],99)
    def test_stop_blocks_order(self):
        trade,q,result=self.fixtures()
        (r.STATE/"STOP").touch()
        with patch.object(r,"OpenSecTradeContext",return_value=trade),patch.object(r,"utcnow",return_value=NOW):
            with self.assertRaises(RuntimeError):r.execute(result,q,self.db,True)
        trade.place_order.assert_not_called()
    def test_broker_failure_persists_intent(self):
        trade,q,result=self.fixtures()
        trade.place_order.return_value=(-1,"timeout")
        with patch.object(r,"OpenSecTradeContext",return_value=trade),patch.object(r,"utcnow",return_value=NOW):
            with self.assertRaises(RuntimeError):r.execute(result,q,self.db,True)
            with self.assertRaises(RuntimeError):r.execute(result,q,self.db,True)
        self.assertEqual(trade.place_order.call_count,1)

if __name__=="__main__": unittest.main()

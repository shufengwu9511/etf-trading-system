"""拉取回测所需完整历史数据"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tushare as ts
from db import get_connection
from config import TUSHARE_TOKEN

ts.set_token(TUSHARE_TOKEN)
pro = ts.pro_api()

conn = get_connection()
START = "20240501"
END = "20260428"

# 宽基用 index_daily, 行业用 fund_daily
CORE_CODES = ["000300.SH", "000905.SH", "000688.SH"]
SAT_CODES = ["159695.SZ", "159558.SZ", "562970.SH", "159566.SZ",
             "159530.SZ", "516080.SH", "563020.SH", "159934.SZ",
             "159546.SZ", "159755.SZ", "159206.SZ", "516510.SH", "512980.SH",
             "562910.SH"]

print("=" * 60)
print("拉取回测历史数据")
print(f"范围: {START} ~ {END}")
print("=" * 60)

# 1. 宽基行情 + 估值
print("\n[1/3] 宽基ETF行情...")
for code in CORE_CODES:
    df = pro.index_daily(ts_code=code, start_date=START, end_date=END)
    if df is not None and len(df) > 0:
        for _, row in df.iterrows():
            conn.execute("INSERT OR IGNORE INTO index_daily (ts_code,trade_date,open,high,low,close,vol,amount,pct_chg) VALUES (?,?,?,?,?,?,?,?,?)",
                        (row["ts_code"], row["trade_date"], row["open"], row["high"], row["low"], row["close"], row["vol"], row["amount"], row["pct_chg"]))
        conn.commit()
        cnt = conn.execute("SELECT COUNT(*) FROM index_daily WHERE ts_code=?", (code,)).fetchone()[0]
        print(f"  {code}: {len(df)}条, 总计{cnt}条")

print("\n[2/3] 宽基估值...")
for code in CORE_CODES:
    try:
        df = pro.index_dailybasic(ts_code=code, start_date=START, end_date=END, fields="ts_code,trade_date,pe_ttm,pb")
        if df is not None and len(df) > 0:
            for _, row in df.iterrows():
                conn.execute("INSERT OR IGNORE INTO index_valuation (ts_code,trade_date,pe_ttm,pb) VALUES (?,?,?,?)",
                            (row["ts_code"], row["trade_date"], row.get("pe_ttm"), row.get("pb")))
            conn.commit()
            cnt = conn.execute("SELECT COUNT(*) FROM index_valuation WHERE ts_code=?", (code,)).fetchone()[0]
            print(f"  {code}: {len(df)}条, 总计{cnt}条")
    except Exception as e:
        print(f"  {code}: {e}")

# 3. 行业ETF (用 fund_daily)
print("\n[3/3] 行业ETF行情(fund_daily)...")
for code in SAT_CODES:
    try:
        df = pro.fund_daily(ts_code=code, start_date=START, end_date=END)
        if df is not None and len(df) > 0:
            for _, row in df.iterrows():
                conn.execute("INSERT OR IGNORE INTO index_daily (ts_code,trade_date,open,high,low,close,vol,amount,pct_chg) VALUES (?,?,?,?,?,?,?,?,?)",
                            (row["ts_code"], row["trade_date"], row["open"], row["high"], row["low"], row["close"], row["vol"], row["amount"], row["pct_chg"]))
            conn.commit()
            cnt = conn.execute("SELECT COUNT(*) FROM index_daily WHERE ts_code=?", (code,)).fetchone()[0]
            print(f"  {code}: {len(df)}条, 总计{cnt}条")
        else:
            print(f"  {code}: 无数据(未上市)")
    except Exception as e:
        print(f"  {code}: {e}")

conn.close()
print("\n完成!")

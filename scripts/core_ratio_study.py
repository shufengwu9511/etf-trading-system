# -*- coding: utf-8 -*-
"""宽基配比 / 标的选择 测算脚本（一次性分析用，不接入主流程）

思路:
  1) 动量腿: 把回测引擎的宽基清空(CORE_ETFS=[])、卫星比例设 100%,
     跑出"纯动量轮动"的每日净值曲线(无信号时留现金)。
  2) 宽基腿: 用 kline.db 的前复权 ETF 日线(含分红再投)构造固定权重组合,
     月度再平衡。
  3) 按 w 权重把两条腿拼成组合, 比较年化/波动/最大回撤/Sharpe/Calmar。

注意: 引擎的 index_daily 里宽基指数历史不全(000016/000688/000905 缺失),
      所以宽基腿必须用 kline.db 的 ETF 前复权价, 不能用引擎口径。
"""
import os
import sys
import io
import json
import contextlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np

import backtest
import tdx_data

START = "20240506"
END = "20261103"
CAPITAL = backtest.TOTAL_CAPITAL


def get_satellite_equity():
    """跑纯动量腿, 返回 {date: value} 每日净值"""
    prices, pe_data, trade_dates = backtest.load_all_data()

    orig_core = backtest.CORE_ETFS
    orig_core_ratio = backtest.CORE_RATIO
    orig_sat_ratio = backtest.SATELLITE_RATIO
    try:
        backtest.CORE_ETFS = []          # 清空宽基 -> 纯动量
        backtest.CORE_RATIO = 0.0
        backtest.SATELLITE_RATIO = 1.0
        eng = backtest.BacktestEngine(prices, pe_data, trade_dates, START, END)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            eng.run()
        return {dv["date"]: dv["value"] for dv in eng.daily_values}
    finally:
        backtest.CORE_ETFS = orig_core
        backtest.CORE_RATIO = orig_core_ratio
        backtest.SATELLITE_RATIO = orig_sat_ratio


def load_px(code):
    """从 kline.db 取前复权收盘价, 返回 Series(index=YYYYMMDD)"""
    df = tdx_data.get_etf_kline(code, days=3000)
    if df is None or df.empty:
        return pd.Series(dtype=float)
    s = df.set_index(df["trade_date"].astype(str))["close"].astype(float)
    return s.sort_index()


def core_equity(weights, dates):
    """固定权重宽基组合净值(月度再平衡, 起点=1.0)"""
    px = {c: load_px(c) for c in weights}
    df = pd.DataFrame(px).reindex(dates).ffill().dropna()
    if df.empty:
        return pd.Series(dtype=float)
    rets = df.pct_change().fillna(0.0)
    w = pd.Series(weights)
    w = w / w.sum()

    # 月度再平衡: 每个自然月第一天把权重拉回目标
    months = pd.Series(df.index, index=df.index).str[:6]
    vals, cur = [], 1.0
    cur_w = w.copy()
    prev_month = None
    for d in df.index:
        m = d[:6]
        if prev_month is not None and m != prev_month:
            cur_w = w.copy()          # 再平衡
        r = float((cur_w * rets.loc[d]).sum())
        cur *= (1 + r)
        # 权重自然漂移
        grown = cur_w * (1 + rets.loc[d])
        s = grown.sum()
        if s > 0:
            cur_w = grown / s
        vals.append(cur)
        prev_month = m
    return pd.Series(vals, index=df.index)


def metrics(eq, label):
    r = eq.pct_change().dropna()
    if len(r) < 20:
        return None
    days = (pd.to_datetime(eq.index[-1]) - pd.to_datetime(eq.index[0])).days
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (365 / max(days, 1)) - 1
    vol = r.std() * np.sqrt(252)
    cum = eq / eq.cummax()
    mdd = (cum - 1).min()
    sharpe = (cagr - 0.015) / vol if vol > 0 else 0
    calmar = cagr / abs(mdd) if mdd < 0 else 0
    return {
        "label": label,
        "days": days,
        "cagr": cagr * 100,
        "vol": vol * 100,
        "mdd": mdd * 100,
        "sharpe": sharpe,
        "calmar": calmar,
        "total": (eq.iloc[-1] / eq.iloc[0] - 1) * 100,
    }


def main():
    print("=" * 78)
    print("  一、纯动量腿（宽基清空，卫星 100%）")
    print("=" * 78)
    sat = get_satellite_equity()
    sat_s = pd.Series(sat).sort_index()
    print(f"  交易日 {len(sat_s)} 天  {sat_s.index[0]} ~ {sat_s.index[-1]}")
    m = metrics(sat_s, "纯动量")
    print("  年化 %+.2f%%  波动 %.2f%%  回撤 %.2f%%  Sharpe %.2f  Calmar %.2f" %
          (m["cagr"], m["vol"], m["mdd"], m["sharpe"], m["calmar"]))

    dates = list(sat_s.index)

    print()
    print("=" * 78)
    print("  二、宽基 15% 插槽：候选标的选择（其余 300 25% + 500 20% 固定）")
    print("=" * 78)
    variants = {
        "现行 科创50 15%": {"510310.SH": 0.25, "510580.SH": 0.20, "588080.SH": 0.15},
        "换 上证50 15%": {"510310.SH": 0.25, "510580.SH": 0.20, "510100.SH": 0.15},
        "上证50 10% + 500 25%": {"510310.SH": 0.25, "510580.SH": 0.25, "510100.SH": 0.10},
        "换 上证180 15%": {"510310.SH": 0.25, "510580.SH": 0.20, "510180.SH": 0.15},
        "换 中证1000 15%": {"510310.SH": 0.25, "510580.SH": 0.20, "159633.SZ": 0.15},
        "仅 300+500 (30/30)": {"510310.SH": 0.30, "510580.SH": 0.30},
    }
    rows = []
    for name, w in variants.items():
        eq = core_equity(w, dates)
        if eq.empty:
            print(f"  {name}: 数据不足")
            continue
        mm = metrics(eq, name)
        mm["n"] = len(eq)
        rows.append(mm)
        print("  %-24s 年化 %+6.2f%%  波动 %5.2f%%  回撤 %6.2f%%  Sharpe %5.2f  Calmar %5.2f  (样本%d)" %
              (name, mm["cagr"], mm["vol"], mm["mdd"], mm["sharpe"], mm["calmar"], mm["n"]))

    print()
    print("=" * 78)
    print("  三、宽基 : 动量 配比测算")
    print("=" * 78)
    core_def = {"510310.SH": 0.25, "510580.SH": 0.20, "510100.SH": 0.15}
    core_eq = core_equity(core_def, dates)
    print(f"  宽基腿(300/500/50) {len(core_eq)} 天  动量腿 {len(sat_s)} 天")
    idx = core_eq.index.intersection(sat_s.index)
    c = core_eq.reindex(idx).ffill()
    s = sat_s.reindex(idx).ffill()
    print(f"  对齐后 {len(idx)} 天: {idx[0]} ~ {idx[-1]}")
    print()
    print("  %-8s %10s %8s %8s %8s %8s %12s" %
          ("宽基占比", "年化", "波动", "最大回撤", "Sharpe", "Calmar", "期末(200万起)"))
    results = []
    for w in [0.60, 0.50, 0.40, 0.30, 0.20, 0.10, 0.00]:
        blend = (w * c / c.iloc[0] + (1 - w) * s / s.iloc[0])
        mm = metrics(blend, f"{int(w*100)}/{int((1-w)*100)}")
        results.append({"w_core": w, **mm})
        print("  %-8s %+9.2f%% %7.2f%% %7.2f%% %8.2f %8.2f %12s" %
              (f"{int(w*100)}/{int((1-w)*100)}", mm["cagr"], mm["vol"], mm["mdd"],
               mm["sharpe"], mm["calmar"], "¥%s" % format(round(CAPITAL * blend.iloc[-1] / blend.iloc[0]), ",")))

    out = {"satellite": m, "slot_variants": rows, "ratio_sweep": results,
           "window": [idx[0], idx[-1]], "days": len(idx)}
    os.makedirs("logs", exist_ok=True)
    with open("logs/core_ratio_study.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\n  结果已存 logs/core_ratio_study.json")


if __name__ == "__main__":
    main()

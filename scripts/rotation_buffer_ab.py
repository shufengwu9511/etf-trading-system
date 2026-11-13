#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
排名缓冲区 + 超额持仓 A/B 回测 (2026-11-09)

  新口径: 卖出按 Top(3+ROTATION_RANK_BUFFER)=Top5 缓冲区判定,
          持仓仍在Top5内且动量>0、MA20上方则继续持有;
          新建仓允许超过基础3只, 上限 ROTATION_MAX_HOLD_COUNT=5,
          前提是所有现有卫星持仓都还健康。
  旧口径: 跌出Top3即卖, 持仓恒为3只 (BT_ROTATION_BUFFER=0)。

两组都按实盘单笔上限 SINGLE_ETF_MAX_RATIO 执行, 保证口径可比。
用法: .venv/Scripts/python.exe scripts/rotation_buffer_ab.py
"""
import sys
import os
import io
import re
import contextlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import backtest
from config import SINGLE_ETF_MAX_RATIO, ROTATION_HOLD_COUNT, ROTATION_RANK_BUFFER, ROTATION_MAX_HOLD_COUNT

START = "20240506"
END = "20261106"

KEYS = [
    ("总收益率", r"总收益率:\s*([+-]?[\d.]+)%"),
    ("年化收益率", r"年化收益率:\s*([+-]?[\d.]+)%"),
    ("最大回撤", r"最大回撤:\s*([+-]?[\d.]+)%"),
    ("交易次数", r"交易次数:\s*(\d+)"),
    ("卖出胜率", r"卖出胜率:\s*([\d.]+)%"),
    ("盈亏比", r"盈亏比:\s*([\d.]+)"),
]


def run(flag, prices, pe, td, st):
    backtest.BT_ROTATION_BUFFER = flag
    backtest.BT_SINGLE_MAX_RATIO = SINGLE_ETF_MAX_RATIO
    eng = backtest.BacktestEngine(prices, pe, td, st, END)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rep = eng.run()
    out = {}
    for k, pat in KEYS:
        m = re.search(pat, rep)
        out[k] = m.group(1) if m else "--"
    valid = [d for d in eng.daily_values if d["value"] > 0]
    out["平均现金占比"] = f"{sum(d['cash'] / d['value'] for d in valid) / len(valid) * 100:.1f}"
    out["期末卫星只数"] = str(len(eng.sat_holdings))
    return out


def main():
    prices, pe, td = backtest.load_all_data()
    st = next(d for d in td if d >= START)
    old = run(False, prices, pe, td, st)
    new = run(True, prices, pe, td, st)

    print()
    print("=" * 82)
    print(f"  排名缓冲区 A/B 回测   {st} ~ {END}   (单笔上限 {SINGLE_ETF_MAX_RATIO:.0%})")
    print(f"  新口径: Top{ROTATION_HOLD_COUNT}买入 / Top{ROTATION_HOLD_COUNT + ROTATION_RANK_BUFFER}缓冲区卖出 / 上限{ROTATION_MAX_HOLD_COUNT}只")
    print("=" * 82)
    print(f"{'指标':<16}{'旧口径(跌出即卖)':>20}{'新口径(缓冲+超额)':>20}{'变化':>16}")
    print("-" * 82)
    for k, _ in KEYS:
        a, b = old[k], new[k]
        try:
            delta = f"{float(b) - float(a):+.2f}"
        except ValueError:
            delta = "-"
        unit = "%" if k in ("总收益率", "年化收益率", "卖出胜率") else ""
        print(f"{k:<16}{a + unit:>20}{b + unit:>20}{delta:>16}")
    for k in ("平均现金占比", "期末卫星只数"):
        print(f"{k:<16}{old[k]:>20}{new[k]:>20}{'-':>16}")
    print("=" * 82)
    print("注: 样本 2.5 年含单边牛市, 缓冲区的真实价值在于换手与边界抖动, 不在绝对收益")


if __name__ == "__main__":
    main()

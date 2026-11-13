# -*- coding: utf-8 -*-
"""轮动周期 14天 vs 7天 对照回测 (2026-11-05)

用户 2026-11-05 决定取消"跌出Top3需持有满14天"的缓冲, 改为过赎回费锁(7天)
即可卖出。本脚本用同一套数据跑两种参数, 量化这条规则改动的影响。

用法: .venv/Scripts/python.exe scripts/rotation_cycle_ab.py
"""
import io
import os
import re
import sys
import contextlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backtest


KEYS = [
    ("总收益率", r"总收益率[:：]\s*([+-]?[\d.]+)%"),
    ("年化收益率", r"年化收益率[:：]\s*([+-]?[\d.]+)%"),
    ("最大回撤", r"最大回撤[:：]\s*([+-]?[\d.]+)%"),
    ("盈亏比", r"盈亏比[:：]\s*([\d.]+)"),
    ("胜率", r"胜率[:：]\s*([\d.]+)%"),
    ("交易笔数", r"交易笔数[:：]\s*(\d+)"),
    ("夏普", r"夏普比率[:：]\s*([+-]?[\d.]+)"),
]


def parse(report: str) -> dict:
    out = {}
    for name, pat in KEYS:
        m = re.search(pat, report)
        out[name] = m.group(1) if m else "-"
    return out


def main():
    print("加载数据...")
    prices, pe_data, trade_dates = backtest.load_all_data()
    start, end = "20240506", "20260428"
    actual_start = next((d for d in trade_dates if d >= start), None)
    if not actual_start:
        print("无可用数据")
        return

    rows = []
    for cycle in (14, 7):
        backtest.ROTATION_CYCLE_DAYS = cycle
        engine = backtest.BacktestEngine(prices, pe_data, trade_dates, actual_start, end)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            report = engine.run()
        rows.append((cycle, parse(report)))
        print(f"[完成] 轮动周期 {cycle} 天")

    print()
    print("=" * 74)
    print("  轮动周期对照回测 (2024-05-06 ~ 2026-04-28)")
    print("=" * 74)
    hdr = f"{'指标':<12}" + "".join(f"{str(c) + '天':>14}" for c, _ in rows)
    print(hdr)
    print("-" * 74)
    for name, _ in KEYS:
        line = f"{name:<12}"
        for _, d in rows:
            line += f"{d[name]:>14}"
        print(line)
    print("=" * 74)


if __name__ == "__main__":
    main()

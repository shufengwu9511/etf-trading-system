# -*- coding: utf-8 -*-
"""卫星单笔额度方案回测 (2026-11-05)

背景:
  实盘 strategy.calc_position_sizes 用 SINGLE_ETF_MAX_RATIO=8% 约束单笔买入,
  但 backtest.py 原先没有这个约束 —— 直接按 sat_amount/ROTATION_HOLD_COUNT 满配,
  60%卫星+3只 时假设每只40万, 而实盘单笔封顶16万。回测系统性高估收益。

本脚本把 SINGLE_ETF_MAX_RATIO 接进回测(BT_SINGLE_MAX_RATIO), 在真实执行口径下
对比几种方案:

  ① 40/60 无约束      = 之前的测算口径(每只40万), 作校验基准
  ② 40/60 单笔 8%     = 实盘现状(每只16万, 卫星容量48万)
  ③ 40/60 单笔 12%    = 折中(每只24万, 容量72万)
  ④ 40/60 单笔 20%    = 理论上满配(每只40万, 应与①等价)
  ⑤ 40/60 单笔 8% + 持5只 = 靠分散提容量(每只16万, 容量80万)
  ⑥ 60/40 无约束      = 上一版配比(校验 +19.51%)
  ⑦ 60/40 单笔 8%     = 退回宽基为主 + 实盘约束

用法: .venv/Scripts/python.exe scripts/satellite_quota_study.py
"""
import os
import re
import sys
import io
import contextlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backtest

START = "20240506"
END = "20261103"

KEYS = [
    ("总收益率", r"总收益率[:：]\s*([+-]?[\d.]+)%"),
    ("年化收益率", r"年化收益率[:：]\s*([+-]?[\d.]+)%"),
    ("最大回撤", r"最大回撤[:：]\s*([+-]?[\d.]+)%"),
    ("盈亏比", r"盈亏比[:：]\s*([\d.]+)"),
    ("胜率", r"胜率[:：]\s*([\d.]+)%"),
    ("交易次数", r"交易次数[:：]\s*(\d+)"),
    ("平均盈利", r"平均盈利[:：]\s*([+-]?[\d.]+)%"),
    ("平均亏损", r"平均亏损[:：]\s*([+-]?[\d.]+)%"),
]

# (label, core_ratio, sat_ratio, single_max, hold_count)
# 卫星"实际可部署比例" = min(sat_ratio, single_max * hold_count)
#   剩余部分会长期躺在货币基金里, 所以 core+sat 未必等于 100% 的实际风险敞口
CASES = [
    ("① 40/60 无约束", 0.40, 0.60, None, 3),
    ("② 40/60 单笔8%", 0.40, 0.60, 0.08, 3),
    ("③ 40/60 单笔12%", 0.40, 0.60, 0.12, 3),
    ("④ 40/60 单笔20%", 0.40, 0.60, 0.20, 3),
    ("⑤ 40/60 单笔8%/5只", 0.40, 0.60, 0.08, 5),
    ("⑥ 60/40 无约束", 0.60, 0.40, None, 3),
    ("⑦ 60/40 单笔8%", 0.60, 0.40, 0.08, 3),
    # 以下: 让 sat_ratio = single_max * hold_count, 把额度真正用满
    ("⑧ 76/24 单笔8%", 0.76, 0.24, 0.08, 3),
    ("⑨ 64/36 单笔12%", 0.64, 0.36, 0.12, 3),
    ("⑩ 55/45 单笔15%", 0.55, 0.45, 0.15, 3),
    ("⑪ 52/48 单笔8%/6只", 0.52, 0.48, 0.08, 6),
]


def parse(report):
    out = {}
    for name, pat in KEYS:
        m = re.search(pat, report)
        out[name] = m.group(1) if m else "-"
    return out


def main():
    print("加载数据...")
    prices, pe_data, trade_dates = backtest.load_all_data()
    actual_start = next((d for d in trade_dates if d >= START), None)
    if not actual_start:
        print("无可用数据")
        return

    orig = {
        "CORE_ETFS": backtest.CORE_ETFS,
        "CORE_RATIO": backtest.CORE_RATIO,
        "SATELLITE_RATIO": backtest.SATELLITE_RATIO,
        "ROTATION_HOLD_COUNT": backtest.ROTATION_HOLD_COUNT,
        "BT_SINGLE_MAX_RATIO": backtest.BT_SINGLE_MAX_RATIO,
    }
    # CORE_ETFS 权重是 40% 口径的, 跑 60% 宽基时需整体放大 1.5 倍
    base_core = orig["CORE_ETFS"]
    core_w_sum = sum(e["target_weight"] for e in base_core)

    rows = []
    try:
        for label, core_r, sat_r, single_max, hold_n in CASES:
            scale = core_r / core_w_sum
            backtest.CORE_ETFS = [{**e, "target_weight": e["target_weight"] * scale}
                                  for e in base_core]
            backtest.CORE_RATIO = core_r
            backtest.SATELLITE_RATIO = sat_r
            backtest.ROTATION_HOLD_COUNT = hold_n
            backtest.BT_SINGLE_MAX_RATIO = single_max

            eng = backtest.BacktestEngine(prices, pe_data, trade_dates, actual_start, END)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                report = eng.run()

            d = parse(report)
            # 平均闲置现金占比 (现金 / 总权益)
            cash_ratios = [dv["cash"] / dv["value"] for dv in eng.daily_values
                           if dv["value"] > 0]
            d["平均现金占比"] = f"{sum(cash_ratios) / len(cash_ratios) * 100:.1f}"
            # 年化波动 / Sharpe / Calmar (报告里没有, 自算)
            vals = [dv["value"] for dv in eng.daily_values]
            rets = [vals[i] / vals[i - 1] - 1 for i in range(1, len(vals)) if vals[i - 1] > 0]
            mean_r = sum(rets) / len(rets)
            var = sum((r - mean_r) ** 2 for r in rets) / (len(rets) - 1)
            ann_vol = (var ** 0.5) * (252 ** 0.5)
            ann_ret = float(d["年化收益率"]) / 100
            mdd = abs(float(d["最大回撤"])) / 100
            d["年化波动"] = f"{ann_vol * 100:.1f}"
            d["夏普"] = f"{(mean_r * 252 - 0.02) / ann_vol:.2f}" if ann_vol > 0 else "-"
            d["Calmar"] = f"{ann_ret / mdd:.2f}" if mdd > 0 else "-"
            # 引擎实际使用的单只卫星金额
            per = sat_r * backtest.TOTAL_CAPITAL / hold_n
            if single_max:
                per = min(per, backtest.TOTAL_CAPITAL * single_max)
            d["单只卫星"] = f"{per / 10000:.0f}万"
            d["卫星容量"] = f"{per * hold_n / 10000:.0f}万"
            rows.append((label, d))
            print(f"[完成] {label}  年化 {d['年化收益率']}%  现金均值 {d['平均现金占比']}%")
    finally:
        for k, v in orig.items():
            setattr(backtest, k, v)

    KEYS_ALL = ([k for k, _ in KEYS] + ["年化波动", "夏普", "Calmar"]
                + ["单只卫星", "卫星容量", "平均现金占比"])
    PCT = {"总收益率", "年化收益率", "最大回撤", "胜率", "平均现金占比",
           "平均盈利", "平均亏损", "年化波动"}

    print()
    print("=" * 118)
    print(f"  卫星单笔额度方案回测  {actual_start} ~ {END}")
    print("=" * 118)
    print(f"{'指标':<12}" + "".join(f"{lab:>14}" for lab, _ in rows))
    print("-" * 118)
    for name in KEYS_ALL:
        line = f"{name:<12}"
        for _, d in rows:
            unit = "%" if name in PCT else ""
            line += f"{d[name] + unit:>14}"
        print(line)
    print("=" * 118)
    print("注: 单只卫星/卫星容量按 TOTAL_CAPITAL=200万 计算; 现金均值=回测期平均闲置现金占权益比")


if __name__ == "__main__":
    main()

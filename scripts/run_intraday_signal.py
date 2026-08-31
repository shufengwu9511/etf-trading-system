#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
========================================================================
  盘中信号脚本 — 用盘中现价代理"今日收盘价", 15:00前提交申赎
  (核心逻辑在 intraday_signal.py, 本脚本只负责命令行与输出)
========================================================================

背景痛点:
  场外ETF联接基金的申赎规则是"15:00前提交 → 按当日收盘净值成交"。
  而原有的每日信号 (main.py) 依赖当日收盘价, 只能在收盘后计算,
  提交申赎时实际成交的是 T+1 的未知净值 —— 存在一个时间差。

  本脚本利用所有决策规则 (动量/MA20/Top3) 都只依赖"当日收盘价"
  的性质: 在 14:40-14:50 用场内ETF的盘中现价当作今日收盘价的代理,
  算一遍信号并立刻在15:00前提交申赎, 即可做到:

      当日决策 → 当日成交(按当日收盘净值) → 消除T+1时间差

  盘中现价对当日收盘的预测误差通常 <0.3%, 对动量/MA20/排名这类
  慢变量完全够用 (这正是公募平台"盘中预估净值"的做法)。

用法:
  python scripts/run_intraday_signal.py
      # 查看盘中信号 (只显示不记账; 持仓状态每天从主系统数据库读取)

  python scripts/run_intraday_signal.py --holdings "021893:20260813,019666:20260820"
      # 可选: 数据库无持仓时手动声明 (基金代码:买入日期YYYYMMDD, 逗号分隔)

  python scripts/run_intraday_signal.py --dry-run
      # 兼容旧习惯保留, 脚本本身不记账, 该参数无实际影响

注意:
  1. 必须在 14:40-14:50 运行, 14:50 前提交才有意义; 收盘后跑只是重复昨日结论
  2. 若某只ETF处于临界 (排名3/4交替、MA20贴线), 建议当天不动等次日确认
  3. 7天最短持有锁: 持有<7天的持仓不触发赎回, 避免1.5%惩罚赎回费
  4. 持仓状态每天从主系统数据库(portfolio表)读取; 脚本只显示信号不记账,
     实际申赎请自行在持仓系统(main.py)中维护
  5. 同一份信号已集成到主看板 (logs/dashboard.html), 更新看板时自动计算
========================================================================
"""
import sys
import os
import io
import argparse
from datetime import datetime

# 修复 Windows 控制台编码问题
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from config import ROTATION_HOLD_COUNT
from intraday_signal import (
    RECOMMEND_START, RECOMMEND_END,
    init_holdings_state,
    generate_signals,
)


def main():
    parser = argparse.ArgumentParser(description="盘中信号脚本: 现价代理收盘价, 当日决策当日成交")
    parser.add_argument("--dry-run", action="store_true",
                        help="兼容旧习惯保留, 脚本本身不记账, 该参数无实际影响")
    parser.add_argument("--holdings", type=str, default=None,
                        help='首次声明真实持仓, 格式 "基金代码:买入日期YYYYMMDD,..." 如 "021893:20260813,019666:20260820"')
    parser.add_argument("--codes", type=str, default=None,
                        help='仅计算指定ETF (逗号分隔的场内代码), 默认全部卫星池')
    args = parser.parse_args()

    today = datetime.now()
    now_time = today.time()

    print("")
    print("╔" + "═" * 58 + "╗")
    print("║   盘中信号脚本 - 现价代理收盘价                          ║")
    print(f"║   运行时间: {today.strftime('%Y-%m-%d %H:%M:%S')}                      ║")
    print("╚" + "═" * 58 + "╝")

    # 时段提醒
    if now_time < RECOMMEND_START or now_time > RECOMMEND_END:
        print(f"\n  ⚠️ 当前不在建议运行时段 ({RECOMMEND_START.strftime('%H:%M')}-{RECOMMEND_END.strftime('%H:%M')})")
        print("     建议 14:40-14:50 运行, 14:50 前提交申赎才能按当日净值成交")
    if today.weekday() >= 5:
        print("  ⚠️ 今天是周末, 实时行情为上周五收盘价, 信号仅供预览")
    print("")

    # 计算信号 (核心逻辑在 intraday_signal.generate_signals)
    print("  [1/4] 获取盘中实时行情...")
    result = generate_signals(codes=args.codes, holdings_arg=args.holdings)

    if not result.get("ok"):
        print(f"  [ERROR] {result.get('error', '未知错误')}")
        return
    rows = result["rankings"]
    print(f"       获取 {len(rows)} 只ETF实时行情")

    print("  [2/4] 计算动量排名 (盘中现价代理收盘价)...")
    print("  [3/4] 加载持仓状态...")
    state = init_holdings_state(args.holdings)
    holdings = state.get("holdings", {})
    print(f"       {result.get('holdings_source', '')}, 共{len(holdings)}只持仓")
    for h in holdings.values():
        print(f"         - {h['name']} ({h['fund_code']}) 买入日期 {h['buy_date']}")

    print("  [4/4] 生成买卖信号...")

    buy_signals = result["buy"]
    sell_signals = result["sell"]
    hold_signals = result["hold"]

    # 输出
    print("")
    print("=" * 66)
    print(f"  动量排名 (盘中现价, 现价误差通常<0.3%)")
    print("=" * 66)
    for r in rows:
        ma_flag = "MA20上方" if r["above_ma20"] else "MA20下方"
        star = "★" if r["rank"] <= ROTATION_HOLD_COUNT else " "
        print(f"  {star} #{r['rank']:<2d} {r['name']:<10s} ({r['fund_code']})"
              f"  现价{r['price']:.4f} ({r['change_pct']:+.2f}%)"
              f"  10日:{r['momentum_10d']:+.2f}%  30日:{r['momentum_30d']:+.2f}%"
              f"  综合:{r['composite_score']:+.2f}%  {ma_flag}")

    print("")
    print("=" * 66)
    print("  建议操作 (请在14:50前提交申赎)")
    print("=" * 66)
    if buy_signals:
        print(f"  🟢 申购:")
        for s in buy_signals:
            print(f"     → {s['name']} ({s['fund_code']})")
            print(f"        排名第{s['rank']}, 综合得分{s['composite_score']:+.2f}%, MA20上方")
    else:
        print("  🟢 申购: 无")
    if sell_signals:
        print(f"  🔴 赎回:")
        for s in sell_signals:
            print(f"     → {s['name']} ({s['fund_code']})")
            print(f"        排名第{s['rank']}, 持有{s['hold_days']}天 (已过7天锁), 跌出Top{ROTATION_HOLD_COUNT}")
    else:
        print("  🔴 赎回: 无")
    if hold_signals:
        print(f"  ⚪ 持有:")
        for s in hold_signals:
            print(f"     → {s['name']} ({s['fund_code']})  {s['reason']}")

    # 只显示信号, 不修改任何状态 (持仓以主系统数据库为准)
    print("")
    print("  [INFO] 脚本只显示信号, 不自动记账; 实际申赎请在持仓系统(main.py)中维护")
    print("  [INFO] 同一份信号已集成到主看板 logs/dashboard.html, 更新看板时自动计算")

    # 收尾提示
    print("")
    print("=" * 66)
    print("  ⚠️ 执行前请确认:")
    print("  □ 现价≠收盘价 (误差<0.3%), 临界信号建议次日确认")
    print("  □ 已持有>7天 (避免1.5%惩罚赎回费)")
    print("  □ 14:30后提交按当日净值成交, 15:00后提交按次日净值")
    print("=" * 66)


if __name__ == "__main__":
    main()

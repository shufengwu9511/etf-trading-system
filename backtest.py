# ============================================================
# ETF联接基金交易系统 - 回测引擎
# 回测策略: 宽基PE择时 + 行业动量轮动 + 止盈止损
# ============================================================
import sys
import os
import io

# 修复 Windows 控制台编码问题 (仅在作为主脚本时执行, 避免模块导入时搞坏 IO)
if sys.platform == "win32" and __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sqlite3
from datetime import datetime, timedelta
from collections import defaultdict

from config import (
    TOTAL_CAPITAL, CORE_RATIO, SATELLITE_RATIO,
    CORE_ETFS, SATELLITE_ETFS,
    PE_LOOKBACK_YEARS, PE_BUY_THRESHOLD, PE_SELL_THRESHOLD, MA_PERIOD,
    MOMENTUM_SHORT, MOMENTUM_MEDIUM, MOMENTUM_WEIGHT_SHORT, MOMENTUM_WEIGHT_MEDIUM,
    MOMENTUM_MIN_THRESHOLD,
    ROTATION_HOLD_COUNT, ROTATION_CYCLE_DAYS,
    TAKE_PROFIT_TIER1, TAKE_PROFIT_TIER2,
    STOP_LOSS_THRESHOLD, STOP_LOSS_PANIC_THRESHOLD, MIN_HOLD_DAYS,
)


# ============================================================
# 数据加载
# ============================================================

def load_all_data(db_path="data/trading_system.db"):
    """一次性加载所有历史数据到内存"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # 行情数据: ts_code -> [(trade_date, close, pct_chg), ...] 按 trade_date 排序
    prices = defaultdict(list)
    rows = conn.execute("""
        SELECT ts_code, trade_date, close, pct_chg FROM index_daily
        ORDER BY ts_code, trade_date
    """).fetchall()
    for r in rows:
        prices[r["ts_code"]].append({
            "date": r["trade_date"],
            "close": r["close"],
            "pct_chg": r["pct_chg"] if r["pct_chg"] else 0.0
        })

    # 估值数据: ts_code -> [(trade_date, pe_ttm), ...]
    pe_data = defaultdict(list)
    rows = conn.execute("""
        SELECT ts_code, trade_date, pe_ttm FROM index_valuation
        WHERE pe_ttm > 0 AND pe_ttm < 200
        ORDER BY ts_code, trade_date
    """).fetchall()
    for r in rows:
        pe_data[r["ts_code"]].append({
            "date": r["trade_date"],
            "pe_ttm": r["pe_ttm"]
        })

    # 所有交易日
    trade_dates = sorted(set(r["trade_date"] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM index_daily WHERE ts_code='000300.SH' ORDER BY trade_date"
    ).fetchall()))

    conn.close()
    return prices, pe_data, trade_dates


# ============================================================
# 策略函数 (回测版本, 不依赖数据库)
# ============================================================

def calc_pe_percentile_bt(ts_code, current_date, pe_data, prices):
    """
    计算PE百分位 (回测版)
    ts_code: 底层指数代码 (000300.SH 等)
    """
    pe_list = pe_data.get(ts_code, [])
    # 筛选当前日期及之前的数据
    valid = [p["pe_ttm"] for p in pe_list if p["date"] <= current_date]

    # 最近5年
    cutoff = (datetime.strptime(current_date, "%Y%m%d") - timedelta(days=PE_LOOKBACK_YEARS * 365)).strftime("%Y%m%d")
    valid_5y = [p["pe_ttm"] for p in pe_list if p["date"] <= current_date and p["date"] >= cutoff]

    if len(valid_5y) < 60:
        return None, None, "hold"

    current_pe = valid[-1]
    if current_pe is None:
        return None, None, "hold"

    percentile = sum(1 for v in valid_5y if v < current_pe) / len(valid_5y) * 100

    if percentile < PE_BUY_THRESHOLD:
        signal = "buy"
    elif percentile > PE_SELL_THRESHOLD:
        signal = "sell"
    else:
        signal = "hold"

    return current_pe, percentile, signal


def check_ma_trend_bt(ts_code, current_date, prices):
    """检查均线趋势 (回测版)"""
    price_list = prices.get(ts_code, [])
    valid = [p for p in price_list if p["date"] <= current_date]

    if len(valid) < MA_PERIOD:
        return "unknown"

    closes = [p["close"] for p in valid[-MA_PERIOD:]]
    ma = sum(closes) / len(closes)
    current = closes[-1]
    return "up" if current > ma else "down"


def calc_momentum_bt(ts_code, current_date, prices):
    """计算动量得分 (回测版)"""
    price_list = prices.get(ts_code, [])
    valid = [p for p in price_list if p["date"] <= current_date]

    if len(valid) < MOMENTUM_MEDIUM:
        return None

    pct_changes = [p["pct_chg"] for p in valid]

    # 20日动量 (乘积法)
    m20 = 1.0
    for r in pct_changes[-MOMENTUM_SHORT:]:
        m20 *= (1 + r / 100.0)
    m20 = (m20 - 1) * 100.0

    # 60日动量
    m60 = 1.0
    for r in pct_changes[-MOMENTUM_MEDIUM:]:
        m60 *= (1 + r / 100.0)
    m60 = (m60 - 1) * 100.0

    composite = m20 * MOMENTUM_WEIGHT_SHORT + m60 * MOMENTUM_WEIGHT_MEDIUM
    return {"momentum_20d": m20, "momentum_60d": m60, "composite": composite}


# ============================================================
# 回测引擎
# ============================================================

class BacktestEngine:
    def __init__(self, prices, pe_data, trade_dates, start_date, end_date):
        self.prices = prices
        self.pe_data = pe_data
        self.trade_dates = [d for d in trade_dates if start_date <= d <= end_date]
        self.start_date = start_date
        self.end_date = end_date

        # 宽基持仓: {etf_name: {"shares": float, "cost_nav": float, "buy_date": str}}
        self.core_holdings = {}
        # 行业持仓: {etf_code: {"name": str, "shares": float, "cost_nav": float, "buy_date": str}}
        self.sat_holdings = {}
        self.cash = TOTAL_CAPITAL

        # 交易记录
        self.trades = []
        # 每日净值曲线
        self.daily_values = []

    def _date_diff(self, d1, d2):
        """计算两个YYYYMMDD之间的自然日天数"""
        return (datetime.strptime(d1, "%Y%m%d") - datetime.strptime(d2, "%Y%m%d")).days

    def _get_price(self, ts_code, date):
        """获取某日收盘价"""
        plist = self.prices.get(ts_code, [])
        for p in plist:
            if p["date"] == date:
                return p["close"]
        return None

    def _get_core_code(self, core_etf):
        """获取宽基ETF的底层指数代码"""
        return core_etf["index_code"]

    def _portfolio_value(self, date):
        """计算当日组合总市值"""
        total = self.cash
        for name, h in self.core_holdings.items():
            # 找到对应的ETF价格
            for etf in CORE_ETFS:
                if etf["name"] == name:
                    price = self._get_price(self._get_core_code(etf), date)
                    if price:
                        total += h["shares"] * price
                    break
        for code, h in self.sat_holdings.items():
            price = self._get_price(code, date)
            if price:
                total += h["shares"] * price
        return total

    def _buy_etf(self, name, ts_code, amount, date, category="satellite"):
        """买入ETF (以当日收盘价为买入净值)"""
        price = self._get_price(ts_code, date)
        if price is None or price <= 0:
            return False
        if amount <= 0 or amount > self.cash:
            return False

        shares = amount / price
        self.cash -= amount

        if category == "core":
            if name in self.core_holdings:
                # 加仓
                old = self.core_holdings[name]
                total_cost = old["shares"] * old["cost_nav"] + amount
                old["shares"] += shares
                old["cost_nav"] = total_cost / old["shares"]
            else:
                self.core_holdings[name] = {
                    "shares": shares, "cost_nav": price, "buy_date": date
                }
        else:
            if ts_code in self.sat_holdings:
                old = self.sat_holdings[ts_code]
                total_cost = old["shares"] * old["cost_nav"] + amount
                old["shares"] += shares
                old["cost_nav"] = total_cost / old["shares"]
            else:
                self.sat_holdings[ts_code] = {
                    "name": name, "shares": shares, "cost_nav": price, "buy_date": date
                }

        self.trades.append({
            "date": date, "type": "buy", "name": name, "code": ts_code,
            "amount": amount, "shares": shares, "price": price
        })
        return True

    def _sell_etf(self, name, ts_code, date, category="satellite"):
        """卖出ETF (以当日收盘价为卖出净值)"""
        price = self._get_price(ts_code, date)
        if price is None or price <= 0:
            return False

        if category == "core":
            if name not in self.core_holdings:
                return False
            h = self.core_holdings[name]
            amount = h["shares"] * price
            self.cash += amount
            self.trades.append({
                "date": date, "type": "sell", "name": name, "code": ts_code,
                "amount": amount, "shares": h["shares"], "price": price,
                "pnl_pct": (price / h["cost_nav"] - 1) * 100
            })
            del self.core_holdings[name]
        else:
            if ts_code not in self.sat_holdings:
                return False
            h = self.sat_holdings[ts_code]
            amount = h["shares"] * price
            self.cash += amount
            self.trades.append({
                "date": date, "type": "sell", "name": name, "code": ts_code,
                "amount": amount, "shares": h["shares"], "price": price,
                "pnl_pct": (price / h["cost_nav"] - 1) * 100
            })
            del self.sat_holdings[ts_code]
        return True

    def run(self):
        """执行回测"""
        print(f"回测区间: {self.start_date} ~ {self.end_date}")
        print(f"交易日数: {len(self.trade_dates)}")
        print(f"初始资金: ¥{TOTAL_CAPITAL:,.0f}")
        print()

        # 宽基初始建仓: 第一个交易日按比例买入
        first_date = self.trade_dates[0]
        core_amount = TOTAL_CAPITAL * CORE_RATIO
        sat_amount = TOTAL_CAPITAL * SATELLITE_RATIO

        # 宽基建仓
        print(f"[建仓日 {first_date}]")
        for etf in CORE_ETFS:
            idx_code = self._get_core_code(etf)
            price = self._get_price(idx_code, first_date)
            if price:
                amt = TOTAL_CAPITAL * etf["target_weight"]
                self._buy_etf(etf["name"], idx_code, amt, first_date, "core")
                print(f"  买入 {etf['name']}: ¥{amt:,.0f} @ {price:.4f}")

        # 行业初始建仓: 按动量排名选前3
        momentum_scores = []
        for etf in SATELLITE_ETFS:
            score = calc_momentum_bt(etf["code"], first_date, self.prices)
            if score is not None:
                momentum_scores.append((etf, score))
        momentum_scores.sort(key=lambda x: x[1]["composite"], reverse=True)

        per_sat = sat_amount / ROTATION_HOLD_COUNT
        for etf, score in momentum_scores[:ROTATION_HOLD_COUNT]:
            price = self._get_price(etf["code"], first_date)
            if price:
                self._buy_etf(etf["name"], etf["code"], per_sat, first_date, "satellite")
                print(f"  买入 {etf['name']}: ¥{per_sat:,.0f} @ {price:.4f} (动量#{momentum_scores.index((etf, score))+1})")

        print(f"  建仓后现金: ¥{self.cash:,.0f}")
        print()

        # 逐日模拟 (每14天检查一次轮动, 每天检查止盈止损)
        last_rotation_date = first_date
        trade_count = 0

        for i, date in enumerate(self.trade_dates):
            if i == 0:
                # 记录建仓日净值
                val = self._portfolio_value(date)
                self.daily_values.append({"date": date, "value": val, "cash": self.cash})
                continue

            # --- 止盈止损检查 (每日) ---
            # 先计算所有行业ETF动量排名 (止盈判断需要)
            mscores = []
            for etf in SATELLITE_ETFS:
                sc = calc_momentum_bt(etf["code"], date, self.prices)
                if sc is not None:
                    mscores.append((etf["code"], sc))
            mscores.sort(key=lambda x: x[1]["composite"], reverse=True)
            # 动量门槛过滤: 只取得分>0的ETF
            eligible_scores = [(c, s) for c, s in mscores if s["composite"] > MOMENTUM_MIN_THRESHOLD]
            top_codes = set(c for c, _ in eligible_scores[:ROTATION_HOLD_COUNT])

            for code, h in list(self.sat_holdings.items()):
                price = self._get_price(code, date)
                if price is None:
                    continue
                pnl_pct = (price / h["cost_nav"] - 1) * 100
                hold_days = self._date_diff(date, h["buy_date"])

                # 最短持有期检查 (盈利时)
                if hold_days < MIN_HOLD_DAYS and pnl_pct > 0:
                    continue

                # 动量排名前N, 跳过止盈让利润奔跑
                if code in top_codes:
                    continue

                # 阶梯止盈
                if pnl_pct >= TAKE_PROFIT_TIER2:
                    # 第二档: 全部卖出
                    self._sell_etf(h["name"], code, date, "satellite")
                    self.trades[-1]["tier"] = "T2"
                    trade_count += 1
                elif pnl_pct >= TAKE_PROFIT_TIER1:
                    # 第一档: 卖出一半
                    half_shares = h["shares"] / 2
                    amount = half_shares * price
                    self.cash += amount
                    self.trades.append({
                        "date": date, "type": "sell_half", "name": h["name"], "code": code,
                        "amount": amount, "shares": half_shares, "price": price,
                        "pnl_pct": pnl_pct, "tier": "T1"
                    })
                    h["shares"] -= half_shares
                    trade_count += 1

                # 止损 (不受动量排名限制)
                stop_loss = abs(STOP_LOSS_THRESHOLD)
                if pnl_pct <= -stop_loss and hold_days >= MIN_HOLD_DAYS:
                    if code in self.sat_holdings:  # 可能已止盈卖出
                        self._sell_etf(h["name"], code, date, "satellite")
                        trade_count += 1

            # --- 轮动检查 (每14天) ---
            days_since_rotation = self._date_diff(date, last_rotation_date)
            if days_since_rotation >= ROTATION_CYCLE_DAYS:
                # 计算所有行业ETF动量排名 (使用当日已计算的 mscores)
                momentum_scores = [(etf, sc) for etf in SATELLITE_ETFS
                                   for c, sc in mscores if c == etf["code"]]

                if momentum_scores:
                    momentum_scores.sort(key=lambda x: x[1]["composite"], reverse=True)
                    # 动量门槛过滤
                    eligible = [(etf, sc) for etf, sc in momentum_scores
                                if sc["composite"] > MOMENTUM_MIN_THRESHOLD]
                    top_codes = set(etf["code"] for etf, _ in eligible[:ROTATION_HOLD_COUNT])

                    # 如果没有合格的候选, 不卖出也不买入
                    if eligible:
                        # 卖出: 持有但排名靠后, 且持有满14天
                        for code, h in list(self.sat_holdings.items()):
                            if code not in top_codes:
                                hold_days = self._date_diff(date, h["buy_date"])
                                if hold_days >= ROTATION_CYCLE_DAYS:
                                    self._sell_etf(h["name"], code, date, "satellite")
                                    trade_count += 1

                        # 买入: 排名靠前但未持有
                        sat_target = TOTAL_CAPITAL * SATELLITE_RATIO
                        sat_invested = sum(
                            self._get_price(code, date) * h["shares"]
                            for code, h in self.sat_holdings.items()
                            if self._get_price(code, date)
                        )
                        available_for_sat = max(sat_target - sat_invested, 0)
                        per_etf = sat_amount / ROTATION_HOLD_COUNT

                        for etf, score in eligible[:ROTATION_HOLD_COUNT]:
                            if etf["code"] not in self.sat_holdings and self.cash >= per_etf:
                                buy_amt = min(per_etf, self.cash)
                                self._buy_etf(etf["name"], etf["code"], buy_amt, date, "satellite")
                                trade_count += 1

                    last_rotation_date = date

            # 记录每日净值
            val = self._portfolio_value(date)
            self.daily_values.append({"date": date, "value": val, "cash": self.cash})

        print(f"回测完成! 总交易次数: {trade_count}")
        return self.generate_report()

    def generate_report(self):
        """生成回测报告"""
        if not self.daily_values:
            return "无数据"

        start_val = self.daily_values[0]["value"]
        end_val = self.daily_values[-1]["value"]
        total_return = (end_val / start_val - 1) * 100

        # 最大回撤
        peak = start_val
        max_drawdown = 0
        max_dd_date = ""
        for dv in self.daily_values:
            if dv["value"] > peak:
                peak = dv["value"]
            dd = (peak - dv["value"]) / peak * 100
            if dd > max_drawdown:
                max_drawdown = dd
                max_dd_date = dv["date"]

        # 年化收益率
        days = self._date_diff(self.end_date, self.start_date)
        annual_return = ((end_val / start_val) ** (365 / max(days, 1)) - 1) * 100 if days > 0 else 0

        # 胜率
        sell_trades = [t for t in self.trades if t["type"] == "sell"]
        wins = [t for t in sell_trades if t.get("pnl_pct", 0) > 0]
        win_rate = len(wins) / len(sell_trades) * 100 if sell_trades else 0

        # 平均盈亏
        avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t["pnl_pct"] for t in sell_trades if t.get("pnl_pct", 0) <= 0)
        avg_loss = avg_loss / (len(sell_trades) - len(wins)) if (len(sell_trades) - len(wins)) > 0 else 0

        # 盈亏比
        profit_factor = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")

        # 按月统计收益
        monthly_returns = {}
        for dv in self.daily_values:
            month = dv["date"][:6]
            if month not in monthly_returns:
                monthly_returns[month] = {"start": dv["value"], "end": dv["value"]}
            monthly_returns[month]["end"] = dv["value"]

        report_lines = []
        report_lines.append("=" * 65)
        report_lines.append("  ETF联接基金策略回测报告")
        report_lines.append("=" * 65)
        report_lines.append(f"  回测区间:     {self.start_date} ~ {self.end_date}")
        report_lines.append(f"  交易天数:     {len(self.daily_values)} 天")
        report_lines.append(f"  初始资金:     ¥{start_val:>12,.2f}")
        report_lines.append(f"  最终市值:     ¥{end_val:>12,.2f}")
        report_lines.append(f"  总收益:       ¥{end_val - start_val:>12,.2f}")
        report_lines.append(f"  总收益率:     {total_return:>12.2f}%")
        report_lines.append(f"  年化收益率:   {annual_return:>12.2f}%")
        report_lines.append(f"  最大回撤:     {max_drawdown:>12.2f}%  ({max_dd_date})")
        report_lines.append(f"  交易次数:     {len(self.trades)} 笔 (买{sum(1 for t in self.trades if t['type']=='buy')}/卖{sum(1 for t in self.trades if t['type']=='sell')})")
        report_lines.append(f"  卖出胜率:     {win_rate:>12.1f}%  ({len(wins)}/{len(sell_trades)})")
        report_lines.append(f"  平均盈利:     {avg_win:>12.2f}%")
        report_lines.append(f"  平均亏损:     {avg_loss:>12.2f}%")
        report_lines.append(f"  盈亏比:       {profit_factor:>12.2f}")
        report_lines.append("=" * 65)

        # 月度收益
        report_lines.append("\n  月度收益:")
        report_lines.append(f"  {'月份':<10} {'期初':>12} {'期末':>12} {'收益率':>10}")
        report_lines.append(f"  {'-'*46}")
        months_sorted = sorted(monthly_returns.keys())
        for m in months_sorted:
            mr = monthly_returns[m]
            ret = (mr["end"] / mr["start"] - 1) * 100 if mr["start"] > 0 else 0
            report_lines.append(f"  {m:<10} ¥{mr['start']:>10,.0f} ¥{mr['end']:>10,.0f} {ret:>+9.2f}%")

        # 所有交易记录
        report_lines.append(f"\n  交易记录 (共{len(self.trades)}笔):")
        report_lines.append(f"  {'日期':<12} {'操作':<4} {'名称':<12} {'金额':>12} {'价格':>8} {'盈亏%':>8}")
        report_lines.append(f"  {'-'*60}")
        for t in self.trades:
            direction = "买入" if t["type"] == "buy" else "卖出"
            pnl = f"{t.get('pnl_pct', 0):>+.2f}%" if t["type"] == "sell" else ""
            report_lines.append(f"  {t['date']:<12} {direction:<4} {t['name']:<12} ¥{t['amount']:>10,.0f} {t['price']:>8.4f} {pnl:>8}")

        # 策略参数
        report_lines.append(f"\n  策略参数:")
        report_lines.append(f"  宽基比例: {CORE_RATIO*100:.0f}%  行业比例: {SATELLITE_RATIO*100:.0f}%")
        report_lines.append(f"  动量周期: {MOMENTUM_SHORT}日/{MOMENTUM_MEDIUM}日, 权重{MOMENTUM_WEIGHT_SHORT}/{MOMENTUM_WEIGHT_MEDIUM}")
        report_lines.append(f"  轮动持仓: 前{ROTATION_HOLD_COUNT}只, 轮动周期{ROTATION_CYCLE_DAYS}天")
        report_lines.append(f"  止盈: +{TAKE_PROFIT_TIER1:.0f}%卖半仓 / +{TAKE_PROFIT_TIER2:.0f}%清仓  止损: {STOP_LOSS_THRESHOLD:.0f}%")
        report_lines.append(f"  PE择时: <{PE_BUY_THRESHOLD}%买入, >{PE_SELL_THRESHOLD}%卖出 (5年百分位)")
        report_lines.append(f"  动量门槛: 综合得分>{MOMENTUM_MIN_THRESHOLD}%才纳入轮动")
        report_lines.append("=" * 65)

        report = "\n".join(report_lines)
        print(report)
        return report


# ============================================================
# 基准对比: 沪深300买入持有
# ============================================================

def benchmark_buy_hold(prices, trade_dates, start_date, end_date, capital=TOTAL_CAPITAL):
    """沪深300买入持有基准"""
    code = "000300.SH"
    plist = prices.get(code, [])
    start_p = next((p for p in plist if p["date"] >= start_date), None)
    end_p = next((p for p in plist if p["date"] >= end_date), None)
    if not plist or not start_p:
        return None

    if end_p is None:
        end_p = plist[-1]

    shares = capital / start_p["close"]
    final_value = shares * end_p["close"]
    days = (datetime.strptime(end_p["date"], "%Y%m%d") - datetime.strptime(start_p["date"], "%Y%m%d")).days
    annual = ((final_value / capital) ** (365 / max(days, 1)) - 1) * 100
    total_ret = (final_value / capital - 1) * 100

    # 最大回撤
    peak = start_p["close"]
    max_dd = 0
    for p in plist:
        if p["date"] < start_p["date"]:
            continue
        if p["date"] > end_p["date"]:
            break
        if p["close"] > peak:
            peak = p["close"]
        dd = (peak - p["close"]) / peak * 100
        if dd > max_dd:
            max_dd = dd

    return {
        "name": "沪深300买入持有",
        "start_date": start_p["date"],
        "end_date": end_p["date"],
        "total_return": total_ret,
        "annual_return": annual,
        "max_drawdown": max_dd,
        "final_value": final_value
    }


# ============================================================
# 主函数
# ============================================================

if __name__ == "__main__":
    print("加载数据...")
    prices, pe_data, trade_dates = load_all_data()

    # 回测区间: 2024-05-06 (第一个完整交易周) ~ 2026-04-28
    start = "20240506"
    end = "20260428"

    # 找到实际的交易日起始
    actual_start = next((d for d in trade_dates if d >= start), None)
    if not actual_start:
        print("无可用数据!")
        sys.exit(1)

    # 策略回测
    engine = BacktestEngine(prices, pe_data, trade_dates, actual_start, end)
    report = engine.run()

    # 基准对比
    print("\n")
    bm = benchmark_buy_hold(prices, trade_dates, actual_start, end)
    if bm:
        print("=" * 65)
        print("  基准对比: 沪深300买入持有")
        print("=" * 65)
        print(f"  区间:       {bm['start_date']} ~ {bm['end_date']}")
        print(f"  总收益率:   {bm['total_return']:>+12.2f}%")
        print(f"  年化收益率: {bm['annual_return']:>+12.2f}%")
        print(f"  最大回撤:   {bm['max_drawdown']:>12.2f}%")
        print(f"  最终市值:   ¥{bm['final_value']:>12,.2f}")
        print("=" * 65)

    # 保存报告
    report_path = os.path.join("logs", f"backtest_report_{actual_start}_{end}.txt")
    os.makedirs("logs", exist_ok=True)

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
        if bm:
            f.write(f"\n\n{'='*65}\n")
            f.write("  基准对比: 沪深300买入持有\n")
            f.write(f"{'='*65}\n")
            f.write(f"  区间:       {bm['start_date']} ~ {bm['end_date']}\n")
            f.write(f"  总收益率:   {bm['total_return']:>+12.2f}%\n")
            f.write(f"  年化收益率: {bm['annual_return']:>+12.2f}%\n")
            f.write(f"  最大回撤:   {bm['max_drawdown']:>12.2f}%\n")
            f.write(f"  最终市值:   ¥{bm['final_value']:>12,.2f}\n")
            f.write(f"{'='*65}\n")

    print(f"\n报告已保存: {report_path}")

# ============================================================
# ETF联接基金交易系统 - 策略引擎
# 包含: PE估值择时 / 动量轮动 / 止盈止损 / 恐慌指数监控
# ============================================================
import pandas as pd
from datetime import datetime, timedelta
from db import get_connection
from config import (
    # 宽基PE择时
    PE_LOOKBACK_YEARS, PE_BUY_THRESHOLD, PE_SELL_THRESHOLD, MA_PERIOD,
    # 动量轮动
    MOMENTUM_SHORT, MOMENTUM_MEDIUM, MOMENTUM_WEIGHT_SHORT, MOMENTUM_WEIGHT_MEDIUM,
    MOMENTUM_MIN_THRESHOLD,
    ROTATION_HOLD_COUNT, ROTATION_CYCLE_DAYS,
    # 移动止盈 + 止损
    TRAILING_STOP_ACTIVATE, TRAILING_STOP_PULLBACK,
    TRAILING_STOP_PANIC_PULLBACK, TRAILING_STOP_TOP1_PULLBACK,
    STOP_LOSS_THRESHOLD, STOP_LOSS_PANIC_THRESHOLD, MIN_HOLD_DAYS,
    MA20_STOP_LOSS_RATIO,
    # 恐慌监控
    PANIC_HS300_DROP, PANIC_NORTH_OUTFLOW, PANIC_LIMIT_DOWN_COUNT,
    PANIC_VOLUME_RATIO, PANIC_TRIGGER_COUNT,
    # 标的
    CORE_ETFS, SATELLITE_ETFS,
    # 资金
    TOTAL_CAPITAL, SINGLE_ETF_MAX_RATIO,
)


# ============================================================
# 模块1: 市场恐慌指数监控
# ============================================================

def check_panic_alert():
    """
    检测市场是否处于恐慌状态
    返回: {"is_panic": bool, "triggered": [...], "details": {...}}
    """
    conn = get_connection()
    try:
        triggered = []
        details = {}
        dates = {}  # 各指标各自的数据日期

        # 指标1: 沪深300单日跌幅 (取HS300最新可用数据)
        hs300_pct, hs300_date = _get_hs300_pct_chg(conn)
        details["hs300_pct_chg"] = hs300_pct
        dates["hs300_date"] = hs300_date
        if hs300_pct is not None and hs300_pct <= PANIC_HS300_DROP:
            triggered.append(f"沪深300跌幅{hs300_pct:.2f}% (阈值{PANIC_HS300_DROP:.1f}%)")

        # 指标2: 北向资金净流出 (取最新可用数据)
        north_money, north_date = _get_north_money(conn)
        details["north_money"] = north_money
        dates["north_date"] = north_date
        if north_money is not None and north_money <= -PANIC_NORTH_OUTFLOW:
            triggered.append(f"北向资金净流出{abs(north_money)/10000:.1f}亿 (阈值{PANIC_NORTH_OUTFLOW/10000:.0f}亿)")

        # 指标3: 跌停家数（AkShare, 当天盘中就有）
        limit_down, ld_date = _get_limit_down(conn)
        details["limit_down_count"] = limit_down
        dates["limit_down_date"] = ld_date
        if limit_down is not None and limit_down >= PANIC_LIMIT_DOWN_COUNT:
            triggered.append(f"跌停{limit_down}家 (阈值{PANIC_LIMIT_DOWN_COUNT}家)")

        # 指标4: 成交量异常放大 (基于HS300最新数据)
        vol_ratio, vol_date = _get_volume_ratio(conn)
        details["volume_ratio"] = vol_ratio
        dates["volume_date"] = vol_date
        if vol_ratio is not None and vol_ratio >= PANIC_VOLUME_RATIO:
            triggered.append(f"成交量放大{vol_ratio:.1f}倍 (阈值{PANIC_VOLUME_RATIO:.1f}倍)")

        is_panic = len(triggered) >= PANIC_TRIGGER_COUNT

        # 用各数据源最新日期中最大的作为记录日期
        valid_dates = [d for d in dates.values() if d]
        record_date = max(valid_dates) if valid_dates else None
        _save_panic_alert(conn, record_date, is_panic, triggered, details, dates)

        return {
            "is_panic": is_panic,
            "triggered": triggered,
            "trigger_count": len(triggered),
            "details": details,
            "dates": dates,
            "trade_date": record_date
        }

    finally:
        conn.close()


# ============================================================
# 模块2: PE估值择时 (宽基ETF)
# ============================================================

def calc_pe_percentile(ts_code):
    """
    计算某指数当前PE百分位
    返回: {"pe_ttm": float, "percentile": float, "signal": "buy"|"sell"|"hold"}
    """
    conn = get_connection()
    try:
        cutoff_date = (datetime.now() - timedelta(days=PE_LOOKBACK_YEARS * 365)).strftime("%Y%m%d")
        df = pd.read_sql_query("""
            SELECT pe_ttm FROM index_valuation
            WHERE ts_code = ? AND trade_date >= ? AND pe_ttm > 0 AND pe_ttm < 200
            ORDER BY trade_date
        """, conn, params=(ts_code, cutoff_date))

        if len(df) < 60:
            return {"pe_ttm": None, "percentile": None, "signal": "hold", "reason": "pe_data_missing"}

        current_pe = df.iloc[-1]["pe_ttm"]
        if current_pe is None or current_pe <= 0:
            return {"pe_ttm": None, "percentile": None, "signal": "hold", "reason": "PE数据缺失, 仅用均线判断"}

        percentile = (df["pe_ttm"] < current_pe).sum() / len(df) * 100

        if percentile < PE_BUY_THRESHOLD:
            signal = "buy"
        elif percentile > PE_SELL_THRESHOLD:
            signal = "sell"
        else:
            signal = "hold"

        return {
            "pe_ttm": round(current_pe, 2),
            "percentile": round(percentile, 1),
            "signal": signal,
            "reason": f"PE={current_pe:.1f}, 百分位={percentile:.1f}%"
        }

    finally:
        conn.close()


def check_core_etf_signals():
    """
    综合PE估值 + MA120趋势，输出宽基ETF信号
    返回: list of signal dicts
    """
    signals = []
    conn = get_connection()

    for etf in CORE_ETFS:
        pe_result = calc_pe_percentile(etf["code"])

        # MA120趋势判断
        ma_signal = _check_ma_trend(etf["code"], conn)

        # 综合信号
        pe_signal = pe_result["signal"]
        pct_str = f"{pe_result['percentile']:.0f}%" if pe_result["percentile"] is not None else ("N/A" if pe_result.get("reason") == "pe_data_missing" else "数据不足")
        if pe_signal == "buy" and ma_signal == "up":
            direction = "buy"
            reason = f"PE低估({pct_str}) + 均线向上"
        elif pe_signal == "sell" or ma_signal == "down":
            direction = "reduce"
            reason = f"PE高估({pct_str})" if pe_signal == "sell" else "均线向下"
        else:
            direction = "hold"
            reason = f"PE中性({pct_str}), 均线{ma_signal}"

        signals.append({
            "etf_code": etf["code"],
            "fund_code": etf["fund_code"],
            "name": etf["name"],
            "category": "core",
            "direction": direction,
            "target_weight": etf["target_weight"],
            "pe_info": pe_result,
            "ma_signal": ma_signal,
            "reason": reason
        })

    conn.close()
    return signals


# ============================================================
# 模块3: 动量轮动 (行业ETF)
# ============================================================

def calc_momentum_scores():
    """
    计算所有行业ETF的动量得分并排名
    返回: DataFrame with composite scores and ranks
    """
    conn = get_connection()
    results = []

    for etf in SATELLITE_ETFS:
        score = _calc_single_momentum(etf["code"], conn)
        if score:
            score["etf_code"] = etf["code"]
            score["name"] = etf["name"]
            score["category"] = etf["category"]
            score["fund_code"] = etf["fund_code"]
            results.append(score)

    if not results:
        conn.close()
        return pd.DataFrame()

    df = pd.DataFrame(results)
    df["composite_score"] = (
        df["momentum_20d"] * MOMENTUM_WEIGHT_SHORT +
        df["momentum_60d"] * MOMENTUM_WEIGHT_MEDIUM
    )
    df = df.sort_values("composite_score", ascending=False).reset_index(drop=True)
    df["rank"] = range(1, len(df) + 1)

    # 保存到数据库
    trade_date = _get_latest_trade_date(conn)
    for _, row in df.iterrows():
        conn.execute("""
            INSERT OR REPLACE INTO momentum_scores
            (trade_date, etf_code, name, category, momentum_20d, momentum_60d, composite_score, rank, ma20, above_ma20)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (trade_date, row["etf_code"], row["name"], row["category"],
              row["momentum_20d"], row["momentum_60d"], row["composite_score"], row["rank"],
              row.get("ma20"), 1 if row.get("above_ma20") else 0))
    conn.commit()
    conn.close()

    return df


def check_rotation_signals(holdings):
    """
    根据动量排名生成轮动信号
    holdings: 当前持仓列表
    返回: {"buy": [...], "sell": [...], "hold": [...]}
    """
    momentum_df = calc_momentum_scores()
    if momentum_df.empty:
        return {"buy": [], "sell": [], "hold": [], "momentum_df": momentum_df, "top_codes": set()}

    # 动量门槛过滤: 综合得分 <= 0 的ETF不纳入轮动
    eligible = momentum_df[momentum_df["composite_score"] > MOMENTUM_MIN_THRESHOLD].copy()

    # MA20趋势闸门: 收盘价在MA20上方才允许买入 (趋势方向确认)
    eligible = eligible[eligible["above_ma20"] == True] if "above_ma20" in eligible.columns else eligible

    if eligible.empty:
        # 所有ETF动量都不够, 没有新的买入信号
        top_codes = set()
        # 已持有的也不卖出 (没有更好的替代品)
        hold_signals = []
        for h in holdings:
            if h["category"] == "satellite":
                hold_signals.append({
                    "etf_code": h["etf_code"],
                    "fund_code": h["fund_code"],
                    "name": h["name"],
                    "category": h["category"],
                    "direction": "hold",
                    "reason": "所有候选ETF动量不足, 继续持有"
                })
        return {"buy": [], "sell": [], "hold": hold_signals, "momentum_df": momentum_df, "top_codes": top_codes}

    top_n = eligible.head(ROTATION_HOLD_COUNT)
    top_codes = set(top_n["etf_code"].tolist())

    current_codes = {h["etf_code"] for h in holdings if h["category"] == "satellite"}

    buy_signals = []
    sell_signals = []
    hold_signals = []

    # 需要买入的: 排名靠前但未持有的
    for _, row in top_n.iterrows():
        if row["etf_code"] not in current_codes:
            buy_signals.append({
                "etf_code": row["etf_code"],
                "fund_code": row["fund_code"],
                "name": row["name"],
                "category": row["category"],
                "direction": "buy",
                "reason": f"动量排名第{row['rank']}, 综合得分{row['composite_score']:.4f}, MA20上方",
                "momentum_20d": row["momentum_20d"],
                "momentum_60d": row["momentum_60d"],
                "composite_score": row["composite_score"],
                "above_ma20": True
            })

    # 需要卖出的: 持有但排名靠后, 且持有满轮动周期
    for h in holdings:
        if h["category"] == "satellite" and h["etf_code"] not in top_codes:
            hold_days = h.get("hold_days", 0)
            if hold_days < ROTATION_CYCLE_DAYS:
                # 持有未满轮动周期, 暂不赎回, 加入持有列表
                hold_signals.append({
                    "etf_code": h["etf_code"],
                    "fund_code": h["fund_code"],
                    "name": h["name"],
                    "category": h["category"],
                    "direction": "hold",
                    "reason": f"排名跌出前{ROTATION_HOLD_COUNT}, 但持有仅{hold_days}天 (需满{ROTATION_CYCLE_DAYS}天)"
                })
                continue
            sell_signals.append({
                "etf_code": h["etf_code"],
                "fund_code": h["fund_code"],
                "name": h["name"],
                "category": h["category"],
                "direction": "sell",
                "reason": "动量排名跌出前N",
                "profit_pct": h.get("profit_pct", 0),
                "hold_days": hold_days
            })

    # 继续持有的 / 仓位不足需补仓的
    for h in holdings:
        if h["category"] == "satellite" and h["etf_code"] in top_codes:
            rank_row = top_n[top_n["etf_code"] == h["etf_code"]].iloc[0]
            mv = h.get("market_value", 0)
            # 目标仓位: 单只卫星ETF上限
            target_mv = TOTAL_CAPITAL * SINGLE_ETF_MAX_RATIO
            if mv < target_mv * 0.5:
                # 仓位不足 (不到目标的一半), 生成补仓买入信号
                buy_signals.append({
                    "etf_code": h["etf_code"],
                    "fund_code": h["fund_code"],
                    "name": h["name"],
                    "category": h["category"],
                    "direction": "buy",
                    "reason": f"动量排名第{rank_row['rank']}, 仓位不足(当前{mv:,.0f}元, 目标{target_mv:,.0f}元), 建议补仓",
                    "momentum_20d": rank_row["momentum_20d"],
                    "momentum_60d": rank_row["momentum_60d"],
                    "composite_score": rank_row["composite_score"],
                    "current_value": mv,
                    "target_value": target_mv
                })
            else:
                hold_signals.append({
                    "etf_code": h["etf_code"],
                    "fund_code": h["fund_code"],
                    "name": h["name"],
                    "category": h["category"],
                    "direction": "hold",
                    "reason": f"动量排名第{rank_row['rank']}, 继续持有"
                })

    return {"buy": buy_signals, "sell": sell_signals, "hold": hold_signals, "momentum_df": momentum_df, "top_codes": top_codes}


# ============================================================
# 模块4: 止盈止损
# ============================================================

def check_stop_signals(holdings, is_panic=False, momentum_top_codes=None):
    """
    检查所有持仓的止盈止损信号 (移动止盈版)
    is_panic: 是否处于恐慌预警状态 (收紧止损阈值和回撤阈值)
    momentum_top_codes: 动量排名第1的ETF代码集合, 使用更宽的移动止盈容限

    移动止盈逻辑:
      利润 >= 激活阈值 → 开始跟踪每日利润峰值 (峰值持久化到数据库)
      从峰值回撤 >= 回撤阈值 → 清仓
      动量排名第1: 回撤容限放宽至 TRAILING_STOP_TOP1_PULLBACK
    """
    if momentum_top_codes is None:
        momentum_top_codes = set()
    signals = []
    stop_loss = abs(STOP_LOSS_PANIC_THRESHOLD if is_panic else STOP_LOSS_THRESHOLD)
    trailing_pullback = TRAILING_STOP_PANIC_PULLBACK if is_panic else TRAILING_STOP_PULLBACK

    conn = get_connection()
    peak_updates = []

    for h in holdings:
        profit_pct = h.get("profit_pct", 0)
        hold_days = h.get("hold_days", 0)

        # 最短持有期检查
        if hold_days < MIN_HOLD_DAYS and profit_pct > 0:
            continue

        # 宽基ETF跳过止盈 (由PE估值择时管理)
        if h.get("category") == "core":
            continue

        # 确定此持仓的回撤阈值
        is_top1 = bool(momentum_top_codes) and h.get("etf_code") in momentum_top_codes
        if is_top1 and not is_panic:
            etf_pullback = TRAILING_STOP_TOP1_PULLBACK
        else:
            etf_pullback = trailing_pullback

        # === 移动止盈 ===
        peak_profit = h.get("peak_profit_pct") or 0

        # 利润≥激活阈值且创新高 → 更新峰值并持久化
        if profit_pct >= TRAILING_STOP_ACTIVATE and profit_pct > peak_profit:
            peak_profit = profit_pct
            if h.get("id"):
                peak_updates.append((peak_profit, h["id"]))

        # 从峰值回撤超过阈值 → 清仓信号
        if peak_profit > 0 and (peak_profit - profit_pct) >= etf_pullback:
            signals.append({
                "etf_code": h["etf_code"],
                "fund_code": h["fund_code"],
                "name": h["name"],
                "category": h["category"],
                "direction": "sell",
                "signal_type": "trailing_stop",
                "reason": f"移动止盈: 峰值+{peak_profit:.2f}%, 回撤至+{profit_pct:.2f}% (回撤{peak_profit - profit_pct:.2f}%, 阈值{etf_pullback:.0f}%)",
                "profit_pct": profit_pct,
                "peak_profit_pct": peak_profit,
                "sell_ratio": 1.0,
                "priority": "normal"
            })
            continue  # 已生成止盈信号, 不再检查止损

        # === MA20趋势止损 ===
        # 收盘价跌破MA20×0.95视为趋势结构破坏, 离场
        # 受MIN_HOLD_DAYS约束 (方案A: 7天内先扛, 省赎回费, 极端情况由恐慌止损兜底)
        if hold_days >= MIN_HOLD_DAYS:
            close, ma20 = _get_latest_close_and_ma20(conn, h["etf_code"])
            if close is not None and ma20 is not None and close < ma20 * MA20_STOP_LOSS_RATIO:
                below_pct = (close / ma20 - 1) * 100
                signals.append({
                    "etf_code": h["etf_code"],
                    "fund_code": h["fund_code"],
                    "name": h["name"],
                    "category": h["category"],
                    "direction": "sell",
                    "signal_type": "trend_stop",
                    "reason": f"趋势止损: 收盘{close:.4f} < MA20×{MA20_STOP_LOSS_RATIO}({ma20 * MA20_STOP_LOSS_RATIO:.4f}), 跌破均线{below_pct:.2f}%",
                    "profit_pct": profit_pct,
                    "sell_ratio": 1.0,
                    "priority": "high"
                })
                continue  # 趋势止损触发, 不再检查固定止损

        # === 止损 ===
        if profit_pct <= -stop_loss:
            signals.append({
                "etf_code": h["etf_code"],
                "fund_code": h["fund_code"],
                "name": h["name"],
                "category": h["category"],
                "direction": "sell",
                "signal_type": "stop_loss",
                "reason": f"{'[恐慌加急] ' if is_panic else ''}止损: 亏损{abs(profit_pct):.2f}% (阈值-{stop_loss:.0f}%)",
                "profit_pct": profit_pct,
                "sell_ratio": 1.0,
                "priority": "urgent" if is_panic else "high"
            })

    # 批量更新峰值到数据库
    if peak_updates:
        for peak_val, hid in peak_updates:
            conn.execute("UPDATE portfolio SET peak_profit_pct = ? WHERE id = ?", (peak_val, hid))
        conn.commit()
        print(f"  [OK] 利润峰值更新: {len(peak_updates)}只持仓")

    conn.close()
    return signals


# ============================================================
# 内部辅助函数
# ============================================================

def _get_latest_trade_date(conn):
    """从数据库获取最新交易日（取各数据源最新日期的最大值）"""
    row = conn.execute("""
        SELECT MAX(trade_date) as latest FROM index_daily
    """).fetchone()
    return row["latest"] if row and row["latest"] else None


def _get_latest_close_and_ma20(conn, ts_code, ma_period=20):
    """获取最新收盘价和MA20均线值, 返回 (close, ma20)"""
    rows = conn.execute("""
        SELECT close FROM index_daily
        WHERE ts_code = ? AND close IS NOT NULL AND close > 0
        ORDER BY trade_date DESC LIMIT ?
    """, (ts_code, ma_period)).fetchall()
    if len(rows) < ma_period:
        return (None, None)
    closes = [r["close"] for r in reversed(rows)]
    return (closes[-1], round(sum(closes) / ma_period, 4))


def _get_hs300_pct_chg(conn):
    """获取沪深300最新交易日的涨跌幅, 返回 (value, date)"""
    row = conn.execute("""
        SELECT pct_chg, trade_date FROM index_daily
        WHERE ts_code = '000300.SH'
        ORDER BY trade_date DESC LIMIT 1
    """).fetchone()
    if row and row["pct_chg"] is not None:
        return (row["pct_chg"], row["trade_date"])
    return (None, None)


def _get_north_money(conn):
    """获取最新交易日的北向资金��流入, 返回 (value, date)"""
    row = conn.execute("""
        SELECT north_money, trade_date FROM north_money_flow
        ORDER BY trade_date DESC LIMIT 1
    """).fetchone()
    if row and row["north_money"] is not None:
        return (row["north_money"], row["trade_date"])
    return (None, None)


def _get_limit_down(conn):
    """获取最新跌停家数, 返回 (value, date)"""
    row = conn.execute("""
        SELECT limit_down_count, trade_date FROM limit_down_stats
        ORDER BY trade_date DESC LIMIT 1
    """).fetchone()
    if row and row["limit_down_count"] is not None:
        return (row["limit_down_count"], row["trade_date"])
    return (None, None)


def _get_volume_ratio(conn):
    """计算沪深300成交量较5日均量的比值, 返回 (value, date)"""
    rows = conn.execute("""
        SELECT close, vol, trade_date FROM index_daily
        WHERE ts_code = '000300.SH'
        ORDER BY trade_date DESC LIMIT 6
    """).fetchall()

    if len(rows) < 6:
        return (None, None)

    current_vol = rows[0]["vol"]
    vol_date = rows[0]["trade_date"]
    avg_vol = sum(r["vol"] for r in rows[1:6]) / 5
    ratio = current_vol / avg_vol if avg_vol > 0 else None
    return (ratio, vol_date)


def _save_panic_alert(conn, date, is_panic, triggered, details, dates=None):
    """保存恐慌预警记录, dates为各指标各自的数据日期"""
    level = "PANIC" if is_panic else ("WARNING" if len(triggered) > 0 else "NORMAL")
    dates = dates or {}
    conn.execute("""
        INSERT INTO panic_alerts (trade_date, alert_level, trigger_details,
                                   hs300_pct_chg, hs300_date,
                                   north_money, north_date,
                                   limit_down_count, limit_down_date,
                                   volume_ratio, volume_date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (date, level, "|".join(triggered),
          details.get("hs300_pct_chg"), dates.get("hs300_date"),
          details.get("north_money"), dates.get("north_date"),
          details.get("limit_down_count"), dates.get("limit_down_date"),
          details.get("volume_ratio"), dates.get("volume_date")))
    conn.commit()


def _check_ma_trend(ts_code, conn):
    """检查MA120趋势方向"""
    rows = conn.execute("""
        SELECT trade_date, close FROM index_daily
        WHERE ts_code = ?
        ORDER BY trade_date DESC LIMIT ?
    """, (ts_code, MA_PERIOD + 10)).fetchall()

    if len(rows) < MA_PERIOD:
        return "unknown"

    closes = [r["close"] for r in reversed(rows)]
    ma120 = sum(closes[-MA_PERIOD:]) / MA_PERIOD
    current = closes[-1]

    return "up" if current > ma120 else "down"


def _calc_single_momentum(ts_code, conn):
    """计算单只ETF的动量得分 (价格比法) + MA20趋势状态

    与 etf_discovery.py / breakout_discovery.py 保持一致:
      n日动量 = 最新收盘价 / n日前收盘价 - 1
    直接用 close 价格计算, 避免 pct_chg 缺失导致的累乘误差。

    返回额外字段:
      ma20: 20日均线值
      above_ma20: 最新收盘价是否在MA20上方 (买入闸门条件之一)
    """
    rows = conn.execute("""
        SELECT trade_date, close FROM index_daily
        WHERE ts_code = ? AND close IS NOT NULL AND close > 0
        ORDER BY trade_date DESC LIMIT ?
    """, (ts_code, MOMENTUM_MEDIUM + 10)).fetchall()

    if len(rows) < MOMENTUM_MEDIUM + 1:
        return None

    # rows 是倒序 (最新在前), 反转为正序 (最旧在前)
    rows = list(reversed(rows))
    closes = [r["close"] for r in rows]

    # 短期动量: close[-1] / close[-(SHORT+1)] - 1
    momentum_20d = (closes[-1] / closes[-(MOMENTUM_SHORT + 1)] - 1) * 100.0

    # 中期动量: close[-1] / close[-(MEDIUM+1)] - 1
    momentum_60d = (closes[-1] / closes[-(MOMENTUM_MEDIUM + 1)] - 1) * 100.0

    # MA20 趋势判断
    ma20 = None
    above_ma20 = False
    if len(closes) >= 20:
        ma20 = round(sum(closes[-20:]) / 20, 4)
        above_ma20 = closes[-1] > ma20

    return {
        "momentum_20d": round(momentum_20d, 4),
        "momentum_60d": round(momentum_60d, 4),
        "ma20": ma20,
        "above_ma20": above_ma20,
    }

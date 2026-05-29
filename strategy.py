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
    # 止盈止损
    TAKE_PROFIT_TIER1, TAKE_PROFIT_TIER2,
    STOP_LOSS_THRESHOLD, STOP_LOSS_PANIC_THRESHOLD, MIN_HOLD_DAYS,
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
        latest_date = _get_latest_trade_date(conn)
        if not latest_date:
            return {"is_panic": False, "triggered": [], "details": {}}

        triggered = []
        details = {}

        # 指标1: 沪深300单日跌幅
        hs300_pct = _get_hs300_pct_chg(conn, latest_date)
        details["hs300_pct_chg"] = hs300_pct
        if hs300_pct is not None and hs300_pct <= PANIC_HS300_DROP:
            triggered.append(f"沪深300跌幅{hs300_pct:.2f}% (阈值{PANIC_HS300_DROP:.1f}%)")

        # 指标2: 北向资金净流出
        north_money = _get_north_money(conn, latest_date)
        details["north_money"] = north_money
        if north_money is not None and north_money <= -PANIC_NORTH_OUTFLOW:
            triggered.append(f"北向资金净流出{abs(north_money)/10000:.1f}亿 (阈值{PANIC_NORTH_OUTFLOW/10000:.0f}亿)")

        # 指标3: 跌停家数（单独用 limit_down_stats 最新日期，避免 index_daily T+1 延迟导致数据错位）
        ld_date = _get_latest_limit_down_date(conn) or latest_date
        limit_down = _get_limit_down(conn, ld_date)
        details["limit_down_count"] = limit_down
        if limit_down is not None and limit_down >= PANIC_LIMIT_DOWN_COUNT:
            triggered.append(f"跌停{limit_down}家 (阈值{PANIC_LIMIT_DOWN_COUNT}家)")

        # 指标4: 成交量异常放大
        vol_ratio = _get_volume_ratio(conn, latest_date)
        details["volume_ratio"] = vol_ratio
        if vol_ratio is not None and vol_ratio >= PANIC_VOLUME_RATIO:
            triggered.append(f"成交量放大{vol_ratio:.1f}倍 (阈值{PANIC_VOLUME_RATIO:.1f}倍)")

        is_panic = len(triggered) >= PANIC_TRIGGER_COUNT

        # 记录预警（用 ld_date 作为基准，确保跌停数据与记录日期一致）
        record_date = ld_date if ld_date > latest_date else latest_date
        _save_panic_alert(conn, record_date, is_panic, triggered, details)

        return {
            "is_panic": is_panic,
            "triggered": triggered,
            "trigger_count": len(triggered),
            "details": details,
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
            (trade_date, etf_code, name, category, momentum_20d, momentum_60d, composite_score, rank)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (trade_date, row["etf_code"], row["name"], row["category"],
              row["momentum_20d"], row["momentum_60d"], row["composite_score"], row["rank"]))
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
                "reason": f"动量排名第{row['rank']}, 综合得分{row['composite_score']:.4f}",
                "momentum_20d": row["momentum_20d"],
                "momentum_60d": row["momentum_60d"],
                "composite_score": row["composite_score"]
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
    检查所有持仓的止盈止损信号 (阶梯止盈版)
    is_panic: 是否处于恐慌预警状态 (收紧止损阈值)
    momentum_top_codes: 动量排名前N的ETF代码集合, 在其中的行业ETF不触发止盈(让利润奔跑)
    
    阶梯止盈逻辑:
      第一档: 收益 >= TAKE_PROFIT_TIER1(+8%) → 赎回一半 (sell_ratio=0.5)
      第二档: 收益 >= TAKE_PROFIT_TIER2(+15%) → 赎回全部
      动量排名前N的行业ETF: 完全不触发止盈
    """
    if momentum_top_codes is None:
        momentum_top_codes = set()
    signals = []
    # stop_loss 取绝对值 (正数), 判断时用 profit_pct <= -stop_loss
    stop_loss = abs(STOP_LOSS_PANIC_THRESHOLD if is_panic else STOP_LOSS_THRESHOLD)

    for h in holdings:
        profit_pct = h.get("profit_pct", 0)
        hold_days = h.get("hold_days", 0)

        # 最短持有期检查
        if hold_days < MIN_HOLD_DAYS and profit_pct > 0:
            # 盈利但持有不足7天, 不触发止盈 (避免赎回费)
            continue

        # 行业ETF在动量排名前N, 跳过止盈让利润奔跑
        if h["category"] == "satellite" and h["etf_code"] in momentum_top_codes:
            continue

        # 阶梯止盈: 第二档优先判断
        if profit_pct >= TAKE_PROFIT_TIER2:
            signals.append({
                "etf_code": h["etf_code"],
                "fund_code": h["fund_code"],
                "name": h["name"],
                "category": h["category"],
                "direction": "sell",
                "signal_type": "take_profit",
                "reason": f"阶梯止盈(第二档): 收益+{profit_pct:.2f}% (阈值+{TAKE_PROFIT_TIER2:.0f}%)",
                "profit_pct": profit_pct,
                "sell_ratio": 1.0,
                "priority": "normal"
            })
        elif profit_pct >= TAKE_PROFIT_TIER1:
            signals.append({
                "etf_code": h["etf_code"],
                "fund_code": h["fund_code"],
                "name": h["name"],
                "category": h["category"],
                "direction": "sell",
                "signal_type": "take_profit",
                "reason": f"阶梯止盈(第一档): 收益+{profit_pct:.2f}% (阈值+{TAKE_PROFIT_TIER1:.0f}%), 建议赎回一半",
                "profit_pct": profit_pct,
                "sell_ratio": 0.5,
                "priority": "normal"
            })
        elif profit_pct <= -stop_loss:
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


def _get_latest_limit_down_date(conn):
    """获取 limit_down_stats 里的最新交易日（可能比 index_daily 更新）"""
    row = conn.execute("""
        SELECT MAX(trade_date) as latest FROM limit_down_stats
    """).fetchone()
    return row["latest"] if row and row["latest"] else None


def _get_hs300_pct_chg(conn, date):
    """获取沪深300当日涨跌幅 (百分比数值, 如 -2.5 表示跌2.5%)"""
    row = conn.execute("""
        SELECT pct_chg FROM index_daily
        WHERE ts_code = '000300.SH' AND trade_date = ?
    """, (date,)).fetchone()
    return row["pct_chg"] if row and row["pct_chg"] is not None else None


def _get_north_money(conn, date):
    """获取当日北向资金净流入"""
    row = conn.execute("""
        SELECT north_money FROM north_money_flow WHERE trade_date = ?
    """, (date,)).fetchone()
    return row["north_money"] if row else None


def _get_limit_down(conn, date):
    """获取当日跌停家数"""
    row = conn.execute("""
        SELECT limit_down_count FROM limit_down_stats WHERE trade_date = ?
    """, (date,)).fetchone()
    return row["limit_down_count"] if row else None


def _get_volume_ratio(conn, date):
    """计算沪深300成交量较5日均量的比值"""
    row = conn.execute("""
        SELECT close, vol FROM index_daily
        WHERE ts_code = '000300.SH' AND trade_date <= ?
        ORDER BY trade_date DESC LIMIT 6
    """, (date,)).fetchall()

    if len(row) < 6:
        return None

    current_vol = row[0]["vol"]
    avg_vol = sum(r["vol"] for r in row[1:6]) / 5
    return current_vol / avg_vol if avg_vol > 0 else None


def _save_panic_alert(conn, date, is_panic, triggered, details):
    """保存恐慌预警记录"""
    level = "PANIC" if is_panic else ("WARNING" if len(triggered) > 0 else "NORMAL")
    conn.execute("""
        INSERT INTO panic_alerts (trade_date, alert_level, trigger_details,
                                   hs300_pct_chg, north_money, limit_down_count, volume_ratio)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (date, level, "|".join(triggered),
          details.get("hs300_pct_chg"), details.get("north_money"),
          details.get("limit_down_count"), details.get("volume_ratio")))
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
    """计算单只ETF的动量得分"""
    rows = conn.execute("""
        SELECT trade_date, pct_chg FROM index_daily
        WHERE ts_code = ?
        ORDER BY trade_date DESC LIMIT ?
    """, (ts_code, MOMENTUM_MEDIUM + 10)).fetchall()

    if len(rows) < MOMENTUM_MEDIUM:
        return None

    rows = list(reversed(rows))
    # pct_chg: 百分比形式 (如 0.2219 表示涨0.2219%)
    pct_changes = []
    for r in rows:
        if r["pct_chg"] is not None:
            pct_changes.append(r["pct_chg"])

    if len(pct_changes) < MOMENTUM_MEDIUM:
        return None

    # 短期动量: 最近MOMENTUM_SHORT日累乘收益率 (乘积法)
    # pct_chg 为百分比形式 (0.2219 = 涨0.2219%), 先÷100转小数计算，结果再×100保持百分比单位
    momentum_20d = 1.0
    for r in pct_changes[-MOMENTUM_SHORT:]:
        momentum_20d *= (1 + r / 100.0)
    momentum_20d = (momentum_20d - 1) * 100.0  # 结果单位与 pct_chg 一致 (百分比)

    # 中期动量: 最近MOMENTUM_MEDIUM日累乘收益率
    momentum_60d = 1.0
    for r in pct_changes[-MOMENTUM_MEDIUM:]:
        momentum_60d *= (1 + r / 100.0)
    momentum_60d = (momentum_60d - 1) * 100.0  # 结果单位与 pct_chg 一致 (百分比)

    return {
        "momentum_20d": round(momentum_20d, 4),
        "momentum_60d": round(momentum_60d, 4)
    }

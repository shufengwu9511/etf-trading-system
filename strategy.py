# ============================================================
# ETF联接基金交易系统 - 策略引擎
# 包含: PE估值择时 / 动量轮动 / 止盈止损 / 恐慌指数监控
# ============================================================
import pandas as pd
from datetime import datetime, timedelta
from db import get_connection
from momentum_persistence import check_persistence
from config import (
    # 宽基PE择时
    PE_LOOKBACK_YEARS, PE_BUY_THRESHOLD, PE_SELL_THRESHOLD, MA_PERIOD,
    # 动量轮动
    MOMENTUM_SHORT, MOMENTUM_MEDIUM, MOMENTUM_WEIGHT_SHORT, MOMENTUM_WEIGHT_MEDIUM,
    MOMENTUM_MIN_THRESHOLD,
    MOMENTUM_PERSIST_ENABLED, MOMENTUM_PERSIST_MODE,
    MOMENTUM_PERSIST_LOOKBACK, MOMENTUM_PERSIST_MIN_DELTA,
    MOMENTUM_PERSIST_REQUIRE_POSITIVE,
    ROTATION_HOLD_COUNT, ROTATION_CYCLE_DAYS,
    ROTATION_RANK_BUFFER, ROTATION_MAX_HOLD_COUNT,
    # 移动止盈 + 止损
    TRAILING_STOP_ACTIVATE, TRAILING_STOP_PULLBACK,
    TRAILING_STOP_PANIC_PULLBACK, TRAILING_STOP_TOP1_PULLBACK,
    STOP_LOSS_THRESHOLD, STOP_LOSS_PANIC_THRESHOLD, MIN_HOLD_DAYS,
    MA20_STOP_LOSS_RATIO,
    # 宽基底仓专用 (兜底止损 + 减仓比例)
    CORE_STOP_LOSS_THRESHOLD, CORE_STOP_LOSS_PANIC_THRESHOLD, CORE_REDUCE_RATIO,
    MIN_TRADE_AMOUNT,
    # 观察仓阈值
    MIN_POSITION_VALUE,
    # 执行纪律 (反摊平红线 + 卖出信号逾期回看)
    ANTI_AVERAGING_LOSS_THRESHOLD,
    SIGNAL_OVERDUE_LOOKBACK_DAYS, SIGNAL_OVERDUE_GRACE_DAYS,
    # 恐慌监控
    PANIC_HS300_DROP, PANIC_NORTH_OUTFLOW,
    PANIC_NORTH_TURNOVER_RATIO, PANIC_NORTH_MA_DAYS,
    PANIC_LIMIT_DOWN_COUNT, PANIC_VOLUME_RATIO, PANIC_TRIGGER_COUNT,
    # 标的
    CORE_ETFS, SATELLITE_ETFS, preferred_buy_code,
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

        # 指标2: 北向成交额异动 (较近20日均值放大)
        # 注: 原用 north_money 判净流出, 但该字段实为 hgt+sgt 成交流水(恒正),
        #     判据永远不成立 → 指标失效。2024-08起净流入已停止披露, 改用成交额异动。
        north_ratio, north_date = _get_north_turnover_ratio(conn)
        details["north_turnover_ratio"] = north_ratio
        dates["north_date"] = north_date
        if north_ratio is not None and north_ratio >= PANIC_NORTH_TURNOVER_RATIO:
            triggered.append(
                f"北向成交额放大{north_ratio:.2f}倍 "
                f"(阈值{PANIC_NORTH_TURNOVER_RATIO:.1f}倍)"
            )

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


def check_core_etf_signals(holdings=None):
    """
    综合PE估值 + MA100趋势，输出宽基ETF信号，并落地为"可执行动作"。

    估值/趋势只决定"方向偏好"，真正能执行的动作必须结合当前持仓 (2026-10-14修正):
      - 看多(PE低估 + 均线向上): 距目标仓位还差 > MIN_TRADE_AMOUNT → 建议申购差额；否则维持
      - 看空(PE高估 或 均线向下): 有持仓 → 建议减仓 CORE_REDUCE_RATIO；无持仓 → 仅观望
      - 中性: 维持当前仓位
    修正原因: 原实现只看估值/趋势就报"🟡建议减仓 + 目标金额¥X"，其中目标金额其实是
    "总资金×目标权重"(绝对目标仓位)，对空仓/低仓标的会产生方向错误的减仓提示
    (例如未持有的中证500/科创50也天天被"建议减仓")。

    返回: list of signal dicts (含 current_amount / target_amount / amount 落地金额)
    """
    holdings = holdings or []

    # 汇总当前宽基持仓市值 (在途申购按已投入金额计) + 市值加权浮亏
    cur_map = {}
    pnl_parts = {}
    for h in holdings:
        if h.get("category") != "core":
            continue
        code = h.get("etf_code")
        if not code:
            continue
        if h.get("status") == "pending_buy":
            val = h.get("total_cost") or 0
        else:
            val = h.get("market_value") or 0
        cur_map[code] = (cur_map.get(code) or 0) + val
        # 同一宽基可能分批建仓, 按市值加权汇总浮亏 (用于反摊平红线)
        pnl_parts.setdefault(code, []).append((val, h.get("profit_pct") or 0))
    pnl_map = {}
    for code, parts in pnl_parts.items():
        wsum = sum(v for v, _ in parts)
        if wsum > 0:
            pnl_map[code] = sum(v * p for v, p in parts) / wsum

    signals = []
    conn = get_connection()

    for etf in CORE_ETFS:
        pe_result = calc_pe_percentile(etf["code"])

        # MA100趋势判断
        ma_signal = _check_ma_trend(etf["code"], conn)

        pe_signal = pe_result["signal"]
        pct_str = f"{pe_result['percentile']:.0f}%" if pe_result["percentile"] is not None else ("N/A" if pe_result.get("reason") == "pe_data_missing" else "数据不足")

        current_amount = round(cur_map.get(etf["code"], 0) or 0, 2)
        target_amount = round(TOTAL_CAPITAL * etf["target_weight"], 2)

        # --- 方向偏好 (估值 + 趋势) ---
        if pe_signal == "buy" and ma_signal == "up":
            bias = "bullish"
            bias_reason = f"PE低估({pct_str}) + 均线向上"
        elif pe_signal == "sell" or ma_signal == "down":
            bias = "bearish"
            bias_reason = f"PE高估({pct_str})" if pe_signal == "sell" else "均线向下(MA100)"
        else:
            bias = "neutral"
            bias_reason = f"PE中性({pct_str}), 均线{ma_signal}"

        # --- 落地为可执行动作 (结合当前持仓) ---
        if bias == "bullish":
            gap = target_amount - current_amount
            cur_pnl = pnl_map.get(etf["code"])
            # 反摊平红线 (2026-10-23): 已有持仓且浮亏超红线时, 即使估值看多也不加仓
            if gap > MIN_TRADE_AMOUNT and cur_pnl is not None and cur_pnl <= ANTI_AVERAGING_LOSS_THRESHOLD:
                direction, amount = "hold", 0.0
                reason = (f"{bias_reason}; 但现有持仓浮亏{cur_pnl:.2f}%已触及加仓红线"
                          f"({ANTI_AVERAGING_LOSS_THRESHOLD:.0f}%), 暂不加仓 "
                          f"(现持¥{current_amount:,.0f}/目标¥{target_amount:,.0f})")
            elif gap > MIN_TRADE_AMOUNT:
                direction, amount = "buy", round(gap, 2)
                reason = (f"{bias_reason}; 现持¥{current_amount:,.0f}/目标¥{target_amount:,.0f}, "
                          f"距目标仓位还差¥{gap:,.0f}")
            else:
                direction, amount = "hold", 0.0
                reason = (f"{bias_reason}; 仓位已达目标 (现持¥{current_amount:,.0f}/目标¥{target_amount:,.0f})")
        elif bias == "bearish":
            if current_amount > 0:
                direction = "reduce"
                amount = round(current_amount * CORE_REDUCE_RATIO, 2)
                reason = (f"{bias_reason}; 现持¥{current_amount:,.0f}, "
                          f"建议减仓{CORE_REDUCE_RATIO:.0%} (约¥{amount:,.0f})")
            else:
                direction, amount = "hold", 0.0
                reason = f"{bias_reason}; 当前无持仓, 暂时观望 (不建议新建仓)"
        else:
            direction, amount = "hold", 0.0
            reason = (f"{bias_reason}; 维持当前仓位 (现持¥{current_amount:,.0f}/目标¥{target_amount:,.0f})")

        signals.append({
            "etf_code": etf["code"],
            # 买入(新钱)用低费率份额代码; 减仓/持有必须指回实际持仓的 A 类老代码,
            # 否则用户会拿着没有份额的代码去赎回
            "fund_code": (preferred_buy_code(etf) if direction == "buy"
                          else etf["fund_code"]),
            "name": etf["name"],
            "category": "core",
            "direction": direction,
            "signal_bias": bias,
            "target_weight": etf["target_weight"],
            "pe_info": pe_result,
            "ma_signal": ma_signal,
            "current_amount": current_amount,
            "target_amount": target_amount,
            "amount": amount,
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
            score["fund_code"] = preferred_buy_code(etf)
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


def is_watch_only(h):
    """
    观察仓判定 (大树规则 2026-09-11):
    市值低于 MIN_POSITION_VALUE 的持仓不参与任何信号计算 —— 不赎回、不补仓、不止盈止损。
    这类仓位通常只剩几百元, 申赎一次的成本与精力不划算, 保留只为查看实际涨跌幅。

    注意: 仅排除"信号", 持仓仍会显示在看板中(标注为观察仓)。
    """
    mv = h.get("market_value") or 0
    # 在途申购(pending_buy)份额未确认, 市值记0, 按投入金额判断
    if h.get("status") == "pending_buy":
        mv = h.get("total_cost") or 0
    return mv < MIN_POSITION_VALUE


def is_anti_averaging_blocked(h):
    """
    反摊平红线判定 (2026-10-23新增):
    已持仓浮亏达到 ANTI_AVERAGING_LOSS_THRESHOLD(-10%) 时, 禁止再买入/补仓。

    为什么: 实证复盘显示, 半导体ETF在净值 3.10→2.14 的下跌区间被连续加仓17笔
    共184万, 加权成本只从3.10摊到2.4761, 而净值跌到1.9695 —— 摊平只是把
    仓位敞口放大, 亏损绝对额从6万滚到30万。真正的风险管理是止损, 不是加仓。

    返回 True 表示该持仓已被红线拦住, 不应产生买入信号。
    """
    return (h.get("profit_pct") or 0) <= ANTI_AVERAGING_LOSS_THRESHOLD


def _rotation_healthy(row):
    """持仓是否"还升": 综合动量 > 0 且 收盘价在 MA20 上方。

    用途 (2026-11-09 新增): ① 排名缓冲区内的持有保护
                            ② 允许超额持仓(>基础3只)的前提
    口径必须与 intraday_signal.py / backtest.py 保持一致。
    """
    if row is None:
        return False
    try:
        return (float(row["composite_score"]) > MOMENTUM_MIN_THRESHOLD
                and bool(row["above_ma20"]))
    except (KeyError, TypeError, ValueError):
        return False


def _all_satellite_healthy(holdings, momentum_df):
    """超额买入前提: 所有有效卫星持仓(排除观察仓/在途申购)都还健康。"""
    for h in holdings:
        if h.get("category") != "satellite" or is_watch_only(h):
            continue
        if h.get("status") == "pending_buy":
            continue  # 份额未确认, 资金已投入但无法判趋势, 视为健康
        matches = momentum_df[momentum_df["etf_code"] == h.get("etf_code")]
        row = matches.iloc[0] if not matches.empty else None
        if not _rotation_healthy(row):
            return False
    return True


def check_rotation_signals(holdings):
    """
    根据动量排名生成轮动信号
    holdings: 当前持仓列表
    返回: {"buy": [...], "sell": [...], "hold": [...]}
    """
    momentum_df = calc_momentum_scores()
    if momentum_df.empty:
        return {"buy": [], "sell": [], "hold": [], "momentum_df": momentum_df,
                "top_codes": set(), "top1_code": None}

    # 动量门槛过滤: 综合得分 <= 0 的ETF不纳入轮动
    eligible = momentum_df[momentum_df["composite_score"] > MOMENTUM_MIN_THRESHOLD].copy()

    # 卖出判定TopN = 纯动量前N (用户规则: 卖出条件之"跌出Top3"中的Top3指动量排名)
    # 注意: MA20只是买入侧闸门, 不能提前过滤进TopN, 否则会误伤"动量Top3内但当日收盘
    #       跌破MA20"的持仓 (如黄金ETF动量#3却因MA20下方被误判跌出前N建议赎回)
    top_n = eligible.head(ROTATION_HOLD_COUNT)
    top_codes = set(top_n["etf_code"].tolist())
    # 真·动量第1名 (2026-11-18 P0-3): 移动止盈的 8% 宽限只应给最强的那一只。
    # 旧实现把这个 top_codes(Top3全体) 传给 check_stop_signals, 与回测
    # backtest.py 的 eligible_scores[0][0] 口径分裂, 导致 11-13 机器人(#2)
    # 回撤 6.21pp 被 8% 宽限放过。
    top1_code = top_n.iloc[0]["etf_code"] if not top_n.empty else None
    # 排名缓冲区: 持仓跌出 Top3 后, 仍在 Top(HOLD+BUFFER) 内且健康 → 继续持有不卖
    buffer_limit = ROTATION_HOLD_COUNT + ROTATION_RANK_BUFFER

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
        return {"buy": [], "sell": [], "hold": hold_signals, "momentum_df": momentum_df,
                "top_codes": top_codes, "top1_code": top1_code}

    current_codes = {h["etf_code"] for h in holdings if h["category"] == "satellite"}

    buy_signals = []
    sell_signals = []
    hold_signals = []

    # 需要买入的: 动量Top3中未持有的 + MA20买入闸门
    # (MA20为买入侧独立闸门: 收盘价须在MA20上方, 否则跳过——不买入趋势走弱标的)
    # --- 新建仓数量约束 (2026-11-09) ---
    # 基础目标 3 只; 允许短期超额至 ROTATION_MAX_HOLD_COUNT 只,
    # 但前提是所有现有卫星持仓"动量>0 且 MA20上方"(都还升)。
    # 解决"满3只 + 7天赎回费锁卖不掉 + Top3有新标的"的死锁, 又不至于无限扩张。
    sat_count = sum(1 for h in holdings
                    if h.get("category") == "satellite" and not is_watch_only(h))
    all_healthy = _all_satellite_healthy(holdings, momentum_df)
    if sat_count < ROTATION_HOLD_COUNT:
        buy_slots = ROTATION_HOLD_COUNT - sat_count
    elif sat_count < ROTATION_MAX_HOLD_COUNT and all_healthy:
        buy_slots = ROTATION_MAX_HOLD_COUNT - sat_count
    else:
        buy_slots = 0
        if sat_count >= ROTATION_MAX_HOLD_COUNT:
            print(f"    [SKIP] 卫星持仓已达上限 {ROTATION_MAX_HOLD_COUNT} 只, 不再新增买入")
        else:
            print(f"    [SKIP] 卫星持仓 {sat_count} 只已超基础 {ROTATION_HOLD_COUNT} 只, "
                  f"且有持仓动量/趋势转弱, 不超额买入")

    for _, row in top_n.iterrows():
        if buy_slots <= 0:
            break
        if row["etf_code"] not in current_codes:
            if "above_ma20" in row.index and not row["above_ma20"]:
                print(f"    [SKIP] {row['name']} 动量Top{ROTATION_HOLD_COUNT}但收盘在MA20下方, 不满足买入闸门")
                continue
            # 动量持续性闸门 (2026-11-17): 拦住反弹尖峰/动量从高位掉下来的假信号
            if MOMENTUM_PERSIST_ENABLED and "persist_ok" in row.index and not row["persist_ok"]:
                print(f"    [SKIP] {row['name']} 动量持续性不足: {row.get('persist_info', '')}")
                continue
            buy_slots -= 1
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
        # 在途申购 (pending_buy): 份额尚未确认, 不能参与止损/轮动卖出判定
        if h.get("status") == "pending_buy":
            continue
        # 观察仓: 市值<MIN_POSITION_VALUE, 不赎回 (留作查看涨跌)
        if is_watch_only(h):
            continue

        if h["category"] == "satellite" and h["etf_code"] not in top_codes:
            code = h["etf_code"]
            matches = momentum_df[momentum_df["etf_code"] == code]
            row = matches.iloc[0] if not matches.empty else None
            healthy = _rotation_healthy(row)
            rank = int(row["rank"]) if row is not None else 9999

            # 缓冲区保护: 跌出Top3但仍在Top(HOLD+BUFFER)内, 且动量/趋势都还向上 → 不卖
            if rank <= buffer_limit and healthy:
                hold_signals.append({
                    "etf_code": code,
                    "fund_code": h["fund_code"],
                    "name": h["name"],
                    "category": h["category"],
                    "direction": "hold",
                    "reason": (f"动量第{rank}, 在Top{buffer_limit}缓冲区内"
                               f"(跌出Top{ROTATION_HOLD_COUNT}不硬切), 动量>0且MA20上方, 继续持有")
                })
                continue

            hold_days = h.get("hold_days", 0)
            if hold_days < ROTATION_CYCLE_DAYS:
                # 持有未满轮动周期, 暂不赎回, 加入持有列表
                hold_signals.append({
                    "etf_code": code,
                    "fund_code": h["fund_code"],
                    "name": h["name"],
                    "category": h["category"],
                    "direction": "hold",
                    "reason": f"跌出Top{buffer_limit}缓冲区或动量/趋势转弱, 但持有仅{hold_days}天 (需满{ROTATION_CYCLE_DAYS}天)"
                })
                continue
            sell_signals.append({
                "etf_code": code,
                "fund_code": h["fund_code"],
                "name": h["name"],
                "category": h["category"],
                "direction": "sell",
                "reason": (f"动量第{rank}, 跌出Top{buffer_limit}缓冲区"
                           if rank > buffer_limit else "动量转负或跌破MA20"),
                "profit_pct": h.get("profit_pct", 0),
                "hold_days": hold_days
            })

    # 继续持有的 / 仓位不足需补仓的
    for h in holdings:
        if h["category"] != "satellite" or h["etf_code"] not in top_codes:
            continue
        # 观察仓: 市值<MIN_POSITION_VALUE, 不补仓 (避免为几百元仓位追加资金)
        if is_watch_only(h):
            continue
        rank_row = top_n[top_n["etf_code"] == h["etf_code"]].iloc[0]

        # 反摊平红线 (2026-10-23): 浮亏已超 -10% 的持仓禁止补仓,
        # 避免"越跌越买"把仓位敞口放大 (半导体17笔摊平的历史教训)
        if is_anti_averaging_blocked(h):
            hold_signals.append({
                "etf_code": h["etf_code"],
                "fund_code": h["fund_code"],
                "name": h["name"],
                "category": h["category"],
                "direction": "hold",
                "reason": (f"动量排名第{rank_row['rank']}, 但浮亏{h.get('profit_pct', 0):.2f}%"
                           f"已触及加仓红线({ANTI_AVERAGING_LOSS_THRESHOLD:.0f}%), 禁止摊平加仓")
            })
            continue

        mv = h.get("market_value") or 0
        # 在途申购 (pending_buy): 份额未确认, 市值记为0, 但资金已占用 → 按投入金额计仓位, 避免重复申购
        if h.get("status") == "pending_buy":
            mv = h.get("total_cost") or 0
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

    return {"buy": buy_signals, "sell": sell_signals, "hold": hold_signals,
            "momentum_df": momentum_df, "top_codes": top_codes, "top1_code": top1_code}


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
        # 在途申购 (pending_buy): 份额未确认, 市值0会被误算成-100%亏损而触发假止损
        if h.get("status") == "pending_buy":
            continue
        # 观察仓: 市值<MIN_POSITION_VALUE, 不触发止盈止损 (留作查看涨跌)
        if is_watch_only(h):
            continue

        profit_pct = h.get("profit_pct", 0)
        hold_days = h.get("hold_days", 0)

        # === 峰值跟踪 (2026-11-18 P0-1) ===
        # 🔴 峰值「记账」必须与「能不能卖」解耦: 7天赎回费锁内虽然不能赎回, 但
        #    盈利峰值依然要入库。旧实现把峰值更新放在 `hold_days < 7 and profit_pct > 0`
        #    的 continue 之后, 导致锁内急涨的峰值永久丢失 —— 解锁时浮盈已回落,
        #    peak 恒为 0, 移动止盈再也不会激活。实证: 机器人 020972 于 11-10 申购 /
        #    11-11 确认, 11-11 净值 1.3646 浮盈 +9.05%(持有1天) 未记峰值, 11-17
        #    解锁时已 -4.11%, 最终只能靠 MA20 趋势止损兜底。
        #    持有期只约束下面的「是否生成卖出信号」, 不再影响记账。
        # 宽基不走移动止盈, 不记峰值(记了也不会用); 观察仓/在途已在上面排除。
        peak_profit = h.get("peak_profit_pct") or 0
        if (h.get("category") != "core"
                and profit_pct >= TRAILING_STOP_ACTIVATE
                and profit_pct > peak_profit):
            peak_profit = profit_pct
            if h.get("id"):
                # 立即单独落库: 万一后面的 MA20 查询/卖出判定抛异常, 峰值也不会
                # 被同一事务回滚掉 (这正是「记账与判定解耦」的最后一层)。
                conn.execute("UPDATE portfolio SET peak_profit_pct = ? WHERE id = ?",
                             (peak_profit, h["id"]))
                conn.commit()
                peak_updates.append((peak_profit, h["id"]))

        # 最短持有期检查 (只约束卖出信号, 不影响上面的峰值记账)
        if hold_days < MIN_HOLD_DAYS and profit_pct > 0:
            continue

        # 宽基ETF: 不做移动止盈/MA20趋势止损 (那套由PE估值+MA100趋势管理),
        # 仅保留一条"超宽固定止损"兜底 (2026-10-14新增), 防止极端单边行情下底仓无底线拖累
        if h.get("category") == "core":
            core_stop = abs(CORE_STOP_LOSS_PANIC_THRESHOLD if is_panic else CORE_STOP_LOSS_THRESHOLD)
            if hold_days >= MIN_HOLD_DAYS and profit_pct <= -core_stop:
                signals.append({
                    "etf_code": h["etf_code"],
                    "fund_code": h["fund_code"],
                    "name": h["name"],
                    "category": h["category"],
                    "direction": "sell",
                    "signal_type": "stop_loss",
                    "reason": f"{'[恐慌加急] ' if is_panic else ''}宽基兜底止损: 亏损{abs(profit_pct):.2f}% (阈值-{core_stop:.0f}%)",
                    "profit_pct": profit_pct,
                    "sell_ratio": 1.0,
                    "priority": "urgent" if is_panic else "high"
                })
            continue

        # 确定此持仓的回撤阈值
        is_top1 = bool(momentum_top_codes) and h.get("etf_code") in momentum_top_codes
        if is_top1 and not is_panic:
            etf_pullback = TRAILING_STOP_TOP1_PULLBACK
        else:
            etf_pullback = trailing_pullback

        # === 移动止盈 (峰值已在循环开头无条件记账, 这里只做回撤判定) ===
        # 从峰值回撤超过阈值 → 清仓信号
        if peak_profit > 0 and (peak_profit - profit_pct) >= etf_pullback:
            signals.append({
                "etf_code": h["etf_code"],
                "fund_code": h["fund_code"],
                "name": h["name"],
                "category": h["category"],
                "direction": "sell",
                "signal_type": "trailing_stop",
                "reason": (f"移动止盈: 峰值+{peak_profit:.2f}%, 现{profit_pct:+.2f}%, "
                           f"回撤{peak_profit - profit_pct:.2f}pp ≥ {etf_pullback:.0f}pp"),
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

    # 峰值已在循环里即时落库, 这里只报告数量
    if peak_updates:
        print(f"  [OK] 利润峰值更新: {len(peak_updates)}只持仓")

    conn.close()
    return signals


# ============================================================
# 模块5: 执行纪律检查 (卖出信号逾期)
# ============================================================

def check_unexecuted_signals(holdings=None, lookback_days=None, grace_days=None):
    """
    检查"已经报警、但至今没有执行赎回"的卖出信号 (2026-10-23新增)。

    背景: 2026-09-14 之后连续26个交易日零交易, 同期趋势止损每天都在报警,
    三只持仓从 ¥164.8万 拖到 ¥132.97万, 拖延成本约32万。
    本函数把"不执行"这件事本身变成看板顶部的一条硬提醒。

    判定口径:
      1) 先取最近 lookback_days 个有卖出信号的交易日 → 筛出"当下仍在报警"的标的
      2) 对每个标的, 从最新一条卖出信号往前回溯"连续报警段"的起点
         (相邻两次信号之间不超过 2 个交易日视为连续; 一旦断档就停, 避免把
          几个月前孤立出现过的信号也算进这一次的拖延)
      3) 该连续段起点之后若已有卖出交易 → 视为已响应
      4) 逾期交易日数 > grace_days 才算逾期 (信号当日/次日视为在办理)

    返回: list[dict], 按逾期天数降序
    """
    lookback_days = lookback_days or SIGNAL_OVERDUE_LOOKBACK_DAYS
    grace_days = SIGNAL_OVERDUE_GRACE_DAYS if grace_days is None else grace_days
    conn = get_connection()

    recent = conn.execute(
        """SELECT DISTINCT signal_date FROM signals
           WHERE direction = 'sell' ORDER BY signal_date DESC LIMIT ?""",
        (lookback_days,)).fetchall()
    if not recent:
        conn.close()
        return []
    window_start = recent[-1]["signal_date"]
    latest_signal_date = recent[0]["signal_date"]

    active = conn.execute(
        """SELECT fund_code, name, etf_code, MAX(signal_date) last_seen
           FROM signals
           WHERE direction = 'sell' AND signal_date >= ? AND fund_code IS NOT NULL
             AND signal_type IN ('trend_stop', 'stop_loss', 'trailing_stop')
             AND is_executed = 0
           GROUP BY fund_code""",
        (window_start,)).fetchall()

    mv_map = {}
    if holdings is None:
        holdings = [dict(r) for r in conn.execute(
            "SELECT * FROM portfolio WHERE status IN ('holding','pending_buy')")]
    for h in holdings:
        fc = h.get("fund_code")
        if fc:
            mv_map[fc] = (mv_map.get(fc) or 0) + (h.get("market_value") or 0)

    result = []
    for r in active:
        fc = r["fund_code"]
        mv = mv_map.get(fc, 0) or 0
        # 已清仓 / 观察仓不提醒 (几百元的仓位本来就不值得动)
        if mv < MIN_POSITION_VALUE:
            continue

        # 回溯"连续报警段"的起点 (从最新信号往前, 断档即止)
        sig_dates = [x["signal_date"] for x in conn.execute(
            """SELECT DISTINCT signal_date FROM signals
               WHERE fund_code = ? AND direction = 'sell'
                 AND signal_type IN ('trend_stop', 'stop_loss', 'trailing_stop')
                 AND is_executed = 0
               ORDER BY signal_date""", (fc,)).fetchall()]
        if not sig_dates:
            continue
        first = sig_dates[-1]
        for d in reversed(sig_dates[:-1]):
            if _count_trading_days(conn, d, first) <= 2:
                first = d
            else:
                break

        # 这段连续报警期间若有卖出成交, 说明已响应
        last_sell = conn.execute(
            """SELECT MAX(submit_date) d FROM trades
               WHERE fund_code = ? AND trade_type = 'sell'""", (fc,)).fetchone()["d"]
        if last_sell and last_sell >= first:
            continue

        days = _count_trading_days(conn, first, latest_signal_date)
        if days <= grace_days:
            continue
        last = conn.execute(
            """SELECT signal_date, signal_type, reason, name FROM signals
               WHERE fund_code = ? AND direction = 'sell'
                 AND signal_type IN ('trend_stop', 'stop_loss', 'trailing_stop')
               ORDER BY id DESC LIMIT 1""", (fc,)).fetchone()
        chg = _price_change_since(conn, r["etf_code"], first)
        est = None
        if chg is not None and chg > -100 and mv > 0:
            # 若在信号日就卖出, 现在的现金会比当前持仓市值多出多少
            est = round(mv * (1 / (1 + chg / 100) - 1), 2)
        result.append({
            "name": last["name"] if last else r["name"],
            "fund_code": fc,
            "etf_code": r["etf_code"],
            "first_signal_date": first,
            "last_signal_date": last["signal_date"] if last else first,
            "days": days,
            "signal_type": last["signal_type"] if last else "",
            "reason": last["reason"] if last else "",
            "price_change_pct": round(chg, 2) if chg is not None else None,
            "market_value": mv,
            "est_slippage": est,
        })

    conn.close()
    result.sort(key=lambda x: (-x["days"], -(x["market_value"] or 0)))
    return result


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


def _count_trading_days(conn, start_date, end_date):
    """统计 (start_date, end_date] 之间的交易日数 (用 index_daily 的真实交易日历)"""
    row = conn.execute(
        """SELECT COUNT(DISTINCT trade_date) n FROM index_daily
           WHERE trade_date > ? AND trade_date <= ?""",
        (start_date, end_date)).fetchone()
    return row["n"] if row and row["n"] else 0


def _price_change_since(conn, etf_code, since_date):
    """信号日之后该ETF场内价格的累计涨跌幅(%), 数据不足返回 None"""
    if not etf_code:
        return None
    a = conn.execute(
        """SELECT close FROM index_daily WHERE ts_code = ? AND trade_date >= ?
           ORDER BY trade_date LIMIT 1""", (etf_code, since_date)).fetchone()
    b = conn.execute(
        """SELECT close FROM index_daily WHERE ts_code = ?
           ORDER BY trade_date DESC LIMIT 1""", (etf_code,)).fetchone()
    if a and b and a["close"]:
        return (b["close"] / a["close"] - 1) * 100
    return None


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
    """获取最新交易日的北向成交额 (注: 该字段实为 hgt+sgt 成交流水, 非净流入)"""
    row = conn.execute("""
        SELECT north_money, trade_date FROM north_money_flow
        ORDER BY trade_date DESC LIMIT 1
    """).fetchone()
    if row and row["north_money"] is not None:
        return (row["north_money"], row["trade_date"])
    return (None, None)


def _get_north_turnover_ratio(conn, ma_days=None):
    """计算北向成交额异动倍数 = 最新成交额 / 近N日均值, 返回 (ratio, date)

    north_money 实为 hgt+sgt 的成交流水(恒正), 无法表达"净流出";
    2024-08起北向净流入已停止披露, 改用"成交额相对自身均值是否骤增"捕捉外资异动。
    """
    ma_days = ma_days or PANIC_NORTH_MA_DAYS
    rows = conn.execute("""
        SELECT north_money, trade_date FROM north_money_flow
        WHERE north_money IS NOT NULL AND north_money > 0
        ORDER BY trade_date DESC LIMIT ?
    """, (ma_days + 1,)).fetchall()
    if len(rows) < ma_days:
        return (None, None)

    latest = rows[0]["north_money"]
    # rows[1:] 为除当日外的近N日, 用它们算均值 (避免当日值稀释分母)
    history = [r["north_money"] for r in rows[1:ma_days + 1]]
    if not history:
        return (None, None)
    avg = sum(history) / len(history)
    if avg <= 0:
        return (None, None)
    return (latest / avg, rows[0]["trade_date"])



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
                                   north_money, north_turnover_ratio, north_date,
                                   limit_down_count, limit_down_date,
                                   volume_ratio, volume_date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (date, level, "|".join(triggered),
          details.get("hs300_pct_chg"), dates.get("hs300_date"),
          details.get("north_money"), details.get("north_turnover_ratio"),
          dates.get("north_date"),
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

    # 动量持续性过滤 (2026-11-17): 只约束新买入, 不影响排名/卖出判定
    persist_ok, persist_info = check_persistence(
        closes,
        enabled=MOMENTUM_PERSIST_ENABLED,
        mode=MOMENTUM_PERSIST_MODE,
        lookback=MOMENTUM_PERSIST_LOOKBACK,
        min_delta=MOMENTUM_PERSIST_MIN_DELTA,
        require_positive=MOMENTUM_PERSIST_REQUIRE_POSITIVE,
    )

    return {
        "momentum_20d": round(momentum_20d, 4),
        "momentum_60d": round(momentum_60d, 4),
        "ma20": ma20,
        "above_ma20": above_ma20,
        "persist_ok": persist_ok,
        "persist_info": persist_info.get("reason", ""),
    }

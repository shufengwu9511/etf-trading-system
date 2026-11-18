#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
========================================================================
  盘中信号核心逻辑 (共享模块)
  - 被 scripts/run_intraday_signal.py (CLI) 和 dashboard.py (看板) 复用
  - 只计算并返回结构化结果, 不打印、不写任何状态
========================================================================
  原理:
    场外申赎 15:00前提交 → 按当日收盘净值成交; 全部决策规则
    (动量/MA20/Top3) 都只依赖"当日收盘价"。在 14:40-14:50 用场内ETF
    盘中现价代理今日收盘价计算信号, 即可做到当日决策 → 当日按净值成交,
    消除 T+1 时间差。盘中现价对收盘的预测误差通常 <0.3%。
  持仓状态:
    每天从主系统数据库 portfolio 表读取 (买入日期=份额确认日);
    本模块不自动记账, 实际申赎由用户在持仓系统(main.py)中维护。
========================================================================
"""
from datetime import datetime

from config import (
    SATELLITE_ETFS, preferred_buy_code,
    MOMENTUM_SHORT, MOMENTUM_MEDIUM,
    MOMENTUM_WEIGHT_SHORT, MOMENTUM_WEIGHT_MEDIUM,
    MOMENTUM_MIN_THRESHOLD, ROTATION_HOLD_COUNT, MIN_HOLD_DAYS,
    MOMENTUM_PERSIST_ENABLED, MOMENTUM_PERSIST_MODE,
    MOMENTUM_PERSIST_LOOKBACK, MOMENTUM_PERSIST_MIN_DELTA,
    MOMENTUM_PERSIST_REQUIRE_POSITIVE,
    ROTATION_CYCLE_DAYS, MIN_POSITION_VALUE,
    ROTATION_RANK_BUFFER, ROTATION_MAX_HOLD_COUNT,
    # 2026-11-18 P0-2: 盘中补齐止盈/止损判定, 与 strategy.check_stop_signals 同参数
    TRAILING_STOP_ACTIVATE, TRAILING_STOP_PULLBACK,
    TRAILING_STOP_PANIC_PULLBACK, TRAILING_STOP_TOP1_PULLBACK,
    STOP_LOSS_THRESHOLD, STOP_LOSS_PANIC_THRESHOLD,
    MA20_STOP_LOSS_RATIO,
)
from db import get_connection
from tdx_data import get_realtime_quotes
from momentum_persistence import check_persistence

# 交易时段窗口 (建议运行时间)
RECOMMEND_START = datetime.strptime("14:30", "%H:%M").time()
RECOMMEND_END = datetime.strptime("14:50", "%H:%M").time()


# ============================================================
# 历史K线与动量计算
# ============================================================

def load_history_closes(conn, ts_code, today_str):
    """从数据库读取ETF历史收盘价序列 (截至昨收, 不含今日)

    每日 main.py 会把当日场内K线写入 index_daily, 盘中再跑 main.py 时
    数据库里可能有"今日未收盘bar", 所以这里强制排除今日。
    """
    rows = conn.execute("""
        SELECT trade_date, close FROM index_daily
        WHERE ts_code = ? AND close IS NOT NULL AND close > 0 AND trade_date < ?
        ORDER BY trade_date
    """, (ts_code, today_str)).fetchall()
    return [r["close"] for r in rows]


def compute_momentum(closes, price):
    """用盘中现价替换最新收盘价, 计算动量得分 + MA20趋势状态

    与 strategy.py 的 _calc_single_momentum 保持一致 (价格比法):
      10日动量 = 最新价 / 10日前收盘 - 1
      30日动量 = 最新价 / 30日前收盘 - 1
      综合得分 = 0.6 * 10日动量 + 0.4 * 30日动量
      MA20     = 最近20日收盘(含今日现价)均值
    """
    if len(closes) < MOMENTUM_MEDIUM + 1:
        return None

    # 历史序列是升序, 把现价追加到末尾作为"今日收盘代理"
    series = closes + [price]

    mom_short = (series[-1] / series[-(MOMENTUM_SHORT + 1)] - 1) * 100.0
    mom_medium = (series[-1] / series[-(MOMENTUM_MEDIUM + 1)] - 1) * 100.0

    ma20 = None
    above_ma20 = False
    if len(series) >= 20:
        ma20 = sum(series[-20:]) / 20
        above_ma20 = series[-1] > ma20

    # 动量持续性过滤 (2026-11-17): 与 strategy / backtest 共用同一模块, 口径一致
    persist_ok, persist_info = check_persistence(
        series,
        enabled=MOMENTUM_PERSIST_ENABLED,
        mode=MOMENTUM_PERSIST_MODE,
        lookback=MOMENTUM_PERSIST_LOOKBACK,
        min_delta=MOMENTUM_PERSIST_MIN_DELTA,
        require_positive=MOMENTUM_PERSIST_REQUIRE_POSITIVE,
    )

    return {
        "momentum_10d": round(mom_short, 4),
        "momentum_30d": round(mom_medium, 4),
        "composite_score": round(mom_short * MOMENTUM_WEIGHT_SHORT + mom_medium * MOMENTUM_WEIGHT_MEDIUM, 4),
        "ma20": round(ma20, 4) if ma20 else None,
        "above_ma20": above_ma20,
        "persist_ok": persist_ok,
        "persist_info": persist_info.get("reason", ""),
    }


# ============================================================
# 持仓状态获取 (每天从数据库同步, 不自动记账)
# ============================================================

def _fund_code_to_etf(fund_code):
    """联接基金代码 → 场内ETF配置信息(含低费率替代份额代码)"""
    from config import find_etf_by_fund_code
    info = find_etf_by_fund_code(fund_code)
    if info and info["category"] == "satellite":
        return info
    return None


def parse_holdings_arg(arg_str):
    """解析 --holdings "基金代码:买入日期YYYYMMDD,基金代码:买入日期" 参数
    返回 {etf_code: {"fund_code","name","buy_date"}}
    """
    result = {}
    for token in arg_str.split(","):
        token = token.strip()
        if not token:
            continue
        if ":" in token:
            fund_code, buy_date = token.split(":", 1)
        else:
            fund_code, buy_date = token, datetime.now().strftime("%Y%m%d")
        fund_code = fund_code.strip()
        buy_date = buy_date.strip()
        etf = _fund_code_to_etf(fund_code)
        if not etf:
            print(f"  [WARN] 基金代码 {fund_code} 不在卫星候选池中, 跳过")
            continue
        result[etf["code"]] = {
            "fund_code": etf["fund_code"],
            "etf_code": etf["code"],
            "name": etf["name"],
            "buy_date": buy_date,
        }
    return result


def sync_holdings_from_portfolio():
    """从主系统数据库同步当前卫星持仓
    买入日期优先用份额确认日 confirm_date, 否则用申购日 buy_date
    """
    conn = get_connection()
    rows = conn.execute("""
        SELECT fund_code, etf_code, name, confirm_date, buy_date, status,
               COALESCE(market_value, 0) AS market_value,
               COALESCE(profit_pct, 0) AS profit_pct,
               COALESCE(peak_profit_pct, 0) AS peak_profit_pct,
               nav_date
        FROM portfolio
        WHERE status IN ('holding', 'pending_buy') AND category = 'satellite'
    """).fetchall()
    conn.close()

    result = {}
    for r in rows:
        # 观察仓: 市值<MIN_POSITION_VALUE 不参与信号计算 (留作查看实际涨跌幅)
        if r["market_value"] < MIN_POSITION_VALUE:
            continue
        etf = _fund_code_to_etf(r["fund_code"])
        if etf is None:
            continue
        buy_date = r["confirm_date"] or r["buy_date"] or datetime.now().strftime("%Y%m%d")
        result[r["etf_code"]] = {
            "fund_code": r["fund_code"],
            "etf_code": r["etf_code"],
            "name": r["name"],
            "buy_date": buy_date,
            "status": r["status"],
            # 2026-11-18 P0-2: 盘中止盈/止损要用 DB 里的浮盈与峰值(基于 T-1 场外净值),
            # 再用当日场内涨跌幅做增量修正。nav_date 用于识别净值滞后。
            "profit_pct": float(r["profit_pct"] or 0),
            "peak_profit_pct": float(r["peak_profit_pct"] or 0),
            "nav_date": r["nav_date"],
        }
    return result


def init_holdings_state(holdings_arg=None):
    """获取当前持仓状态: 每天从主系统数据库(portfolio表)同步
    数据库为空或需要覆盖时, 可用 --holdings 手动声明
    返回 {"holdings": {...}, "source": "..."}
    """
    holdings = sync_holdings_from_portfolio()
    source = "数据库(portfolio表)同步"

    if not holdings and holdings_arg:
        holdings = parse_holdings_arg(holdings_arg)
        source = "--holdings 参数 (数据库无持仓)"

    return {"holdings": holdings, "source": source}


def calc_hold_days(buy_date_str):
    """计算持有天数 (自然日, 含确认日当天), 与 portfolio._calc_hold_days 同源。

    入参来自 sync_holdings_from_portfolio(), 实际传的是 confirm_date (份额确认日)。
    🔴 2026-11-12 修正 off-by-one: 易方达口径为「申购确认日(含) ~ 赎回确认日
    前一个自然日(含)」, 今日提交赎回则 T+1 确认, 故 = (today - confirm).days + 1。
    旧实现少算 1 天, 会把可赎回日推迟一天; 盘中与主流程必须同口径, 否则
    又会重现「盘中喊卖、主流程锁着」的指令分裂。
    """
    try:
        d = datetime.strptime(str(buy_date_str).replace("-", ""), "%Y%m%d")
        return max(0, (datetime.now() - d).days + 1)
    except Exception:
        return 0


def _row_healthy(r):
    """持仓是否"还升": 综合动量>0 且 现价在MA20上方。
    口径与 strategy._rotation_healthy / backtest 保持一致 (2026-11-09)。
    """
    if r is None:
        return False
    return (r.get("composite_score", 0) > MOMENTUM_MIN_THRESHOLD
            and bool(r.get("above_ma20")))


# ============================================================
# 止盈止损 (2026-11-18 P0-2)
# 旧版盘中信号只有轮动类卖出, 14:40 这个场外申赎唯一决策窗口看不到
# 移动止盈/趋势止损/固定止损 → 只能等盘后 main.py, 再 T+1 提交, 结构性滑一天。
# ============================================================

def _read_panic_state(today_str):
    """读取当日最新一次恐慌判定, 返回 (is_panic, 当日有无记录)。

    盘中不重算恐慌指标(要抓全市场快照/跌停家数, 成本高), 直接复用主流程
    当天已落库的 panic_alerts。当日无记录则按非恐慌处理, 结果里会标注来源。
    与主流程完全一致的实时判定仍以当晚 main.py 为准。
    """
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT alert_level FROM panic_alerts
            WHERE trade_date = ? ORDER BY id DESC LIMIT 1
        """, (today_str,)).fetchone()
    finally:
        conn.close()
    return bool(row and row["alert_level"] == "PANIC"), bool(row)


def _check_intraday_stop(h, row, profit_pct, hold_days, is_panic, top1_code):
    """盘中版单持仓止盈止损判定, 与 strategy.check_stop_signals 同口径/同优先级。

    判定顺序: 移动止盈 → MA20趋势止损 → 固定止损, 命中即返回一条卖出信号。
    刻意保留的差异:
      · 盘中不写库。峰值只在本次估算里用 max(库内峰值, 当日新高), 记账由当晚
        main.py 负责(P0-1 已保证锁内也会记)。
      · 浮盈 = DB(T-1 场外净值) × 当日场内涨跌幅, 是估算值, reason 明确标注。
    """
    code = h["etf_code"]
    # 与盘后一致: 7天赎回费锁内且仍盈利 → 不产生任何卖出信号
    if hold_days < MIN_HOLD_DAYS and profit_pct > 0:
        return None

    stop_loss = abs(STOP_LOSS_PANIC_THRESHOLD if is_panic else STOP_LOSS_THRESHOLD)
    pullback = TRAILING_STOP_PANIC_PULLBACK if is_panic else TRAILING_STOP_PULLBACK
    # 8% 宽限只给真·动量第1名 (P0-3 口径)
    if top1_code and code == top1_code and not is_panic:
        pullback = TRAILING_STOP_TOP1_PULLBACK

    peak = h.get("peak_profit_pct") or 0
    if profit_pct >= TRAILING_STOP_ACTIVATE and profit_pct > peak:
        peak = profit_pct

    base = {"name": h["name"], "fund_code": h["fund_code"],
            "rank": row.get("rank"), "hold_days": hold_days,
            "direction": "sell", "profit_pct": round(profit_pct, 2),
            "priority": "urgent" if is_panic else "high"}

    if peak > 0 and (peak - profit_pct) >= pullback:
        return {**base, "signal_type": "trailing_stop",
                "reason": (f"移动止盈(盘中估算): 峰值+{peak:.2f}%, 现约{profit_pct:+.2f}%, "
                           f"回撤{peak - profit_pct:.2f}pp ≥ {pullback:.0f}pp"
                           + (" [恐慌加急]" if is_panic else ""))}

    ma20, price = row.get("ma20"), row.get("price")
    if hold_days >= MIN_HOLD_DAYS and ma20 and price:
        if price < ma20 * MA20_STOP_LOSS_RATIO:
            below = (price / ma20 - 1) * 100
            return {**base, "signal_type": "trend_stop",
                    "reason": (f"趋势止损: 现价{price:.4f} < MA20×{MA20_STOP_LOSS_RATIO}"
                               f"({ma20 * MA20_STOP_LOSS_RATIO:.4f}), 跌破{below:.2f}%")}

    if profit_pct <= -stop_loss:
        return {**base, "signal_type": "stop_loss",
                "reason": (f"{'[恐慌加急] ' if is_panic else ''}止损: 盘中估算浮亏{profit_pct:.2f}% "
                           f"(阈值-{stop_loss:.0f}%)")}
    return None


# ============================================================
# 主计算入口
# ============================================================

def generate_signals(codes=None, holdings_arg=None, is_panic=None):
    """计算盘中信号 (现价代理收盘价), 返回结构化结果 dict

    Args:
        codes: 逗号分隔的场内ETF代码, 仅计算指定ETF (默认全部卫星池)
        holdings_arg: 手动声明持仓 "基金代码:买入日期YYYYMMDD,..."
                      (仅在数据库无持仓时生效)
        is_panic: 恐慌状态。None = 读当日 panic_alerts; 显式 bool 可覆盖。

    Returns:
        {"ok": True, "generated_at", "in_recommend_window", "is_weekend",
         "rankings": [...], "top_n": [...],
         "buy": [...], "sell": [...], "hold": [...], "locked": [...],
         "is_panic", "panic_source", "top1_code", "data_warnings": [...]}
        失败时返回 {"ok": False, "error": "..."}
    """
    today = datetime.now()
    today_str = today.strftime("%Y%m%d")

    # 1. 确定候选池
    if codes:
        target_codes = {c.strip() for c in codes.split(",") if c.strip()}
        etfs = [e for e in SATELLITE_ETFS if e["code"] in target_codes]
        if not etfs:
            return {"ok": False, "error": "指定的 codes 均不在卫星候选池中"}
    else:
        etfs = SATELLITE_ETFS

    # 2. 历史K线 (截至昨收) + 昨收用于实时报价缩放校准
    conn = get_connection()
    history = {}
    prev_close = {}
    for etf in etfs:
        closes = load_history_closes(conn, etf["code"], today_str)
        history[etf["code"]] = closes
        if closes:
            prev_close[etf["code"]] = closes[-1]
    conn.close()

    # 3. 盘中实时行情
    quotes = get_realtime_quotes([e["code"] for e in etfs], prev_close_map=prev_close)
    if not quotes:
        return {"ok": False, "error": "实时行情获取失败 (pytdx与AkShare均不可用, 或已休市)"}

    # 4. 计算动量并排名
    rows = []
    for etf in etfs:
        code = etf["code"]
        q = quotes.get(code)
        if not q:
            continue
        closes = history.get(code, [])
        if len(closes) < MOMENTUM_MEDIUM + 1:
            continue
        mom = compute_momentum(closes, q["price"])
        if not mom:
            continue
        rows.append({
            "etf_code": code,
            # 买入信号用低费率替代份额代码; 卖出/持有仍用持仓表实际 fund_code
            "fund_code": preferred_buy_code(etf),
            "name": etf["name"],
            "price": q["price"],
            "last_close": q["last_close"],
            "change_pct": q["change_pct"],
            **mom,
        })

    if not rows:
        return {"ok": False, "error": "无有效动量数据"}

    rows.sort(key=lambda r: r["composite_score"], reverse=True)
    for i, r in enumerate(rows, 1):
        r["rank"] = i

    # 5. 持仓状态 (每天从数据库同步, 不自动记账)
    state = init_holdings_state(holdings_arg)
    holdings = state.get("holdings", {})

    # 5b. 恐慌状态 + 净值新鲜度 (2026-11-18 P0-2)
    if is_panic is None:
        panic_state, has_record = _read_panic_state(today_str)
        panic_source = "当日 panic_alerts" if has_record else "当日无恐慌记录(按正常处理)"
    else:
        panic_state, panic_source = bool(is_panic), "调用方指定"

    data_warnings = []
    for _h in holdings.values():
        nav_date = str(_h.get("nav_date") or "").replace("-", "")
        if len(nav_date) == 8:
            try:
                lag = (today - datetime.strptime(nav_date, "%Y%m%d")).days
            except ValueError:
                continue
            if lag > 4:
                data_warnings.append(
                    f"{_h['name']} 净值停留在 {nav_date} (滞后{lag}天), "
                    f"盘中止盈止损的浮盈估算可能失真, 请先跑 main.py 刷新净值")
                break

    # 6. 生成信号
    # 卖出判定TopN = 纯动量前N (MA20只约束买入, 不提前过滤进TopN,
    # 否则动量Top3内但当日跌破MA20的持仓会被误判"跌出前N", 与主系统修复后一致)
    momentum_top = [r for r in rows if r["composite_score"] > MOMENTUM_MIN_THRESHOLD][:ROTATION_HOLD_COUNT]
    top_codes = {r["etf_code"] for r in momentum_top}
    # 真·动量第1名: 8% 移动止盈宽限只给这一只 (P0-3 与盘后/回测对齐)
    top1_code = momentum_top[0]["etf_code"] if momentum_top else None
    buffer_limit = ROTATION_HOLD_COUNT + ROTATION_RANK_BUFFER
    row_by_code = {r["etf_code"]: r for r in rows}

    buy_signals = []
    sell_signals = []
    hold_signals = []
    locked = []  # 7天锁未解锁

    # --- 新建仓数量约束 (2026-11-09) ---
    # 基础3只, 全部持仓仍健康时允许超额至 ROTATION_MAX_HOLD_COUNT 只。
    # sync_holdings_from_portfolio 已排除观察仓; 在途申购无法判趋势, 视为健康。
    sat_count = len(holdings)
    all_healthy = all(_row_healthy(row_by_code.get(code))
                      for code, h in holdings.items()
                      if h.get("status") != "pending_buy")
    if sat_count < ROTATION_HOLD_COUNT:
        buy_slots = ROTATION_HOLD_COUNT - sat_count
    elif sat_count < ROTATION_MAX_HOLD_COUNT and all_healthy:
        buy_slots = ROTATION_MAX_HOLD_COUNT - sat_count
    else:
        buy_slots = 0

    persist_skipped = []
    for r in momentum_top:
        if buy_slots <= 0:
            break
        # 买入闸门: 未持有 + MA20上方 + 动量持续性 (综合得分>0 已由 momentum_top 过滤)
        if r["etf_code"] in holdings or not r["above_ma20"]:
            continue
        if MOMENTUM_PERSIST_ENABLED and not r.get("persist_ok", True):
            persist_skipped.append({"name": r["name"], "fund_code": r["fund_code"],
                                    "reason": r.get("persist_info", "")})
            continue
        buy_slots -= 1
        buy_signals.append(r)

    for code, h in holdings.items():
        # 该持仓是否还在候选池 (可能已不在 SATELLITE_ETFS)
        row = next((r for r in rows if r["etf_code"] == code), None)
        if row is None:
            hold_signals.append({"name": h["name"], "fund_code": h["fund_code"],
                                 "reason": "不在候选池, 保持现状"})
            continue

        # 持有期统一在循环开头算, 止盈止损与轮动共用同一把尺子
        hold_days = calc_hold_days(h.get("buy_date", ""))
        need_days = max(MIN_HOLD_DAYS, ROTATION_CYCLE_DAYS)

        # --- 2026-11-18 P0-2: 止盈/止损优先于轮动, 与盘后同优先级 ---
        # 浮盈 = DB(T-1 场外净值) 复利叠加当日场内涨跌幅 → 当日净值的盘中估算。
        chg = float(row.get("change_pct") or 0)
        profit_pct = ((1 + (h.get("profit_pct") or 0) / 100) * (1 + chg / 100) - 1) * 100
        stop_sig = _check_intraday_stop(h, row, profit_pct, hold_days,
                                        panic_state, top1_code)
        if stop_sig:
            # 赎回费锁未解 → 只登记不喊卖, 避免与主流程指令分裂
            if hold_days < need_days:
                locked.append({"name": h["name"], "fund_code": h["fund_code"],
                               "rank": row["rank"], "hold_days": hold_days})
                hold_signals.append({"name": h["name"], "fund_code": h["fund_code"],
                                     "rank": row["rank"],
                                     "reason": f"{stop_sig['reason']}, 但持有仅{hold_days}天"
                                               f" (需满{need_days}天), 暂不赎回"})
                continue
            sell_signals.append(stop_sig)
            continue

        if code in top_codes:
            hold_signals.append({"name": h["name"], "fund_code": h["fund_code"],
                                 "rank": row["rank"],
                                 "reason": f"动量第{row['rank']}, 继续持有"})
        else:
            # 缓冲区保护 (2026-11-09): 跌出Top3但仍在Top(HOLD+BUFFER)内,
            # 且动量/趋势都还向上 → 继续持有, 不因排名边界抖动就赎回
            if row["rank"] <= buffer_limit and _row_healthy(row):
                hold_signals.append({"name": h["name"], "fund_code": h["fund_code"],
                                     "rank": row["rank"],
                                     "reason": (f"动量第{row['rank']}, 在Top{buffer_limit}缓冲区内"
                                                f"(跌出Top{ROTATION_HOLD_COUNT}不硬切), 动量>0且MA20上方, 继续持有")})
                continue

            # 卖出门槛必须与主流程 strategy.py 保持一致: 跌出 TopN 后, 持有满
            # ROTATION_CYCLE_DAYS 即可赎回。2026-11-05 用户取消 14 天缓冲,
            # ROTATION_CYCLE_DAYS 已改 7 (与 MIN_HOLD_DAYS 赎回费锁相等),
            # 所以现在"过 7 天就能卖"。hold_days / need_days 已在循环开头统一算,
            # 盘中止盈止损也用同一把尺子, 不再出现「盘中喊卖、主流程锁着」的分裂。
            if hold_days < need_days:
                locked.append({"name": h["name"], "fund_code": h["fund_code"],
                               "rank": row["rank"], "hold_days": hold_days})
                hold_signals.append({"name": h["name"], "fund_code": h["fund_code"],
                                     "rank": row["rank"],
                                     "reason": f"跌出Top{buffer_limit}缓冲区或动量/趋势转弱, 但持有仅{hold_days}天 (需满{need_days}天), 暂不赎回"})
            else:
                # 卖出原因必须写清楚: Top5缓冲生效后, "跌出Top3"不再是唯一理由 —
                # 缓冲内但不健康(动量转负/跌破MA20)同样触发赎回。旧文案一律写
                # "跌出Top3"会让人误以为缓冲没生效 (2026-11-13 实测创新药 #4)。
                if row["rank"] > buffer_limit:
                    reason = f"动量第{row['rank']}, 跌出Top{buffer_limit}缓冲区"
                else:
                    bits = []
                    if row["composite_score"] <= MOMENTUM_MIN_THRESHOLD:
                        bits.append(f"动量转负({row['composite_score']:+.2f}%)")
                    if not row["above_ma20"]:
                        bits.append("跌破MA20")
                    reason = (f"动量第{row['rank']}, 在Top{buffer_limit}缓冲区内但"
                              + "/".join(bits or ["趋势转弱"]) + ", 缓冲保护失效")
                sell_signals.append({"name": h["name"], "fund_code": h["fund_code"],
                                     "rank": row["rank"], "hold_days": hold_days,
                                     "reason": reason})

    return {
        "ok": True,
        "generated_at": today.strftime("%Y-%m-%d %H:%M:%S"),
        "in_recommend_window": RECOMMEND_START <= today.time() <= RECOMMEND_END,
        "is_weekend": today.weekday() >= 5,
        "holdings_source": state.get("source", ""),
        "rankings": rows,
        "top_codes": top_codes,
        "top_n": momentum_top,
        "persist_skipped": persist_skipped,
        "is_panic": panic_state,
        "panic_source": panic_source,
        "top1_code": top1_code,
        "data_warnings": data_warnings,
        "buy": buy_signals,
        "sell": sell_signals,
        "hold": hold_signals,
        "locked": locked,
    }

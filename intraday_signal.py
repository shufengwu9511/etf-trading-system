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
    ROTATION_CYCLE_DAYS, MIN_POSITION_VALUE,
    ROTATION_RANK_BUFFER, ROTATION_MAX_HOLD_COUNT,
)
from db import get_connection
from tdx_data import get_realtime_quotes

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

    return {
        "momentum_10d": round(mom_short, 4),
        "momentum_30d": round(mom_medium, 4),
        "composite_score": round(mom_short * MOMENTUM_WEIGHT_SHORT + mom_medium * MOMENTUM_WEIGHT_MEDIUM, 4),
        "ma20": round(ma20, 4) if ma20 else None,
        "above_ma20": above_ma20,
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
               COALESCE(market_value, 0) AS market_value
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
# 主计算入口
# ============================================================

def generate_signals(codes=None, holdings_arg=None):
    """计算盘中信号 (现价代理收盘价), 返回结构化结果 dict

    Args:
        codes: 逗号分隔的场内ETF代码, 仅计算指定ETF (默认全部卫星池)
        holdings_arg: 手动声明持仓 "基金代码:买入日期YYYYMMDD,..."
                      (仅在数据库无持仓时生效)

    Returns:
        {"ok": True, "generated_at", "in_recommend_window", "is_weekend",
         "rankings": [...], "top_n": [...],
         "buy": [...], "sell": [...], "hold": [...], "locked": [...]}
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

    # 6. 生成信号
    # 卖出判定TopN = 纯动量前N (MA20只约束买入, 不提前过滤进TopN,
    # 否则动量Top3内但当日跌破MA20的持仓会被误判"跌出前N", 与主系统修复后一致)
    momentum_top = [r for r in rows if r["composite_score"] > MOMENTUM_MIN_THRESHOLD][:ROTATION_HOLD_COUNT]
    top_codes = {r["etf_code"] for r in momentum_top}
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

    for r in momentum_top:
        if buy_slots <= 0:
            break
        # 买入闸门: 未持有 + MA20上方 (综合得分>0 已由 momentum_top 过滤)
        if r["etf_code"] not in holdings and r["above_ma20"]:
            buy_slots -= 1
            buy_signals.append(r)

    for code, h in holdings.items():
        # 该持仓是否还在候选池 (可能已不在 SATELLITE_ETFS)
        row = next((r for r in rows if r["etf_code"] == code), None)
        if row is None:
            hold_signals.append({"name": h["name"], "fund_code": h["fund_code"],
                                 "reason": "不在候选池, 保持现状"})
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

            hold_days = calc_hold_days(h.get("buy_date", ""))
            # 卖出门槛必须与主流程 strategy.py 保持一致: 跌出 TopN 后, 持有满
            # ROTATION_CYCLE_DAYS 即可赎回。2026-11-05 用户取消 14 天缓冲,
            # ROTATION_CYCLE_DAYS 已改 7 (与 MIN_HOLD_DAYS 赎回费锁相等),
            # 所以现在"过 7 天就能卖"。历史上这里曾只判 MIN_HOLD_DAYS(7) 而
            # 主流程判 14, 导致盘中比主流程提前一周喊卖; 用 max() 兜住两边,
            # 将来任一常量变动都不会再出现口径分裂。
            need_days = max(MIN_HOLD_DAYS, ROTATION_CYCLE_DAYS)
            if hold_days < need_days:
                locked.append({"name": h["name"], "fund_code": h["fund_code"],
                               "rank": row["rank"], "hold_days": hold_days})
                hold_signals.append({"name": h["name"], "fund_code": h["fund_code"],
                                     "rank": row["rank"],
                                     "reason": f"跌出Top{buffer_limit}缓冲区或动量/趋势转弱, 但持有仅{hold_days}天 (需满{need_days}天), 暂不赎回"})
            else:
                sell_signals.append({"name": h["name"], "fund_code": h["fund_code"],
                                     "rank": row["rank"], "hold_days": hold_days})

    return {
        "ok": True,
        "generated_at": today.strftime("%Y-%m-%d %H:%M:%S"),
        "in_recommend_window": RECOMMEND_START <= today.time() <= RECOMMEND_END,
        "is_weekend": today.weekday() >= 5,
        "holdings_source": state.get("source", ""),
        "rankings": rows,
        "top_codes": top_codes,
        "top_n": momentum_top,
        "buy": buy_signals,
        "sell": sell_signals,
        "hold": hold_signals,
        "locked": locked,
    }

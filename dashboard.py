# ============================================================
# ETF联接基金交易系统 - HTML可视化看板
# 从 SQLite 读取数据，生成完整的 HTML 看板
# ============================================================
import json
import os
import sqlite3
from datetime import datetime, timedelta
from db import get_connection
from config import (
    TOTAL_CAPITAL, CORE_RATIO, SATELLITE_RATIO,
    CORE_ETFS, SATELLITE_ETFS, LOG_DIR,
    PANIC_HS300_DROP, PANIC_NORTH_OUTFLOW,
    PANIC_LIMIT_DOWN_COUNT, PANIC_VOLUME_RATIO,
    PE_LOOKBACK_YEARS,
    MOMENTUM_SHORT, MOMENTUM_MEDIUM,
    MOMENTUM_WEIGHT_SHORT, MOMENTUM_WEIGHT_MEDIUM,
    TRAILING_STOP_ACTIVATE, TRAILING_STOP_PULLBACK,
)
from etf_discovery import discover_candidates
from breakout_discovery import discover_breakout_candidates
from intraday_signal import generate_signals


def collect_dashboard_data():
    """从数据库收集看板需要的所有数据"""
    conn = get_connection()
    data = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_capital": TOTAL_CAPITAL,
    }

    # ---- 1. 持仓数据 ----
    rows = conn.execute("""
        SELECT * FROM portfolio
        WHERE status IN ('holding', 'pending_buy')
        ORDER BY category, market_value DESC
    """).fetchall()
    holdings = [dict(r) for r in rows]
    for h in holdings:
        if h.get("buy_date"):
            buy = datetime.strptime(h["buy_date"], "%Y%m%d")
            h["hold_days"] = (datetime.now() - buy).days
        else:
            h["hold_days"] = 0
    data["holdings"] = holdings
    data["total_market_value"] = sum(h["market_value"] for h in holdings)
    data["total_cost"] = sum(h["total_cost"] for h in holdings)
    data["total_profit_loss"] = sum(h["profit_loss"] for h in holdings)
    data["total_profit_pct"] = (
        data["total_profit_loss"] / data["total_cost"] * 100
        if data["total_cost"] > 0 else 0
    )
    data["available_cash"] = TOTAL_CAPITAL - data["total_market_value"]

    # ---- 1b. 所有ETF近30日K线数据 (用于所有基金名称悬浮提示) ----
    # 收集所有需要K线的ETF代码 (去重), 按 etf_code (如 510310.SH) 索引
    all_kline_etfs = set()
    # 构建 fund_code → etf_code 映射
    fund2etf = {}
    all_config_etfs = CORE_ETFS + SATELLITE_ETFS
    for e in all_config_etfs:
        all_kline_etfs.add(e["code"])
        fc = e.get("fund_code", "")
        if fc:
            fund2etf[fc] = e["code"]

    kline_data = {}
    for etf_code in all_kline_etfs:
        rows = conn.execute("""
            SELECT trade_date, open, close, high, low, vol
            FROM index_daily
            WHERE ts_code = ?
            ORDER BY trade_date ASC
        """, (etf_code,)).fetchall()
        if rows:
            kline_data[etf_code] = [
                {"d": r["trade_date"], "o": r["open"], "c": r["close"],
                 "h": r["high"], "l": r["low"], "v": r["vol"]}
                for r in rows
            ]
    data["holdings_kline"] = kline_data
    data["fund2etf"] = fund2etf

    # 分组
    data["core_holdings"] = [h for h in holdings if h["category"] == "core"]
    data["satellite_holdings"] = [h for h in holdings if h["category"] == "satellite"]
    data["other_holdings"] = [h for h in holdings if h["category"] not in ("core", "satellite")]

    # 饼图数据
    data["pie_data"] = {}
    for h in holdings:
        cat = "宽基底仓" if h["category"] == "core" else ("行业卫星" if h["category"] == "satellite" else "其他")
        if cat not in data["pie_data"]:
            data["pie_data"][cat] = 0
        data["pie_data"][cat] += h["market_value"]
    if data["available_cash"] > 0:
        data["pie_data"]["现金"] = data["available_cash"]

    # ---- 2. 恐慌指数 ----
    panic_row = conn.execute("""
        SELECT * FROM panic_alerts
        ORDER BY created_at DESC LIMIT 1
    """).fetchone()
    data["panic"] = dict(panic_row) if panic_row else {
        "alert_level": "UNKNOWN", "trigger_details": "",
        "hs300_pct_chg": None, "hs300_date": None,
        "north_money": None, "north_date": None,
        "limit_down_count": None, "limit_down_date": None,
        "volume_ratio": None, "volume_date": None
    }

    # ---- 3. 动量排名 ----
    momentum_rows = conn.execute("""
        SELECT * FROM momentum_scores
        WHERE trade_date = (SELECT MAX(trade_date) FROM momentum_scores)
        ORDER BY rank
    """).fetchall()
    # 构建 fund_code → 基金公司 映射 (从 config 动态读取, 不再硬编码)
    _all_etfs = CORE_ETFS + SATELLITE_ETFS
    _FUND_COMPANY_MAP = {e.get("fund_code", ""): e.get("company", "") for e in _all_etfs}
    _etf_fund_map = {e["code"]: e.get("fund_code", "") for e in SATELLITE_ETFS}
    data["momentum"] = []
    for r in momentum_rows:
        item = dict(r)
        fc = _etf_fund_map.get(item.get("etf_code", ""), "")
        item["fund_code"] = fc
        item["company"] = _FUND_COMPANY_MAP.get(fc, "")
        data["momentum"].append(item)

    # 构建 etf_code → above_ma20 映射 (供持仓表使用)
    _ma20_map = {m.get("etf_code", ""): m.get("above_ma20", 0) for m in data["momentum"]}
    for h in data["holdings"]:
        h["above_ma20"] = _ma20_map.get(h.get("etf_code", ""), 1)  # 默认1(上方), 未在动量表中的宽基不显示

    # ---- 4. 今日交易指令 (结构化) ----
    today = datetime.now().strftime("%Y%m%d")
    signal_rows = conn.execute("""
        SELECT * FROM signals WHERE signal_date = ?
        ORDER BY signal_type, id
    """, (today,)).fetchall()
    signals = [dict(r) for r in signal_rows]

    # 按类型分组
    # 止盈止损类信号: 策略引擎细分了 stop_loss(固定止损) / trend_stop(MA20趋势止损) /
    # trailing_stop(移动止盈), 入库保留原始类型, 这里统一归入"止盈/止损"区块展示
    _stop_types = {"stop", "stop_loss", "trend_stop", "trailing_stop"}
    stop_sigs = [s for s in signals if s.get("signal_type") in _stop_types]
    # 止损类按优先级排前 (urgent > high > normal), 再按id稳定排序
    _pri_order = {"urgent": 0, "high": 1, "normal": 2}
    stop_sigs.sort(key=lambda s: (_pri_order.get(s.get("priority", "normal"), 3), s.get("id", 0)))
    core_sigs = [s for s in signals if s.get("signal_type") == "core_pe"]
    rotation_buy = [s for s in signals if s.get("signal_type") == "rotation_buy"]
    rotation_sell = [s for s in signals if s.get("signal_type") == "rotation_sell"]
    data["action"] = {
        "date": today,
        "stop": stop_sigs,
        "core": core_sigs,
        "rotation_buy": rotation_buy,
        "rotation_sell": rotation_sell,
        "has_signals": bool(signals),
    }

    # ---- 5. 历史信号 (最近5天非今日) ----
    recent_signal_rows = conn.execute("""
        SELECT * FROM signals WHERE signal_date != ?
        ORDER BY created_at DESC LIMIT 10
    """, (today,)).fetchall()
    data["recent_signals"] = [dict(r) for r in recent_signal_rows]

    # ---- 6. 最近交易 ----
    trade_rows = conn.execute("""
        SELECT * FROM trades
        ORDER BY created_at DESC LIMIT 20
    """).fetchall()
    data["recent_trades"] = [dict(r) for r in trade_rows]

    # ---- 6. 宽基PE估值 ----
    pe_data = []
    for etf in CORE_ETFS:
        row = conn.execute("""
            SELECT pe_ttm, trade_date FROM index_valuation
            WHERE ts_code = ? AND pe_ttm > 0
            ORDER BY trade_date DESC LIMIT 1
        """, (etf["code"],)).fetchone()
        pe_val = row["pe_ttm"] if row else None
        pe_date = row["trade_date"] if row else None

        # 计算百分位 (与策略引擎一致: 最近5年)
        cutoff = (datetime.now() - timedelta(days=PE_LOOKBACK_YEARS * 365)).strftime("%Y%m%d")
        pct_row = conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN pe_ttm < ? THEN 1 ELSE 0 END) as below
            FROM index_valuation
            WHERE ts_code = ? AND pe_ttm > 0 AND pe_ttm < 200 AND trade_date >= ?
        """, (pe_val, etf["code"], cutoff)).fetchone() if pe_val else None

        percentile = (pct_row["below"] / pct_row["total"] * 100) if pct_row and pct_row["total"] > 0 else None

        # MA趋势
        ma_row = conn.execute("""
            SELECT close FROM index_daily
            WHERE ts_code = ?
            ORDER BY trade_date DESC LIMIT 101
        """, (etf["code"],)).fetchall()
        ma_trend = "unknown"
        if len(ma_row) >= 101:
            closes = [r["close"] for r in reversed(ma_row)]
            ma100 = sum(closes[-100:]) / 100
            ma_trend = "up" if closes[-1] > ma100 else "down"

        # 判断PE数据状态
        pe_status = "ok"
        if pe_val is None:
            # 检查是否有任何估值数据
            cnt_row = conn.execute("""
                SELECT COUNT(*) as cnt FROM index_valuation WHERE ts_code = ?
            """, (etf["code"],)).fetchone()
            pe_status = "missing" if (cnt_row is None or cnt_row["cnt"] == 0) else "insufficient"

        # MA最新日期 (用于显示)
        ma_date_row = conn.execute("""
            SELECT MAX(trade_date) as d FROM index_daily WHERE ts_code = ?
        """, (etf["code"],)).fetchone()
        ma_date_raw = ma_date_row["d"] if ma_date_row else ""
        ma_date_fmt = f"{ma_date_raw[:4]}-{ma_date_raw[4:6]}-{ma_date_raw[6:8]}" if ma_date_raw else ""

        pe_data.append({
            "name": etf["name"],
            "code": etf["code"],
            "pe_ttm": round(pe_val, 2) if pe_val else None,
            "percentile": round(percentile, 1) if percentile else None,
            "ma_trend": ma_trend,
            "pe_status": pe_status,
            "pe_date": pe_date,
            "ma_date_fmt": ma_date_fmt
        })
    data["core_pe"] = pe_data

    # ---- 7. 系统日志 ----
    log_rows = conn.execute("""
        SELECT * FROM system_logs
        ORDER BY created_at DESC LIMIT 10
    """).fetchall()
    data["recent_logs"] = [dict(r) for r in log_rows]

    conn.close()

    # ---- 7.5. ETF动量发现 ----
    print("  扫描动量候选ETF...")
    try:
        data["etf_candidates"] = discover_candidates(momentum_threshold=0.05, top_n=10)
    except Exception as e:
        print(f"  [WARN] ETF候选发现失败: {e}")
        data["etf_candidates"] = []
    
    # ---- 7.6. ETF箱体突破发现 ----
    print("  扫描箱体突破ETF...")
    try:
        data["breakout_candidates"] = discover_breakout_candidates(direction="up", top_n=10)
    except Exception as e:
        print(f"  [WARN] 箱体突破发现失败: {e}")
        data["breakout_candidates"] = []
    
    # ---- 7.7. 补充发现/突破候选ETF的K线数据 (来自 etf_cache.db) ----
    _etf_cache_path = os.path.join(os.path.dirname(__file__), "data", "tdx_cache", "etf_cache.db")
    if os.path.exists(_etf_cache_path):
        _cache_conn = sqlite3.connect(_etf_cache_path)
        _cache_conn.row_factory = sqlite3.Row
        
        # 收集所有需要K线的候选ETF代码 (6位数字, 无后缀)
        _candidate_codes = set()
        for c in data.get("etf_candidates", []):
            _candidate_codes.add(c["code"])
        for c in data.get("breakout_candidates", []):
            _candidate_codes.add(c["code"])
        
        if _candidate_codes:
            _placeholders = ",".join("?" for _ in _candidate_codes)
            _rows = _cache_conn.execute(
                f"SELECT code, date, open, close, high, low, volume FROM kline_data "
                f"WHERE code IN ({_placeholders}) ORDER BY code, date",
                list(_candidate_codes)
            ).fetchall()
            
            for r in _rows:
                code = r["code"]
                if code not in kline_data:
                    kline_data[code] = []
                kline_data[code].append({
                    "d": r["date"], "o": r["open"], "c": r["close"],
                    "h": r["high"], "l": r["low"], "v": r["volume"]
                })
        
        _cache_conn.close()
    data["holdings_kline"] = kline_data
    
    # ---- 8. 实时行情数据 ----
    realtime = get_realtime_quotes()
    data["realtime"] = realtime
    
    # 构建实时行情表格HTML
    realtime_table = ""
    if realtime.get("success") and realtime.get("quotes"):
        # 按我们关注的ETF排序
        all_etfs = CORE_ETFS + SATELLITE_ETFS
        for etf in all_etfs:
            code = etf["code"][:6]  # 去掉后缀
            quote = realtime["quotes"].get(code, {})
            if quote:
                pct = quote.get('pct_chg', 0)
                pct_color = "#22c55e" if pct < 0 else "#ef4444"
                pct_sign = "+" if pct >= 0 else ""
                realtime_table += f'''
                <tr>
                    <td style="padding:6px 12px;font-weight:500;cursor:pointer" class="fund-name" data-fund-code="{etf['code']}">{etf['name']}</td>
                    <td style="padding:6px 12px;text-align:right">{quote.get('price', '--')}</td>
                    <td style="padding:6px 12px;text-align:right;color:{pct_color};font-weight:600">{pct_sign}{pct:.2f}%</td>
                </tr>'''
    
    data["realtime_table"] = realtime_table
    
    # 实时动量排名 (按涨跌幅排序)
    if realtime.get("success") and realtime.get("quotes"):
        rt_momentum = []
        for etf in SATELLITE_ETFS:
            code = etf["code"][:6]
            quote = realtime["quotes"].get(code, {})
            if quote:
                rt_momentum.append({
                    'name': etf['name'],
                    'fund_code': etf.get('fund_code', ''),
                    'company': etf.get('company', ''),
                    'pct_chg': quote.get('pct_chg', 0),
                })
        rt_momentum.sort(key=lambda x: x['pct_chg'], reverse=True)
        data["realtime_momentum"] = rt_momentum
    else:
        data["realtime_momentum"] = []

    # ---- 8b. 盘中信号 (现价代理收盘价, 14:30-14:50 运行最有意义) ----
    try:
        intraday = generate_signals()
        if not intraday.get("ok"):
            intraday = None
    except Exception as e:
        print(f"  [WARN] 盘中信号计算失败: {e}")
        intraday = None
    data["intraday"] = intraday
    data["intraday_html"] = _render_intraday(intraday)

    return data


def _fmt_pct(val):
    if val is None:
        return "--"
    sign = "+" if val >= 0 else ""
    return f"{sign}{val:.2f}%"


def get_realtime_quotes():
    """
    获取所有ETF的实时行情 (通过AkShare)
    返回: {
        'etf_quotes': [{'code','name','price','pct_chg'}, ...],
        'panic_index': 实时恐慌指数 (0-100),
        'avg_drop': 平均跌幅,
        'down_count': 下跌ETF数量,
        'update_time': 更新时间
    }
    """
    try:
        import akshare as ak
        import time
        
        # 获取全市场ETF实时行情
        df = ak.fund_etf_spot_em()
        
        # 构建 ETF代码 → 实时数据 映射
        quotes = {}
        for _, row in df.iterrows():
            code = str(row['代码']).zfill(6)
            quotes[code] = {
                'code': code,
                'name': row['名称'],
                'price': float(row['最新价']) if row['最新价'] else None,
                'pct_chg': float(row['涨跌幅']) if row['涨跌幅'] else 0,
                'volume': float(row['成交量']) if row['成交量'] else 0,
            }
        
        # 计算实时恐慌指数
        # 规则: 下跌ETF占比 + 平均跌幅 + 跌停数量(如果有)
        all_etfs = list(quotes.values())
        down_count = sum(1 for q in all_etfs if q['pct_chg'] < 0)
        down_ratio = down_count / len(all_etfs) if all_etfs else 0
        avg_pct = sum(q['pct_chg'] for q in all_etfs) / len(all_etfs) if all_etfs else 0
        
        # 恐慌指数计算 (0-100):
        # - 下跌占比 50%以下: 0-30
        # - 下跌占比 50-75%: 30-60  
        # - 下跌占比 75%以上: 60-100
        # - 加总平均跌幅的影响 (负得越多越恐慌)
        panic_score = down_ratio * 70  # 占比贡献最多70分
        panic_score += min(max(avg_pct * -3, 0), 30)  # 跌幅贡献最多30分
        panic_score = min(max(panic_score, 0), 100)
        
        return {
            'quotes': quotes,
            'panic_score': round(panic_score, 1),
            'down_count': down_count,
            'down_ratio': round(down_ratio * 100, 1),
            'avg_pct': round(avg_pct, 2),
            'update_time': time.strftime('%Y-%m-%d %H:%M:%S'),
            'success': True
        }
        
    except Exception as e:
        print(f"[WARN] 获取实时行情失败: {e}")
        return {
            'quotes': {},
            'panic_score': None,
            'down_count': 0,
            'down_ratio': 0,
            'avg_pct': 0,
            'update_time': None,
            'success': False,
            'error': str(e)
        }


def _render_action(action, panic):
    """渲染结构化的今日交易指令"""
    if not action.get("has_signals"):
        return '<div style="padding:20px;text-align:center;color:#9ca3af">今日无交易指令</div>'

    parts = []

    # 恐慌预警
    if panic and panic.get("alert_level") == "PANIC":
        parts.append(f'''
        <div style="background:#fef2f2;border:1px solid #fca5a5;border-radius:8px;padding:12px 16px;margin-bottom:16px">
            <div style="font-weight:600;color:#991b1b;margin-bottom:4px">🚨 市场恐慌预警</div>
            <div style="font-size:13px;color:#b91c1c">{panic.get("trigger_details","")}</div>
            <div style="font-size:12px;color:#9ca3af;margin-top:4px">建议暂停申购，持仓触发止损需提前赎回</div>
        </div>''')
    elif panic and panic.get("alert_level") == "WARNING":
        parts.append(f'''
        <div style="background:#fffbeb;border:1px solid #fcd34d;border-radius:8px;padding:12px 16px;margin-bottom:16px">
            <div style="font-weight:600;color:#92400e;margin-bottom:4px">⚠️ 市场波动提醒</div>
            <div style="font-size:13px;color:#a16207">{panic.get("trigger_details","")}</div>
        </div>''')

    # 止盈止损信号
    stop_sigs = action.get("stop", [])
    if stop_sigs:
        stop_rows = ""
        for sig in stop_sigs:
            icon = "🚨" if sig.get("priority") == "urgent" else "🔴"
            amount = sig.get("amount", 0) or sig.get("target_amount", 0)
            stop_rows += f'''
            <tr>
                <td style="padding:8px 12px;font-weight:500;cursor:pointer" class="fund-name" data-fund-code="{sig.get('etf_code','')}">{icon} {sig.get("name","")}</td>
                <td style="padding:8px 12px;color:#6b7280">{sig.get("fund_code","")}</td>
                <td style="padding:8px 12px">{_fmt_money(amount)}</td>
                <td style="padding:8px 12px;font-size:12px">{sig.get("reason","")}</td>
            </tr>'''
        parts.append(f'''
        <div style="margin-bottom:16px">
            <h4 style="font-size:15px;color:#991b1b;margin-bottom:8px">🔴 止盈/止损信号 (最高优先级)</h4>
            <table style="width:100%">
                <thead><tr><th>标的</th><th>基金代码</th><th>金额</th><th>原因</th></tr></thead>
                <tbody>{stop_rows}</tbody>
            </table>
        </div>''')

    # 宽基信号
    core_sigs = action.get("core", [])
    if core_sigs:
        core_rows = ""
        for sig in core_sigs:
            dir_icon = {"buy":"🟢","sell":"🔴","hold":"⚪","reduce":"🟡"}.get(sig.get("direction",""), "")
            amount = sig.get("amount", 0) or sig.get("target_amount", 0)
            core_rows += f'''
            <tr>
                <td style="padding:8px 12px;font-weight:500;cursor:pointer" class="fund-name" data-fund-code="{sig.get('etf_code','')}">{dir_icon} {sig.get("name","")}</td>
                <td style="padding:8px 12px;color:#6b7280">{sig.get("fund_code","")}</td>
                <td style="padding:8px 12px">{_fmt_money(amount)}</td>
                <td style="padding:8px 12px;font-size:12px">{sig.get("reason","")}</td>
            </tr>'''
        parts.append(f'''
        <div style="margin-bottom:16px">
            <h4 style="font-size:15px;color:#374151;margin-bottom:8px">📊 宽基底仓信号</h4>
            <table style="width:100%">
                <thead><tr><th>标的</th><th>基金代码</th><th>目标金额</th><th>逻辑</th></tr></thead>
                <tbody>{core_rows}</tbody>
            </table>
        </div>''')

    # 轮动信号
    rot_buy = action.get("rotation_buy", [])
    rot_sell = action.get("rotation_sell", [])
    if rot_buy or rot_sell:
        # 申购
        buy_rows = ""
        for sig in rot_buy:
            amount = sig.get("amount", 0) or sig.get("target_amount", 0)
            buy_rows += f'''
            <tr>
                <td style="padding:8px 12px;color:#6b7280;font-size:12px">{sig.get("signal_date","")}</td>
                <td style="padding:8px 12px;font-weight:500;cursor:pointer" class="fund-name" data-fund-code="{sig.get('etf_code','')}">🟢 {sig.get("name","")}</td>
                <td style="padding:8px 12px;color:#6b7280">{sig.get("fund_code","")}</td>
                <td style="padding:8px 12px">{_fmt_money(amount)}</td>
                <td style="padding:8px 12px;font-size:12px">{sig.get("reason","")}</td>
            </tr>'''
        # 赎回
        sell_rows = ""
        for sig in rot_sell:
            amount = sig.get("amount", 0) or sig.get("target_amount", 0)
            sell_rows += f'''
            <tr>
                <td style="padding:8px 12px;color:#6b7280;font-size:12px">{sig.get("signal_date","")}</td>
                <td style="padding:8px 12px;font-weight:500;cursor:pointer" class="fund-name" data-fund-code="{sig.get('etf_code','')}">🔴 {sig.get("name","")}</td>
                <td style="padding:8px 12px;color:#6b7280">{sig.get("fund_code","")}</td>
                <td style="padding:8px 12px">{_fmt_money(amount)}</td>
                <td style="padding:8px 12px;font-size:12px">{sig.get("reason","")}</td>
            </tr>'''
        parts.append(f'''
        <div style="margin-bottom:16px">
            <h4 style="font-size:15px;color:#374151;margin-bottom:8px">🔄 行业ETF动量轮动</h4>
            <div style="font-size:12px;color:#6b7280;margin-bottom:8px">
                <span style="font-weight:600;color:#1e40af">买入闸门(三重全满足):</span> ① 动量Top3 ② 复合动量&gt;0 ③ 收盘价在MA20上方 &nbsp;|&nbsp;
                <span style="font-weight:600;color:#991b1b">卖出防线(任一触发):</span> ① 移动止盈(8%激活/6%回撤) ② MA20×0.95趋势止损 ③ 固定止损(-15%) ④ 跌出Top3(14天缓冲)
            </div>
            {'<table style="width:100%;margin-bottom:8px"><thead><tr><th>信号日期</th><th>标的</th><th>基金代码</th><th>金额</th><th>原因</th></tr></thead><tbody>' + buy_rows + '</tbody></table>' if rot_buy else ''}
            {'<table style="width:100%"><thead><tr><th>信号日期</th><th>标的</th><th>基金代码</th><th>金额</th><th>原因</th></tr></thead><tbody>' + sell_rows + '</tbody></table>' if rot_sell else ''}
        </div>''')

    return "\n".join(parts)


def _fmt_money(val):
    """格式化金额"""
    if val is None:
        return "--"
    return f"¥{val:,.2f}"


def _fmt_pct(val):
    """格式化百分比"""
    if val is None:
        return "--"
    return f"{val:+.2f}%"


def _render_intraday(intraday):
    """渲染盘中信号板块 HTML (intraday=None 或失败时显示不可用提示)"""
    if not intraday:
        return '''
<div class="section-full">
    <div class="card" style="border:2px dashed #cbd5e1">
        <h3 style="margin-bottom:12px;font-size:16px;color:#374151">🕐 盘中信号 <span style="font-size:12px;color:#9ca3af;font-weight:400">(现价代理收盘价)</span></h3>
        <div style="padding:16px;text-align:center;color:#9ca3af">
            盘中信号暂不可用（实时行情获取失败或休市）<br>
            <span style="font-size:12px">可于交易时段 14:30-14:50 重新生成看板查看</span>
        </div>
    </div>
</div>'''

    now = intraday.get("generated_at", "")
    in_window = intraday.get("in_recommend_window", False)
    is_weekend = intraday.get("is_weekend", False)

    if is_weekend:
        window_tip = '<span style="font-size:12px;color:#f59e0b">周末运行, 行情为上周五收盘, 仅供参考</span>'
    elif in_window:
        window_tip = '<span style="font-size:12px;color:#22c55e">✅ 建议运行窗口内, 可提交当日申赎</span>'
    else:
        window_tip = '<span style="font-size:12px;color:#f59e0b">当前不在 14:30-14:50 窗口, 仅供参考</span>'

    # 建议操作
    parts = []
    if intraday.get("buy"):
        names = "、".join(f"<b>{s['name']}</b>({s['fund_code']})" for s in intraday["buy"])
        parts.append(f'<div>🟢 <span style="font-weight:600;color:#1d4ed8">申购:</span> {names}</div>')
    else:
        parts.append('<div>🟢 <span style="font-weight:600;color:#1d4ed8">申购:</span> 无</div>')
    if intraday.get("sell"):
        names = "、".join(f"<b>{s['name']}</b>({s['fund_code']})" for s in intraday["sell"])
        parts.append(f'<div>🔴 <span style="font-weight:600;color:#b91c1c">赎回:</span> {names}</div>')
    else:
        parts.append('<div>🔴 <span style="font-weight:600;color:#b91c1c">赎回:</span> 无</div>')
    locked_codes = {s["fund_code"] for s in intraday.get("locked", [])}
    if intraday.get("hold"):
        hold_names = []
        for s in intraday["hold"]:
            lock = "🔒" if s["fund_code"] in locked_codes else ""
            hold_names.append(f"{s['name']}({s['fund_code']}){lock}")
        parts.append(f'<div>⚪ <span style="font-weight:600;color:#6b7280">持有:</span> {"、".join(hold_names)}</div>')
    action_html = "<br>".join(parts)

    # 动量排名表
    top_codes = intraday.get("top_codes", set())
    rank_rows = ""
    for r in intraday["rankings"]:
        ma_flag = '<span style="color:#22c55e">上方</span>' if r["above_ma20"] else '<span style="color:#ef4444">下方</span>'
        star = "⭐" if r["etf_code"] in top_codes else ""
        pct_color = "#ef4444" if r["composite_score"] >= 0 else "#22c55e"
        chg_color = "#ef4444" if r["change_pct"] >= 0 else "#22c55e"
        rank_rows += f'''
            <tr>
                <td style="padding:6px 10px;white-space:nowrap">{star} #{r['rank']}</td>
                <td style="padding:6px 10px;font-weight:500;cursor:pointer" class="fund-name" data-fund-code="{r['etf_code']}">{r['name']}</td>
                <td style="padding:6px 10px;color:#6b7280">{r['fund_code']}</td>
                <td style="padding:6px 10px;text-align:right">{r['price']:.4f} <span style="color:{chg_color};font-size:11px">({r['change_pct']:+.2f}%)</span></td>
                <td style="padding:6px 10px;text-align:right">{r['momentum_10d']:+.2f}%</td>
                <td style="padding:6px 10px;text-align:right">{r['momentum_30d']:+.2f}%</td>
                <td style="padding:6px 10px;text-align:right;font-weight:600;color:{pct_color}">{r['composite_score']:+.2f}%</td>
                <td style="padding:6px 10px">{ma_flag}</td>
            </tr>'''

    return f'''
<div class="section-full">
    <div class="card" style="border:2px solid #3b82f6">
        <h3 style="margin-bottom:12px;font-size:16px;color:#374151">
            🕐 盘中信号 <span style="font-size:12px;color:#9ca3af;font-weight:400">现价代理收盘价 · {now}</span>
            {window_tip}
        </h3>
        <div style="font-size:12px;color:#6b7280;margin-bottom:10px">
            原理: 场外申赎 15:00前提交按当日收盘净值成交, 全部决策规则只依赖当日收盘价, 故用盘中现价(误差&lt;0.3%)提前计算信号 — 当日决策当日成交, 消除T+1时间差。持仓状态来自主系统数据库, 本板块不记账。
        </div>
        <div style="background:#eff6ff;border-radius:8px;padding:12px 16px;font-size:13px;line-height:1.8;margin-bottom:12px">
            {action_html}
        </div>
        <div style="overflow-x:auto">
            <table style="width:100%;border-collapse:collapse;font-size:12px">
                <thead><tr style="background:#f9fafb;border-bottom:2px solid #e5e7eb">
                    <th style="padding:6px 10px;text-align:left;font-weight:600;color:#6b7280">排名</th>
                    <th style="padding:6px 10px;text-align:left;font-weight:600;color:#6b7280">标的</th>
                    <th style="padding:6px 10px;text-align:left;font-weight:600;color:#6b7280">基金代码</th>
                    <th style="padding:6px 10px;text-align:right;font-weight:600;color:#6b7280">现价</th>
                    <th style="padding:6px 10px;text-align:right;font-weight:600;color:#6b7280">10日</th>
                    <th style="padding:6px 10px;text-align:right;font-weight:600;color:#6b7280">30日</th>
                    <th style="padding:6px 10px;text-align:right;font-weight:600;color:#6b7280">综合</th>
                    <th style="padding:6px 10px;text-align:left;font-weight:600;color:#6b7280">MA20</th>
                </tr></thead>
                <tbody>
                    {rank_rows}
                </tbody>
            </table>
        </div>
        <div style="margin-top:8px;font-size:11px;color:#9ca3af">
            ⭐=买入闸门通过(Top3) · 🔒=7天锁内暂不赎回 · 本信号仅作参考, 临界信号建议次日确认; 实际申赎请在持仓系统(main.py)中维护
        </div>
    </div>
</div>'''


def _render_etf_candidates(candidates):
    """渲染ETF动量发现候选表"""
    if not candidates:
        return '''<div class="section-full">
    <div class="card">
        <h3 style="margin-bottom:12px;font-size:16px;color:#374151">🔍 ETF动量发现</h3>
        <div style="padding:20px;text-align:center;color:#9ca3af">
            暂无符合条件的候选ETF<br>
            <span style="font-size:12px">(数据来自通达信全市场ETF缓存 data/tdx_cache/etf_cache.db)</span>
        </div>
    </div>
</div>'''

    rows = ""
    for i, c in enumerate(candidates, 1):
        score_color = "#ef4444" if c["score"] >= 0 else "#22c55e"
        m10_color = "#ef4444" if c["m10"] >= 0 else "#22c55e"
        m30_color = "#ef4444" if c["m30"] >= 0 else "#22c55e"
        vol_str = f"{c['avg_vol']/10000:.0f}万"
        rows += f'''
            <tr>
                <td style="padding:8px 12px;text-align:center;font-weight:600;color:#6b7280">{i}</td>
                <td style="padding:8px 12px;font-weight:500;cursor:pointer" class="fund-name" data-fund-code="{c['code']}">{c['name']}</td>
                <td style="padding:8px 12px;color:#6b7280;font-family:monospace">{c['code']}</td>
                <td style="padding:8px 12px;text-align:right;font-weight:600;color:{m10_color}">{_fmt_pct(c['m10'])}</td>
                <td style="padding:8px 12px;text-align:right;font-weight:600;color:{m30_color}">{_fmt_pct(c['m30'])}</td>
                <td style="padding:8px 12px;text-align:right;font-weight:700;color:{score_color}">{_fmt_pct(c['score'])}</td>
                <td style="padding:8px 12px;text-align:right;color:#6b7280">{vol_str}</td>
                <td style="padding:8px 12px;color:#6b7280;font-size:12px">{c['klines']}条</td>
            </tr>'''

    return f'''<div class="section-full">
    <div class="card" style="border:2px solid #e5e7eb">
        <h3 style="margin-bottom:8px;font-size:16px;color:#374151">
            🔍 ETF动量发现
            <span style="font-size:12px;color:#9ca3af;font-weight:400;margin-left:8px">未在候选池中的高动量ETF</span>
        </h3>
        <div style="font-size:12px;color:#9ca3af;margin-bottom:12px">
            动量≥5% | 日均成交≥500万 | 排除债券/货币/海外ETF | 数据来自通达信缓存
        </div>
        <div class="section">
            <div style="overflow-x:auto">
                <table style="width:100%;border-collapse:collapse;font-size:14px">
                    <thead>
                        <tr style="background:#f9fafb;border-bottom:2px solid #e5e7eb">
                            <th style="padding:10px 12px;text-align:center;font-weight:600;color:#6b7280;width:40px">#</th>
                            <th style="padding:10px 12px;text-align:left;font-weight:600;color:#6b7280">名称</th>
                            <th style="padding:10px 12px;text-align:left;font-weight:600;color:#6b7280">代码</th>
                            <th style="padding:10px 12px;text-align:right;font-weight:600;color:#6b7280">10日动量</th>
                            <th style="padding:10px 12px;text-align:right;font-weight:600;color:#6b7280">30日动量</th>
                            <th style="padding:10px 12px;text-align:right;font-weight:600;color:#6b7280">综合得分</th>
                            <th style="padding:10px 12px;text-align:right;font-weight:600;color:#6b7280">日均成交</th>
                            <th style="padding:10px 12px;text-align:right;font-weight:600;color:#6b7280">K线</th>
                        </tr>
                    </thead>
                    <tbody>{rows}</tbody>
                </table>
            </div>
            <div style="position:relative;height:240px;min-width:300px">
                <canvas id="discoveryChart"></canvas>
            </div>
        </div>
        <div style="margin-top:16px;padding:12px 16px;background:#eff6ff;border-radius:8px;border-left:4px solid #3b82f6;font-size:13px;color:#1e40af">
            <span style="font-weight:600">💡 提示：</span>
            发现感兴趣的ETF？使用命令添加到候选池：<br>
            <code style="background:#dbeafe;padding:2px 8px;border-radius:4px;font-size:12px">python main.py add-etf --fund-code &lt;联接基金代码&gt; --name "名称" --etf-code &lt;ETF代码&gt;</code>
            <br><span style="font-size:11px;color:#6b7280">（联接基金代码请到天天基金/易方达APP查询确认）</span>
        </div>
    </div>
</div>'''


def _render_breakout_candidates(candidates):
    """渲染ETF箱体突破候选表"""
    if not candidates:
        return '''<div class="section-full">
    <div class="card">
        <h3 style="margin-bottom:12px;font-size:16px;color:#374151">📈 ETF箱体突破发现</h3>
        <div style="padding:20px;text-align:center;color:#9ca3af">
            暂无向上突破的ETF<br>
            <span style="font-size:12px">(布林带 20日 ±2σ，数据来自通达信全市场ETF缓存)</span>
        </div>
    </div>
</div>'''

    rows = ""
    for i, c in enumerate(candidates, 1):
        dev_color = "#ef4444" if c["pct_deviation"] >= 0 else "#22c55e"
        vol_color = "#ef4444" if c["vol_ratio"] >= 1.5 else ("#f59e0b" if c["vol_ratio"] >= 1.0 else "#6b7280")
        vol_str = f"{c['avg_vol']/10000:.0f}万"
        rows += f'''
            <tr>
                <td style="padding:8px 12px;text-align:center;font-weight:600;color:#6b7280">{i}</td>
                <td style="padding:8px 12px;font-weight:500;cursor:pointer" class="fund-name" data-fund-code="{c['code']}">{c['name']}</td>
                <td style="padding:8px 12px;color:#6b7280;font-family:monospace">{c['code']}</td>
                <td style="padding:8px 12px;text-align:right;font-family:monospace">{c['close']:.4f}</td>
                <td style="padding:8px 12px;text-align:right;font-weight:700;color:{dev_color}">{_fmt_pct(c['pct_deviation'])}</td>
                <td style="padding:8px 12px;text-align:right;font-weight:600;color:{vol_color}">{c['vol_ratio']:.2f}x</td>
                <td style="padding:8px 12px;text-align:right;color:#6b7280">{c['band_width']:.2f}%</td>
                <td style="padding:8px 12px;text-align:right;color:#6b7280">{vol_str}</td>
            </tr>'''

    return f'''<div class="section-full">
    <div class="card" style="border:2px solid #e5e7eb">
        <h3 style="margin-bottom:8px;font-size:16px;color:#374151">
            📈 ETF箱体突破发现
            <span style="font-size:12px;color:#9ca3af;font-weight:400;margin-left:8px">布林带向上突破的ETF候选</span>
        </h3>
        <div style="font-size:12px;color:#9ca3af;margin-bottom:12px">
            布林带 20日 ±2σ | 收盘价突破上轨 | 日均成交≥500万 | 排除债券/货币/海外 | 按偏离度排序
        </div>
        <div class="section">
            <div style="overflow-x:auto">
                <table style="width:100%;border-collapse:collapse;font-size:14px">
                    <thead>
                        <tr style="background:#f9fafb;border-bottom:2px solid #e5e7eb">
                            <th style="padding:10px 12px;text-align:center;font-weight:600;color:#6b7280;width:40px">#</th>
                            <th style="padding:10px 12px;text-align:left;font-weight:600;color:#6b7280">名称</th>
                            <th style="padding:10px 12px;text-align:left;font-weight:600;color:#6b7280">代码</th>
                            <th style="padding:10px 12px;text-align:right;font-weight:600;color:#6b7280">收盘价</th>
                            <th style="padding:10px 12px;text-align:right;font-weight:600;color:#6b7280">偏离中轨</th>
                            <th style="padding:10px 12px;text-align:right;font-weight:600;color:#6b7280">量比</th>
                            <th style="padding:10px 12px;text-align:right;font-weight:600;color:#6b7280">带宽</th>
                            <th style="padding:10px 12px;text-align:right;font-weight:600;color:#6b7280">日均成交</th>
                        </tr>
                    </thead>
                    <tbody>{rows}</tbody>
                </table>
            </div>
            <div style="position:relative;height:240px;min-width:300px">
                <canvas id="breakoutChart"></canvas>
            </div>
        </div>
        <div style="margin-top:16px;padding:12px 16px;background:#fef3c7;border-radius:8px;border-left:4px solid #f59e0b;font-size:13px;color:#92400e">
            <span style="font-weight:600">💡 策略说明：</span>
            箱体突破是技术分析中的趋势信号。收盘价突破布林带上轨，意味着价格打破近期盘整区间，可能开启上涨趋势。
            量比&gt;1.5表示放量突破，信号更强。带宽反映波动率，较窄的带宽突破更具参考价值。<br>
            <span style="font-size:11px;color:#6b7280">发现感兴趣的ETF？同样使用 add-etf 命令添加到候选池</span>
        </div>
    </div>
</div>'''


def generate_html(data):
    """生成 HTML 看板"""
    d = data
    panic = d["panic"]
    realtime = d.get("realtime", {})
    realtime_table = d.get("realtime_table", "")
    intraday_html = d.get("intraday_html", "")
    is_panic = panic.get("alert_level") == "PANIC"
    is_warning = panic.get("alert_level") == "WARNING"
    
    # 实时恐慌指数
    rt_panic_score = realtime.get("panic_score")
    rt_panic_color = "#22c55e" if (rt_panic_score is not None and rt_panic_score < 30) else ("#f59e0b" if (rt_panic_score and rt_panic_score < 60) else ("#ef4444" if rt_panic_score else "#6b7280"))
    rt_panic_text = "实时恐慌" if (rt_panic_score and rt_panic_score >= 60) else ("实时警惕" if (rt_panic_score and rt_panic_score >= 30) else ("实时正常" if rt_panic_score else "实时N/A"))
    rt_panic_icon = "🚨" if (rt_panic_score and rt_panic_score >= 60) else ("⚠️" if (rt_panic_score and rt_panic_score >= 30) else ("✅" if rt_panic_score else "❓"))
    
    # 恐慌仪表盘颜色
    panic_bg = "#ef4444" if is_panic else ("#f59e0b" if is_warning else "#22c55e")
    panic_text = "恐慌" if is_panic else ("警惕" if is_warning else "正常")
    panic_icon = "🚨" if is_panic else ("⚠️" if is_warning else "✅")

    # 总盈亏颜色
    pl_color = "#22c55e" if d["total_profit_loss"] < 0 else "#ef4444"

    # 饼图数据
    pie_labels = json.dumps(list(d["pie_data"].keys()))
    pie_values = json.dumps([round(v, 0) for v in d["pie_data"].values()])
    pie_colors = json.dumps(["#3b82f6", "#8b5cf6", "#f59e0b", "#6b7280"][:len(d["pie_data"])])

    # 动量柱状图
    mom_labels = json.dumps([m["name"] for m in d["momentum"]])
    mom_values = json.dumps([round(m.get("composite_score", 0), 2) for m in d["momentum"]])
    mom_fund_codes = json.dumps([m.get("fund_code", "") for m in d["momentum"]])
    mom_companies = json.dumps([m.get("company", "") for m in d["momentum"]])
    mom_colors = json.dumps([
        "#ef4444" if m.get("composite_score", 0) >= 0 else "#22c55e"
        for m in d["momentum"]
    ])
    # MA20趋势状态: 上方=绿点, 下方=红点
    mom_ma20_status = json.dumps([
        "above" if m.get("above_ma20", 0) else "below"
        for m in d["momentum"]
    ])
    
    # 实时动量排名柱状图
    rt_mom = d.get("realtime_momentum", [])
    rt_mom_labels = json.dumps([m["name"] for m in rt_mom]) if rt_mom else "[]"
    rt_mom_values = json.dumps([round(m.get("pct_chg", 0), 2) for m in rt_mom]) if rt_mom else "[]"
    rt_mom_fund_codes = json.dumps([m.get("fund_code", "") for m in rt_mom]) if rt_mom else "[]"
    rt_mom_companies = json.dumps([m.get("company", "") for m in rt_mom]) if rt_mom else "[]"
    rt_mom_colors = json.dumps([
        "#ef4444" if m.get("pct_chg", 0) >= 0 else "#22c55e"
        for m in rt_mom
    ]) if rt_mom else "[]"
    
    # PE数据
    pe_items = ""
    for p in d["core_pe"]:
        pct = p["percentile"]
        pe_status = p.get("pe_status", "ok")
        
        if pe_status == "missing":
            # PE数据缺失 (如科创50)
            ma_date_fmt = p.get("ma_date_fmt", "")
            pe_html = f'''
        <div style="flex:1;min-width:200px;background:#f9fafb;border-radius:8px;padding:16px;border:1px dashed #d1d5db">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                <span style="font-weight:600;cursor:pointer" class="fund-name" data-fund-code="{p['code']}">{p['name']}</span>
                <span>📊</span>
            </div>
            <div style="font-size:20px;font-weight:700;color:#9ca3af;margin-bottom:4px">N/A</div>
            <div style="font-size:13px;color:#6b7280">PE数据缺失，仅用均线判断</div>
            <div style="margin-top:8px;font-size:12px;color:#9ca3af">MA趋势: {"📈 向上" if p["ma_trend"] == "up" else ("📉 向下" if p["ma_trend"] == "down" else "❓ 未知")}{" (" + ma_date_fmt + ")" if ma_date_fmt else ""}</div>
        </div>'''
            pe_items += pe_html
            continue
        
        pct_color = "#ef4444" if (pct and pct > 80) else ("#22c55e" if (pct and pct < 30) else "#f59e0b")
        pe_bar = f'<div style="background:#e5e7eb;border-radius:4px;height:8px;width:100%"><div style="background:{pct_color};height:8px;border-radius:4px;width:{min(pct or 0, 100)}%"></div></div>'
        ma_icon = "📈" if p["ma_trend"] == "up" else ("📉" if p["ma_trend"] == "down" else "❓")
        pct_text = f"{pct:.1f}%" if pct is not None else "数据不足"
        pe_text = f"{p['pe_ttm']:.2f}" if p["pe_ttm"] is not None else "--"
        pe_date_raw = p.get("pe_date", "")
        pe_date_fmt = f"{pe_date_raw[:4]}-{pe_date_raw[4:6]}-{pe_date_raw[6:8]}" if pe_date_raw else ""
        pe_items += f'''
        <div style="flex:1;min-width:200px;background:#fff;border-radius:8px;padding:16px;box-shadow:0 1px 3px rgba(0,0,0,0.1)">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                <span style="font-weight:600;cursor:pointer" class="fund-name" data-fund-code="{p['code']}">{p['name']}</span>
                <span>{ma_icon}</span>
            </div>
            <div style="font-size:24px;font-weight:700;color:{pct_color};margin-bottom:4px">{pe_text}</div>
            <div style="font-size:13px;color:#6b7280">PE百分位: <span style="color:{pct_color};font-weight:600">{pct_text}</span></div>
            <div style="margin-top:4px;font-size:11px;color:#9ca3af">估值日期: {pe_date_fmt}</div>
            <div style="margin-top:8px">{pe_bar}</div>
        </div>'''

    # 持仓行
    def _holdings_table(items, title):
        if not items:
            return ""
        rows_html = ""
        for h in items:
            pl_cls = "#22c55e" if h["profit_pct"] < 0 else "#ef4444"
            # 止盈跟踪状态
            if h.get("category") == "core":
                trail_html = '<span style="color:#9ca3af">估值管理</span>'
            else:
                peak = h.get("peak_profit_pct") or 0
                profit = h["profit_pct"] or 0
                if peak > 0:
                    drawdown = peak - profit
                    # 距触发越近颜色越警示
                    if drawdown >= TRAILING_STOP_PULLBACK:
                        trail_color = "#ef4444"
                    elif drawdown >= TRAILING_STOP_PULLBACK * 0.6:
                        trail_color = "#f59e0b"
                    else:
                        trail_color = "#22c55e"
                    trail_html = (
                        f'<span style="font-weight:600">峰值+{peak:.2f}%</span><br>'
                        f'<span style="font-size:11px;color:{trail_color}">'
                        f'回撤{drawdown:.2f}% / 阈值{TRAILING_STOP_PULLBACK:.0f}%</span>'
                    )
                elif profit >= TRAILING_STOP_ACTIVATE:
                    trail_html = '<span style="color:#f59e0b">待激活</span>'
                else:
                    trail_html = (
                        f'<span style="color:#9ca3af">未激活</span><br>'
                        f'<span style="font-size:11px;color:#9ca3af">'
                        f'{profit:.2f}% / {TRAILING_STOP_ACTIVATE:.0f}%</span>'
                    )
                # 追加MA20趋势状态
                if h.get("above_ma20", 1) == 1:
                    trail_html += '<br><span style="font-size:11px;color:#22c55e">🟢 MA20上方</span>'
                else:
                    trail_html += '<br><span style="font-size:11px;color:#ef4444;font-weight:600">🔴 MA20下方(趋势止损区)</span>'
            rows_html += f'''
            <tr>
                <td style="padding:10px 12px;font-weight:500;cursor:pointer" class="fund-name" data-fund-code="{h['etf_code']}">{h['name']}</td>
                <td style="padding:10px 12px;color:#6b7280">{h['fund_code']}</td>
                <td style="padding:10px 12px;text-align:right">{h['shares']:,.2f}</td>
                <td style="padding:10px 12px;text-align:right">{h['cost_nav']:.4f}</td>
                <td style="padding:10px 12px;text-align:right">{h['current_nav']:.4f}<br><span style="font-size:11px;color:#9ca3af">{h.get('nav_date','')}</span></td>
                <td style="padding:10px 12px;text-align:right;font-weight:600">{_fmt_money(h['market_value'])}</td>
                <td style="padding:10px 12px;text-align:right;font-weight:700;color:{pl_cls}">{_fmt_pct(h['profit_pct'])}</td>
                <td style="padding:10px 12px;text-align:right;font-size:12px">{trail_html}</td>
                <td style="padding:10px 12px;text-align:right;font-weight:600;color:{pl_cls}">{_fmt_money(h['profit_loss'])}</td>
                <td style="padding:10px 12px;text-align:right;color:#6b7280">{h.get('hold_days', 0)}天</td>
            </tr>'''
        return f'''
        <div style="background:#fff;border-radius:12px;padding:20px;box-shadow:0 1px 3px rgba(0,0,0,0.1);margin-bottom:20px">
            <h3 style="margin:0 0 12px;font-size:16px;color:#374151">{title}</h3>
            <div style="overflow-x:auto">
            <table style="width:100%;border-collapse:collapse;font-size:14px">
                <thead>
                    <tr style="background:#f9fafb;border-bottom:2px solid #e5e7eb">
                        <th style="padding:10px 12px;text-align:left;font-weight:600;color:#6b7280">名称</th>
                        <th style="padding:10px 12px;text-align:left;font-weight:600;color:#6b7280">基金代码</th>
                        <th style="padding:10px 12px;text-align:right;font-weight:600;color:#6b7280">份额</th>
                        <th style="padding:10px 12px;text-align:right;font-weight:600;color:#6b7280">成本净值</th>
                        <th style="padding:10px 12px;text-align:right;font-weight:600;color:#6b7280">当前净值(日期)</th>
                        <th style="padding:10px 12px;text-align:right;font-weight:600;color:#6b7280">市值</th>
                        <th style="padding:10px 12px;text-align:right;font-weight:600;color:#6b7280">盈亏%</th>
                        <th style="padding:10px 12px;text-align:right;font-weight:600;color:#6b7280">止盈跟踪</th>
                        <th style="padding:10px 12px;text-align:right;font-weight:600;color:#6b7280">盈利金额</th>
                        <th style="padding:10px 12px;text-align:right;font-weight:600;color:#6b7280">持有天数</th>
                    </tr>
                </thead>
                <tbody>{rows_html}</tbody>
            </table>
            </div>
        </div>'''

    holdings_html = _holdings_table(d["core_holdings"], "📊 宽基底仓")
    holdings_html += _holdings_table(d["satellite_holdings"], "🔄 行业卫星")
    holdings_html += _holdings_table(d["other_holdings"], "📦 其他持仓")
    if not d["holdings"]:
        holdings_html = '<div style="background:#fff;border-radius:12px;padding:40px;text-align:center;color:#9ca3af;box-shadow:0 1px 3px rgba(0,0,0,0.1);margin-bottom:20px">📭 当前无持仓</div>'

    # 历史信号列表
    _type_labels = {
        "stop": "止盈止损", "stop_loss": "止损", "trend_stop": "趋势止损",
        "trailing_stop": "移动止盈", "core_pe": "宽基择时",
        "rotation_buy": "轮动申购", "rotation_sell": "轮动赎回",
    }
    recent_signal_items = ""
    for s in d["recent_signals"]:
        dir_icon = {"buy": "🟢", "sell": "🔴", "hold": "⚪", "reduce": "🟡"}.get(s.get("direction", ""), "")
        type_label = _type_labels.get(s.get("signal_type", ""), s.get("signal_type", ""))
        recent_signal_items += f'''
        <tr>
            <td style="padding:8px 12px;color:#6b7280">{s.get('signal_date','')}</td>
            <td style="padding:8px 12px">{dir_icon} {s.get('name','')}</td>
            <td style="padding:8px 12px;color:#6b7280">{type_label}</td>
            <td style="padding:8px 12px">{s.get('reason','')}</td>
        </tr>'''

    # 交易记录
    trade_items = ""
    for t in d["recent_trades"]:
        tt_icon = "🟢" if t.get("trade_type") == "buy" else "🔴"
        trade_items += f'''
        <tr>
            <td style="padding:8px 12px;color:#6b7280">{t.get('submit_date','')}</td>
            <td style="padding:8px 12px">{tt_icon} {t.get('name','')}</td>
            <td style="padding:8px 12px">{_fmt_money(t.get('amount'))}</td>
            <td style="padding:8px 12px;color:#6b7280;font-size:12px">{t.get('remark','')}</td>
        </tr>'''

    # ETF候选发现
    etf_candidates_html = _render_etf_candidates(d.get("etf_candidates", []))

    # ETF发现图表数据
    disc_labels = json.dumps([c["name"] for c in d.get("etf_candidates", [])])
    disc_codes = json.dumps([c["code"] for c in d.get("etf_candidates", [])])
    disc_scores = json.dumps([c["score"] for c in d.get("etf_candidates", [])])
    disc_colors = json.dumps([
        "#ef4444" if c["score"] >= 0 else "#22c55e"
        for c in d.get("etf_candidates", [])
    ])

    # ETF箱体突破
    breakout_html = _render_breakout_candidates(d.get("breakout_candidates", []))

    # 箱体突破图表数据
    brk_labels = json.dumps([c["name"] for c in d.get("breakout_candidates", [])])
    brk_codes = json.dumps([c["code"] for c in d.get("breakout_candidates", [])])
    brk_devs = json.dumps([c["pct_deviation"] for c in d.get("breakout_candidates", [])])
    brk_vols = json.dumps([c["vol_ratio"] for c in d.get("breakout_candidates", [])])

    # 恐慌指标详情 (每个指标有各自的数据日期)
    def _fmt_date(d):
        if not d or d == "None":
            return ""
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}"

    hs300_pct = panic.get("hs300_pct_chg")
    hs300_display = f"{hs300_pct:.2f}%" if hs300_pct is not None else "--"
    hs300_color = "#22c55e" if (hs300_pct is not None and hs300_pct < 0) else "#ef4444"
    hs300_date_str = _fmt_date(panic.get("hs300_date"))

    north = panic.get("north_money")
    north_display = f"{north/10000:.1f}亿" if north is not None else "--"
    north_color = "#22c55e" if (north is not None and north < 0) else "#ef4444"
    north_date_str = _fmt_date(panic.get("north_date"))

    vol_ratio = panic.get("volume_ratio")
    vol_display = f"{vol_ratio:.1f}x" if vol_ratio is not None else "--"
    vol_color = "#ef4444" if (vol_ratio is not None and vol_ratio > 1.5) else "#22c55e"
    vol_date_str = _fmt_date(panic.get("volume_date"))

    limit_down = panic.get("limit_down_count")
    ld_display = f"{limit_down}家" if limit_down is not None else "--"
    ld_color = "#ef4444" if (limit_down is not None and limit_down >= 100) else "#22c55e"
    ld_date_str = _fmt_date(panic.get("limit_down_date"))

    # 恐慌仪表盘标题日期 (取所有指标中最新的)
    all_dates = [d for d in [hs300_date_str, north_date_str, vol_date_str, ld_date_str] if d]
    panic_date_fmt = max(all_dates) if all_dates else panic.get("trade_date", "")

    # K线悬浮提示 JavaScript (独立变量, 避免f-string转义)
    kline_js = '''<script>
(function() {
    var kdata = window._klineData || {};
    var tooltip = document.createElement('div');
    tooltip.className = 'kline-tooltip';
    tooltip.innerHTML = '<canvas id="klineCanvas" width="300" height="160"></canvas>';
    document.body.appendChild(tooltip);
    var canvas = tooltip.querySelector('canvas');
    var ctx = canvas.getContext('2d');

    function renderKline(fundCode) {
        var rows = kdata[fundCode];
        if (!rows || rows.length < 5) { tooltip.style.display = 'none'; return; }
        var data = rows.slice(-30);
        var n = data.length;
        var w = canvas.width, h = canvas.height;
        var pad = {top:28, right:52, bottom:26, left:8};
        var pw = w - pad.left - pad.right;
        var ph = h - pad.top - pad.bottom;
        var barW = Math.max(2, Math.floor(pw / n * 0.7));
        var gap = Math.floor(pw / n) - barW;
        var maxH = -Infinity, minL = Infinity;
        for (var i = 0; i < n; i++) {
            if (data[i].h > maxH) maxH = data[i].h;
            if (data[i].l < minL) minL = data[i].l;
        }
        var range = maxH - minL || 0.01;
        var xScale = function(i) { return pad.left + i * (barW + gap) + gap/2; };
        var yScale = function(price) { return pad.top + (maxH - price) / range * ph; };

        // 先构建标题和canvas HTML, 替换后再画图
        var first = data[0], last = data[n-1];
        var chgPct = ((last.c - first.c) / first.c * 100).toFixed(2);
        var chgCls = parseFloat(chgPct) >= 0 ? '#ef4444' : '#22c55e';
        var volTotal = 0;
        for (var i = 0; i < n; i++) volTotal += data[i].v;
        var avgVol = (volTotal / n / 10000).toFixed(0);
        var titleHtml = '<div class="kline-title">' + fundCode + ' 近30日K线</div>' +
            '<div class="kline-stats">' +
            '<span>区间涨跌: <b style="color:' + chgCls + '">' + (parseFloat(chgPct) >= 0 ? '+' : '') + chgPct + '%</b></span>' +
            '<span>最高: ' + maxH.toFixed(3) + '</span>' +
            '<span>最低: ' + minL.toFixed(3) + '</span>' +
            '<span>日均量: ' + avgVol + '万手</span>' +
            '</div>';
        tooltip.innerHTML = titleHtml + '<canvas id="klineCanvas" width="300" height="160"></canvas>';
        canvas = tooltip.querySelector('canvas');
        ctx = canvas.getContext('2d');

        // ---- 在新建的canvas上画K线 ----
        ctx.clearRect(0, 0, w, h);
        ctx.fillStyle = '#fafbfc';
        ctx.fillRect(pad.left, pad.top, pw, ph);
        ctx.strokeStyle = '#e5e7eb';
        ctx.lineWidth = 0.5;
        for (var g = 0; g <= 4; g++) {
            var gy = pad.top + g * ph / 4;
            ctx.beginPath();
            ctx.moveTo(pad.left, gy);
            ctx.lineTo(pad.left + pw, gy);
            ctx.stroke();
            ctx.fillStyle = '#9ca3af';
            ctx.font = '9px monospace';
            ctx.textAlign = 'right';
            var price = maxH - g * range / 4;
            ctx.fillText(price.toFixed(2), w - 4, gy + 3);
        }
        for (var i = 0; i < n; i++) {
            var d = data[i];
            var x = xScale(i);
            var yOpen = yScale(d.o), yClose = yScale(d.c);
            var yHigh = yScale(d.h), yLow = yScale(d.l);
            var isUp = d.c >= d.o;
            ctx.strokeStyle = isUp ? '#ef4444' : '#22c55e';
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(x + barW/2, yHigh);
            ctx.lineTo(x + barW/2, yLow);
            ctx.stroke();
            var bodyTop = Math.min(yOpen, yClose);
            var bodyH = Math.max(1, Math.abs(yClose - yOpen));
            ctx.fillStyle = isUp ? '#ef4444' : '#22c55e';
            ctx.fillRect(x, bodyTop, barW, bodyH);
        }
        ctx.strokeStyle = '#3b82f6';
        ctx.lineWidth = 1.2;
        ctx.setLineDash([3, 2]);
        ctx.beginPath();
        var started = false;
        for (var i = 4; i < n; i++) {
            var sum = 0;
            for (var j = i-4; j <= i; j++) sum += data[j].c;
            var ma = sum / 5;
            var mx = xScale(i) + barW/2, my = yScale(ma);
            if (!started) { ctx.moveTo(mx, my); started = true; }
            else ctx.lineTo(mx, my);
        }
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = '#9ca3af';
        ctx.font = '8px sans-serif';
        ctx.textAlign = 'center';
        for (var i = 0; i < n; i += Math.max(1, Math.floor(n/5))) {
            var dateStr = data[i].d;
            var label = dateStr.length === 8 ? dateStr.slice(4,6) + '/' + dateStr.slice(6,8) : dateStr.slice(-5);
            ctx.fillText(label, xScale(i) + barW/2, h - 6);
        }
    }

    document.addEventListener('mouseover', function(e) {
        var target = e.target;
        if (!target.classList.contains('fund-name')) return;
        var code = target.getAttribute('data-fund-code');
        if (!code) return;
        renderKline(code);
        if (kdata[code] && kdata[code].length >= 5) {
            tooltip.style.display = 'block';
        }
    });

    document.addEventListener('mousemove', function(e) {
        if (tooltip.style.display !== 'block') return;
        var x = e.clientX + 16, y = e.clientY - 10;
        if (x + 330 > window.innerWidth) x = e.clientX - 340;
        if (y + 210 > window.innerHeight) y = e.clientY - 220;
        tooltip.style.left = x + 'px';
        tooltip.style.top = y + 'px';
    });

    document.addEventListener('mouseout', function(e) {
        if (e.target.classList.contains('fund-name') || e.target.closest('.fund-name')) {
            tooltip.style.display = 'none';
        }
    });
})();
</script>'''

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ETF联接基金交易系统 - 可视化看板</title>
<script src="chart.umd.min.js"></script>
<style>
    * {{ margin:0; padding:0; box-sizing:border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif; background:#f3f4f6; color:#1f2937; }}
    .container {{ max-width:1400px; margin:0 auto; padding:20px; }}
    .header {{ background:linear-gradient(135deg, #1e40af, #3b82f6); color:#fff; padding:24px 32px; border-radius:16px; margin-bottom:20px; display:flex; justify-content:space-between; align-items:center; }}
    .header h1 {{ font-size:22px; font-weight:700; }}
    .header .time {{ font-size:13px; opacity:0.8; }}
    .cards {{ display:grid; grid-template-columns:repeat(4, 1fr); gap:16px; margin-bottom:20px; }}
    @media (max-width:768px) {{ .cards {{ grid-template-columns:repeat(2,1fr); }} }}
    .card {{ background:#fff; border-radius:12px; padding:20px; box-shadow:0 1px 3px rgba(0,0,0,0.1); }}
    .card-label {{ font-size:13px; color:#6b7280; margin-bottom:6px; }}
    .card-value {{ font-size:26px; font-weight:700; }}
    .card-sub {{ font-size:13px; margin-top:4px; }}
    .section {{ display:grid; grid-template-columns:1fr 1fr; gap:20px; margin-bottom:20px; }}
    @media (max-width:768px) {{ .section {{ grid-template-columns:1fr; }} }}
    .section-full {{ margin-bottom:20px; }}
    table {{ border-collapse:collapse; font-size:14px; }}
    thead tr {{ background:#f9fafb; }}
    thead th {{ padding:10px 12px; font-weight:600; color:#6b7280; border-bottom:2px solid #e5e7eb; text-align:left; }}
    tbody tr {{ border-bottom:1px solid #f3f4f6; }}
    tbody tr:hover {{ background:#f9fafb; }}
    .panic-gauge {{ text-align:center; }}
    .panic-circle {{ width:120px; height:120px; border-radius:50%; background:{panic_bg}; display:inline-flex; align-items:center; justify-content:center; flex-direction:column; color:#fff; margin-bottom:12px; box-shadow:0 4px 12px rgba(0,0,0,0.15); }}
    .panic-circle .icon {{ font-size:32px; }}
    .panic-circle .text {{ font-size:16px; font-weight:700; margin-top:4px; }}
    .panic-metrics {{ display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-top:12px; }}
    .panic-metric {{ text-align:center; padding:8px; background:#f9fafb; border-radius:8px; }}
    .panic-metric .val {{ font-size:18px; font-weight:700; }}
    .panic-metric .label {{ font-size:11px; color:#9ca3af; }}
    .flex {{ display:flex; gap:16px; flex-wrap:wrap; }}
    .badge {{ display:inline-block; padding:2px 8px; border-radius:4px; font-size:12px; font-weight:500; }}
    .badge-buy {{ background:#dcfce7; color:#166534; }}
    .badge-sell {{ background:#fee2e2; color:#991b1b; }}
    .badge-hold {{ background:#f3f4f6; color:#374151; }}
    .badge-reduce {{ background:#fef3c7; color:#92400e; }}
    .footer {{ text-align:center; padding:16px; color:#9ca3af; font-size:12px; }}
    /* K线悬浮提示 */
    .fund-name {{ position:relative; }}
    .fund-name:hover {{ background:#eff6ff; border-radius:4px; }}
    .kline-tooltip {{
        display:none; position:fixed; z-index:9999;
        background:#fff; border-radius:10px; box-shadow:0 8px 30px rgba(0,0,0,0.18);
        padding:12px; min-width:320px; pointer-events:none;
    }}
    .kline-tooltip .kline-title {{ font-size:13px; font-weight:700; color:#1f2937; margin-bottom:4px; text-align:left; }}
    .kline-tooltip .kline-stats {{ font-size:11px; color:#6b7280; margin-bottom:6px; display:flex; gap:16px; }}
    .kline-tooltip canvas {{ display:block; }}

</style>
</head>
<body>
<div class="container">

<!-- 头部 -->
<div class="header">
    <div>
        <h1>📊 ETF联接基金交易系统 - 可视化看板</h1>
        <div class="time">生成时间: {d["generated_at"]}</div>
    </div>
    <div style="text-align:right">
        <div style="font-size:14px;opacity:0.9">资金规模</div>
        <div style="font-size:28px;font-weight:800">{_fmt_money(d["total_capital"])}</div>
    </div>
</div>

<!-- 核心指标卡片 -->
<div class="cards">
    <div class="card">
        <div class="card-label">已投入市值</div>
        <div class="card-value">{_fmt_money(d["total_market_value"])}</div>
        <div class="card-sub" style="color:#6b7280">占比 {(d["total_market_value"]/d["total_capital"]*100) if d["total_capital"]>0 else 0:.1f}%</div>
    </div>
    <div class="card">
        <div class="card-label">可用现金</div>
        <div class="card-value" style="color:#3b82f6">{_fmt_money(d["available_cash"])}</div>
        <div class="card-sub" style="color:#6b7280">占比 {(d["available_cash"]/d["total_capital"]*100) if d["total_capital"]>0 else 0:.1f}%</div>
    </div>
    <div class="card">
        <div class="card-label">总盈亏</div>
        <div class="card-value" style="color:{pl_color}">{_fmt_money(d["total_profit_loss"])}</div>
        <div class="card-sub" style="color:{pl_color}">{_fmt_pct(d["total_profit_pct"])}</div>
    </div>
    <div class="card">
        <div class="card-label">持仓数量</div>
        <div class="card-value">{len(d["holdings"])} <span style="font-size:14px;font-weight:400">只</span></div>
        <div class="card-sub" style="color:#6b7280">宽基{len(d["core_holdings"])} + 卫星{len(d["satellite_holdings"])}</div>
    </div>
</div>

<!-- 恐慌指数 + 资产配置 -->
<div class="section">
    <!-- 恐慌仪表盘 -->
    <div class="card">
        <h3 style="margin-bottom:16px;font-size:16px;color:#374151">🛡️ 市场恐慌指数 <span style="font-size:12px;color:#9ca3af;font-weight:400">({panic_date_fmt})</span></h3>
        <div class="panic-gauge">
            <div class="panic-circle">
                <div class="icon">{panic_icon}</div>
                <div class="text">{panic_text}</div>
            </div>
            <div class="panic-metrics">
                <div class="panic-metric">
                    <div class="val" style="color:{hs300_color}">{hs300_display}</div>
                    <div class="label">沪深300涨跌 <span style="font-size:10px;opacity:0.7">{'(' + hs300_date_str + ')' if hs300_date_str else ''}</span></div>
                </div>
                <div class="panic-metric">
                    <div class="val" style="color:{north_color}">{north_display}</div>
                    <div class="label">北向资金 <span style="font-size:10px;opacity:0.7">{'(' + north_date_str + ')' if north_date_str else ''}</span></div>
                </div>
                <div class="panic-metric">
                    <div class="val" style="color:{ld_color}">{ld_display}</div>
                    <div class="label">跌停家数 <span style="font-size:10px;opacity:0.7">{'(' + ld_date_str + ')' if ld_date_str else ''}</span></div>
                </div>
                <div class="panic-metric">
                    <div class="val" style="color:{vol_color}">{vol_display}</div>
                    <div class="label">成交量比 <span style="font-size:10px;opacity:0.7">{'(' + vol_date_str + ')' if vol_date_str else ''}</span></div>
                </div>
            </div>
        </div>
    </div>
    <!-- 资产配置饼图 -->
    <div class="card">
        <h3 style="margin-bottom:16px;font-size:16px;color:#374151">💰 资产配置</h3>
        <div style="position:relative;height:220px">
            <canvas id="pieChart"></canvas>
        </div>
    </div>
</div>

<!-- 实时行情 + 实时恐慌 -->
<div class="section">
    <div class="card">
        <h3 style="margin-bottom:16px;font-size:16px;color:#374151">⚡ 实时行情 <span style="font-size:12px;color:#9ca3af;font-weight:400">{realtime.get('update_time','')}</span></h3>
        <div style="overflow-x:auto">
            <table style="width:100%;border-collapse:collapse;font-size:13px">
                <thead><tr style="background:#f9fafb;border-bottom:2px solid #e5e7eb">
                    <th style="padding:8px 12px;text-align:left;font-weight:600;color:#6b7280">ETF名称</th>
                    <th style="padding:8px 12px;text-align:right;font-weight:600;color:#6b7280">最新价</th>
                    <th style="padding:8px 12px;text-align:right;font-weight:600;color:#6b7280">涨跌幅</th>
                </tr></thead>
                <tbody>
                    {realtime_table}
                </tbody>
            </table>
        </div>
    </div>
    <div class="card">
        <h3 style="margin-bottom:16px;font-size:16px;color:#374151">⚡ 实时恐慌指数</h3>
        <div class="panic-gauge">
            <div class="panic-circle" style="background:{rt_panic_color}">
                <div class="icon">{rt_panic_icon}</div>
                <div class="text">{rt_panic_text}</div>
                <div style="font-size:12px;margin-top:2px;opacity:0.8">{rt_panic_score if rt_panic_score else '--'} 分</div>
            </div>
            <div class="panic-metrics">
                <div class="panic-metric">
                    <div class="val" style="color:#ef4444">{realtime.get('down_count',0)}</div>
                    <div class="label">下跌ETF数</div>
                </div>
                <div class="panic-metric">
                    <div class="val" style="color:#ef4444">{realtime.get('down_ratio','--')}%</div>
                    <div class="label">下跌占比</div>
                </div>
                <div class="panic-metric">
                    <div class="val" style="color:#6b7280">{realtime.get('avg_pct','--')}%</div>
                    <div class="label">平均涨跌幅</div>
                </div>
            </div>
        </div>
    </div>
</div>

<!-- 盘中信号 -->
{intraday_html}

<div class="section-full">
    <div class="card" style="grid-column:span 2">
        <h3 style="margin-bottom:16px;font-size:16px;color:#374151">⚡ 实时动量排名</h3>
        <div style="position:relative;height:240px">
            <canvas id="realtimeMomentumChart"></canvas>
        </div>
    </div>
</div>

<!-- 宽基PE估值 -->
<div class="section-full">
    <h3 style="margin-bottom:12px;font-size:16px;color:#374151">📈 宽基PE估值 + 趋势</h3>
    <div class="flex">{pe_items}</div>
</div>

<!-- 行业动量排名 -->
<div class="section">
    <div class="card" style="grid-column:span 2">
        <h3 style="margin-bottom:16px;font-size:16px;color:#374151">🔄 行业ETF动量排名</h3>
        <div style="position:relative;height:240px">
            <canvas id="momentumChart"></canvas>
        </div>
    </div>
</div>

<!-- 动量计算公式说明 -->
<div class="section-full">
    <div class="card">
        <h3 style="margin-bottom:12px;font-size:16px;color:#374151">📐 动量计算公式</h3>
        <div style="background:#f9fafb;border-radius:8px;padding:16px;font-size:13px;line-height:2;color:#374151">
            <div style="margin-bottom:12px">
                <div style="font-weight:600;color:#1f2937;margin-bottom:4px">① 短期动量（10日）</div>
                <div style="font-family:monospace;background:#fff;padding:8px 12px;border-radius:6px;color:#1d4ed8">
                    M₁ = (close<sub>今日</sub> / close<sub>10日前</sub> − 1) × 100
                </div>
                <div style="font-size:12px;color:#6b7280;margin-top:4px">价格比法：直接用最新收盘价除以N个交易日前的收盘价，避免涨跌幅缺失值导致窗口偏移</div>
            </div>
            <div style="margin-bottom:12px">
                <div style="font-weight:600;color:#1f2937;margin-bottom:4px">② 中期动量（30日）</div>
                <div style="font-family:monospace;background:#fff;padding:8px 12px;border-radius:6px;color:#1d4ed8">
                    M₂ = (close<sub>今日</sub> / close<sub>30日前</sub> − 1) × 100
                </div>
            </div>
            <div style="margin-bottom:12px">
                <div style="font-weight:600;color:#1f2937;margin-bottom:4px">③ 综合得分（加权融合）</div>
                <div style="font-family:monospace;background:#fff;padding:8px 12px;border-radius:6px;color:#059669">
                    Score = M₁ × 0.6 + M₂ × 0.4
                </div>
            </div>
            <div style="padding:10px 12px;background:#eff6ff;border-radius:6px;border-left:4px solid #3b82f6">
                <span style="font-weight:600;color:#1e40af">当前参数：</span>
                短期周期 = 10日，中期周期 = 30日，
                短期权重 = 0.6，中期权重 = 0.4<br>
                <span style="font-weight:600;color:#1e40af">计算方法：</span>
                价格比法（与ETF动量发现、箱体突破发现一致），数据来自通达信pytdx缓存
            </div>
        </div>
    </div>
</div>

<!-- 持仓列表 -->
{holdings_html}

<!-- ETF动量发现 (候选推荐) -->
{etf_candidates_html}

<!-- ETF箱体突破发现 -->
{breakout_html}

<!-- 今日交易指令 -->
<div class="section-full">
    <div class="card" style="border:2px solid #e5e7eb">
        <h3 style="margin-bottom:16px;font-size:18px;color:#374151">
            📋 今日交易指令
            <span style="font-size:13px;color:#9ca3af;font-weight:400;margin-left:8px">{d["action"]["date"]}</span>
        </h3>
        {_render_action(d["action"], panic)}
    </div>
</div>

<!-- 最近信号 + 交易记录 -->
<div class="section">
    <div class="card">
        <h3 style="margin-bottom:12px;font-size:16px;color:#374151">📋 历史信号</h3>
        <div style="overflow-x:auto">
            <table style="width:100%">
                <thead><tr><th>日期</th><th>标的</th><th>类型</th><th>原因</th></tr></thead>
                <tbody>{recent_signal_items if recent_signal_items else '<tr><td colspan="4" style="padding:20px;text-align:center;color:#9ca3af">暂无历史信号</td></tr>'}</tbody>
            </table>
        </div>
    </div>
    <div class="card">
        <h3 style="margin-bottom:12px;font-size:16px;color:#374151">💼 最近交易</h3>
        <div style="overflow-x:auto">
            <table style="width:100%">
                <thead><tr><th>日期</th><th>标的</th><th>金额</th><th>备注</th></tr></thead>
                <tbody>{trade_items if trade_items else '<tr><td colspan="4" style="padding:20px;text-align:center;color:#9ca3af">暂无交易记录</td></tr>'}</tbody>
            </table>
        </div>
    </div>
</div>

<div class="footer">
    ETF联接基金交易系统 v1.0 | 数据来源: Tushare Pro | 仅供参考, 不构成投资建议
</div>

</div>

<!-- K线数据 -->
<script>
window._klineData = {json.dumps(d["holdings_kline"], ensure_ascii=False)};
</script>

<script>
// 资产配置饼图
new Chart(document.getElementById('pieChart'), {{
    type: 'doughnut',
    data: {{
        labels: {pie_labels},
        datasets: [{{
            data: {pie_values},
            backgroundColor: {pie_colors},
            borderWidth: 2,
            borderColor: '#fff'
        }}]
    }},
    options: {{
        responsive: true,
        maintainAspectRatio: false,
        plugins: {{
            legend: {{
                position: 'bottom',
                labels: {{ padding: 16, font: {{ size: 13 }} }}
            }},
            tooltip: {{
                callbacks: {{
                    label: function(ctx) {{
                        var total = ctx.dataset.data.reduce(function(a,b){{ return a+b; }}, 0);
                        var pct = (ctx.raw / total * 100).toFixed(1);
                        return ctx.label + ': ¥' + ctx.raw.toLocaleString() + ' (' + pct + '%)';
                    }}
                }}
            }}
        }}
    }}
}});

// 动量排名柱状图
new Chart(document.getElementById('momentumChart'), {{
    type: 'bar',
    data: {{
        labels: {mom_labels},
        datasets: [{{
            label: '综合动量得分(%)',
            data: {mom_values},
            backgroundColor: {mom_colors},
            borderRadius: 6,
            barThickness: 32
        }}]
    }},
    options: {{
        responsive: true,
        maintainAspectRatio: false,
        plugins: {{
            legend: {{ display: false }},
            tooltip: {{
                callbacks: {{
                    title: function(items) {{
                        var idx = items[0].dataIndex;
                        var fundCode = {mom_fund_codes}[idx];
                        var company = {mom_companies}[idx];
                        var extra = [];
                        if (fundCode) extra.push(fundCode);
                        if (company) extra.push(company);
                        return extra.length ? items[0].label + ' (' + extra.join(' · ') + ')' : items[0].label;
                    }},
                    label: function(ctx) {{ return '综合动量: ' + ctx.raw + '%'; }},
                    afterBody: function(items) {{
                        var idx = items[0].dataIndex;
                        var ma20Status = {mom_ma20_status}[idx];
                        return ma20Status === 'above' ? '🟢 MA20上方 (可买入)' : '🔴 MA20下方 (禁止买入)';
                    }}
                }}
            }}
        }},
        scales: {{
            y: {{
                grid: {{ color: '#f3f4f6' }},
                ticks: {{ callback: function(v) {{ return v + '%'; }} }}
            }},
            x: {{
                grid: {{ display: false }}
            }}
        }}
    }}
}});

// 实时动量排名柱状图
new Chart(document.getElementById('realtimeMomentumChart'), {{
    type: 'bar',
    data: {{
        labels: {rt_mom_labels},
        datasets: [{{
            label: '实时涨跌幅(%)',
            data: {rt_mom_values},
            backgroundColor: {rt_mom_colors},
            borderRadius: 6,
            barThickness: 32
        }}]
    }},
    options: {{
        responsive: true,
        maintainAspectRatio: false,
        plugins: {{
            legend: {{ display: false }},
            tooltip: {{
                callbacks: {{
                    title: function(items) {{
                        var idx = items[0].dataIndex;
                        var fundCode = {rt_mom_fund_codes}[idx];
                        var company = {rt_mom_companies}[idx];
                        var extra = [];
                        if (fundCode) extra.push(fundCode);
                        if (company) extra.push(company);
                        return extra.length ? items[0].label + ' (' + extra.join(' · ') + ')' : items[0].label;
                    }},
                    label: function(ctx) {{ return '实时涨跌幅: ' + ctx.raw + '%'; }}
                }}
            }}
        }},
        scales: {{
            y: {{
                grid: {{ color: '#f3f4f6' }},
                ticks: {{ callback: function(v) {{ return v + '%'; }} }}
            }},
            x: {{
                grid: {{ display: false }}
            }}
        }}
    }}
}});

// ETF动量发现柱状图
new Chart(document.getElementById('discoveryChart'), {{
    type: 'bar',
    data: {{
        labels: {disc_labels},
        datasets: [{{
            label: '综合动量得分(%)',
            data: {disc_scores},
            backgroundColor: {disc_colors},
            borderRadius: 6,
            barThickness: 28
        }}]
    }},
    options: {{
        indexAxis: 'y',
        responsive: true,
        maintainAspectRatio: false,
        plugins: {{
            legend: {{ display: false }},
            tooltip: {{
                callbacks: {{
                    title: function(items) {{
                        var idx = items[0].dataIndex;
                        var code = {disc_codes}[idx];
                        return code ? items[0].label + ' (' + code + ')' : items[0].label;
                    }},
                    label: function(ctx) {{ return '综合动量: ' + ctx.raw + '%'; }}
                }}
            }}
        }},
        scales: {{
            x: {{
                grid: {{ color: '#f3f4f6' }},
                ticks: {{ callback: function(v) {{ return v + '%'; }} }}
            }},
            y: {{
                grid: {{ display: false }}
            }}
        }}
    }}
}});

// ETF箱体突破柱状图
new Chart(document.getElementById('breakoutChart'), {{
    type: 'bar',
    data: {{
        labels: {brk_labels},
        datasets: [{{
            label: '偏离中轨(%)',
            data: {brk_devs},
            backgroundColor: '#f59e0b',
            borderRadius: 6,
            barThickness: 28
        }}, {{
            label: '量比(x)',
            data: {brk_vols},
            backgroundColor: '#3b82f6',
            borderRadius: 6,
            barThickness: 28
        }}]
    }},
    options: {{
        indexAxis: 'y',
        responsive: true,
        maintainAspectRatio: false,
        plugins: {{
            legend: {{
                display: true,
                position: 'top',
                labels: {{ font: {{ size: 11 }} }}
            }},
            tooltip: {{
                callbacks: {{
                    title: function(items) {{
                        var idx = items[0].dataIndex;
                        var code = {brk_codes}[idx];
                        return code ? items[0].label + ' (' + code + ')' : items[0].label;
                    }}
                }}
            }}
        }},
        scales: {{
            x: {{
                grid: {{ color: '#f3f4f6' }},
                ticks: {{ callback: function(v) {{ return v; }} }}
            }},
            y: {{
                grid: {{ display: false }}
            }}
        }}
    }}
}});
</script>
{kline_js}
</body>
</html>'''

    return html


def generate_dashboard(output_path=None):
    """
    生成 HTML 看板文件
    output_path: 输出路径，默认 logs/dashboard.html
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    if output_path is None:
        output_path = os.path.join(LOG_DIR, "dashboard.html")

    print("  收集看板数据...")
    data = collect_dashboard_data()

    print("  生成 HTML...")
    html = generate_html(data)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"  [OK] 看板已生成: {output_path}")
    return output_path


if __name__ == "__main__":
    generate_dashboard()

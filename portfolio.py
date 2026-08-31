# ============================================================
# ETF联接基金交易系统 - 组合管理与信号输出模块
# 负责仓位计算、持仓管理、净值刷新、操作提示单生成
# ============================================================
import json
import csv
import pandas as pd
from datetime import datetime, timedelta
from db import get_connection
from config import (
    TOTAL_CAPITAL, CORE_RATIO, SATELLITE_RATIO,
    SINGLE_ETF_MAX_RATIO, MIN_TRADE_AMOUNT,
    SIGNAL_CUTOFF_TIME, TRADE_EXECUTION_REMINDER,
    CORE_ETFS, SATELLITE_ETFS, MIN_HOLD_DAYS,
    TUSHARE_TOKEN, MOMENTUM_SHORT, MOMENTUM_MEDIUM,
    TRAILING_STOP_ACTIVATE,
)


def _calc_confirm_date(submit_date_str):
    """
    根据提交日期(T日)计算份额确认日期(T+1)。
    使用 trade_cal 表（上交所官方交易日历），is_open=1 为交易日。
    返回: 确认日期字符串 (YYYYMMDD), 失败返回 None
    """
    if not submit_date_str:
        return None
    try:
        conn = get_connection()
        row = conn.execute("""
            SELECT cal_date FROM trade_cal
            WHERE exchange = 'SSE'
              AND is_open = 1
              AND cal_date > ?
            ORDER BY cal_date ASC
            LIMIT 1
        """, (submit_date_str,)).fetchone()
        conn.close()
        if row:
            return row["cal_date"]
        # fallback: trade_cal 数据未覆盖时，向后找10个自然日
        buy = datetime.strptime(submit_date_str, "%Y%m%d")
        for i in range(1, 11):
            d = buy + timedelta(days=i)
            # 简单跳过周末
            if d.weekday() < 5:
                return d.strftime("%Y%m%d")
    except Exception:
        pass
    return None


def _calc_hold_days(confirm_date_str):
    """
    根据份额确认日期计算持有天数(自然日)
    赎回费按自然日计算, 所以这里也用自然日
    """
    if not confirm_date_str:
        return 0
    try:
        confirm = datetime.strptime(confirm_date_str, "%Y%m%d")
        return (datetime.now() - confirm).days
    except Exception:
        return 0


def _get_etf_info_by_code(etf_code):
    """
    根据场内ETF代码查找对应的配置信息
    返回: {"name", "category", "fund_code"} 或 None
    """
    for etf in CORE_ETFS + SATELLITE_ETFS:
        if etf["code"] == etf_code:
            return {"name": etf["name"], "category": "core" if etf in CORE_ETFS else "satellite",
                    "fund_code": etf["fund_code"]}
    return None


def _get_etf_info_by_fund_code(fund_code):
    """
    根据联接基金代码查找对应的配置信息
    返回: {"name", "category", "etf_code", "fund_code"} 或 None
    """
    for etf in CORE_ETFS + SATELLITE_ETFS:
        if etf["fund_code"] == fund_code:
            return {"name": etf["name"], "category": "core" if etf in CORE_ETFS else "satellite",
                    "etf_code": etf["code"], "fund_code": etf["fund_code"]}
    return None


# ============================================================
# 持仓管理
# ============================================================

def get_current_holdings():
    """
    获取当前有效持仓 (含在途)
    返回: list of holding dicts
    """
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM portfolio
        WHERE status IN ('holding', 'pending_buy', 'pending_sell')
        ORDER BY buy_date
    """).fetchall()
    conn.close()

    holdings = []
    for r in rows:
        h = dict(r)
        # 计算持有天数: 优先用 confirm_date(份额确认日), fallback 到 buy_date+1
        confirm = h.get("confirm_date")
        if confirm:
            h["hold_days"] = _calc_hold_days(confirm)
        elif h.get("buy_date"):
            # 旧数据没有 confirm_date, 用 buy_date 计算 (会多算1天, 偏保守)
            buy = datetime.strptime(h["buy_date"], "%Y%m%d")
            h["hold_days"] = (datetime.now() - buy).days
        holdings.append(h)
    return holdings


def get_available_cash():
    """
    计算可用现金
    = 总资金 - 持仓市值 - 在途申购金额
    """
    conn = get_connection()
    row = conn.execute("""
        SELECT 
            COALESCE(SUM(CASE WHEN status IN ('holding','pending_buy','pending_sell') THEN market_value ELSE 0 END), 0) as invested,
            COALESCE(SUM(CASE WHEN status = 'pending_buy' THEN buy_amount ELSE 0 END), 0) as pending_buy
        FROM portfolio
    """).fetchone()
    conn.close()

    invested = row["invested"] if row["invested"] else 0
    pending_buy = row["pending_buy"] if row["pending_buy"] else 0
    available = TOTAL_CAPITAL - invested - pending_buy
    return max(available, 0)


# ============================================================
# 持仓导入
# ============================================================

def import_holdings_from_csv(csv_path):
    """
    从 CSV 文件导入现有持仓到 portfolio 表

    CSV 格式要求 (UTF-8编码, 第一行为表头):
      fund_code    : 联接基金代码 (必填, 如 110020)
      etf_code     : 场内ETF代码 (选填, 如 159919.SZ, 不填则自动匹配)
      name         : 基金名称 (选填, 不填则自动匹配)
      shares       : 持有份额 (必填)
      cost_nav     : 持仓成本净值 (必填)
      current_nav  : 当前净值 (选填, 不填则自动更新)
      buy_date     : 首次买入日期 (选填, 格式 YYYYMMDD)
      buy_amount   : 买入金额 (选填, 默认=份额×成本净值)
      category     : 类别 (选填, core/satellite/auto, 默认auto自动判断)
      total_cost   : 总成本 (选填, 默认=份额×成本净值)
    """
    import os
    if not os.path.exists(csv_path):
        print(f"  [ERROR] 文件不存在: {csv_path}")
        return 0

    conn = get_connection()
    imported = 0
    skipped = 0

    try:
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, 1):
                fund_code = row.get("fund_code", "").strip()
                if not fund_code:
                    print(f"  [SKIP] 第{i}行: 缺少 fund_code")
                    skipped += 1
                    continue

                # 自动匹配 ETF 信息
                etf_code = row.get("etf_code", "").strip() or ""
                name = row.get("name", "").strip() or ""
                category = row.get("category", "auto").strip().lower()

                info = _get_etf_info_by_fund_code(fund_code)
                if info:
                    if not etf_code:
                        etf_code = info["etf_code"]
                    if not name:
                        name = info["name"]
                    if category == "auto":
                        category = info["category"]
                else:
                    # 也尝试用 etf_code 查找
                    if etf_code:
                        info2 = _get_etf_info_by_code(etf_code)
                        if info2:
                            if not name:
                                name = info2["name"]
                            if category == "auto":
                                category = info2["category"]
                    if category == "auto":
                        category = "satellite"  # 默认归为卫星

                # 解析数值字段
                try:
                    shares = float(row.get("shares", 0))
                    cost_nav = float(row.get("cost_nav", 0))
                except ValueError:
                    print(f"  [SKIP] 第{i}行 {fund_code}: shares/cost_nav 格式错误")
                    skipped += 1
                    continue

                if shares <= 0 or cost_nav <= 0:
                    print(f"  [SKIP] 第{i}行 {fund_code}: 份额或成本净值必须>0")
                    skipped += 1
                    continue

                # 可选字段
                current_nav = None
                if row.get("current_nav", "").strip():
                    try:
                        current_nav = float(row["current_nav"])
                    except ValueError:
                        pass
                if current_nav is None:
                    current_nav = cost_nav  # 默认等于成本, 后续自动刷新

                buy_date = row.get("buy_date", "").strip() or None
                confirm_date = _calc_confirm_date(buy_date) if buy_date else None
                buy_amount = None
                if row.get("buy_amount", "").strip():
                    try:
                        buy_amount = float(row["buy_amount"])
                    except ValueError:
                        pass
                if buy_amount is None:
                    buy_amount = shares * cost_nav

                total_cost = None
                if row.get("total_cost", "").strip():
                    try:
                        total_cost = float(row["total_cost"])
                    except ValueError:
                        pass
                if total_cost is None:
                    total_cost = buy_amount

                market_value = shares * current_nav
                profit_loss = market_value - total_cost
                profit_pct = (profit_loss / total_cost * 100) if total_cost > 0 else 0

                # 写入数据库 (如果已有同 fund_code 的 holding 记录则跳过)
                existing = conn.execute(
                    "SELECT id FROM portfolio WHERE fund_code = ? AND status IN ('holding','pending_buy')",
                    (fund_code,)
                ).fetchone()

                if existing:
                    print(f"  [SKIP] {name} ({fund_code}): 已存在持仓记录 (id={existing['id']}), 如需更新请先删除")
                    skipped += 1
                    continue

                conn.execute("""
                    INSERT INTO portfolio (fund_code, etf_code, name, category, shares, cost_nav,
                                          current_nav, total_cost, market_value, profit_loss, profit_pct,
                                          status, buy_date, confirm_date, buy_amount)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'holding', ?, ?, ?)
                """, (fund_code, etf_code, name, category, shares, cost_nav,
                      current_nav, total_cost, market_value, profit_loss, profit_pct,
                      buy_date, confirm_date, round(buy_amount, 2)))

                imported += 1
                print(f"  [OK] {name} ({fund_code}): {shares}份, 成本{cost_nav}, 市值¥{market_value:,.0f}, 盈亏{profit_pct:+.2f}%")

    except Exception as e:
        print(f"  [ERROR] 读取CSV失败: {e}")
        conn.close()
        return 0

    conn.commit()
    conn.close()
    print(f"\n  导入完成: 成功{imported}只, 跳过{skipped}只")
    return imported


# ============================================================
# 持仓净值刷新
# ============================================================

def _fetch_fund_nav_akshare(fund_code):
    """
    通过 AkShare 获取联接基金最新单位净值
    fund_code: 场外基金代码 (如 '017646')
    返回: float (最新单位净值), 失败返回 None
    """
    try:
        import akshare as ak
        df = ak.fund_open_fund_info_em(symbol=fund_code, indicator="单位净值走势")
        if df is None or df.empty:
            return None
        # 取最后一行的单位净值
        last_row = df.iloc[-1]
        nav = float(last_row.iloc[1])  # 第2列为"单位净值"
        nav_date = str(last_row.iloc[0])  # 第1列为"净值日期"
        return nav, nav_date
    except Exception as e:
        return None


def update_holdings_nav():
    """
    用 AkShare 获取联接基金真实单位净值, 刷新持仓的 current_nav / market_value / profit_pct
    不再用场内ETF收盘价估算, 避免联接基金净值与ETF价格差异导致的偏差
    """
    conn = get_connection()
    holdings = conn.execute("""
        SELECT id, fund_code, etf_code, name, shares, cost_nav, total_cost, status
        FROM portfolio WHERE status IN ('holding', 'pending_buy')
    """).fetchall()

    if not holdings:
        conn.close()
        print("  [INFO] 无持仓需要刷新")
        return

    updated = 0
    for h in holdings:
        fund_code = h["fund_code"]
        name = h["name"]

        # 通过 AkShare 获取联接基金真实净值
        result = _fetch_fund_nav_akshare(fund_code)
        if result is None:
            print(f"  [WARN] {name} ({fund_code}): AkShare获取净值失败, 跳过")
            continue

        current_nav, nav_date = result
        shares = h["shares"]
        total_cost = h["total_cost"]
        market_value = shares * current_nav
        profit_loss = market_value - total_cost
        profit_pct = (profit_loss / total_cost * 100) if total_cost > 0 else 0

        conn.execute("""
            UPDATE portfolio
            SET current_nav = ?, nav_date = ?, market_value = ?, profit_loss = ?, profit_pct = ?,
                updated_at = datetime('now', 'localtime')
            WHERE id = ?
        """, (current_nav, nav_date, round(market_value, 2), round(profit_loss, 2), round(profit_pct, 2), h["id"]))

        updated += 1
        emoji = "🔴" if profit_pct < 0 else "🟢"
        print(f"  {emoji} {name}: 净值={current_nav:.4f} ({nav_date}), 市值=¥{market_value:,.0f}, 盈亏={profit_pct:+.2f}%")

    conn.commit()
    conn.close()
    print(f"  [OK] 净值刷新完成: {updated}只 (AkShare)")


# ============================================================
# 持仓卖出 (标记赎回)
# ============================================================

def sell_holding(fund_code, submit_date=None, confirm_date=None):
    """
    标记某只基金为已赎回, 记录交易日志
    fund_code: 联接基金代码
    """
    conn = get_connection()
    row = conn.execute("""
        SELECT * FROM portfolio
        WHERE fund_code = ? AND status IN ('holding', 'pending_buy')
    """, (fund_code,)).fetchone()

    if not row:
        conn.close()
        # 尝试模糊匹配
        candidates = conn.execute("""
            SELECT fund_code, name FROM portfolio
            WHERE status IN ('holding', 'pending_buy')
              AND (fund_code LIKE ? OR name LIKE ?)
        """, (f"%{fund_code}%", f"%{fund_code}%")).fetchall() if not conn.closed else []
        if candidates:
            print(f"  未找到精确匹配, 相似结果:")
            for c in candidates:
                print(f"    - {c['name']} ({c['fund_code']})")
        else:
            print(f"  [ERROR] 未找到基金 {fund_code} 的持仓记录")
        # 需要重新获取连接
        conn = get_connection()
        conn.close()
        return False

    h = dict(row)
    # 确认日: 使用指定的，或自动计算下一个交易日(T+1)
    if confirm_date is None:
        _submit = submit_date or h.get("buy_date") or datetime.now().strftime("%Y%m%d")
        confirm_date = _calc_confirm_date(_submit)
        if confirm_date is None:
            confirm_date = datetime.now().strftime("%Y%m%d")
    if submit_date is None:
        submit_date = h.get("buy_date") or confirm_date
    sell_amount = h["market_value"]

    # 更新状态
    conn.execute("""
        UPDATE portfolio
        SET status = 'sold', updated_at = datetime('now', 'localtime')
        WHERE id = ?
    """, (h["id"],))

    # 记录交易
    conn.execute("""
        INSERT INTO trades (fund_code, etf_code, name, trade_type, amount, shares, nav, fee,
                           status, submit_date, confirm_date, settle_date, signal_source, remark)
        VALUES (?, ?, ?, 'sell', ?, ?, ?, 0, 'done', ?, ?, ?, 'manual',
                ?)
    """, (h["fund_code"], h["etf_code"], h["name"],
          round(sell_amount, 2), h["shares"], h["current_nav"],
          h["buy_date"] or confirm_date, confirm_date, None,
          f"手动赎回: 成本¥{h['total_cost']:,.0f}, 市值¥{sell_amount:,.0f}, 盈亏{h['profit_pct']:+.2f}%"))

    conn.commit()
    conn.close()

    emoji = "🟢" if h["profit_pct"] >= 0 else "🔴"
    print(f"  {emoji} {h['name']} ({fund_code}) 已标记为赎回")
    print(f"     份额: {h['shares']}份  成本净值: {h['cost_nav']}  当前净值: {h['current_nav']}")
    print(f"     成本: ¥{h['total_cost']:,.0f}  市值: ¥{sell_amount:,.0f}  盈亏: {h['profit_pct']:+.2f}%")
    return True


# ============================================================
# 持仓更新 (加仓/调仓)
# ============================================================

def update_holding(fund_code, new_shares=None, new_cost_nav=None, add_shares=None, add_amount=None,
                   submit_date=None, confirm_date=None):
    """
    更新持仓信息 (支持两种模式):
    模式1 - 替换: new_shares + new_cost_nav → 直接替换份额和成本
    模式2 - 追加: add_shares + add_amount → 在原有基础上加仓 (加权平均成本)

    submit_date: 申购提交日期 (YYYYMMDD, 默认今天), 用于追加模式的交易记录
    confirm_date: 份额确认日期 (YYYYMMDD, 默认今天), 用于追加模式的交易记录

    返回: bool 是否成功
    """
    conn = get_connection()
    row = conn.execute("""
        SELECT * FROM portfolio
        WHERE fund_code = ? AND status IN ('holding', 'pending_buy')
    """, (fund_code,)).fetchone()

    if not row:
        conn.close()
        print(f"  [ERROR] 未找到基金 {fund_code} 的持仓记录")
        return False

    h = dict(row)
    old_shares = h["shares"]
    old_cost_nav = h["cost_nav"]
    old_total_cost = h["total_cost"]

    if add_shares is not None and add_shares > 0:
        # 追加模式: 加权平均成本
        if add_amount is None:
            add_amount = add_shares * old_cost_nav  # 默认按原成本价估算
        new_total_shares = old_shares + add_shares
        new_total_cost = old_total_cost + add_amount
        avg_cost_nav = new_total_cost / new_total_shares if new_total_shares > 0 else old_cost_nav

        # 重新计算市值和盈亏
        new_market_value = new_total_shares * h["current_nav"]
        new_profit_loss = new_market_value - new_total_cost
        new_profit_pct = (new_profit_loss / new_total_cost * 100) if new_total_cost > 0 else 0

        # 加仓后成本基准变化, 峰值重新起算:
        # 新盈亏已达激活阈值 → 继续跟踪(peak=当前盈亏); 否则置0等待重新激活
        new_peak = new_profit_pct if new_profit_pct >= TRAILING_STOP_ACTIVATE else 0

        conn.execute("""
            UPDATE portfolio
            SET shares = ?, cost_nav = ?, total_cost = ?, buy_amount = ?,
                market_value = ?, profit_loss = ?, profit_pct = ?,
                peak_profit_pct = ?,
                updated_at = datetime('now', 'localtime')
            WHERE id = ?
        """, (new_total_shares, round(avg_cost_nav, 4), round(new_total_cost, 2),
              round(new_total_cost, 2),
              round(new_market_value, 2), round(new_profit_loss, 2), round(new_profit_pct, 2),
              round(new_peak, 2),
              h["id"]))

        # 记录交易
        _today = datetime.now().strftime("%Y%m%d")
        _submit = submit_date or _today
        _confirm = confirm_date or _today
        conn.execute("""
            INSERT INTO trades (fund_code, etf_code, name, trade_type, amount, shares, nav, fee,
                               status, submit_date, confirm_date, settle_date, signal_source, remark)
            VALUES (?, ?, ?, 'buy', ?, ?, ?, 0, 'confirmed', ?, ?, ?, 'manual', ?)
        """, (h["fund_code"], h["etf_code"], h["name"],
              round(add_amount, 2), add_shares, round(add_amount / add_shares, 4) if add_shares else old_cost_nav,
              _submit, _confirm, None,
              f"手动加仓: +{add_shares}份, 加仓¥{add_amount:,.0f}, 申购{_submit}, 确认{_confirm}"))

        conn.commit()
        conn.close()
        print(f"  📈 {h['name']} ({fund_code}) 加仓完成")
        print(f"     原持仓: {old_shares}份 × {old_cost_nav} = ¥{old_total_cost:,.0f}")
        print(f"     本次加: +{add_shares}份, +¥{add_amount:,.0f}")
        print(f"     新持仓: {new_total_shares}份 × {avg_cost_nav:.4f} = ¥{new_total_cost:,.0f}")

    elif new_shares is not None and new_cost_nav is not None:
        # 替换模式
        if new_shares <= 0 or new_cost_nav <= 0:
            conn.close()
            print(f"  [ERROR] 份额和成本净值必须>0")
            return False

        new_total_cost = new_shares * new_cost_nav
        new_market_value = new_shares * h["current_nav"]
        new_profit_loss = new_market_value - new_total_cost
        new_profit_pct = (new_profit_loss / new_total_cost * 100) if new_total_cost > 0 else 0

        conn.execute("""
            UPDATE portfolio
            SET shares = ?, cost_nav = ?, total_cost = ?, buy_amount = ?,
                market_value = ?, profit_loss = ?, profit_pct = ?,
                updated_at = datetime('now', 'localtime')
            WHERE id = ?
        """, (new_shares, new_cost_nav, round(new_total_cost, 2),
              round(new_total_cost, 2),
              round(new_market_value, 2), round(new_profit_loss, 2), round(new_profit_pct, 2),
              h["id"]))

        conn.commit()
        conn.close()
        print(f"  ✏️ {h['name']} ({fund_code}) 持仓已更新")
        print(f"     {old_shares}份 × {old_cost_nav} → {new_shares}份 × {new_cost_nav}")

    else:
        conn.close()
        print(f"  [ERROR] 请指定 --shares + --cost_nav (替换) 或 --add-shares + --add-amount (追加)")
        return False

    return True


# ============================================================
# 持仓概览
# ============================================================

def show_holdings():
    """
    显示当前持仓概览
    """
    conn = get_connection()
    holdings = conn.execute("""
        SELECT * FROM portfolio
        WHERE status IN ('holding', 'pending_buy')
        ORDER BY category, market_value DESC
    """).fetchall()

    cash = get_available_cash()
    conn.close()

    if not holdings:
        print("\n  📭 当前无持仓")
        print(f"  💰 可用现金: ¥{cash:,.0f}")
        return

    # 转为dict以支持 .get()
    holdings = [dict(h) for h in holdings]
    # 动态计算持有天数 (数据库hold_days字段可能过时)
    for h in holdings:
        confirm = h.get("confirm_date")
        if confirm:
            h["hold_days"] = _calc_hold_days(confirm)
        elif h.get("buy_date"):
            buy = datetime.strptime(h["buy_date"], "%Y%m%d")
            h["hold_days"] = (datetime.now() - buy).days
        else:
            h["hold_days"] = 0

    total_mv = sum(h["market_value"] for h in holdings)
    total_cost = sum(h["total_cost"] for h in holdings)
    total_pl = sum(h["profit_loss"] for h in holdings)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines = []
    lines.append("")
    lines.append("=" * 80)
    lines.append(f"  持仓概览  ({now})")
    lines.append("=" * 80)

    # 汇总
    lines.append(f"  总资金: ¥{TOTAL_CAPITAL:>12,.0f}    已投入: ¥{total_mv:>12,.0f}    可用: ¥{cash:>12,.0f}")
    pl_emoji = "🟢" if total_pl >= 0 else "🔴"
    lines.append(f"  总成本: ¥{total_cost:>12,.0f}    总盈亏: {pl_emoji}¥{total_pl:>+12,.0f}")

    # 按类别分组
    core_holdings = [h for h in holdings if h["category"] == "core"]
    sat_holdings = [h for h in holdings if h["category"] == "satellite"]
    other_holdings = [h for h in holdings if h["category"] not in ("core", "satellite")]

    if core_holdings:
        lines.append("")
        lines.append("-" * 80)
        lines.append("  📊 宽基底仓")
        lines.append(f"  {'名称':<14s} {'基金代码':<10s} {'份额':>10s} {'成本净值':>8s} {'当前净值':>8s} {'市值':>12s} {'盈亏%':>8s} {'持有天数':>6s}")
        lines.append("  " + "-" * 82)
        for h in core_holdings:
            emoji = "🟢" if h["profit_pct"] >= 0 else "🔴"
            hold_d = h.get("hold_days", 0)
            lines.append(f"  {h['name']:<12s} {h['fund_code']:<10s} {h['shares']:>10.2f} "
                        f"{h['cost_nav']:>8.4f} {h['current_nav']:>8.4f} "
                        f"¥{h['market_value']:>10,.0f} {emoji}{h['profit_pct']:>6.2f}% {hold_d:>5d}天")

    if sat_holdings:
        lines.append("")
        lines.append("-" * 80)
        lines.append("  🔄 行业卫星")
        lines.append(f"  {'名称':<14s} {'基金代码':<10s} {'份额':>10s} {'成本净值':>8s} {'当前净值':>8s} {'市值':>12s} {'盈亏%':>8s} {'持有天数':>6s}")
        lines.append("  " + "-" * 82)
        for h in sat_holdings:
            emoji = "🟢" if h["profit_pct"] >= 0 else "🔴"
            hold_d = h.get("hold_days", 0)
            lines.append(f"  {h['name']:<12s} {h['fund_code']:<10s} {h['shares']:>10.2f} "
                        f"{h['cost_nav']:>8.4f} {h['current_nav']:>8.4f} "
                        f"¥{h['market_value']:>10,.0f} {emoji}{h['profit_pct']:>6.2f}% {hold_d:>5d}天")

    if other_holdings:
        lines.append("")
        lines.append("-" * 80)
        lines.append("  📦 其他持仓")
        lines.append(f"  {'名称':<14s} {'基金代码':<10s} {'份额':>10s} {'成本净值':>8s} {'当前净值':>8s} {'市值':>12s} {'盈亏%':>8s} {'持有天数':>6s}")
        lines.append("  " + "-" * 82)
        for h in other_holdings:
            emoji = "🟢" if h["profit_pct"] >= 0 else "🔴"
            hold_d = h.get("hold_days", 0)
            lines.append(f"  {h['name']:<12s} {h['fund_code']:<10s} {h['shares']:>10.2f} "
                        f"{h['cost_nav']:>8.4f} {h['current_nav']:>8.4f} "
                        f"¥{h['market_value']:>10,.0f} {emoji}{h['profit_pct']:>6.2f}% {hold_d:>5d}天")

    lines.append("")
    lines.append("=" * 80)

    output = "\n".join(lines)
    print(output)
    return output


# ============================================================
# 仓位计算
# ============================================================

def calc_position_sizes(signals_result, panic_alert):
    """
    根据信号计算建议仓位大小
    signals_result: {"core": [...], "rotation": {"buy":[...],"sell":[...],"hold":[...], "stop": [...]}}
    """
    available = get_available_cash()

    # 宽基仓位: 总资金 × 目标权重
    core_positions = []
    for sig in signals_result.get("core", []):
        target_amount = TOTAL_CAPITAL * sig["target_weight"]
        core_positions.append({
            **sig,
            "target_amount": round(target_amount, 2),
            "reason": sig.get("reason", "")
        })

    # 行业卫星仓位: 卫星资金 = 总资金 × 卫星比例 (首次建仓)
    # 如果已有持仓, 用总资金卫星比例减去已有行业持仓
    satellite_cash = TOTAL_CAPITAL * SATELLITE_RATIO
    satellite_positions = []

    buy_signals = signals_result.get("rotation", {}).get("buy", [])
    if buy_signals:
        # 计算新买入和补仓分别需要的资金
        new_buy_count = 0
        total_replenish_amount = 0
        for sig in buy_signals:
            current_val = sig.get("current_value", 0)
            target_val = sig.get("target_value", 0)
            if current_val > 0 and target_val > 0:
                # 补仓: 金额 = 目标 - 当前市值
                replenish_amount = max(target_val - current_val, MIN_TRADE_AMOUNT)
                sig["_replenish_amount"] = replenish_amount
                total_replenish_amount += replenish_amount
            else:
                # 首次建仓
                new_buy_count += 1

        # 先分配补仓资金, 剩余平分给新买入
        remaining_cash = satellite_cash - total_replenish_amount
        for sig in buy_signals:
            if "_replenish_amount" in sig:
                amount = sig["_replenish_amount"]
            elif new_buy_count > 0:
                amount = max(min(remaining_cash / new_buy_count,
                                 TOTAL_CAPITAL * SINGLE_ETF_MAX_RATIO), MIN_TRADE_AMOUNT)
            else:
                amount = max(min(satellite_cash / max(len(buy_signals), 1),
                                 TOTAL_CAPITAL * SINGLE_ETF_MAX_RATIO), MIN_TRADE_AMOUNT)
            satellite_positions.append({**sig, "target_amount": round(amount, 2)})

    hold_signals = signals_result.get("rotation", {}).get("hold", [])
    for sig in hold_signals:
        satellite_positions.append({**sig, "target_amount": None, "reason": "继续持有"})

    return {
        "core": core_positions,
        "satellite_buy": satellite_positions,
        "satellite_sell": signals_result.get("rotation", {}).get("sell", []),
        "available_cash": round(available, 2),
        "is_panic": panic_alert.get("is_panic", False)
    }


# ============================================================
# 操作提示单生成
# ============================================================

def generate_action_sheet(signals_result, panic_alert, position_sizes):
    """
    生成当日操作提示单 (纯文本格式)
    """
    now = datetime.now()
    lines = []
    lines.append("=" * 70)
    lines.append(f"  ETF联接基金交易系统 - 操作提示单")
    lines.append(f"  生成时间: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"  ⚠️ {TRADE_EXECUTION_REMINDER}")
    lines.append("=" * 70)

    # 恐慌状态
    if panic_alert.get("is_panic"):
        lines.append("")
        lines.append("  🚨🚨🚨  市场恐慌预警  🚨🚨🚨")
        lines.append("  触发指标:")
        for t in panic_alert["triggered"]:
            lines.append(f"    ❌ {t}")
        lines.append("  >>> 建议暂停申购，持仓触发止损需提前赎回 <<<")
    elif panic_alert.get("trigger_count", 0) > 0:
        lines.append("")
        lines.append(f"  ⚠️ 市场波动提醒 ({panic_alert['trigger_count']}项指标触发)")
        for t in panic_alert["triggered"]:
            lines.append(f"    ⚠ {t}")

    # 止盈止损信号 (最高优先级)
    stop_signals = signals_result.get("stop", [])
    if stop_signals:
        lines.append("")
        lines.append("-" * 70)
        lines.append("  🔴 止盈/止损信号 (最高优先级)")
        lines.append("-" * 70)
        for sig in stop_signals:
            priority_mark = "🚨" if sig.get("priority") == "urgent" else "🔴"
            sell_ratio = sig.get("sell_ratio", 1.0)
            if sell_ratio < 1.0:
                action = f"赎回一半 ({sell_ratio:.0%})"
            else:
                action = "赎回全部"
            lines.append(f"  {priority_mark} {sig['name']} ({sig.get('fund_code','')})")
            lines.append(f"     操作: {action}")
            lines.append(f"     原因: {sig['reason']}")
            lines.append("")

    # 宽基信号
    core_signals = signals_result.get("core", [])
    if core_signals:
        lines.append("-" * 70)
        lines.append("  📊 宽基底仓信号")
        lines.append("-" * 70)
        for sig in core_signals:
            direction_map = {"buy": "🟢 建议申购", "sell": "🔴 建议赎回", "hold": "⚪ 维持不变", "reduce": "🟡 建议减仓"}
            lines.append(f"  {direction_map.get(sig['direction'], sig['direction'])}  {sig['name']}")
            lines.append(f"     联接基金代码: {sig.get('fund_code','待确认')}")
            lines.append(f"     目标金额: ¥{sig.get('target_amount',0):,.0f}")
            lines.append(f"     逻辑: {sig.get('reason','')}")
            lines.append("")

    # 行业轮动信号
    sat_buy = signals_result.get("rotation", {}).get("buy", [])
    sat_sell = signals_result.get("rotation", {}).get("sell", [])
    sat_hold = signals_result.get("rotation", {}).get("hold", [])

    if sat_buy or sat_sell or sat_hold:
        lines.append("-" * 70)
        lines.append("  🔄 行业ETF动量轮动")
        lines.append("-" * 70)

        # 动量排名
        momentum_df = signals_result.get("rotation", {}).get("momentum_df")
        if momentum_df is not None and not momentum_df.empty:
            lines.append("  动量排名:")
            for _, row in momentum_df.iterrows():
                marker = "★" if row["rank"] <= 3 else " "
                fund_code = row.get("fund_code", "")
                lines.append(f"    {marker} #{row['rank']} {row['name']:8s} ({fund_code})  "
                            f"{MOMENTUM_SHORT}日:{row['momentum_20d']:+.2f}%  "
                            f"{MOMENTUM_MEDIUM}日:{row['momentum_60d']:+.2f}%  "
                            f"综合:{row['composite_score']:+.2f}%")

        lines.append("")
        if sat_buy:
            lines.append("  🟢 建议申购:")
            for sig in sat_buy:
                lines.append(f"    → {sig['name']} ({sig.get('fund_code','待确认')})")
                lines.append(f"      金额: ¥{sig.get('target_amount',0):,.0f}  原因: {sig.get('reason','')}")

        if sat_sell:
            lines.append("")
            lines.append("  🔴 建议赎回:")
            for sig in sat_sell:
                hold_days = sig.get("hold_days", 0)
                can_sell = "可赎回" if hold_days >= MIN_HOLD_DAYS else f"⚠️ 持有仅{hold_days}天,赎回费1.5%"
                lines.append(f"    → {sig['name']} ({sig.get('fund_code','待确认')})")
                lines.append(f"      状态: {can_sell}  原因: {sig.get('reason','')}")

    # 资金概览
    lines.append("")
    lines.append("-" * 70)
    lines.append("  💰 资金概览")
    lines.append("-" * 70)
    lines.append(f"  总资金:        ¥{TOTAL_CAPITAL:>12,.0f}")
    lines.append(f"  可用现金:      ¥{position_sizes.get('available_cash',0):>12,.0f}")
    lines.append(f"  宽基配置:      ¥{TOTAL_CAPITAL * CORE_RATIO:>12,.0f} ({CORE_RATIO*100:.0f}%)")
    lines.append(f"  行业卫星:      ¥{TOTAL_CAPITAL * SATELLITE_RATIO:>12,.0f} ({SATELLITE_RATIO*100:.0f}%)")

    lines.append("")
    lines.append("=" * 70)
    lines.append("  ⚠️ 执行前请确认:")
    lines.append("  □ 今日无重大国际/国内突发事件")
    lines.append("  □ 已阅读信号原因并认可操作逻辑")
    lines.append("  □ 赎回操作确认持有超过7天 (避免惩罚赎回费)")
    lines.append(f"  □ 将在14:30前完成申赎操作")
    lines.append("=" * 70)

    return "\n".join(lines)


def save_signals_to_db(signals_result, panic_alert):
    """将信号保存到数据库"""
    conn = get_connection()
    today = datetime.now().strftime("%Y%m%d")

    # 先清除当天旧信号, 避免重复运行导致数据重复
    conn.execute("DELETE FROM signals WHERE signal_date = ?", (today,))

    for sig in signals_result.get("core", []):
        _insert_signal(conn, today, "core_pe", sig)

    for sig in signals_result.get("rotation", {}).get("buy", []):
        _insert_signal(conn, today, "rotation_buy", sig)
    for sig in signals_result.get("rotation", {}).get("sell", []):
        _insert_signal(conn, today, "rotation_sell", sig)

    for sig in signals_result.get("stop", []):
        priority = sig.get("priority", "high")
        _insert_signal(conn, today, sig.get("signal_type", "stop"), sig, priority=priority)

    conn.commit()
    conn.close()


def _insert_signal(conn, date, signal_type, sig, priority="normal"):
    """插入单条信号记录"""
    conn.execute("""
        INSERT INTO signals (signal_date, signal_type, etf_code, fund_code, name,
                            category, direction, amount, reason, priority)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (date, signal_type, sig["etf_code"], sig.get("fund_code"), sig["name"],
          sig.get("category", ""), sig.get("direction", ""),
          sig.get("target_amount"), sig.get("reason", ""), priority))


# ============================================================
# 新增建仓
# ============================================================

def buy_new_holding(fund_code, amount, etf_code=None, name=None, category="satellite",
                    submit_date=None):
    """
    新增建仓: python main.py buy <fund_code> --amount N [--submit-date YYYYMMDD]
    fund_code: 联接基金代码 (必填)
    amount: 申购金额 (必填)
    etf_code: 场内ETF代码 (选填, 不填则从配置自动匹配)
    name: 基金名称 (选填, 不填则从配置自动匹配)
    category: core/satellite (默认satellite)
    submit_date: 申购日期 (YYYYMMDD, 默认今天), 支持补录历史申购
    """
    # 自动匹配 ETF 信息
    info = _get_etf_info_by_fund_code(fund_code)
    if info:
        if not etf_code:
            etf_code = info["etf_code"]
        if not name:
            name = info["name"]
        category = info["category"]
    else:
        if not etf_code:
            etf_code = ""
        if not name:
            name = f"基金{fund_code}"

    conn = get_connection()

    # 检查是否已有持仓
    existing = conn.execute(
        "SELECT id FROM portfolio WHERE fund_code = ? AND status IN ('holding','pending_buy')",
        (fund_code,)
    ).fetchone()
    if existing:
        conn.close()
        print(f"  [ERROR] {name} ({fund_code}) 已有持仓记录 (id={existing['id']})")
        print(f"  如需加仓请用: python main.py update {fund_code} --add-amount {amount:.0f}")
        return False

    buy_date = submit_date or datetime.now().strftime("%Y%m%d")
    confirm_date = _calc_confirm_date(buy_date)

    # 新建持仓: 份额=0, 状态=pending_buy (等待T+1确认后用 update 补填)
    conn.execute("""
        INSERT INTO portfolio (fund_code, etf_code, name, category, shares, cost_nav,
                              current_nav, total_cost, market_value, profit_loss, profit_pct,
                              status, buy_date, confirm_date, buy_amount)
        VALUES (?, ?, ?, ?, 0, 0, 0, ?, 0, 0, 0, 'pending_buy', ?, ?, ?)
    """, (fund_code, etf_code, name, category, amount, buy_date, confirm_date, amount))

    # 记录交易
    conn.execute("""
        INSERT INTO trades (fund_code, etf_code, name, trade_type, amount, shares, nav, fee,
                           status, submit_date, confirm_date, settle_date, signal_source, remark)
        VALUES (?, ?, ?, 'buy', ?, 0, 0, 0, 'pending', ?, ?, ?, 'manual', ?)
    """, (fund_code, etf_code, name, round(amount, 2), buy_date, confirm_date or buy_date, None,
          f"新建申购: ¥{amount:,.0f}, 待确认份额"))

    conn.commit()
    conn.close()

    print(f"  🆕 {name} ({fund_code}) 新建申购已记录")
    print(f"     申购金额: ¥{amount:,.0f}")
    print(f"     申购日期: {buy_date}")
    if confirm_date:
        print(f"     预计确认: {confirm_date} (T+1)")
    print(f"     状态: pending_buy (待确认份额)")
    print(f"     确认后执行: python main.py confirm {fund_code} --shares <实际份额> --nav <实际净值>")
    return True


# ============================================================
# 确认在途申购
# ============================================================

def confirm_buy(fund_code, shares, nav, confirm_date=None):
    """
    确认在途申购: 补填实际份额和净值
    python main.py confirm <fund_code> --shares N --nav N [--confirm-date YYYYMMDD]
    confirm_date: 实际确认日期 (YYYYMMDD, 默认今天), 支持补录历史确认
    """
    conn = get_connection()
    row = conn.execute("""
        SELECT * FROM portfolio
        WHERE fund_code = ? AND status = 'pending_buy'
    """, (fund_code,)).fetchone()

    if not row:
        conn.close()
        print(f"  [ERROR] 未找到 {fund_code} 的 pending_buy 记录")
        return False

    h = dict(row)
    amount = h["buy_amount"]
    _confirm = confirm_date or datetime.now().strftime("%Y%m%d")

    conn.execute("""
        UPDATE portfolio
        SET shares = ?, cost_nav = ?, current_nav = ?,
            total_cost = ?, market_value = ?,
            profit_loss = 0, profit_pct = 0,
            status = 'holding',
            confirm_date = ?,
            updated_at = datetime('now', 'localtime')
        WHERE id = ?
    """, (shares, nav, nav, amount, shares * nav, _confirm, h["id"]))

    # 更新交易记录 (SQLite UPDATE不支持ORDER BY, 先查id)
    old_remark = h.get("remark") or "新建申购"
    new_remark = f"{old_remark}, 确认份额{shares:.2f}份, 净值{nav}, 确认日{_confirm}"
    trade_row = conn.execute("""
        SELECT id FROM trades
        WHERE fund_code = ? AND status = 'pending' AND trade_type = 'buy'
        ORDER BY id DESC LIMIT 1
    """, (fund_code,)).fetchone()
    if trade_row:
        conn.execute("""
            UPDATE trades
            SET shares = ?, nav = ?, status = 'confirmed', confirm_date = ?, remark = ?
            WHERE id = ?
        """, (shares, nav, _confirm, new_remark, trade_row["id"]))

    conn.commit()
    conn.close()

    print(f"  ✅ {h['name']} ({fund_code}) 申购已确认")
    print(f"     份额: {shares:.2f}份  净值: {nav}  金额: ¥{amount:,.0f}")
    print(f"     确认日期: {_confirm}")
    print(f"     状态: pending_buy → holding")
    return True


# ============================================================
# 部分赎回
# ============================================================

def sell_holding_partial(fund_code, ratio=None, shares=None, submit_date=None, confirm_date=None):
    """
    赎回持仓 (支持部分赎回)
    参数二选一:
      - shares: 直接指定赎回份额数 (如 5000)
      - ratio:  赎回比例 (0.0 ~ 1.0), 如 0.5=赎回一半
    可选参数:
      - submit_date: 申请日期 (YYYYMMDD格式), 默认使用持仓买入日期或当天
      - confirm_date: 确认日期 (YYYYMMDD格式), 默认使用当天
    若都不指定, 则全部赎回
    """
    conn = get_connection()
    row = conn.execute("""
        SELECT * FROM portfolio
        WHERE fund_code = ? AND status IN ('holding', 'pending_buy')
    """, (fund_code,)).fetchone()

    if not row:
        conn.close()
        candidates = conn.execute("""
            SELECT fund_code, name FROM portfolio
            WHERE status IN ('holding', 'pending_buy')
              AND (fund_code LIKE ? OR name LIKE ?)
        """, (f"%{fund_code}%", f"%{fund_code}%")).fetchall() if not conn.closed else []
        if candidates:
            print(f"  未找到精确匹配, 相似结果:")
            for c in candidates:
                print(f"    - {c['name']} ({c['fund_code']})")
        else:
            print(f"  [ERROR] 未找到基金 {fund_code} 的持仓记录")
        conn = get_connection()
        conn.close()
        return False

    h = dict(row)
    # 确认日: 使用指定的，或自动计算下一个交易日(T+1)
    if confirm_date is None:
        confirm_date = _calc_confirm_date(submit_date or h["buy_date"] or datetime.now().strftime("%Y%m%d"))
        if confirm_date is None:
            confirm_date = datetime.now().strftime("%Y%m%d")
    # 提交日期: 使用指定的，或持仓买入日期
    if submit_date is None:
        submit_date = h["buy_date"] or confirm_date
    total_shares = h["shares"]

    # 计算赎回份额
    if shares is not None:
        # 直接指定份额
        if shares <= 0 or shares > total_shares:
            print(f"  [ERROR] 赎回份额必须在 (0, {total_shares:.2f}] 范围内, 当前: {shares}")
            conn.close()
            return False
        sell_shares = shares
        actual_ratio = shares / total_shares
    elif ratio is not None:
        # 按比例
        if ratio <= 0 or ratio > 1.0:
            print(f"  [ERROR] 赎回比例必须在 (0, 1.0] 范围内, 当前: {ratio}")
            conn.close()
            return False
        sell_shares = total_shares * ratio
        actual_ratio = ratio
    else:
        # 全部赎回
        sell_shares = total_shares
        actual_ratio = 1.0

    sell_amount = sell_shares * h["current_nav"]
    sell_cost = h["total_cost"] * actual_ratio
    sell_profit = sell_amount - sell_cost

    if actual_ratio >= 1.0:
        # 全部赎回
        conn.execute("""
            UPDATE portfolio SET status = 'sold', updated_at = datetime('now', 'localtime')
            WHERE id = ?
        """, (h["id"],))

        conn.execute("""
            INSERT INTO trades (fund_code, etf_code, name, trade_type, amount, shares, nav, fee,
                               status, submit_date, confirm_date, settle_date, signal_source, remark)
            VALUES (?, ?, ?, 'sell', ?, ?, ?, 0, 'done', ?, ?, ?, 'manual', ?)
        """, (h["fund_code"], h["etf_code"], h["name"],
              round(sell_amount, 2), h["shares"], h["current_nav"],
              submit_date, confirm_date, None,
              f"手动赎回: 成本¥{h['total_cost']:,.0f}, 市值¥{sell_amount:,.0f}, 盈亏{h['profit_pct']:+.2f}%"))

        emoji = "🟢" if h["profit_pct"] >= 0 else "🔴"
        print(f"  {emoji} {h['name']} ({fund_code}) 已全部赎回")
        print(f"     份额: {h['shares']}份  成本净值: {h['cost_nav']}  当前净值: {h['current_nav']}")
        print(f"     成本: ¥{h['total_cost']:,.0f}  市值: ¥{sell_amount:,.0f}  盈亏: {h['profit_pct']:+.2f}%")

    else:
        # 部分赎回: 减少份额和成本
        new_shares = total_shares - sell_shares
        new_total_cost = h["total_cost"] - sell_cost
        new_market_value = new_shares * h["current_nav"]
        new_profit_loss = new_market_value - new_total_cost
        new_profit_pct = (new_profit_loss / new_total_cost * 100) if new_total_cost > 0 else 0

        conn.execute("""
            UPDATE portfolio
            SET shares = ?, total_cost = ?, buy_amount = ?,
                market_value = ?, profit_loss = ?, profit_pct = ?,
                updated_at = datetime('now', 'localtime')
            WHERE id = ?
        """, (new_shares, round(new_total_cost, 2), round(new_total_cost, 2),
              round(new_market_value, 2), round(new_profit_loss, 2), round(new_profit_pct, 2),
              h["id"]))

        conn.execute("""
            INSERT INTO trades (fund_code, etf_code, name, trade_type, amount, shares, nav, fee,
                               status, submit_date, confirm_date, settle_date, signal_source, remark)
            VALUES (?, ?, ?, 'sell', ?, ?, ?, 0, 'done', ?, ?, ?, 'manual', ?)
        """, (h["fund_code"], h["etf_code"], h["name"],
              round(sell_amount, 2), sell_shares, h["current_nav"],
              submit_date, confirm_date, None,
              f"部分赎回({actual_ratio:.1%}): {sell_shares:.2f}份, ¥{sell_amount:,.0f}, 盈亏¥{sell_profit:+,.0f}"))

        emoji = "🟢" if sell_profit >= 0 else "🔴"
        print(f"  {emoji} {h['name']} ({fund_code}) 部分赎回完成")
        print(f"     赎回份额: {sell_shares:.2f}份  赎回金额: ¥{sell_amount:,.0f}  盈亏: ¥{sell_profit:+,.0f}")
        print(f"     剩余份额: {new_shares:.2f}份  剩余市值: ¥{new_market_value:,.0f}")

    conn.commit()
    conn.close()
    return True


# ============================================================
# 交易历史查询
# ============================================================

def show_trades(fund_code=None, trade_type=None, limit=20):
    """
    查看交易历史记录
    fund_code: 按基金代码过滤
    trade_type: 按交易类型过滤 (buy/sell)
    limit: 返回最近N条
    """
    conn = get_connection()
    conditions = []
    params = []

    if fund_code:
        conditions.append("fund_code = ?")
        params.append(fund_code)
    if trade_type:
        conditions.append("trade_type = ?")
        params.append(trade_type)

    where = " AND ".join(conditions) if conditions else "1=1"

    rows = conn.execute(f"""
        SELECT id, fund_code, name, trade_type, amount, shares, nav, fee,
               status, submit_date, confirm_date, signal_source, remark
        FROM trades
        WHERE {where}
        ORDER BY id DESC
        LIMIT ?
    """, params + [limit]).fetchall()

    conn.close()

    if not rows:
        print("\n  📭 无交易记录")
        return

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = []
    lines.append("")
    lines.append("=" * 90)
    lines.append(f"  交易记录  ({now})  共{len(rows)}条")
    lines.append("=" * 90)
    lines.append(f"  {'ID':>4s}  {'日期':>10s}  {'类型':<4s}  {'基金名称':<14s} {'基金代码':<8s} "
                f"{'金额':>12s} {'份额':>10s} {'净值':>8s} {'来源':<8s} {'状态':<8s}")
    lines.append("  " + "-" * 86)

    for r in rows:
        d = dict(r)
        type_mark = "买入" if d["trade_type"] == "buy" else "赎回"
        status_mark = {"done": "✅", "pending": "⏳"}.get(d["status"], d["status"])
        shares_str = f"{d['shares']:.2f}" if d["shares"] and d["shares"] > 0 else "-"
        lines.append(f"  {d['id']:>4d}  {d['submit_date']:>10s}  {type_mark:<4s}  {d['name']:<14s} "
                    f"{d['fund_code']:<8s} ¥{d['amount']:>10,.0f} {shares_str:>10s} "
                    f"{d['nav']:>8.4f} {d['signal_source'] or '-':<8s} {status_mark:<8s}")

    lines.append("")
    lines.append("=" * 90)

    output = "\n".join(lines)
    print(output)
    return output


# ============================================================
# 添加自定义ETF到候选池
# ============================================================

def _fetch_fund_company(fund_code):
    """通过 AkShare 查询基金公司简称 (如 '嘉实', '易方达')"""
    import re
    try:
        import akshare as ak
        df = ak.fund_individual_basic_info_xq(symbol=fund_code)
        row = df[df['item'] == '基金公司']
        if not row.empty:
            company = str(row.iloc[0]['value'])
            m = re.match(r'(.{2,4})基金管理有限公司', company)
            if m:
                return m.group(1)
            return company
    except Exception as e:
        print(f"  [WARN] 查询基金公司失败 ({fund_code}): {e}")
    return ''


def add_etf_to_pool(etf_code, fund_code, name, category="satellite"):
    """
    添加自定义ETF到候选池 (data/etf_pool.json)
    自动通过 AkShare 查询基金公司简称并保存
    """
    import os
    from config import DATA_DIR

    # 字段校验
    if not fund_code or not name:
        print("  [ERROR] fund_code 和 name 为必填项")
        return False

    # 检查是否已在候选池中
    info = _get_etf_info_by_fund_code(fund_code)
    if info:
        print(f"  [SKIP] {name} ({fund_code}) 已在候选池中: {info}")
        return False

    # 读取现有自定义池
    pool_path = os.path.join(DATA_DIR, "etf_pool.json")
    pool = []
    if os.path.exists(pool_path):
        try:
            with open(pool_path, "r", encoding="utf-8") as f:
                pool = json.load(f)
        except Exception:
            pool = []

    # 检查重复
    for item in pool:
        if item.get("fund_code") == fund_code:
            print(f"  [SKIP] {fund_code} 已在自定义候选池中")
            return False

    # 自动查询基金公司
    company = _fetch_fund_company(fund_code)

    # 添加
    entry = {
        "code": etf_code or "",
        "name": name,
        "fund_code": fund_code,
        "category": category,
        "company": company
    }
    pool.append(entry)

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(pool_path, "w", encoding="utf-8") as f:
        json.dump(pool, f, ensure_ascii=False, indent=2)

    print(f"  ✅ {name} ({fund_code}) 已添加到自定义候选池")
    print(f"     ETF代码: {etf_code or '未指定'}  类别: {category}  基金公司: {company or '未知'}")
    print(f"     文件: {pool_path}")
    print(f"     注意: 重启系统后生效 (下次 python main.py 运行时自动加载)")
    return True


# ============================================================
# 列出候选池ETF
# ============================================================

def show_etf_pool():
    """
    列出所有候选池ETF (含自定义)
    """
    from config import CORE_ETFS, SATELLITE_ETFS, CUSTOM_ETFS

    lines = []
    lines.append("")
    lines.append("=" * 80)
    lines.append("  候选池ETF一览")
    lines.append("=" * 80)

    lines.append(f"\n  📊 宽基底仓 ({len(CORE_ETFS)}只)")
    lines.append(f"  {'ETF代码':<14s} {'联接基金':<10s} {'名称':<14s} {'目标权重':>8s}")
    lines.append("  " + "-" * 50)
    for etf in CORE_ETFS:
        lines.append(f"  {etf['code']:<14s} {etf['fund_code']:<10s} {etf['name']:<14s} {etf['target_weight']:>7.0%}")

    lines.append(f"\n  🔄 行业卫星 ({len(SATELLITE_ETFS)}只)")
    lines.append(f"  {'ETF代码':<14s} {'联接基金':<10s} {'名称':<14s} {'方向':<12s} {'来源':<6s}")
    lines.append("  " + "-" * 60)
    for etf in SATELLITE_ETFS:
        source = "自定义" if etf in CUSTOM_ETFS else "内置"
        lines.append(f"  {etf['code']:<14s} {etf['fund_code']:<10s} {etf['name']:<14s} {etf.get('category',''):<12s} {source}")

    lines.append("")
    lines.append("=" * 80)

    output = "\n".join(lines)
    print(output)
    return output

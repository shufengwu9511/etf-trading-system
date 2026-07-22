# ============================================================
# ETF联接基金交易系统 - 数据采集模块
# ETF场内K线 → pytdx 通达信 (免费无限流) — 替代 Tushare index_daily/fund_daily
# 宽基PE估值 → Tushare index_dailybasic (保留)
# 联接基金净值 → AkShare fund_open_fund_info_em (保留)
# 恐慌指标 → Tushare moneyflow_hsgt + AkShare 跌停统计 (保留)
# ============================================================
import tushare as ts
import pandas as pd
import time as _time
from datetime import datetime, timedelta
from db import get_connection
from config import TUSHARE_TOKEN, CORE_ETFS, SATELLITE_ETFS
import tdx_data  # 通达信免费数据源 (ETF场内K线)

# 初始化 Tushare（PE估值 + 北向资金 + 交易日历）
pro = ts.pro_api(TUSHARE_TOKEN)

# 网络重试配置
_MAX_RETRIES = 3
_RETRY_DELAY = 2  # 秒

# 沪深300指数代码 (恐慌监控用)
HS300_INDEX = "000300.SH"

# 数据源标记
USE_TDX_FOR_ETF_KLINE = True  # ETF场内K线使用通达信 (True) 或 Tushare (False)


def _get_date_range(days):
    """计算日期范围"""
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=int(days * 1.5))).strftime("%Y%m%d")
    return start, end


def _get_latest_trade_date():
    """获取最近交易日"""
    try:
        df = pro.trade_cal(exchange="SSE", is_open=1,
                           start_date=(datetime.now() - timedelta(days=10)).strftime("%Y%m%d"),
                           end_date=datetime.now().strftime("%Y%m%d"))
        if df is not None and len(df) > 0:
            return df["cal_date"].max()
    except Exception:
        pass
    return datetime.now().strftime("%Y%m%d")


# ============================================================
# 数据拉取函数
# ============================================================

def _retry_api_call(api_func, label, **kwargs):
    """带重试的API调用，处理网络波动"""
    last_err = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            return api_func(**kwargs)
        except Exception as e:
            last_err = e
            err_msg = str(e)
            # 仅对网络/Token临时错误重试
            if any(kw in err_msg for kw in ("token", "connection", "timeout", "HTTPError", "1006", "502", "503")):
                if attempt < _MAX_RETRIES:
                    print(f"    [RETRY {attempt}/{_MAX_RETRIES}] {label}: {err_msg[:80]}, 等待{_RETRY_DELAY}s...")
                    _time.sleep(_RETRY_DELAY)
                    continue
            else:
                # 非临时错误，不重试
                break
    print(f"    [ERR] {label}: {last_err}")
    return pd.DataFrame()


def fetch_index_daily(index_code, days=120):
    """拉取指数日线行情 (用于宽基ETF)"""
    start, end = _get_date_range(days)
    return _retry_api_call(
        pro.index_daily, f"index_daily {index_code}",
        ts_code=index_code, start_date=start, end_date=end,
        fields="ts_code,trade_date,open,close,high,low,vol,amount,pct_chg"
    )


def fetch_fund_daily(fund_code, days=120):
    """拉取ETF/基金日线行情 (用于行业ETF)"""
    start, end = _get_date_range(days)
    return _retry_api_call(
        pro.fund_daily, f"fund_daily {fund_code}",
        ts_code=fund_code, start_date=start, end_date=end,
        fields="ts_code,trade_date,open,close,high,low,vol,amount,pct_chg"
    )


def fetch_index_valuation(index_code, days=365 * 5):
    """拉取指数估值 (PE/PB) (用于宽基PE择时)"""
    start, end = _get_date_range(days)
    return _retry_api_call(
        pro.index_dailybasic, f"index_dailybasic {index_code}",
        ts_code=index_code, start_date=start, end_date=end,
        fields="ts_code,trade_date,pe,pe_ttm,pb,turnover_rate"
    )


def fetch_north_money(days=60):
    """拉取北向资金流向"""
    return _retry_api_call(
        pro.moneyflow_hsgt, "north_money",
        start_date=(datetime.now() - timedelta(days=int(days * 1.5))).strftime("%Y%m%d"),
        end_date=datetime.now().strftime("%Y%m%d")
    )


def fetch_limit_down_count(trade_date):
    """拉取指定日期跌停家数 (AkShare, 东方财富数据源, 免费)"""
    try:
        import akshare as ak
        df = ak.stock_zt_pool_dtgc_em(date=trade_date)
        return len(df) if df is not None and len(df) > 0 else 0
    except Exception:
        return 0


def fetch_limit_up_count(trade_date):
    """拉取指定日期涨停家数 (AkShare, 东方财富数据源, 免费)"""
    try:
        import akshare as ak
        df = ak.stock_zt_pool_em(date=trade_date)
        return len(df) if df is not None and len(df) > 0 else 0
    except Exception:
        return 0


# ============================================================
# 数据写入数据库
# ============================================================

def update_core_etf_data(etf_config, days=120):
    """
    更新单只宽基ETF的数据: 行情(通达信) + 估值(Tushare)
    etf_config: dict with keys "code", "index_code", "name"
    """
    conn = get_connection()
    total = 0

    # 1. ETF日线行情 (通达信 pytdx 免费数据源)
    etf_code = etf_config["code"]
    if USE_TDX_FOR_ETF_KLINE:
        df = tdx_data.get_etf_kline(etf_code, days)
    else:
        # 回退: Tushare index_daily (用底层指数代码)
        df = fetch_index_daily(etf_config["index_code"], days)

    if not df.empty:
        for _, row in df.iterrows():
            try:
                conn.execute("""
                    INSERT OR REPLACE INTO index_daily
                    (ts_code, trade_date, open, close, high, low, vol, amount, pct_chg)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (etf_code, row["trade_date"],
                      row["open"], row["close"], row["high"], row["low"],
                      row["vol"], row["amount"], row["pct_chg"]))
                total += 1
            except Exception:
                continue
        conn.commit()

    # 2. 指数估值 (PE/PB) — Tushare 保留
    index_code = etf_config["index_code"]
    df_val = fetch_index_valuation(index_code, days=365 * 5)
    val_count = 0
    if not df_val.empty:
        for _, row in df_val.iterrows():
            try:
                pe = row.get("pe_ttm") if pd.notna(row.get("pe_ttm")) else row.get("pe")
                pb = row.get("pb")
                if pe is None or pd.isna(pe):
                    continue
                conn.execute("""
                    INSERT OR REPLACE INTO index_valuation
                    (ts_code, trade_date, pe, pe_ttm, pb)
                    VALUES (?, ?, ?, ?, ?)
                """, (etf_code, row["trade_date"], row.get("pe"), pe, pb))
                val_count += 1
            except Exception:
                continue
        conn.commit()

    conn.close()
    source = "通达信" if USE_TDX_FOR_ETF_KLINE else "Tushare"
    print(f"    [OK] {etf_config['name']} ({etf_code}) [{source}] 行情{total}条 + 估值{val_count}条")
    return total


def update_satellite_etf_data(etf_config, days=180):
    """
    更新单只行业ETF的日线行情 (通达信 pytdx)
    etf_config: dict with keys "code", "name"
    """
    conn = get_connection()
    etf_code = etf_config["code"]

    if USE_TDX_FOR_ETF_KLINE:
        df = tdx_data.get_etf_kline(etf_code, days)
    else:
        df = fetch_fund_daily(etf_code, days)

    count = 0

    if not df.empty:
        for _, row in df.iterrows():
            try:
                conn.execute("""
                    INSERT OR REPLACE INTO index_daily
                    (ts_code, trade_date, open, close, high, low, vol, amount, pct_chg)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (etf_code, row["trade_date"],
                      row["open"], row["close"], row["high"], row["low"],
                      row["vol"], row["amount"], row["pct_chg"]))
                count += 1
            except Exception:
                continue
        conn.commit()

    conn.close()
    source = "通达信" if USE_TDX_FOR_ETF_KLINE else "Tushare"
    print(f"    [OK] {etf_config['name']} ({etf_code}) [{source}] 行情{count}条")
    return count


def update_north_money(days=60):
    """更新北向资金数据"""
    conn = get_connection()
    df = fetch_north_money(days)
    count = 0

    if not df.empty:
        for _, row in df.iterrows():
            try:
                nm = row.get("north_money", 0)
                if pd.isna(nm):
                    nm = 0
                conn.execute("""
                    INSERT OR REPLACE INTO north_money_flow
                    (trade_date, north_money, ggt_ss, ggt_sz, hgt, sgt)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (row["trade_date"], float(nm),
                      _safe_float(row.get("ggt_ss")),
                      _safe_float(row.get("ggt_sz")),
                      _safe_float(row.get("hgt")),
                      _safe_float(row.get("sgt"))))
                count += 1
            except Exception:
                continue
        conn.commit()

    conn.close()
    print(f"    [OK] 北向资金 {count} 条")
    return count


def update_panic_data():
    """更新恐慌监控所需数据 (沪深300行情 + 跌停统计)"""
    conn = get_connection()
    trade_date = _get_latest_trade_date()

    # 确保沪深300行情数据存在
    df = fetch_index_daily(HS300_INDEX, days=10)
    if not df.empty:
        for _, row in df.iterrows():
            try:
                conn.execute("""
                    INSERT OR REPLACE INTO index_daily
                    (ts_code, trade_date, open, close, high, low, vol, amount, pct_chg)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (HS300_INDEX, row["trade_date"],
                      row["open"], row["close"], row["high"], row["low"],
                      row["vol"], row["amount"], row["pct_chg"]))
            except Exception:
                continue
        conn.commit()

    # 跌停/涨停统计 (AkShare 东方财富数据源, 无频率限制)
    limit_down = fetch_limit_down_count(trade_date)
    limit_up = fetch_limit_up_count(trade_date)
    conn.execute("""
        INSERT OR REPLACE INTO limit_down_stats (trade_date, limit_down_count, limit_up_count)
        VALUES (?, ?, ?)
    """, (trade_date, limit_down, limit_up))
    conn.commit()
    conn.close()
    print(f"    [OK] 恐慌数据: {trade_date} 跌停{limit_down}家")


def _safe_float(val):
    """安全转换float"""
    if val is None or pd.isna(val):
        return 0.0
    return float(val)


# ============================================================
# 主更新函数
# ============================================================

def run_full_data_update():
    """执行完整数据更新 (每日运行)"""
    print("=" * 60)
    print(f"[数据更新] 开始: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  ETF场内K线: {'通达信 pytdx (免费)' if USE_TDX_FOR_ETF_KLINE else 'Tushare'}")
    print(f"  PE估值/北向: Tushare Pro")
    print(f"  联接基金净值/跌停: AkShare")
    print("=" * 60)

    # 1. 宽基ETF: 指数行情 + 估值
    print("\n[1/4] 更新宽基ETF数据 (行情+估值)...")
    for etf in CORE_ETFS:
        update_core_etf_data(etf, days=120)

    # 2. 行业ETF: 仅行情
    print("\n[2/4] 更新行业ETF数据 (行情)...")
    for etf in SATELLITE_ETFS:
        update_satellite_etf_data(etf, days=120)

    # 3. 北向资金
    print("\n[3/5] 更新北向资金...")
    update_north_money(days=60)

    # 4. 恐慌监控数据
    print("\n[4/5] 更新恐慌监控数据...")
    update_panic_data()

    # 5. 全市场ETF缓存 (动量发现用, 非阻塞)
    print("\n[5/5] 更新全市场ETF缓存 (动量发现)...")
    try:
        from etf_cache_updater import update_etf_cache
        updated = update_etf_cache()
        if not updated:
            print("  (今日已更新, 跳过)")
    except Exception as e:
        print(f"  [WARN] 全市场ETF缓存更新失败 (不影响主流程): {e}")

    print("\n" + "=" * 60)
    print(f"[数据更新] 完成: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)


if __name__ == "__main__":
    run_full_data_update()

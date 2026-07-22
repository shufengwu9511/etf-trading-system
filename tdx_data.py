"""通达信 pytdx 数据源 — ETF场内日K线行情

替代 Tushare 的 index_daily / fund_daily OHLCV 数据接口:
  - 免费、无频率限制
  - 本地 SQLite 缓存, 增量更新
  - pytdx get_xdxr_info() 获取官方除权除息记录, 精确前复权 (拆分+分红)
  - 保留 Tushare: PE估值 / 北向资金 / 交易日历
  - 保留 AkShare: 跌停统计 / 联接基金净值

用法:
  from tdx_data import get_etf_kline
  df = get_etf_kline("510310.SH", days=120)  # 返回 DataFrame (已前复权)
"""

import os
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Optional, Tuple

import pandas as pd
from pytdx.hq import TdxHq_API
from pytdx.config.hosts import hq_hosts

# ── 缓存路径 ──
TDX_CACHE_DIR = os.path.join("data", "tdx_cache")
TDX_CACHE_DB = os.path.join(TDX_CACHE_DIR, "kline.db")

# ── 优选服务器 (国内行情主站, 实测延迟低) ──
_PREFERRED_SERVERS = [
    ("上海电信主站", "180.153.18.170", 7709),
    ("上海电信主站2", "180.153.18.171", 7709),
    ("深圳电信主站", "119.147.212.81", 7709),
    ("上海移动主站", "180.153.39.51", 7709),
    ("北京联通主站", "123.125.108.90", 7709),
]

# ── 前复权: 通过 pytdx get_xdxr_info() 获取官方除权除息记录, 精确处理拆分+分红 ──

# ── 内部缓存 ──
_best_server: Optional[Tuple[str, int]] = None  # (ip, port)


def _ensure_cache():
    """初始化本地K线缓存库"""
    os.makedirs(TDX_CACHE_DIR, exist_ok=True)
    conn = sqlite3.connect(TDX_CACHE_DB)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kline (
                code    TEXT NOT NULL,
                date    TEXT NOT NULL,
                open    REAL,
                high    REAL,
                low     REAL,
                close   REAL,
                vol     REAL,
                amount  REAL DEFAULT 0,
                PRIMARY KEY (code, date)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_kline_code ON kline(code)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_kline_date ON kline(date)")
        # 除权除息缓存 (pytdx get_xdxr_info, 精确拆分+分红)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS xdxr (
                code       TEXT NOT NULL,
                event_date TEXT NOT NULL,
                category   INTEGER,
                name       TEXT,
                suogu      REAL,
                fenhong    REAL,
                updated_at TEXT,
                PRIMARY KEY (code, event_date)
            )
        """)
    finally:
        conn.close()


def _parse_ts_code(ts_code: str) -> Tuple[int, str]:
    """解析 Tushare 代码格式 → 通达信 (market, raw_code)

    '510310.SH' → (1, '510310')   # 上海
    '159558.SZ' → (0, '159558')   # 深圳
    """
    if ts_code.endswith('.SH'):
        return 1, ts_code[:-3]
    elif ts_code.endswith('.SZ'):
        return 0, ts_code[:-3]
    else:
        # 无后缀, 根据前缀判断
        if ts_code.startswith(('51', '56', '58', '60', '68')):
            return 1, ts_code
        return 0, ts_code


def _scan_server() -> Optional[Tuple[str, int]]:
    """扫描最优通达信服务器 (优先优选列表, 失败则扫全量)"""
    # 先试优选列表
    for name, ip, port in _PREFERRED_SERVERS:
        try:
            api = TdxHq_API()
            if api.connect(ip, port, time_out=2):
                api.disconnect()
                return (ip, port)
        except Exception:
            pass

    # 扫描全量
    for name, ip, port in hq_hosts:
        try:
            api = TdxHq_API()
            if api.connect(ip, port, time_out=2):
                api.disconnect()
                return (ip, port)
        except Exception:
            continue
    return None


def _get_server() -> Tuple[str, int]:
    """获取通达信服务器地址 (带缓存, 连接失败时自动重扫)"""
    global _best_server
    if _best_server is not None:
        # 验证缓存的服务器仍可用
        try:
            api = TdxHq_API()
            if api.connect(_best_server[0], _best_server[1], time_out=2):
                api.disconnect()
                return _best_server
        except Exception:
            pass
        _best_server = None

    server = _scan_server()
    if server is None:
        raise ConnectionError("无可用通达信行情服务器")
    _best_server = server
    return server


def _load_cache(code: str) -> pd.DataFrame:
    """从本地SQLite加载已缓存的K线"""
    _ensure_cache()
    conn = sqlite3.connect(TDX_CACHE_DB)
    try:
        df = pd.read_sql_query(
            "SELECT date, open, high, low, close, vol, amount "
            "FROM kline WHERE code=? ORDER BY date",
            conn, params=(code,)
        )
        return df
    finally:
        conn.close()


def _save_cache(code: str, df: pd.DataFrame):
    """将K线写入本地SQLite缓存 (增量)"""
    if df.empty:
        return
    _ensure_cache()
    conn = sqlite3.connect(TDX_CACHE_DB)
    try:
        for _, row in df.iterrows():
            conn.execute("""
                INSERT OR REPLACE INTO kline (code, date, open, high, low, close, vol, amount)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (code, row["date"], row["open"], row["high"], row["low"],
                  row["close"], row["vol"], row.get("amount", 0)))
        conn.commit()
    finally:
        conn.close()


def _fetch_xdxr(api, market: int, raw_code: str, code: str) -> list[dict]:
    """获取ETF官方除权除息信息 (带缓存)

    通过 pytdx get_xdxr_info() 获取精确记录, 覆盖:
      - category 5/11 (股本变化/扩缩股): 份额拆分/合并 → 自动前复权
      - category 1   (除权除息):       现金分红       → 自动前复权

    Args:
        api: 已连接的 TdxHq_API 实例
        market: 0=深圳, 1=上海
        raw_code: 通达信代码如 '159558'
        code: Tushare格式代码如 '159558.SZ' (用于缓存键)

    Returns:
        [{'date': '20260709', 'type': 'split', 'ratio': 3.0}, ...]
        按日期升序排列
    """
    _ensure_cache()
    conn = sqlite3.connect(TDX_CACHE_DB)
    try:
        cached = conn.execute(
            "SELECT event_date, category, suogu, fenhong FROM xdxr "
            "WHERE code=? ORDER BY event_date",
            (code,)
        ).fetchall()

        if cached:
            result = []
            for date_str, cat, suogu, fenhong in cached:
                # suogu: 扩缩股比例 (N = 1份拆为N份, <1 为缩股)
                #   category 5=股本变化, 11=扩缩股
                if suogu is not None and suogu != 0 and cat in (5, 11):
                    result.append({
                        'date': date_str, 'type': 'split', 'ratio': float(suogu)
                    })
                # fenhong: 每份现金分红金额 (元)
                elif fenhong is not None and fenhong != 0:
                    result.append({
                        'date': date_str, 'type': 'dividend', 'fenhong': float(fenhong)
                    })
            return result

        # 缓存未命中, 从API获取
        raw = api.get_xdxr_info(market, raw_code)
        if not raw:
            return []

        today_str = datetime.now().strftime("%Y%m%d")
        result = []
        for r in raw:
            date_str = f"{r['year']}{r['month']:02d}{r['day']:02d}"
            cat = r['category']
            suogu = r.get('suogu')
            fenhong = r.get('fenhong')

            conn.execute(
                "INSERT OR REPLACE INTO xdxr "
                "(code, event_date, category, name, suogu, fenhong, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (code, date_str, cat, r.get('name', ''),
                 suogu, fenhong, today_str)
            )

            if suogu is not None and suogu != 0 and cat in (5, 11):
                result.append({
                    'date': date_str, 'type': 'split', 'ratio': float(suogu)
                })
            elif fenhong is not None and fenhong != 0:
                result.append({
                    'date': date_str, 'type': 'dividend', 'fenhong': float(fenhong)
                })

        conn.commit()
        result.sort(key=lambda x: x['date'])
        return result
    finally:
        conn.close()


def _apply_xdxr_adjustment(df: pd.DataFrame, xdxr_records: list[dict]) -> pd.DataFrame:
    """使用官方除权除息记录进行前复权 (forward adjustment)

    前复权算法 (从最新到最早倒序处理):
      对每个除权事件日期 D (含拆分或分红):
        - 日期 ≥ D 的价格保持不变 (已反映事件影响)
        - 日期 < D 的 OHLC 除以累积因子, 成交量乘以累积因子

    累积因子更新规则:
      - 拆分 suogu=N:  cum_price_div *= N,  cum_vol_mult *= N
      - 分红 fenhong=F: cum_price_div *= (close_on_D + F) / close_on_D
        (成交量不受分红影响)

    Args:
        df: 按 date 升序排列的 DataFrame, 列: date, open, high, low, close, vol, amount
        xdxr_records: _fetch_xdxr() 返回的记录列表

    Returns:
        前复权后的 DataFrame (副本)
    """
    if not xdxr_records:
        return df.copy()

    result = df.copy()
    dates = result["date"].values

    # 对齐除权日期到实际交易日 (除权日可能非交易日)
    dates_sorted = sorted(dates)
    events_by_date: dict[str, list[dict]] = {}
    for r in xdxr_records:
        xd = r['date']
        if xd in set(dates):
            effective = xd
        else:
            # 找到 >= xd 的第一个交易日
            candidates = [d for d in dates_sorted if d >= xd]
            effective = candidates[0] if candidates else xd
        events_by_date.setdefault(effective, []).append(r)

    # 倒序遍历: 最新 → 最早
    cum_price_div = 1.0   # OHLC 除数
    cum_vol_mult = 1.0    # 成交量乘数

    for i in range(len(result) - 1, -1, -1):
        date = dates[i]
        close_val = result.iloc[i, result.columns.get_loc("close")]

        # 应用当前累积调整
        for col in ["open", "high", "low", "close"]:
            idx = result.columns.get_loc(col)
            result.iloc[i, idx] = result.iloc[i, idx] / cum_price_div
        result.iloc[i, result.columns.get_loc("vol")] = (
            result.iloc[i, result.columns.get_loc("vol")] * cum_vol_mult
        )
        # amount (成交额) 不受拆分/分红影响, 保持不变

        # 从此日期起, 更新累积因子 (影响更早的日期)
        if date in events_by_date:
            for evt in events_by_date[date]:
                if evt['type'] == 'split':
                    cum_price_div *= evt['ratio']
                    cum_vol_mult *= evt['ratio']
                elif evt['type'] == 'dividend':
                    F = evt['fenhong']
                    if close_val > 0:
                        cum_price_div *= (close_val + F) / close_val
                    # cum_vol_mult 不变 (分红不影响成交量)

    return result


def fetch_kline_raw(code: str) -> pd.DataFrame:
    """从通达信拉取单只ETF的完整日K线 (最多800条)

    Args:
        code: Tushare格式如 '510310.SH' 或 '159558.SZ'

    Returns:
        DataFrame with columns: date, open, high, low, close, vol, amount
        失败返回空 DataFrame
    """
    market, raw_code = _parse_ts_code(code)

    for attempt in range(3):
        api = TdxHq_API()
        try:
            ip, port = _get_server()
            if not api.connect(ip, port, time_out=3):
                if attempt < 2:
                    _best_server_clear()
                    time.sleep(1)
                    continue
                return pd.DataFrame()

            # 拉取日K线: category=4, 起始位置0, 最多800条
            raw = api.get_security_bars(4, market, raw_code, 0, 800)

            if not raw:
                api.disconnect()
                return pd.DataFrame()

            rows = []
            for r in raw:
                # pytdx 返回 "2026-07-21 15:00:00", 统一转为 YYYYMMDD 格式
                # (与 Tushare index_daily 格式一致, 避免字符串排序混用)
                date_str = str(r["datetime"])[:10].replace("-", "")
                rows.append({
                    "date": date_str,
                    "open": float(r["open"]),
                    "high": float(r["high"]),
                    "low": float(r["low"]),
                    "close": float(r["close"]),
                    "vol": float(r["vol"]),
                    "amount": float(r.get("amount", 0)),
                })

            df = pd.DataFrame(rows)
            if not df.empty:
                df = df.sort_values("date").reset_index(drop=True)
                # 通过 pytdx get_xdxr_info() 获取官方除权除息, 精确前复权
                # 注意: api 连接仍活跃, _fetch_xdxr 复用同一连接
                xdxr = _fetch_xdxr(api, market, raw_code, code)
                if xdxr:
                    df = _apply_xdxr_adjustment(df, xdxr)
            api.disconnect()
            return df

        except Exception:
            try:
                api.disconnect()
            except Exception:
                pass
            if attempt < 2:
                time.sleep(2)
                continue
            return pd.DataFrame()

    return pd.DataFrame()


def _best_server_clear():
    """清除服务器缓存 (连接失败时调用)"""
    global _best_server
    _best_server = None


def get_etf_kline(code: str, days: int = 120) -> pd.DataFrame:
    """获取ETF日K线数据 (带本地缓存 + 增量更新)

    对标 Tushare 的 index_daily / fund_daily 接口.

    Args:
        code: ETF代码 (Tushare格式), 如 '510310.SH'
        days: 需要最近多少天的数据

    Returns:
        DataFrame: trade_date, open, close, high, low, vol, amount, pct_chg
        列名与原有 Tushare 接口保持一致
    """
    # 1. 从缓存加载
    cached = _load_cache(code)

    # 2. 判断是否需要更新
    need_fetch = False
    if cached.empty:
        need_fetch = True
    else:
        latest_cached = cached["date"].max()
        today = datetime.now().strftime("%Y%m%d")
        if latest_cached < today:
            # 缓存可能不是最新的, 尝试拉取新数据
            need_fetch = True

    # 3. 从通达信拉取最新数据
    if need_fetch:
        fresh = fetch_kline_raw(code)
        if not fresh.empty:
            _save_cache(code, fresh)
            cached = _load_cache(code)

    # 4. 截取最近 days 天
    if cached.empty:
        return pd.DataFrame()

    # 计算 pct_chg (日涨跌幅)
    cached = cached.sort_values("date")
    cached["pct_chg"] = cached["close"].pct_change() * 100

    # 截取最近 days 天
    end_date = cached["date"].max()
    start_date = (datetime.strptime(end_date, "%Y%m%d")
                  - timedelta(days=int(days * 1.5))).strftime("%Y%m%d")
    result = cached[(cached["date"] >= start_date)].copy()

    # 重命名为 Tushare 兼容列名
    result = result.rename(columns={
        "date": "trade_date",
        "vol": "vol",
    })

    # Tushare 兼容: 需要 ts_code 列
    result["ts_code"] = code

    # Tushare 兼容: amount 单位对齐 (pytdx 返回的是元, Tushare 是元, 无需转换)
    if "amount" not in result.columns:
        result["amount"] = 0

    # 列顺序与 Tushare 保持一致
    cols = ["ts_code", "trade_date", "open", "close", "high", "low",
            "vol", "amount", "pct_chg"]
    result = result[cols]

    # 降序排列 (Tushare 默认降序)
    result = result.sort_values("trade_date", ascending=False).reset_index(drop=True)

    return result


def batch_get_etf_klines(codes: list, days: int = 120) -> dict:
    """批量拉取多只ETF的K线数据

    Args:
        codes: ETF代码列表, 如 ['510310.SH', '159558.SZ']
        days: 需要最近多少天

    Returns:
        {code: DataFrame} 字典
    """
    results = {}
    for i, code in enumerate(codes):
        try:
            df = get_etf_kline(code, days)
            results[code] = df
        except Exception as e:
            print(f"    [WARN] 通达信拉取 {code} 失败: {e}")
            results[code] = pd.DataFrame()
    return results

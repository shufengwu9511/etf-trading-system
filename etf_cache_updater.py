"""ETF 全市场缓存更新器

扫描全市场ETF, 增量更新 K线到 data/tdx_cache/etf_cache.db
供 etf_discovery.py 动量发现使用

更新策略: 当日已更新则跳过 (每日最多跑一次全市场扫描)

用法:
  from etf_cache_updater import update_etf_cache
  update_etf_cache()    # 自动判断是否需要更新
  update_etf_cache(force=True)   # 强制更新
"""

import os
import sqlite3
import time
from datetime import date, datetime, timedelta

from pytdx.hq import TdxHq_API

from tdx_data import _get_server, _fetch_xdxr, _apply_xdxr_adjustment
from config import CORE_ETFS, SATELLITE_ETFS

# ── 路径 ──
CACHE_DIR = os.path.join("data", "tdx_cache")
CACHE_DB = os.path.join(CACHE_DIR, "etf_cache.db")
ETL_REFRESH_DAYS = 7

# ── 深交所ETF代码 (已知集合) ──
SZ_ETF_CODES = [
    "159845", "159915", "159919", "159922", "159928", "159929",
    "159941", "159949", "159952", "159954", "159959", "159965",
    "159966", "159967", "159968", "159969", "159985", "159986",
    "159987", "159988", "159989", "159990", "159991", "159992",
    "159993", "159994", "159995", "159996", "159997", "159998",
    "159999",
]


def _ensure_cache_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)


def _init_db(db_path: str):
    """初始化缓存库表结构"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS etf_list (
            code TEXT PRIMARY KEY, name TEXT NOT NULL,
            market INTEGER NOT NULL,
            added_at TEXT NOT NULL DEFAULT (datetime('now'))
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS kline_data (
            code TEXT NOT NULL, date TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL,
            volume REAL, amount REAL DEFAULT 0,
            PRIMARY KEY (code, date)
        )""")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kline_date ON kline_data(date)"
        )
    finally:
        conn.close()


def _today() -> str:
    return date.today().strftime("%Y%m%d")


def _is_already_updated(db_path: str) -> bool:
    """检查当日是否已有数据更新"""
    if not os.path.exists(db_path):
        return False
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM kline_data WHERE date=?", (_today(),)
        ).fetchone()
        return (row[0] or 0) > 100  # 至少100条今日数据才算已更新
    finally:
        conn.close()


def _get_pool_codes() -> set:
    """获取候选池中的ETF代码(纯数字)"""
    codes = set()
    for etf in CORE_ETFS + SATELLITE_ETFS:
        c = etf["code"]
        if "." in c:
            c = c.split(".")[0]
        codes.add(c)
    return codes


# ── ETF 列表 ──

def _is_list_stale(db_path: str) -> bool:
    """ETF列表是否过期"""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT MAX(added_at) FROM etf_list").fetchone()
        if not row or not row[0]:
            return True
        last = date.fromisoformat(row[0][:10])
        return (date.today() - last).days >= ETL_REFRESH_DAYS
    finally:
        conn.close()


def _fetch_sh_etfs(api, ip: str, port: int) -> list[tuple[str, str, int]]:
    """扫描上交所ETF (51/56/58 开头)"""
    etfs = []
    seen = set()
    try:
        if not api.connect(ip, port):
            raise ConnectionError("连接服务器失败")
        for offset in range(1000, 28000, 1000):
            records = api.get_security_list(1, offset)
            if not records:
                break
            for r in records:
                code = r.get("code", "")
                if code.startswith(("51", "56", "58")) and code not in seen:
                    seen.add(code)
                    etfs.append((code, r.get("name", ""), 1))
    finally:
        api.disconnect()
    return etfs


def _fetch_sz_etfs(api, ip: str, port: int) -> list[tuple[str, str, int]]:
    """获取深交所ETF列表"""
    etfs = []
    seen = set()
    try:
        if not api.connect(ip, port):
            return etfs
        quotes = api.get_security_quotes([(0, c) for c in SZ_ETF_CODES])
        if quotes:
            for q in quotes:
                code = q.get("code", "")
                if code and code not in seen:
                    seen.add(code)
                    etfs.append((code, q.get("name", ""), 0))
    finally:
        api.disconnect()
    return etfs


def _fetch_all_etfs(ip: str, port: int) -> list[tuple[str, str, int]]:
    """获取全市场ETF列表"""
    print("    [全市场] 拉取上交所ETF列表...", end=" ")
    api = TdxHq_API()
    sh_etfs = _fetch_sh_etfs(api, ip, port)
    print(f"{len(sh_etfs)} 只")

    print("    [全市场] 拉取深交所ETF列表...", end=" ")
    sz_etfs = _fetch_sz_etfs(api, ip, port)
    print(f"{len(sz_etfs)} 只")

    return sh_etfs + sz_etfs


def _save_etf_list(db_path: str, etfs: list[tuple[str, str, int]]):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DELETE FROM etf_list")
        conn.executemany(
            "INSERT INTO etf_list (code, name, market) VALUES (?, ?, ?)", etfs
        )
        conn.commit()
    finally:
        conn.close()


def _load_etf_list(db_path: str) -> list[tuple[str, str, int]]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT code, name, market FROM etf_list ORDER BY code"
        ).fetchall()
    finally:
        conn.close()
    return [(r[0], r[1], r[2]) for r in rows]


# ── K线 ──

def _get_latest_kline_date(db_path: str, code: str) -> str | None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT MAX(date) FROM kline_data WHERE code=?", (code,)
        ).fetchone()
        return row[0] if row and row[0] else None
    finally:
        conn.close()


def _fetch_kline(api, code: str, market: int,
                 start_date: str, end_date: str) -> list[dict]:
    """拉取单只ETF K线 (复用已有连接), 通过官方除权除息记录精确前复权"""
    try:
        raw = api.get_security_bars(4, market, code, 0, 800)
        if not raw:
            return []

        # 转为 DataFrame 以便做前复权
        import pandas as pd
        rows_raw = []
        for r in raw:
            date_str = str(r["datetime"])[:10].replace("-", "")
            rows_raw.append({
                "date": date_str,
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "vol": float(r["vol"]),
                "amount": float(r.get("amount", 0)),
            })

        df = pd.DataFrame(rows_raw)
        if not df.empty:
            df = df.sort_values("date").reset_index(drop=True)
            # 通过 get_xdxr_info() 获取官方记录, 精确前复权
            etf_code = f"{code}.SH" if market == 1 else f"{code}.SZ"
            xdxr = _fetch_xdxr(api, market, code, etf_code)
            if xdxr:
                df = _apply_xdxr_adjustment(df, xdxr)

        # 过滤日期范围并返回
        result = []
        for _, r in df.iterrows():
            if start_date <= r["date"] <= end_date:
                result.append({
                    "date": r["date"],
                    "open": r["open"],
                    "high": r["high"],
                    "low": r["low"],
                    "close": r["close"],
                    "volume": r["vol"],
                    "amount": r.get("amount", 0),
                })
        return result
    except Exception:
        return []


def _save_klines_batch(db_path: str, code: str, rows: list[dict]):
    if not rows:
        return
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            """INSERT OR REPLACE INTO kline_data
               (code, date, open, high, low, close, volume, amount)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (code, r["date"], r["open"], r["high"], r["low"],
                 r["close"], r["volume"], r.get("amount", 0))
                for r in rows
            ],
        )
        conn.commit()
    finally:
        conn.close()


def update_etf_cache(force: bool = False) -> bool:
    """更新全市场ETF缓存

    Args:
        force: 强制更新, 忽略当日已更新检查

    Returns:
        是否执行了更新
    """
    _ensure_cache_dir()
    _init_db(CACHE_DB)

    today = _today()

    # ── 快速路径: 今日已更新则跳过 ──
    if not force and _is_already_updated(CACHE_DB):
        return False

    pool_codes = _get_pool_codes()

    # ── 1. 获取服务器 ──
    try:
        ip, port = _get_server()
    except Exception as e:
        print(f"  [全市场] 获取通达信服务器失败: {e}")
        return False

    # ── 2. ETF列表 ──
    etfs = _load_etf_list(CACHE_DB) if os.path.exists(CACHE_DB) else []
    if not etfs or _is_list_stale(CACHE_DB):
        try:
            etfs = _fetch_all_etfs(ip, port)
            _save_etf_list(CACHE_DB, etfs)
        except Exception as e:
            if not etfs:
                print(f"  [全市场] ETF列表拉取失败: {e}")
                return False
            print(f"  [全市场] ETF列表拉取失败, 使用缓存 ({len(etfs)} 只)")

    # ── 3. 增量K线 ──
    end_date = date.today()
    start_date = end_date - timedelta(days=90)

    # 仅更新不在候选池中的ETF (池内ETF由 tdx_data.py 负责)
    external_etfs = [
        (c, n, m) for c, n, m in etfs if c not in pool_codes
    ]
    print(f"  [全市场] 候选池外 {len(external_etfs)} 只ETF, 增量更新K线...")

    total_fetched = 0
    api = TdxHq_API()
    try:
        if not api.connect(ip, port, time_out=10):
            print("  [全市场] 连接服务器失败")
            return False

        for idx, (code, name, market) in enumerate(external_etfs):
            latest = _get_latest_kline_date(CACHE_DB, code)
            if latest and latest >= _today():
                continue  # 已是最新

            fetch_start = (
                datetime.strptime(latest, "%Y%m%d").date() + timedelta(days=1)
                if latest else start_date
            )
            rows = _fetch_kline(
                api, code, market,
                fetch_start.strftime("%Y%m%d"),
                end_date.strftime("%Y%m%d"),
            )
            _save_klines_batch(CACHE_DB, code, rows)
            total_fetched += len(rows)

            if (idx + 1) % 200 == 0:
                print(f"      进度 {idx+1}/{len(external_etfs)}, "
                      f"已获取 {total_fetched} 条")

            # 温和限速 (避免触发通达信流控)
            time.sleep(0.03)
    finally:
        api.disconnect()

    print(f"  [全市场] 增量获取 {total_fetched} 条K线, 完成")
    return True


if __name__ == "__main__":
    import time as _time
    t0 = _time.perf_counter()
    updated = update_etf_cache(force=True)
    elapsed = _time.perf_counter() - t0
    if updated:
        print(f"  耗时: {elapsed:.1f}s")
    else:
        print(f"  今日已更新, 跳过 ({elapsed:.1f}s)")

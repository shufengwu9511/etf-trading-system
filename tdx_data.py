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
import threading
import time
from datetime import datetime, timedelta
from typing import Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
from pytdx.hq import TdxHq_API
from pytdx.config.hosts import hq_hosts

# ── 缓存路径 ──
TDX_CACHE_DIR = os.path.join("data", "tdx_cache")
TDX_CACHE_DB = os.path.join(TDX_CACHE_DIR, "kline.db")

# ── 优选服务器 ──
# 2026-09-10 首次全量实测; 2026-10-14 复测(pytdx 内置 104 台 + 117.34.114.10-40 扩扫):
#   - 全量仅 8 台可用, 全部为 国泰君安 117.34.114.x 网段 (连接+行情+K线 三层全过, ~260-300ms)
#   - 僵尸节点(connect 成功但不返回行情) 25 台: 上海电信主站Z1 180.153.18.170、海通 123.125.108.90、
#     **安信 59.36.5.11(原优选, 已死)** 等; connect_fail 70 台
#   - 老主站 180.153.18.170 / 123.125.108.90 等长期为僵尸节点, 勿恢复
#   - 如需复测: `python scripts/tdx_server_check.py` (支持 --timeout/--top/--workers)
_PREFERRED_SERVERS = [
    ("国泰君安1", "117.34.114.13", 7709),
    ("国泰君安2", "117.34.114.14", 7709),
    ("国泰君安3", "117.34.114.15", 7709),
    ("国泰君安4", "117.34.114.16", 7709),
    ("国泰君安5", "117.34.114.17", 7709),
    ("国泰君安6", "117.34.114.18", 7709),
    ("国泰君安7", "117.34.114.20", 7709),
    ("国泰君安8", "117.34.114.27", 7709),
]

# ── 前复权: 通过 pytdx get_xdxr_info() 获取官方除权除息记录, 精确处理拆分+分红 ──

# ── 内部缓存 ──
_best_server: Optional[Tuple[str, int]] = None  # (ip, port)

# ── 服务器扫描结果缓存 (2026-10-23 新增) ──
# 背景: pytdx 服务器全线失效时, _get_server() 每次调用都要全量扫 104 台(~9s),
# 而 fetch_kline_raw 重试 3 次 → 单只 ETF 29.6s, 18 只要 9 分钟, 且数据一条取不到。
# 缓存策略: 扫描失败后 SERVER_SCAN_TTL 秒内直接快速失败, 不再重复扫描。
SERVER_SCAN_TTL = 600           # 失败结果缓存 10 分钟
_server_scan_failed_at: Optional[float] = None
_server_lock = threading.RLock()

# ── xdxr 专用服务器缓存 (2026-10-23 新增) ──
# bars/quotes 被服务端拒绝, 但 get_xdxr_info 仍可用 → xdxr 用独立通道, 独立缓存。
# 除权事件极少变动, 失败后 30 分钟内不再重扫。
XDXR_SCAN_TTL = 1800
_xdxr_server: Optional[Tuple[str, int]] = None
_xdxr_scan_failed_at: Optional[float] = None

# 本进程内确认"取不到K线"的服务器 (quotes 有响应但 bars 空) — 扫描时跳过,
# 避免并行取数时 24 个线程反复选中同一个僵尸节点、各自清缓存重扫造成扫描风暴
_dead_servers: set = set()

# 本进程内"已尝试拉取过"的代码 — 避免预取(并行)之后串行流程又拉一遍
_attempted_codes: set = set()
_attempted_lock = threading.Lock()

# SQLite 缓存写锁 (并行取数时多线程同时写 kline.db 会 database is locked)
_cache_write_lock = threading.Lock()

# 最近一次实时行情的实际来源 ("pytdx" / "sina" / "akshare" / "")
# 2026-10-23 新增: 原调用方一律把结果标成 "pytdx", 实际常常是新浪兜底,
# 导致"实时行情来源: pytdx 21只"这种误标 — 会掩盖 pytdx 已经全挂的事实。
_last_quote_source: str = ""


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


def _server_has_xdxr(ip: str, port: int) -> bool:
    """验证服务器能否返回除权除息记录

    2026-10-23 背景: pytdx 服务端收紧后 get_security_quotes/bars 全被拒,
    但 get_xdxr_info 仍然正常 (实测 8 台国泰君安全部返回 7 条记录)。
    因此 xdxr 通道必须用独立判据, 不能复用 _server_has_data (它只看 quotes,
    会把仍然能提供 xdxr 的服务器误判为不可用)。
    """
    api = TdxHq_API()
    try:
        if not api.connect(ip, port, time_out=2):
            return False
        return bool(api.get_xdxr_info(1, "510310"))
    except Exception:
        return False
    finally:
        try:
            api.disconnect()
        except Exception:
            pass


def _get_xdxr_server() -> Optional[Tuple[str, int]]:
    """获取可提供 xdxr 的服务器 (独立缓存, 与 K线服务器互不影响)

    失败结果同样加 TTL 缓存, 避免取不到时每只 ETF 都重扫一遍。
    """
    global _xdxr_server, _xdxr_scan_failed_at

    with _server_lock:
        if _xdxr_server is not None:
            return _xdxr_server
        if _xdxr_scan_failed_at is not None and \
                (time.time() - _xdxr_scan_failed_at) < XDXR_SCAN_TTL:
            return None

        # 优选列表
        for _name, ip, port in _PREFERRED_SERVERS:
            if _server_has_xdxr(ip, port):
                _xdxr_server = (ip, port)
                return _xdxr_server

        # 全量并发扫描
        seen, uniq = set(), []
        for _, ip, port in hq_hosts:
            if (ip, port) in seen:
                continue
            seen.add((ip, port))
            uniq.append((ip, port))

        found = None
        ex = ThreadPoolExecutor(max_workers=24)
        try:
            futs = {ex.submit(_server_has_xdxr, ip, port): (ip, port)
                    for ip, port in uniq}
            for fut in as_completed(futs):
                try:
                    if fut.result():
                        found = futs[fut]
                        break
                except Exception:
                    continue
        finally:
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass

        if found is None:
            _xdxr_scan_failed_at = time.time()
            return None
        _xdxr_server = found
        return _xdxr_server


def _server_has_data(ip: str, port: int) -> bool:
    """验证服务器能否真正返回行情数据

    部分通达信老主站已退化为"TCP 能 connect 但不下发行情数据"的僵尸节点
    (get_security_list/count 正常, 但 quotes/bars 全空)。因此服务器可用性
    必须以"能否取到行情数据"为准, 不能只看 connect。
    """
    api = TdxHq_API()
    try:
        if not api.connect(ip, port, time_out=2):
            return False
        quotes = api.get_security_quotes([(0, "159934"), (1, "510310")])
        return bool(quotes)
    except Exception:
        return False
    finally:
        try:
            api.disconnect()
        except Exception:
            pass


def _scan_server() -> Optional[Tuple[str, int]]:
    """扫描最优通达信服务器 (优先优选列表, 失败则并发扫全量)

    判据: connect + 实际取到行情数据, 两者都通过才算可用。

    注: pytdx 内置 hq_hosts 逐年退化 (2026-10-14 实测 103 台中仅 8 台可用),
    全量回退若顺序扫描, 104 台 × 2s 超时最坏要 200s+; 这里改为 24 路并发,
    最坏约 10s, 找到即提前返回。
    """
    # 先试优选列表 (跳过本进程已确认无K线的节点)
    for name, ip, port in _PREFERRED_SERVERS:
        if (ip, port) in _dead_servers:
            continue
        if _server_has_data(ip, port):
            return (ip, port)

    # 全量并发扫描 (按 (ip, port) 去重)
    seen, uniq = set(), []
    for _, ip, port in hq_hosts:
        if (ip, port) in seen or (ip, port) in _dead_servers:
            continue
        seen.add((ip, port))
        uniq.append((ip, port))

    ex = ThreadPoolExecutor(max_workers=24)
    try:
        futs = {ex.submit(_server_has_data, ip, port): (ip, port) for ip, port in uniq}
        for fut in as_completed(futs):
            try:
                if fut.result():
                    found = futs[fut]
                    ex.shutdown(wait=False, cancel_futures=True)
                    return found
            except Exception:
                continue
    finally:
        try:
            ex.shutdown(wait=False)
        except Exception:
            pass
    return None


def _get_server(force_rescan: bool = False) -> Tuple[str, int]:
    """获取通达信服务器地址 (带缓存 + 失败结果 TTL 缓存)

    2026-10-23 改造 (pytdx 全线失效后的性能修复):
      1) 加锁 — 并行取数时多个线程同时扫描会做 24×(104台) 次连接, 灾难性
      2) 失败结果缓存 — 扫描失败后 TTL 内直接抛错, 不再重复扫 (原每次 9s)
      3) 不再每次调用都探活 — 服务器挂掉由上层的 connect/bars 失败触发 _best_server_clear()

    Raises:
        ConnectionError: 无可用服务器 (TTL 内为快速失败)
    """
    global _best_server, _server_scan_failed_at

    with _server_lock:
        if not force_rescan:
            # 有可用缓存 → 直接用
            if _best_server is not None:
                return _best_server
            # 近期扫描过且失败 → 快速失败, 避免重复全量扫描
            if _server_scan_failed_at is not None and \
                    (time.time() - _server_scan_failed_at) < SERVER_SCAN_TTL:
                raise ConnectionError(
                    f"无可用通达信行情服务器 ({(time.time() - _server_scan_failed_at):.0f}s 前扫描已失败, "
                    f"{SERVER_SCAN_TTL}s 内不重扫)")

        server = _scan_server()
        if server is None:
            _server_scan_failed_at = time.time()
            raise ConnectionError("无可用通达信行情服务器 (全量扫描未找到可用节点)")

        _best_server = server
        _server_scan_failed_at = None
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
    """将K线写入本地SQLite缓存 (增量)

    并行取数时多线程会同时写 kline.db, SQLite 单写者模型下会抛
    "database is locked"; 用模块级写锁串行化 (单只插入仅几十毫秒, 开销可忽略)。

    2026-10-23: 改用 executemany 批量插入。新浪源单只返回 3000+ 行,
    原来逐行 execute 要 1.5s/只, 批量后约 30ms。
    """
    if df.empty:
        return
    _ensure_cache()

    cols = ["date", "open", "high", "low", "close", "vol"]
    work = df.copy()
    if "amount" not in work.columns:
        work["amount"] = 0
    cols.append("amount")

    rows = list(work[cols].itertuples(index=False, name=None))

    with _cache_write_lock:
        conn = sqlite3.connect(TDX_CACHE_DB, timeout=20)
        try:
            conn.executemany("""
                INSERT OR REPLACE INTO kline (code, date, open, high, low, close, vol, amount)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, [(code, ) + tuple(r) for r in rows])
            conn.commit()
        finally:
            conn.close()


def _fetch_xdxr(api, market: int, raw_code: str, code: str) -> list[dict]:
    """获取ETF官方除权除息信息 (带缓存)

    通过 pytdx get_xdxr_info() 获取精确记录, 覆盖:
      - category 5/11 (股本变化/扩缩股): 份额拆分/合并 → 自动前复权
      - category 1   (除权除息):       现金分红       → 自动前复权

    Args:
        api: 已连接的 TdxHq_API 实例; 传 None 时自动连 xdxr 专用服务器
             (2026-10-23: 新浪主源路径下没有现成连接)
        market: 0=深圳, 1=上海
        raw_code: 通达信代码如 '159558'
        code: Tushare格式代码如 '159558.SZ' (用于缓存键)

    Returns:
        [{'date': '20260709', 'type': 'split', 'ratio': 3.0}, ...]
        按日期升序排列
    """
    global _xdxr_server

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

        # 缓存未命中, 从API获取 (无现成连接时自建)
        own_api = None
        if api is None:
            srv = _get_xdxr_server()
            if srv is None:
                print(f"    [WARN] 无可用服务器获取 {code} 除权记录, 按未复权处理")
                return []
            own_api = TdxHq_API()
            try:
                if not own_api.connect(srv[0], srv[1], time_out=3):
                    _xdxr_server = None
                    print(f"    [WARN] 连接 {srv[0]} 获取 {code} 除权记录失败")
                    return []
            except Exception as e:
                _xdxr_server = None
                print(f"    [WARN] {code} 除权记录请求异常: {type(e).__name__}")
                return []
            api = own_api

        try:
            raw = api.get_xdxr_info(market, raw_code)
        finally:
            if own_api is not None:
                try:
                    own_api.disconnect()
                except Exception:
                    pass

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
        (close_on_D 取除权日**未复权**收盘价; 成交量不受分红影响)

    2026-10-23 性能改造: 新浪源单只 3300 行, 原实现对每行做 4 次 iloc 赋值
    (1.3 万次 pandas 标量写入) 要 4.5s; 改为「先算每日因子数组 → 整列向量化除法」,
    同口径下约 3ms。

    Args:
        df: 按 date 升序排列的 DataFrame, 列: date, open, high, low, close, vol, amount
        xdxr_records: _fetch_xdxr() 返回的记录列表

    Returns:
        前复权后的 DataFrame (副本)
    """
    if not xdxr_records:
        return df.copy()

    import numpy as np

    result = df.copy().reset_index(drop=True)
    dates = result["date"].tolist()

    # 对齐除权日期到实际交易日 (除权日可能非交易日)
    date_set = set(dates)
    dates_sorted = sorted(dates)
    close_raw = dict(zip(dates, result["close"].tolist()))

    ev_price: dict[str, float] = {}
    ev_vol: dict[str, float] = {}
    for r in xdxr_records:
        xd = r['date']
        if xd in date_set:
            effective = xd
        else:
            candidates = [d for d in dates_sorted if d >= xd]
            effective = candidates[0] if candidates else xd

        if r['type'] == 'split':
            ratio = float(r['ratio'])
            ev_price[effective] = ev_price.get(effective, 1.0) * ratio
            ev_vol[effective] = ev_vol.get(effective, 1.0) * ratio
        elif r['type'] == 'dividend':
            # ⚠️ 通达信 xdxr 的 fenhong 单位是「每 10 份派息(元)」, 必须 /10。
            # 2026-11-04 实测确认: 510310 记录 0.804 但除息日实际只跌 0.077 元
            # (2024-10-21: 3.936→3.859)、20260120 记录 0.75 实际跌 0.087;
            # 510100 记录 0.97 实际跌约 0.084。不除以 10 会把分红因子放大约 10 倍,
            # 导致前复权价被系统性压低(510310 2024-05 应为 3.57, 曾错算成 1.98),
            # 且**除权日附近动量/MA20 出现 14~20% 的假跳空** → 假止损。
            F = float(r['fenhong']) / 10.0
            c = close_raw.get(effective)
            if c and c > 0:
                ev_price[effective] = ev_price.get(effective, 1.0) * (c + F) / c

    # 倒序累乘: 第 i 天适用的因子 = 所有 event_date > dates[i] 的因子之积
    n = len(dates)
    price_div = np.ones(n, dtype=float)
    vol_mult = np.ones(n, dtype=float)
    cur_p, cur_v = 1.0, 1.0
    for i in range(n - 1, -1, -1):
        price_div[i] = cur_p
        vol_mult[i] = cur_v
        d = dates[i]
        if d in ev_price:
            cur_p *= ev_price[d]
        if d in ev_vol:
            cur_v *= ev_vol[d]

    for col in ("open", "high", "low", "close"):
        result[col] = result[col].to_numpy(dtype=float) / price_div
    result["vol"] = result["vol"].to_numpy(dtype=float) * vol_mult
    # amount (成交额) 不受拆分/分红影响, 保持不变

    return result


def _fetch_kline_sina(code: str) -> pd.DataFrame:
    """从新浪拉取单只ETF完整日K线 (未复权原始价)

    2026-10-23 新增: pytdx 服务端收紧后 get_security_bars 全节点被拒
    (只回 800 这个总条数, 不带任何K线数据), K线必须有替代源。

    选用新浪的理由 (实测):
      - ak.fund_etf_hist_sina 返回 3000+ 行全量历史 (2013 起), 0.2~0.3s
      - 沪 sh + 深 sz 全覆盖
      - 与 pytdx 缓存逐笔对齐 (10-16~10-21 close 差异 0.000%)
      - volume 单位是"份", 与 pytdx 缓存口径一致, 无需换算
    不选东财: fund_etf_hist_em 走 push2his, 本机被代理/直连双重拦截。

    返回的价格为**未复权**原始价, 复权由调用方叠加 pytdx 官方 xdxr 完成。

    Args:
        code: Tushare格式如 '510310.SH' 或 '159558.SZ'

    Returns:
        DataFrame: date(YYYYMMDD), open, high, low, close, vol, amount
        失败返回空 DataFrame
    """
    market, raw_code = _parse_ts_code(code)
    symbol = ("sh" if market == 1 else "sz") + raw_code
    try:
        import akshare as ak
        raw = ak.fund_etf_hist_sina(symbol=symbol)
    except Exception as e:
        print(f"    [WARN] 新浪K线获取 {code} 失败: {type(e).__name__}: {e}")
        return pd.DataFrame()

    if raw is None or len(raw) == 0:
        return pd.DataFrame()

    try:
        df = pd.DataFrame({
            # 新浪返回 "2026-10-22", 统一为 YYYYMMDD (与 Tushare/旧缓存一致)
            "date": pd.to_datetime(raw["date"]).dt.strftime("%Y%m%d"),
            "open": raw["open"].astype(float),
            "high": raw["high"].astype(float),
            "low": raw["low"].astype(float),
            "close": raw["close"].astype(float),
            "vol": raw["volume"].astype(float),
            "amount": raw["amount"].astype(float),
        })
    except (KeyError, TypeError, ValueError) as e:
        print(f"    [WARN] 新浪K线 {code} 字段异常: {e}")
        return pd.DataFrame()

    # 丢弃无效行 (停牌/脏数据)
    df = df[(df["close"] > 0) & (df["date"].str.len() == 8)]
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    return df


def _fetch_kline_tushare(code: str, start_date: str = "20180101") -> pd.DataFrame:
    """从 Tushare fund_daily 拉取ETF日K线 (未复权), 作为新浪的备用源

    2026-10-23 新增: 新浪是免费非官方接口, 存在随时变更/限流的风险;
    Tushare 是我们已有的积分源, 实测可用 (0.16s/只), 放第二顺位。

    单位换算 (与 新浪/pytdx 缓存口径对齐):
      - vol:    Tushare 返回"手" → ×100 转"份"
                (510310 20261022: 2694327.57 手 × 100 = 2.69 亿份)
      - amount: Tushare 返回"千元" → ×1000 转"元"
                (1079929.393 千元 × 1000 = 10.8 亿元)

    Args:
        code: Tushare格式如 '510310.SH'
        start_date: 起始日期 YYYYMMDD (fund_daily 单次上限约 2000 行)

    Returns:
        DataFrame: date, open, high, low, close, vol, amount (未复权)
    """
    try:
        import tushare as ts
        from config import TUSHARE_TOKEN
        if not TUSHARE_TOKEN:
            return pd.DataFrame()
        pro = ts.pro_api(TUSHARE_TOKEN)
        raw = pro.fund_daily(
            ts_code=code,
            start_date=start_date,
            end_date=datetime.now().strftime("%Y%m%d"),
            fields="trade_date,open,high,low,close,vol,amount",
        )
    except Exception as e:
        print(f"    [WARN] Tushare K线 {code} 失败: {type(e).__name__}: {str(e)[:80]}")
        return pd.DataFrame()

    if raw is None or len(raw) == 0:
        return pd.DataFrame()

    df = pd.DataFrame({
        "date": raw["trade_date"].astype(str),
        "open": raw["open"].astype(float),
        "high": raw["high"].astype(float),
        "low": raw["low"].astype(float),
        "close": raw["close"].astype(float),
        "vol": raw["vol"].astype(float) * 100,
        "amount": raw["amount"].astype(float) * 1000,
    })
    df = df[(df["close"] > 0) & (df["date"].str.len() == 8)]
    return df.sort_values("date").drop_duplicates("date").reset_index(drop=True)


def _fetch_kline_tdx(code: str) -> pd.DataFrame:
    """从通达信拉取单只ETF的完整日K线 (最多800条, 已含 xdxr 前复权)

    2026-10-23: pytdx 服务端收紧后此路径返回空, 保留作为服务端恢复后的备选,
    以及 KLINE_SOURCE="tdx" 时使用。

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
        except ConnectionError:
            # 无可用服务器 — 重试毫无意义 (TTL 内每次都是同样的快速失败),
            # 直接返回空, 让调用方走缓存 (2026-10-23: 原来 3 次 attempt 各扫一遍 9s)
            try:
                api.disconnect()
            except Exception:
                pass
            return pd.DataFrame()

        try:
            if not api.connect(ip, port, time_out=3):
                if attempt < 2:
                    _best_server_clear((ip, port))
                    time.sleep(0.3)
                    continue
                return pd.DataFrame()

            # 拉取日K线: category=4, 起始位置0, 最多800条
            raw = api.get_security_bars(4, market, raw_code, 0, 800)

            if not raw:
                # connect 成功但取不到数据 → 服务器可能已退化(僵尸节点),
                # 清缓存换节点重试, 而不是静默返回空
                api.disconnect()
                print(f"    [WARN] 通达信 {ip} 连通但无K线数据, 换节点重试...")
                _best_server_clear((ip, port))
                if attempt < 2:
                    time.sleep(0.3)
                    continue
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


def fetch_kline_raw(code: str, source: Optional[str] = None) -> pd.DataFrame:
    """拉取单只ETF完整日K线 (自动选择数据源 + 统一前复权)

    数据源编排 (2026-10-23 切换):
      sina (默认) = 新浪原始价 + pytdx 官方 xdxr 前复权
                    — pytdx 的 bars 被服务端拒绝, 但 xdxr 仍正常, 两者组合
                      与原 pytdx 全链路结果口径完全一致
      tdx         = 旧行为 (pytdx bars + xdxr), 备用
      auto        = 先新浪, 空则回落 pytdx

    Args:
        code: Tushare格式如 '510310.SH'
        source: 覆盖 config.KLINE_SOURCE

    Returns:
        DataFrame: date(YYYYMMDD), open, high, low, close, vol, amount
        价格已前复权; 失败返回空 DataFrame
    """
    if source is None:
        try:
            from config import KLINE_SOURCE as source
        except Exception:
            source = "sina"
    source = (source or "sina").lower()

    market, raw_code = _parse_ts_code(code)

    if source in ("sina", "auto"):
        df = _fetch_kline_sina(code)
        if not df.empty:
            # 新浪给的是未复权价, 必须叠加官方除权记录, 否则拆分/分红会造成假暴跌
            # (例: 510310 在 2024-09-23 有 0.4998 扩缩股, 不复权会看成腰斩)
            xdxr = _fetch_xdxr(None, market, raw_code, code)
            if xdxr:
                df = _apply_xdxr_adjustment(df, xdxr)
            return df

        # 第二顺位: Tushare fund_daily (同样未复权, 走同一套 xdxr 复权)
        print(f"    [WARN] {code} 新浪K线为空, 尝试 Tushare fund_daily")
        df = _fetch_kline_tushare(code)
        if not df.empty:
            xdxr = _fetch_xdxr(None, market, raw_code, code)
            if xdxr:
                df = _apply_xdxr_adjustment(df, xdxr)
            return df

        print(f"    [WARN] {code} Tushare K线也为空, 回退通达信")

    return _fetch_kline_tdx(code)


def _best_server_clear(dead: Optional[Tuple[str, int]] = None):
    """清除服务器缓存 (连接失败时调用)

    Args:
        dead: 本进程确认取不到数据的服务器, 加入黑名单避免反复选中

    注意: 不清 _server_scan_failed_at — 那由 _get_server() 内部维护,
    清掉会让"刚扫完发现没服务器"的状态丢失, 又退回每只重扫 9 秒。
    """
    global _best_server
    with _server_lock:
        if dead is not None:
            _dead_servers.add(tuple(dead))
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

    # 本进程已经尝试拉取过这只 (如 prefetch_klines 并行预取) → 不再重复拉。
    # 否则非交易日/取数失败时, 预取完的串行流程会为每只再等一遍超时。
    with _attempted_lock:
        if code in _attempted_codes:
            need_fetch = False

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


def _probe_tushare_newer(latest: str) -> str:
    """探测 Tushare fund_daily 是否已发布比 latest 更新的交易日 (2026-11-04 新增)

    只发 1 个请求 (取沪深300ETF 最近 10 个交易日), 用来判断"全池都停在 T-1"
    时到底该不该补, 以及该补到哪天。

    只在 18:30 之后才探测: 更早的时刻 Tushare 可能给的是盘中快照, 补进去反而
    污染缓存 (历史上 10-22 就这样少算过成交量)。

    Returns:
        可用的最新交易日 YYYYMMDD; 无更新或未到探测时点则返回空串
    """
    now = datetime.now()
    if latest >= now.strftime("%Y%m%d"):
        return ""                       # 已经是最新自然日, 无需探测
    if (now.hour, now.minute) < (18, 30):
        return ""                       # 盘中/刚收盘, 数据可能未落定
    try:
        import tushare as ts
        from config import TUSHARE_TOKEN
        pro = ts.pro_api(TUSHARE_TOKEN)
        start = (now - timedelta(days=15)).strftime("%Y%m%d")
        df = pro.fund_daily(ts_code="510310.SH", start_date=start,
                            end_date=now.strftime("%Y%m%d"),
                            fields="trade_date,close")
        if df is None or df.empty:
            return ""
        return str(df["trade_date"].max())
    except Exception:
        return ""


def _repair_stale_klines(results: dict, latest: str, days: int = 120) -> dict:
    """把滞后的 K线用 Tushare fund_daily 补齐到全池最新交易日 (2026-11-03 新增)

    背景: 新浪 fund_etf_hist_sina 对**深市**代码 (159xxx) 的当日行情更新明显晚于
    沪市 (510310/515650 当晚 20:40 已有 T 日, 159530/159697 仍停在 T-1)。这会让
    同一份动量排名里沪深口径不一致 —— 深市少算一根, 排名失真。

    只处理滞后 1~3 个自然日的代码; 差得更多的多半是停牌/退市, 不去打扰 Tushare。

    情形 B (2026-11-04 新增): **全池都停在 T-1**。新浪整批还没发布当日线时,
    上面的"相对滞后"判断恒不触发 (所有代码都一样新), 动量排名会整天用昨天的
    数据。此时若已过 18:30 (Tushare 日线已落定, 早于此可能是盘中快照), 用一只
    参照标的探一次 Tushare, 若有更新的交易日, 则把全池补到那个日期。

    Args:
        results: {code: DataFrame}, 就地替换修复后的结果
        latest: 全池最新交易日 YYYYMMDD
        days: 截取天数 (与 prefetch_klines 一致)

    Returns:
        修复后的 results
    """
    if not results or not latest or latest == "-":
        return results
    try:
        latest_dt = datetime.strptime(str(latest), "%Y%m%d")
    except Exception:
        return results

    # --- 情形 B: 全池整体滞后, 探测 Tushare 是否已有更新的交易日 ---
    _probe = _probe_tushare_newer(str(latest))
    if _probe and _probe > str(latest):
        latest = _probe
        latest_dt = datetime.strptime(latest, "%Y%m%d")

    stale = []
    for c, df in results.items():
        if df is None or df.empty or "trade_date" not in df.columns:
            continue
        try:
            gap = (latest_dt - datetime.strptime(str(df["trade_date"].iloc[0]), "%Y%m%d")).days
        except Exception:
            continue
        if 0 < gap <= 3:
            stale.append(c)

    if not stale:
        return results

    repaired = 0
    for c in stale:
        try:
            fresh = _fetch_kline_tushare(c)
            if fresh.empty:
                continue
            if str(fresh["date"].max()) <= str(results[c]["trade_date"].iloc[0]):
                continue                      # Tushare 也没更新的, 保持原样
            market, raw_code = _parse_ts_code(c)
            xdxr = _fetch_xdxr(None, market, raw_code, c)
            if xdxr:
                fresh = _apply_xdxr_adjustment(fresh, xdxr)
            _save_cache(c, fresh)
            reloaded = get_etf_kline(c, days)
            if not reloaded.empty:
                results[c] = reloaded
                repaired += 1
        except Exception:
            continue

    if repaired:
        print(f"    [K线补齐] {repaired}/{len(stale)} 只滞后标的已用 Tushare 补至 {latest}")
    return results


def prefetch_klines(codes: list, days: int = 120, max_workers: int = 6) -> dict:
    """并行预取多只ETF的K线到本地缓存 (2026-10-23 新增)

    为什么要预取: 串行取数在 pytdx 失效时单只要 29.6s (3 次重试各扫一遍服务器),
    18 只就是 9 分钟 —— 主流程会跑到超时被杀。预取把这一步并行化, 之后串行的
    update_core/satellite_etf_data 直接命中本地缓存, 不再碰网络。

    与 get_etf_kline 的关系: 预取过的代码会记入 _attempted_codes, 后续
    get_etf_kline 看到标记就不再重复拉取 (否则非交易日会再等一遍超时)。

    Args:
        codes: Tushare 格式代码列表, 如 ['510310.SH', '159558.SZ']
        days: 需要最近多少天 (仅影响返回值截取, 缓存始终存全量)
        max_workers: 并发数 (默认 6; pytdx 可用时 6 路并发对服务器压力很小)

    Returns:
        {code: DataFrame} — 与 batch_get_etf_klines 一致
    """
    codes = list(dict.fromkeys(codes or []))   # 去重且保持顺序
    results = {}
    if not codes:
        return results

    t0 = time.time()

    def _one(c: str):
        try:
            with _attempted_lock:
                _attempted_codes.discard(c)   # 预取阶段强制尝试一次
            return get_etf_kline(c, days)
        except Exception:
            return pd.DataFrame()
        finally:
            with _attempted_lock:
                _attempted_codes.add(c)

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(_one, c): c for c in codes}
        for fut in as_completed(futs):
            c = futs[fut]
            try:
                results[c] = fut.result()
            except Exception:
                results[c] = pd.DataFrame()

    # 统计口径 (2026-10-23 调整): 新浪源给的是 T-1 收盘, 原来用"最新日期 >= 今天"
    # 判断会导致 fresh 恒为 0 的误导性日志; 改为"是否与全体最新交易日对齐"。
    dates = [df["trade_date"].iloc[0] for df in results.values() if not df.empty]
    latest = max(dates) if dates else "-"

    # 深市代码在新浪源上会滞后一天 (沪市当晚已出 T 日, 深市常要更晚)。
    # 不补齐的话, 同一份动量排名里沪市用 T、深市用 T-1, 排名会失真
    # (2026-11-03 实测: 159530/159697 卡在 20261102, 而 510310/515650 已到 20261103)。
    results = _repair_stale_klines(results, latest, days)
    dates = [df["trade_date"].iloc[0] for df in results.values() if not df.empty]
    latest = max(dates) if dates else "-"

    fresh = sum(1 for d in dates if d == latest)
    print(f"    [K线预取] {len(codes)} 只并行完成: {fresh} 只更新到最新交易日 {latest}, "
          f"耗时 {time.time() - t0:.1f}s")
    return results


def batch_get_etf_klines(codes: list, days: int = 120) -> dict:
    """批量拉取多只ETF的K线数据 (并行)

    Args:
        codes: ETF代码列表, 如 ['510310.SH', '159558.SZ']
        days: 需要最近多少天

    Returns:
        {code: DataFrame} 字典
    """
    return prefetch_klines(codes, days=days)


# ============================================================
# 实时行情 (盘中现价) — 供盘中信号脚本使用
# ============================================================

# 基金/ETF类代码前缀 (实时报价为10倍, 需除以10还原)
_FUND_CODE_PREFIXES = ("15", "16", "50", "51", "56", "58", "59")


def _is_fund_code(raw_code: str) -> bool:
    """判断通达信代码是否为基金/ETF类 (这些品种的实时报价是10倍关系)"""
    return raw_code.startswith(_FUND_CODE_PREFIXES)


def _resolve_quote_scale(code: str, last_close: float,
                         prev_close_map: Optional[dict] = None) -> float:
    """确定实时报价的缩放系数

    通达信 pytdx get_security_quotes() 对基金/ETF的价格字段返回的是"10倍"
    值 (报价单位为厘/分), 而股票返回正常元值。例如:
      黄金ETF 159934: 报 99.41 → 真实 9.941
      半导体ETF 159558: 报 11.62 → 真实 1.162

    优先用数据库昨收校准 (日K线价格是正常的):
      若 last_close/10 比 last_close 更接近昨收, 则缩放系数为10;
    无昨收时按代码前缀判断。
    """
    if prev_close_map:
        pc = prev_close_map.get(code)
        if pc:
            d_ten = abs(last_close / 10 - pc)
            d_one = abs(last_close - pc)
            if d_ten < d_one:
                return 10.0
            return 1.0
    _market, raw = _parse_ts_code(code)
    return 10.0 if _is_fund_code(raw) else 1.0


def _fallback_quotes_sina(codes: list) -> dict:
    """pytdx 失效时用新浪批量行情兜底 (2026-09-24 新增)

    为什么不用 AkShare 兜底: `fund_etf_spot_em()` 底层是 `push2delay.eastmoney.com`,
    该域名在部分网络环境下被拦截/限流 (RemoteDisconnected), 等于没有兜底。
    新浪 hq.sinajs.cn 支持单次请求数百个代码, 实测稳定 ~0.3s。

    返回结构与 get_realtime_quotes 一致; 新浪返回真实价, 无需缩放。
    """
    if not codes:
        return {}
    try:
        import requests
    except ImportError:
        return {}

    order = []
    for code in codes:
        market, raw = _parse_ts_code(code)
        order.append((code, ("sh" if market == 1 else "sz") + raw))
    syms = [s for _, s in order]

    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
        "Referer": "https://finance.sina.com.cn/",
    }
    for trust_env in (False, True):
        sess = requests.Session()
        sess.trust_env = trust_env
        raw_map = {}
        try:
            for i in range(0, len(syms), 400):
                url = "https://hq.sinajs.cn/list=" + ",".join(syms[i:i + 400])
                resp = sess.get(url, headers=headers, timeout=10)
                resp.encoding = "gbk"
                for line in resp.text.split("\n"):
                    if "hq_str_" not in line or '"' not in line:
                        continue
                    try:
                        f = line.split('"')[1].split(",")
                        if len(f) < 10:
                            continue
                        cur, prev = float(f[3]), float(f[2])
                        if cur <= 0 or prev <= 0:
                            continue
                        sym = line.split("hq_str_")[1].split("=")[0]
                        raw_map[sym] = (f, cur, prev)
                    except (ValueError, IndexError):
                        continue
        except Exception as e:
            print(f"[WARN] 新浪实时行情兜底失败(trust_env={trust_env}): {type(e).__name__}")
            continue

        if not raw_map:
            continue
        now = time.strftime("%H:%M:%S")
        out = {}
        for code, sym in order:
            item = raw_map.get(sym)
            if not item:
                continue
            f, cur, prev = item
            out[code] = {
                "price": round(cur, 4),
                "last_close": round(prev, 4),
                "open": round(float(f[1] or 0), 4),
                "high": round(float(f[4] or 0), 4),
                "low": round(float(f[5] or 0), 4),
                "change_pct": round((cur / prev - 1) * 100, 2),
                "servertime": now,
            }
        if out:
            print(f"    [OK] 新浪获取 {len(out)}/{len(codes)} 只实时行情")
            return out
    return {}


def _fallback_quotes_akshare(codes: list) -> dict:
    """pytdx 失效时, 用 AkShare 全市场ETF快照兜底获取实时行情

    返回结构与 get_realtime_quotes 一致; AkShare 返回真实价, 无需缩放。
    """
    try:
        import akshare as ak
        df = ak.fund_etf_spot_em()
        if df is None or df.empty:
            return {}
        by_raw = {str(r["代码"]).zfill(6): r for _, r in df.iterrows()}
        now = time.strftime("%H:%M:%S")
        out = {}
        for code in codes:
            _mkt, raw = _parse_ts_code(code)
            row = by_raw.get(raw)
            if row is None:
                continue
            try:
                px = float(row["最新价"])
                lc = float(row["昨收"])
            except (TypeError, ValueError, KeyError):
                continue
            if not px or px <= 0 or not lc or lc <= 0:
                continue  # 停牌/盘前竞价无有效价格
            out[code] = {
                "price": round(px, 4),
                "last_close": round(lc, 4),
                "open": round(float(row.get("开盘价") or 0), 4),
                "high": round(float(row.get("最高价") or 0), 4),
                "low": round(float(row.get("最低价") or 0), 4),
                "change_pct": round((px / lc - 1) * 100, 2),
                "servertime": now,
            }
        return out
    except Exception as e:
        print(f"[WARN] AkShare 实时行情兜底也失败: {e}")
        return {}


def get_realtime_quotes(codes: list, prev_close_map: Optional[dict] = None) -> dict:
    """批量获取ETF盘中实时行情 (pytdx get_security_quotes)

    Args:
        codes: ETF代码列表 (Tushare格式), 如 ['159934.SZ', '510310.SH']
        prev_close_map: {code: 昨收真实价} 用于校准10倍缩放; 传None时仅按前缀判断

    Returns:
        {code: {"price", "last_close", "open", "high", "low", "change_pct", "servertime"}}
        所有价格字段均已还原为真实价格(元)。失败返回空dict。
    """
    if not codes:
        return {}
    codes = [c for c in codes if c]

    # 2026-10-23: pytdx quotes 被服务端拒绝后, 先试它要白跑一次全量节点扫描(~10s)。
    # 默认改为新浪优先 (实测 21只/0.3s), 失败才回落 pytdx。
    global _last_quote_source
    try:
        from config import QUOTE_SOURCE as _qs
    except Exception:
        _qs = "sina"
    if (_qs or "").lower() == "sina":
        q = _fallback_quotes_sina(codes)
        if q:
            _last_quote_source = "sina"
            return q
        print("    [WARN] 新浪实时行情失败, 回落 pytdx")

    # 解析为 (market, raw_code)
    items = []
    for code in codes:
        market, raw = _parse_ts_code(code)
        items.append((market, raw, code))
    item_map = {(m, r): c for m, r, c in items}

    quotes = {}
    api = TdxHq_API()
    try:
        ip, port = _get_server()
        if not api.connect(ip, port, time_out=3):
            raise ConnectionError("无法连接通达信行情服务器")

        # 通达信单次最多返回80条, 分批请求
        reqs = [(m, r) for m, r, _ in items]
        for i in range(0, len(reqs), 80):
            batch = reqs[i:i + 80]
            raw_quotes = api.get_security_quotes(batch) or []
            for rq in raw_quotes:
                code = item_map.get((rq.get("market"), str(rq.get("code"))))
                if not code:
                    continue
                try:
                    price = float(rq["price"])
                    last_close = float(rq["last_close"])
                except (KeyError, TypeError, ValueError):
                    continue
                if price <= 0 or last_close <= 0:
                    continue  # 盘前竞价/停牌时段无有效最新价, 丢弃避免-100%脏数据
                scale = _resolve_quote_scale(code, last_close, prev_close_map)
                if scale <= 0:
                    continue
                # round 消除浮点除法误差 (如 16.62/10 = 1.6620000000000001)
                # ETF 场内报价精度为 0.001, 取 4 位留一位余量
                lc = round(last_close / scale, 4)
                px = round(price / scale, 4)
                quotes[code] = {
                    "price": px,
                    "last_close": lc,
                    "open": round(float(rq.get("open", 0)) / scale, 4),
                    "high": round(float(rq.get("high", 0)) / scale, 4),
                    "low": round(float(rq.get("low", 0)) / scale, 4),
                    "change_pct": round((px / lc - 1) * 100, 2) if lc else 0.0,
                    "servertime": str(rq.get("servertime", "")),
                }
    except Exception as e:
        print(f"[WARN] pytdx 实时行情异常: {type(e).__name__}: {e}, 尝试 AkShare 兜底")
    finally:
        try:
            api.disconnect()
        except Exception:
            pass

    # pytdx 拿不到数据 (连接失败/僵尸节点/休市外时段) → 新浪兜底 → AkShare 兜底
    if quotes:
        _last_quote_source = "pytdx"
    if not quotes:
        quotes = _fallback_quotes_sina(codes)
        if quotes:
            _last_quote_source = "sina"
    if not quotes:
        quotes = _fallback_quotes_akshare(codes)
        if quotes:
            _last_quote_source = "akshare"
            print(f"    [OK] AkShare 兜底获取 {len(quotes)}/{len(codes)} 只实时行情")
    return quotes


def get_realtime_quote(code: str, prev_close_map: Optional[dict] = None) -> Optional[dict]:
    """获取单只ETF盘中实时行情 (见 get_realtime_quotes)"""
    result = get_realtime_quotes([code], prev_close_map)
    return result.get(code)

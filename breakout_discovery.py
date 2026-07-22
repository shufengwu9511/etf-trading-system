"""ETF 箱体突破发现 — 布林带突破筛选

数据源: 本项目通达信全市场ETF缓存 (data/tdx_cache/etf_cache.db)
  由 etf_cache_updater.py 每日自动更新

策略逻辑 (参考 TDXTEST/etf_breakout.py):
  1. 计算布林带 (20日均线 ± 2σ)
  2. 最新收盘价 > 上轨 → 向上突破
  3. 按偏离度排序 (收盘价 vs 中轨)
  4. 成交量放大 (量比 > 1) 为加分项

用法:
  from breakout_discovery import discover_breakouts
  breakouts = discover_breakout_candidates(top_n=10)
"""
import os
import math
import sqlite3

from config import CORE_ETFS, SATELLITE_ETFS

# ── 缓存路径 ──
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
ETF_CACHE_DB = os.path.join(_PROJECT_ROOT, "data", "tdx_cache", "etf_cache.db")

# ── 布林带参数 ──
BOLL_PERIOD = 20          # 布林带周期
BOLL_K = 2.0              # 标准差倍数
MIN_KLINES = BOLL_PERIOD + 2  # 至少需要22条K线

# ── 筛选参数 ──
MIN_AVG_VOLUME = 5_000_000  # 日均成交量下限
VOL_LOOKBACK = 20            # 成交量计算回溯天数

# ── 黑名单 ──
NAME_BLACKLIST = [
    "债券", "国债", "货币", "逆回购", "理财", "短融",
    "转债", "REIT", "QDII", "标普", "纳指", "恒生",
    "港股", "H股", "HK", "日经", "德国", "法国",
    "互联", "海外", "跨境",
]


def _get_existing_codes() -> set:
    """获取已存在于候选池的ETF代码（纯数字，无后缀）"""
    existing = set()
    for etf in CORE_ETFS + SATELLITE_ETFS:
        code = etf["code"]
        if "." in code:
            code = code.split(".")[0]
        existing.add(code)
    return existing


def _should_skip_name(name: str) -> bool:
    """检查ETF名称是否在黑名单中"""
    for kw in NAME_BLACKLIST:
        if kw in name:
            return True
    return False


def _calc_bollinger(closes: list, n: int = BOLL_PERIOD, k: float = BOLL_K):
    """计算布林带 (mid, upper, lower)

    用前n日数据计算（不含当日），用于判断当日是否突破
    """
    if len(closes) < n:
        return None
    window = closes[-n:]
    mid = sum(window) / n
    variance = sum((x - mid) ** 2 for x in window) / n
    std = math.sqrt(variance)
    return mid, mid + k * std, mid - k * std


def detect_breakout(klines: list) -> dict | None:
    """检测单只ETF是否突破布林带箱体

    Args:
        klines: [{"date", "close", "open", "high", "low", "volume"}, ...]

    Returns:
        {
            "direction": "up"/"down",
            "close": float,
            "mid": float,
            "upper": float,
            "lower": float,
            "pct_deviation": float,   # 偏离中轨百分比
            "vol_ratio": float,       # 量比
        } or None
    """
    if len(klines) < MIN_KLINES:
        return None

    closes = [k["close"] for k in klines]
    # 用前n日计算布林带（不含当日）
    result = _calc_bollinger(closes[:-1])
    if result is None:
        return None

    mid, upper, lower = result
    latest = klines[-1]
    close = latest["close"]

    if close > upper:
        direction = "up"
    elif close < lower:
        direction = "down"
    else:
        return None

    pct_deviation = (close - mid) / mid * 100
    vol = latest["volume"]
    avg_vol = sum(k["volume"] for k in klines[-BOLL_PERIOD:]) / BOLL_PERIOD
    vol_ratio = vol / avg_vol if avg_vol > 0 else 1.0

    return {
        "direction": direction,
        "close": close,
        "mid": mid,
        "upper": upper,
        "lower": lower,
        "pct_deviation": pct_deviation,
        "vol_ratio": vol_ratio,
    }


def discover_breakout_candidates(
    direction: str = "up",
    top_n: int = 10,
) -> list[dict]:
    """扫描全市场ETF缓存，发现布林带突破候选

    Args:
        direction: "up" 向上突破, "down" 向下突破, "all" 全部
        top_n: 返回前N个

    Returns:
        [{
            "code", "name", "direction", "close",
            "mid", "upper", "lower",
            "pct_deviation", "vol_ratio",
            "band_width": float,  # 布林带宽度 (upper-lower)/mid %
        }, ...] 按偏离度降序
    """
    if not os.path.exists(ETF_CACHE_DB):
        print(f"  [BREAKOUT] 缓存不存在: {ETF_CACHE_DB}")
        return []

    existing = _get_existing_codes()
    candidates = []

    conn = sqlite3.connect(ETF_CACHE_DB)

    try:
        # 1. 获取所有ETF名称
        name_map = {}
        for code, name in conn.execute(
            "SELECT code, name FROM etf_list"
        ).fetchall():
            name_map[code] = name

        # 2. 获取不在候选池中的代码
        all_codes = list(name_map.keys())
        new_codes = [c for c in all_codes if c not in existing]

        if not new_codes:
            return []

        # 3. 批量获取K线数据
        placeholders = ",".join("?" for _ in new_codes)
        rows = conn.execute(
            f"SELECT code, date, open, high, low, close, volume "
            f"FROM kline_data "
            f"WHERE code IN ({placeholders}) "
            f"ORDER BY code, date",
            new_codes,
        ).fetchall()

        # 4. 按代码分组K线
        from collections import defaultdict
        klines_by_code = defaultdict(list)
        for code, dt, o, h, l, c, v in rows:
            klines_by_code[code].append({
                "date": dt,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": v,
            })

        # 5. 逐个检测突破
        for code in new_codes:
            klines = klines_by_code[code]
            if len(klines) < MIN_KLINES:
                continue

            name = name_map.get(code, "")
            if _should_skip_name(name):
                continue

            # 成交量过滤
            recent_vols = [k["volume"] for k in klines[-VOL_LOOKBACK:]]
            avg_vol = sum(recent_vols) / len(recent_vols)
            if avg_vol < MIN_AVG_VOLUME:
                continue

            info = detect_breakout(klines)
            if info is None:
                continue

            # 方向过滤
            if direction != "all" and info["direction"] != direction:
                continue

            band_width = (info["upper"] - info["lower"]) / info["mid"] * 100

            candidates.append({
                "code": code,
                "name": name,
                "direction": info["direction"],
                "close": round(info["close"], 4),
                "mid": round(info["mid"], 4),
                "upper": round(info["upper"], 4),
                "lower": round(info["lower"], 4),
                "pct_deviation": round(info["pct_deviation"], 2),
                "vol_ratio": round(info["vol_ratio"], 2),
                "band_width": round(band_width, 2),
                "avg_vol": int(avg_vol),
                "klines": len(klines),
            })

    finally:
        conn.close()

    # 按偏离度降序
    candidates.sort(key=lambda x: abs(x["pct_deviation"]), reverse=True)
    return candidates[:top_n]


if __name__ == "__main__":
    print("\n扫描布林带向上突破 ETF...")
    breakouts = discover_breakout_candidates(direction="up", top_n=10)
    if not breakouts:
        print("  无符合条件的向上突破ETF")
    else:
        print(f"\n{'='*80}")
        print(f"  ETF 箱体向上突破 (布林带 20日 ±2σ) — 前{len(breakouts)}名")
        print(f"{'='*80}")
        print(f"  {'排名':>4s}  {'代码':>6s}  {'名称':<14s}  {'收盘':>8s}  "
              f"{'偏离中轨':>8s}  {'量比':>6s}  {'带宽':>6s}  {'日均量':>10s}")
        print(f"  {'─'*4}  {'─'*6}  {'─'*14}  {'─'*8}  {'─'*8}  {'─'*6}  {'─'*6}  {'─'*10}")
        for i, b in enumerate(breakouts, 1):
            vol_str = f"{b['avg_vol']/10000:.0f}万"
            print(f"  {i:>4d}  {b['code']:>6s}  {b['name']:<14s}  "
                  f"{b['close']:>8.4f}  {b['pct_deviation']:>+7.2f}%  "
                  f"{b['vol_ratio']:>5.2f}x  {b['band_width']:>5.2f}%  {vol_str:>10s}")
        print(f"{'='*80}")
        print("\n  提示: 使用以下命令添加到候选池:")
        for b in breakouts:
            print(f"    python main.py add-etf --fund-code <联接基金> --name \"{b['name']}\" --etf-code {b['code']}")

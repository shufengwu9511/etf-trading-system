"""ETF 动量发现 — 扫描全市场ETF，找到未在候选池中的高动量候选

数据源: 本项目的通达信全市场ETF缓存 (data/tdx_cache/etf_cache.db)
  需先运行 D:/TDXTEST/etf_momentum.py 刷新缓存后复制到本项目目录

动量公式 (与 etf_momentum.py 一致):
  10日动量 × 0.6 + 30日动量 × 0.4

用法:
  from etf_discovery import discover_candidates
  candidates = discover_candidates(threshold=0.05, top_n=15)
"""
import os
import sqlite3
from datetime import date, timedelta

from config import CORE_ETFS, SATELLITE_ETFS

# ── 缓存路径 ──
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
ETF_CACHE_DB = os.path.join(_PROJECT_ROOT, "data", "tdx_cache", "etf_cache.db")

# ── 筛选参数 ──
MIN_AVG_VOLUME = 5_000_000      # 日均成交量下限 (过滤迷你ETF)
MIN_KLINES = 30                  # 至少需要30条K线
VOL_LOOKBACK = 20                # 成交量计算回溯天数

# ── 黑名单: ETF名称中包含这些关键词的跳过 ──
NAME_BLACKLIST = ["债券", "国债", "货币", "逆回购", "理财", "短融",
                  "转债", "REIT", "QDII", "标普", "纳指", "恒生",
                  "港股", "H股", "HK", "日经", "德国", "法国",
                  "互联", "海外", "跨境"]


def _get_existing_codes() -> set:
    """获取已存在于候选池的ETF代码（纯数字，无后缀）"""
    existing = set()
    for etf in CORE_ETFS + SATELLITE_ETFS:
        code = etf["code"]
        if "." in code:
            code = code.split(".")[0]
        existing.add(code)
    return existing


def _calc_momentum(closes: list, n: int) -> float | None:
    """计算n日动量: (最新收盘价 / n日前收盘价 - 1)"""
    if len(closes) < n + 1:
        return None
    return closes[-1] / closes[-(n + 1)] - 1


def _calc_composite(m10: float | None, m30: float | None) -> float | None:
    """复合得分: 10日 × 0.6 + 30日 × 0.4"""
    score = 0.0
    has_any = False
    if m10 is not None:
        score += m10 * 0.6
        has_any = True
    if m30 is not None:
        score += m30 * 0.4
        has_any = True
    return score if has_any else None


def _should_skip_name(name: str) -> bool:
    """检查ETF名称是否在黑名单中"""
    for kw in NAME_BLACKLIST:
        if kw in name:
            return True
    return False


def discover_candidates(
    momentum_threshold: float = 0.05,
    top_n: int = 15,
) -> list[dict]:
    """扫描TDXTEST缓存，发现高动量ETF候选

    Args:
        momentum_threshold: 动量阈值 (默认5%)
        top_n: 返回前N个候选

    Returns:
        [{code, name, score, m10, m30, avg_vol}, ...] 按得分降序
    """
    if not os.path.exists(ETF_CACHE_DB):
        print(f"  [DISCOVERY] 缓存不存在: {ETF_CACHE_DB}")
        print(f"  [DISCOVERY] 请先运行 D:/TDXTEST/etf_momentum.py 刷新全市场ETF数据，"
              f"然后将缓存复制到本项目 data/tdx_cache/ 目录")
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

        # 2. 获取所有不在候选池中的代码
        all_codes = list(name_map.keys())
        new_codes = [c for c in all_codes if c not in existing]

        if not new_codes:
            return []

        # 3. 批量获取K线数据（一次性查询，避免逐条查）
        placeholders = ",".join("?" for _ in new_codes)
        rows = conn.execute(
            f"SELECT code, date, close, volume FROM kline_data "
            f"WHERE code IN ({placeholders}) "
            f"ORDER BY code, date",
            new_codes,
        ).fetchall()

        # 4. 按代码分组K线
        from collections import defaultdict
        klines_by_code = defaultdict(list)
        for code, dt, close, vol in rows:
            klines_by_code[code].append((dt, close, vol))

        # 5. 逐个计算动量
        for code in new_codes:
            klines = klines_by_code[code]
            if len(klines) < MIN_KLINES:
                continue

            name = name_map.get(code, "")
            if _should_skip_name(name):
                continue

            closes = [k[1] for k in klines]
            volumes = [k[2] for k in klines]

            # 成交量过滤
            recent_vols = volumes[-VOL_LOOKBACK:] if len(volumes) >= VOL_LOOKBACK else volumes
            avg_vol = sum(recent_vols) / len(recent_vols)
            if avg_vol < MIN_AVG_VOLUME:
                continue

            # 动量计算
            m10 = _calc_momentum(closes, 10)
            m30 = _calc_momentum(closes, 30)
            score = _calc_composite(m10, m30)

            if score is not None and score >= momentum_threshold:
                candidates.append({
                    "code": code,
                    "name": name,
                    "score": round(score * 100, 2),       # 转百分比
                    "m10": round((m10 or 0) * 100, 2),
                    "m30": round((m30 or 0) * 100, 2),
                    "avg_vol": int(avg_vol),
                    "klines": len(klines),
                })

    finally:
        conn.close()

    # 按得分降序
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:top_n]


if __name__ == "__main__":
    candidates = discover_candidates(momentum_threshold=0.05, top_n=20)
    if not candidates:
        print("无符合条件的候选ETF")
    else:
        print(f"\n{'='*70}")
        print(f"  ETF 动量发现 — 未在候选池中的高动量ETF (共{len(candidates)}只)")
        print(f"{'='*70}")
        print(f"  {'排名':>4s}  {'代码':>6s}  {'名称':<14s}  {'10日':>8s}  {'30日':>8s}  {'得分':>8s}  {'日均量':>10s}")
        print(f"  {'─'*4}  {'─'*6}  {'─'*14}  {'─'*8}  {'─'*8}  {'─'*8}  {'─'*10}")
        for i, c in enumerate(candidates, 1):
            vol_str = f"{c['avg_vol']/10000:.0f}万"
            print(f"  {i:>4d}  {c['code']:>6s}  {c['name']:<14s}  "
                  f"{c['m10']:>+7.2f}%  {c['m30']:>+7.2f}%  "
                  f"{c['score']:>+7.2f}%  {vol_str:>10s}")
        print(f"{'='*70}")
        print("\n  提示: 使用以下命令添加到候选池 (需自行查找联接基金代码):")
        for c in candidates:
            print(f"    python main.py add-etf --fund-code <联接基金> --name \"{c['name']}\" --etf-code {c['code']}")

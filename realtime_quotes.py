#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""实时行情获取 (多渠道路由 + 优雅降级)

背景 (2026-09-24 排查):
    dashboard 原先只走 AkShare `fund_etf_spot_em()`, 其底层域名是
    `push2delay.eastmoney.com`。该域名在部分网络环境下被拦截/限流
    (表现为 RemoteDisconnected → 看板"实时行情"整块失败)。
    实测: push2delay 被拦时, fund.eastmoney.com / push2.eastmoney.com /
    腾讯 qt.gtimg.cn 均正常 → 属于单域名策略问题, 不是网络不通。

渠道设计:
    渠道A  候选池/持仓行情 —— 由 tdx_data.get_realtime_quotes() 内部路由。
           2026-10-23 起 pytdx 的 get_security_quotes 被服务端拒绝, 默认改为
           **新浪 hq.sinajs.cn 优先** (实测 21只/0.3s), 失败才回落 pytdx。
           具体渠道由 _last_quote_source 如实回报, 日志不再一律标成 pytdx。
    渠道B  东财 push2 → AkShare(push2delay) —— 全市场快照, 仅用于"实时恐慌"统计。
           受网络策略影响, 失败时实时恐慌显示 N/A, 不阻塞看板其余部分。

用法:
    from realtime_quotes import get_quotes
    rt = get_quotes()
    rt["quotes"]        # {code6: {'code','name','price','pct_chg','volume'}}
    rt["success"]       # 候选池拿到行情即为 True
    rt["market_wide"]   # 全市场快照是否可用 (决定恐慌指标是否有值)
"""
from __future__ import annotations

import time
from pathlib import Path

from config import CORE_ETFS, SATELLITE_ETFS

# 全市场 ETF 代码缓存 (由 etf_cache_updater 维护)
_ETF_CACHE_DB = Path(__file__).resolve().parent / "data" / "tdx_cache" / "etf_cache.db"

# 东财全市场 ETF 板块过滤 (与 AkShare fund_etf_spot_em 保持一致)
_EM_FS = "b:MK0021,b:MK0022,b:MK0023,b:MK0024,b:MK0827"
_EM_FIELDS = "f2,f3,f5,f6,f12,f14,f15,f16,f17,f18"
_EM_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Referer": "https://quote.eastmoney.com/",
}


def _watch_codes() -> list:
    """候选池 + 持仓 需要的场内代码 (去重保序)"""
    seen, out = set(), []
    for etf in list(CORE_ETFS) + list(SATELLITE_ETFS):
        code = etf.get("code")
        if code and code not in seen:
            seen.add(code)
            out.append(code)
    return out


def _name_map() -> dict:
    return {e["code"][:6]: e["name"] for e in list(CORE_ETFS) + list(SATELLITE_ETFS)}


# --------------------------------------------------------------------------
# 渠道A: pytdx —— 候选池 / 持仓
# --------------------------------------------------------------------------
_last_quote_source = ""   # tdx_data 回报的实际渠道: pytdx / sina / akshare


def _quotes_from_pytdx() -> dict:
    """候选池行情 (函数名保留历史, 实际渠道由 tdx_data 内部路由+回报)"""
    codes = _watch_codes()
    if not codes:
        return {}
    import tdx_data
    global _last_quote_source
    px = tdx_data.get_realtime_quotes(codes) or {}
    # 实际来源由 tdx_data 回报 (它内部有新浪/AkShare 兜底, 不能一律当作 pytdx)
    _last_quote_source = getattr(tdx_data, "_last_quote_source", "") or ""
    nm = _name_map()
    out = {}
    for ts_code, q in px.items():
        raw = str(ts_code).split(".")[0]
        if q.get("price") is None:
            continue
        out[raw] = {
            "code": raw,
            "name": nm.get(raw, ""),
            "price": q.get("price"),
            "pct_chg": q.get("change_pct", 0.0),
            "volume": 0.0,
        }
    return out


# --------------------------------------------------------------------------
# 渠道B: 全市场快照 —— 实时恐慌统计
# --------------------------------------------------------------------------
_SINA_HEADERS = {
    "User-Agent": _EM_HEADERS["User-Agent"],
    "Referer": "https://finance.sina.com.cn/",
}


def _market_spot_sina() -> list:
    """新浪 hq.sinajs.cn 批量行情 (代码列表取自 etf_cache.etf_list)

    单请求可带数百个代码 → 3 批覆盖沪市全部 ETF, 实测 ~0.3s。
    相比东财分页(17 次请求易触发限流), 这是最稳的全市场快照渠道。
    注意: etf_cache.etf_list 目前仅含上交所 ETF (深交所列表接口失效),
          故恐慌统计口径 = 沪市 ETF, 样本 900+ 只, 代表性足够。
    """
    try:
        import sqlite3
        import requests
    except ImportError:
        return []
    if not _ETF_CACHE_DB.exists():
        print("    [WARN] 未找到 etf_cache.db, 新浪渠道跳过")
        return []
    try:
        conn = sqlite3.connect(str(_ETF_CACHE_DB))
        rows = list(conn.execute("SELECT code, market FROM etf_list"))
        conn.close()
    except Exception as e:
        print(f"    [WARN] 新浪渠道读取ETF列表失败: {type(e).__name__}")
        return []
    if not rows:
        return []

    syms = [("sh" if int(m or 0) == 1 else "sz") + str(c) for c, m in rows]
    for trust_env in (False, True):
        sess = requests.Session()
        sess.trust_env = trust_env
        out = []
        try:
            for i in range(0, len(syms), 400):
                url = "https://hq.sinajs.cn/list=" + ",".join(syms[i:i + 400])
                resp = sess.get(url, headers=_SINA_HEADERS, timeout=12)
                resp.encoding = "gbk"
                for line in resp.text.split("\n"):
                    if '"' not in line or "hq_str_" not in line:
                        continue
                    try:
                        f = line.split('"')[1].split(",")
                        if len(f) < 10:
                            continue
                        cur, prev = float(f[3]), float(f[2])
                        if cur <= 0 or prev <= 0:
                            continue
                        code = line.split("hq_str_")[1].split("=")[0][2:]
                        out.append({
                            "code": code,
                            "name": f[0],
                            "price": cur,
                            "pct_chg": round((cur / prev - 1) * 100, 2),
                            "volume": float(f[8] or 0),
                        })
                    except (ValueError, IndexError):
                        continue
        except Exception as e:
            print(f"    [WARN] 新浪行情(trust_env={trust_env}) 失败: {type(e).__name__}")
            continue
        if out:
            return out
    return []


def _parse_em_rows(rows) -> list:
    out = []
    for r in rows or []:
        code = str(r.get("f12") or "").zfill(6)
        price = r.get("f2")
        if (not code) or isinstance(price, bool) \
                or not isinstance(price, (int, float)) or price <= 0:
            continue
        out.append({
            "code": code,
            "name": r.get("f14") or "",
            "price": float(price),
            "pct_chg": float(r.get("f3") or 0),
            "volume": float(r.get("f5") or 0),
        })
    return out


def _market_spot_push2() -> list:
    """东财 push2 分页拉全市场; 数据不完整(<90%)则丢弃, 避免统计失真"""
    try:
        import requests
    except ImportError:
        return []
    for trust_env in (False, True):
        sess = requests.Session()
        sess.trust_env = trust_env      # 先直连, 再不济走环境代理
        rows_all, total = [], None
        try:
            for pn in range(1, 21):
                url = ("https://push2.eastmoney.com/api/qt/clist/get"
                       f"?pn={pn}&pz=100&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281"
                       f"&fltt=2&invt=2&fid=f12&fs={_EM_FS}&fields={_EM_FIELDS}")
                data = (sess.get(url, headers=_EM_HEADERS, timeout=10).json() or {}).get("data") or {}
                if total is None:
                    total = data.get("total") or 0
                diff = data.get("diff") or []
                if not diff:
                    break
                rows_all.extend(_parse_em_rows(diff))
                if len(diff) < 100:
                    break
                time.sleep(0.08)
        except Exception as e:
            print(f"    [WARN] 东财push2(trust_env={trust_env}) 失败: {type(e).__name__}")
        if total and len(rows_all) >= total * 0.9:
            return rows_all
        if rows_all:
            print(f"    [WARN] 东财push2 数据不完整 {len(rows_all)}/{total}, 已丢弃")
    return []


def _market_spot_akshare() -> list:
    """AkShare fund_etf_spot_em (push2delay)"""
    try:
        import akshare as ak
        df = ak.fund_etf_spot_em()
    except Exception as e:
        print(f"    [WARN] AkShare 全市场快照不可用: {type(e).__name__}")
        return []
    if df is None or getattr(df, "empty", True):
        return []
    out = []
    for _, row in df.iterrows():
        try:
            price = float(row["最新价"])
        except (TypeError, ValueError, KeyError):
            continue
        if price <= 0:
            continue
        out.append({
            "code": str(row["代码"]).zfill(6),
            "name": row.get("名称") or "",
            "price": price,
            "pct_chg": float(row.get("涨跌幅") or 0),
            "volume": float(row.get("成交量") or 0),
        })
    return out


def _market_spot() -> list:
    """全市场快照: 新浪(快/稳) → 东财push2 → AkShare, 全失败返回 []"""
    for fn in (_market_spot_sina, _market_spot_push2, _market_spot_akshare):
        rows = fn()
        if rows:
            print(f"    [OK] {fn.__name__} 全市场快照 {len(rows)} 只")
            return rows
    return []


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------
def get_quotes() -> dict:
    """实时行情: 候选池 pytdx 主渠道 + 全市场快照可选增强

    候选池行情拿到即 success=True (实时行情表/实时动量可用);
    全市场快照不可用时 panic_score 为 None, 看板显示 N/A。
    """
    quotes, sources = {}, []

    # 渠道A
    try:
        q = _quotes_from_pytdx()
        if q:
            quotes.update(q)
            src = _last_quote_source or "pytdx"
            label = {"pytdx": "pytdx", "sina": "新浪",
                     "akshare": "AkShare"}.get(src, src)
            sources.append(f"{label} {len(q)}只")
    except Exception as e:
        print(f"    [WARN] pytdx 候选池行情失败: {type(e).__name__}: {e}")

    # 渠道B
    market = _market_spot()
    if market:
        for r in market:
            quotes.setdefault(r["code"], r)
        sources.append(f"全市场 {len(market)}只")

    if not quotes:
        msg = "所有实时行情渠道均不可用"
        print(f"    [WARN] {msg}")
        return {
            "quotes": {}, "panic_score": None,
            "down_count": None, "down_ratio": None, "avg_pct": None,
            "update_time": None, "success": False,
            "source": "", "market_wide": False, "error": msg,
        }

    # 实时恐慌: 必须基于全市场, 候选池样本无代表性
    if market:
        pcts = [r["pct_chg"] for r in market]
        down_count = sum(1 for p in pcts if p < 0)
        down_ratio = down_count / len(pcts) if pcts else 0.0
        avg_pct = sum(pcts) / len(pcts) if pcts else 0.0
        panic_score = round(min(max(down_ratio * 70 + min(max(avg_pct * -3, 0), 30), 0), 100), 1)
        stats = {
            "down_count": down_count,
            "down_ratio": round(down_ratio * 100, 1),
            "avg_pct": round(avg_pct, 2),
        }
    else:
        panic_score = None
        stats = {"down_count": None, "down_ratio": None, "avg_pct": None}

    print(f"    [OK] 实时行情来源: {', '.join(sources)}")
    return {
        "quotes": quotes,
        "panic_score": panic_score,
        **stats,
        "update_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "success": True,
        "source": ", ".join(sources),
        "market_wide": bool(market),
    }


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    rt = get_quotes()
    print("--- 概要 ---")
    for k, v in rt.items():
        if k != "quotes":
            print(f"  {k:14} = {v}")
    nm = _name_map()
    print("--- 候选池行情 ---")
    for c, q in rt["quotes"].items():
        if c in nm:
            print(f"  {nm[c]:<18} {c}  {q['price']:>8}  {q['pct_chg']:+.2f}%")

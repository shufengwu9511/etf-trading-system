# -*- coding: utf-8 -*-
"""
通达信(pytdx)行情服务器可用性扫描

背景: 通达信老主站会退化为"TCP能连但不给行情数据"的僵尸节点, 仅测 connect 会误判。
     本脚本对每台服务器依次验证 3 层:
       1) TCP connect 成功
       2) get_security_quotes() 返回行情
       3) get_security_bars() 返回K线
     三层全过才判定可用, 并按总延迟排序, 产出优选列表建议。

用法:
    python scripts/tdx_server_check.py                # 扫描 pytdx 内置全量 + 当前优选
    python scripts/tdx_server_check.py --external     # 额外并入外部候选池(data/tdx_external_hosts.json)
    python scripts/tdx_server_check.py --timeout 1.5  # 自定义单台超时(秒)
    python scripts/tdx_server_check.py --top 8        # 只输出前N名
    python scripts/tdx_server_check.py --workers 24   # 并发数
结果: 打印排名表 + 写入 logs/tdx_servers_<日期>.json

外部候选池: data/tdx_external_hosts.json (来源 mootdx / ze-quant, 2026-10-14 实测均不可用,
           保留备用, 待 pytdx 分支更新后可用 --external 复测)
"""
import os
import sys
import json
import time
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pytdx.hq import TdxHq_API
from pytdx.config.hosts import hq_hosts

# 深市/沪市各一只ETF, 用于验证行情与K线
TEST_QUOTES = [(0, "159934"), (1, "510310")]      # 黄金ETF(深) / 沪深300ETF(沪)
TEST_BAR_MARKET, TEST_BAR_CODE = 0, "159934"
BAR_CATEGORY_DAY = 9                               # pytdx 日K category

# 当前脚本优选列表 (用于对照; 与 tdx_data._PREFERRED_SERVERS 保持一致)
# 2026-10-14 全量复测: 内置103台仅这8台可用(全为国泰君安), 安信 59.36.5.11 已变僵尸节点
CURRENT_PREFERRED = [
    ("国泰君安1", "117.34.114.13", 7709),
    ("国泰君安2", "117.34.114.14", 7709),
    ("国泰君安3", "117.34.114.15", 7709),
    ("国泰君安4", "117.34.114.16", 7709),
    ("国泰君安5", "117.34.114.17", 7709),
    ("国泰君安6", "117.34.114.18", 7709),
    ("国泰君安7", "117.34.114.20", 7709),
    ("国泰君安8", "117.34.114.27", 7709),
]

# 外部候选池文件 (来源 mootdx / ze-quant, 非 pytdx 内置)
EXTERNAL_HOSTS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "tdx_external_hosts.json")


def load_external_hosts():
    """加载外部候选池, 返回 [(name, ip, port), ...]"""
    if not os.path.exists(EXTERNAL_HOSTS_FILE):
        print(f"  [警告] 外部候选池不存在: {EXTERNAL_HOSTS_FILE}")
        return []
    with open(EXTERNAL_HOSTS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [(h["name"], h["ip"], int(h["port"])) for h in data.get("hosts", [])]


def probe(name, ip, port, timeout=2.0):
    """探测单台服务器: connect → quotes → bars, 返回结果 dict"""
    r = {"name": name, "ip": ip, "port": port, "ok": False,
         "connect_ms": None, "quote_ms": None, "bars_ms": None, "reason": ""}
    api = TdxHq_API()
    try:
        t0 = time.time()
        if not api.connect(ip, port, time_out=timeout):
            r["reason"] = "connect_fail"
            return r
        r["connect_ms"] = round((time.time() - t0) * 1000)

        t1 = time.time()
        quotes = api.get_security_quotes(TEST_QUOTES)
        if not quotes:
            r["reason"] = "no_quotes(僵尸节点)"
            return r
        r["quote_ms"] = round((time.time() - t1) * 1000)

        t2 = time.time()
        bars = api.get_security_bars(BAR_CATEGORY_DAY, TEST_BAR_MARKET,
                                     TEST_BAR_CODE, 0, 5)
        if not bars:
            r["reason"] = "no_bars"
            return r
        r["bars_ms"] = round((time.time() - t2) * 1000)

        r["ok"] = True
        r["total_ms"] = r["connect_ms"] + r["quote_ms"] + r["bars_ms"]
    except Exception as e:
        r["reason"] = type(e).__name__
    finally:
        try:
            api.disconnect()
        except Exception:
            pass
    return r


def build_candidates(include_external=False):
    """合并 pytdx 内置全量 + 当前优选 (+ 可选外部池), 按 (ip, port) 去重"""
    pool = list(hq_hosts) + CURRENT_PREFERRED
    if include_external:
        pool += load_external_hosts()
    seen, cands = set(), []
    for name, ip, port in pool:
        key = (ip, port)
        if key in seen:
            continue
        seen.add(key)
        cands.append((name, ip, port))
    return cands


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=2.0)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--workers", type=int, default=20)
    ap.add_argument("--external", action="store_true",
                    help="并入外部候选池 data/tdx_external_hosts.json")
    args = ap.parse_args()

    cands = build_candidates(include_external=args.external)
    print("=" * 78)
    print(f"  通达信服务器可用性扫描  ({datetime.now():%Y-%m-%d %H:%M:%S})")
    print(f"  候选 {len(cands)} 台 | 单台超时 {args.timeout}s | 并发 {args.workers}"
          + (" | 含外部池" if args.external else ""))
    print("  判据: connect + 行情 + K线 三层全过")
    print("=" * 78)

    results = []
    done = 0
    t_start = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(probe, n, ip, p, args.timeout): (n, ip, p)
                   for n, ip, p in cands}
        for fut in as_completed(futures):
            results.append(fut.result())
            done += 1
            if done % 20 == 0:
                print(f"  ...已测 {done}/{len(cands)}")

    ok = [r for r in results if r["ok"]]
    ok.sort(key=lambda x: x["total_ms"])
    fail = [r for r in results if not r["ok"]]

    print()
    print("-" * 78)
    print(f"  可用 {len(ok)} 台 / 共 {len(cands)} 台    (耗时 {time.time()-t_start:.0f}s)")
    print("-" * 78)
    if ok:
        print(f"  {'#':>3}  {'名称':<18}{'IP':<17}{'端口':>5}{'连接':>7}{'行情':>7}{'K线':>7}{'合计':>7}")
        for i, r in enumerate(ok[:args.top], 1):
            print(f"  {i:>3}  {r['name'][:18]:<18}{r['ip']:<17}{r['port']:>5}"
                  f"{r['connect_ms']:>6}ms{r['quote_ms']:>6}ms{r['bars_ms']:>6}ms{r['total_ms']:>6}ms")

    # 当前优选列表实测结果
    print()
    print("-" * 78)
    print("  当前 _PREFERRED_SERVERS 实测:")
    for name, ip, port in CURRENT_PREFERRED:
        hit = next((r for r in results if r["ip"] == ip and r["port"] == port), None)
        if hit and hit["ok"]:
            flag = "✅"
            tail = f"{hit['total_ms']}ms"
        elif hit:
            flag = "❌"
            tail = hit["reason"]
        else:
            flag, tail = "?", "未测"
        print(f"    {flag} {name:<12} {ip}:{port:<5} {tail}")

    # 建议的新优选列表 (取可用前5)
    suggested = [(r["name"], r["ip"], r["port"]) for r in ok[:5]]
    print()
    print("-" * 78)
    print("  建议 _PREFERRED_SERVERS (可用前5, 按延迟):")
    print("_PREFERRED_SERVERS = [")
    for name, ip, port in suggested:
        print(f'    ("{name}", "{ip}", {port}),')
    print("]")

    # 失败原因统计
    if fail:
        from collections import Counter
        cnt = Counter(r["reason"] for r in fail)
        print()
        print(f"  不可用 {len(fail)} 台, 原因分布: " +
              ", ".join(f"{k}×{v}" for k, v in cnt.most_common()))

    # 落盘
    os.makedirs("logs", exist_ok=True)
    out = os.path.join("logs", f"tdx_servers_{datetime.now():%Y%m%d_%H%M}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "scanned_at": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
            "total": len(cands), "available": len(ok),
            "ranked": ok, "failed": fail,
            "suggested_preferred": [{"name": n, "ip": i, "port": p} for n, i, p in suggested],
            "current_preferred": [{"name": n, "ip": i, "port": p} for n, i, p in CURRENT_PREFERRED],
        }, f, ensure_ascii=False, indent=2)
    print()
    print(f"  结果已保存: {out}")
    print("=" * 78)

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""
易方达财富 持仓同步 / 对账

功能:
  1. 驱动本机 Chrome 打开易方达财富, 由用户微信扫码登录
  2. 读取「我的资产」页, 解析每只基金的 份额 / 市值 / 盈亏
  3. 与本地数据库 portfolio 表对账, 输出差异清单

==================== 安全铁律 (禁止修改) ====================
1. 只做: 打开页面 / 读取DOM / 截图 / 本地对账
2. 禁止: 填写任何输入框 / 提交任何表单 / 点击任何交易类按钮
3. 不使用任何密码凭据, 登录由用户本人在浏览器窗口内手动完成
4. 默认不修改数据库 (需显式 --apply 才写库, 且只改份额/净值)
============================================================

用法:
    .venv/Scripts/python.exe scripts/efunds_sync.py            # 抓取并输出对账报告
    .venv/Scripts/python.exe scripts/efunds_sync.py --reparse   # 用已保存的HTML重新解析(免登录)
"""
import sys
import re
import json
import time
import os
import sqlite3
import argparse
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LOGDIR = ROOT / "logs"
PROFILE = ROOT / "data" / "chrome_auto_profile"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
HOME_URL = "https://e.efunds.com.cn/"
ASSETS_URL = "https://e.efunds.com.cn/assets"
RECORDS_URL = "https://e.efunds.com.cn/transaction/records"

# 不纳入策略视野的账户 (大树 2026-09-15 确认: 不追踪)
#   000621 易方达现金增利货币B = 现金账户, 等价于可用现金, 不是策略持仓
#   017253 个人养老金账户, 锁定不能自由申赎, 与ETF轮动策略无关
IGNORE_CODES = {
    "000621": "现金账户(货币基金)",
    "017253": "个人养老金",
}
# 货币基金账户 = 账户真实现金, 同步时单独写入 data/account_cash.json
CASH_ACCOUNT_CODES = {"000621"}
# 名称兜底 (防止换代码)
IGNORE_NAME_KEYS = ("货币", "养老")
DB_PATH = ROOT / "data" / "trading_system.db"

HTML_CACHE = LOGDIR / "efunds_assets.html"
RECORDS_CACHE = LOGDIR / "efunds_records.html"
JSON_OUT = LOGDIR / "efunds_holdings.json"

LOGIN_OK_HINTS = ["退出", "我的资产", "总资产", "安全退出"]

LOGDIR.mkdir(exist_ok=True)


# ---------------- 解析 ----------------
JS_EXTRACT = """
() => {
  const out = [];
  document.querySelectorAll('div.fundAssetsInfo[data-fundcodes]').forEach(div => {
    const code = (div.getAttribute('data-fundcodes') || '').trim();
    if (!/^\\d{6}$/.test(code)) return;
    const totalShares = div.getAttribute('data-totalshares');
    // 页面里第一个 a.a-fundName 是空标签, 真名在 <h3> 里那个 -> 取最长的
    let name = '';
    div.querySelectorAll('a.a-fundName').forEach(a => {
      const t = ((a.textContent || '').replace(/\\s+/g, ' ')).trim();
      if (t.length > name.length) name = t;
    });
    name = name.replace(/\\d{6}$/, '').trim();
    const kv = {};
    div.querySelectorAll('.g-box').forEach(box => {
      const l = box.querySelector('.label');
      const d = box.querySelector('.data');
      if (!l || !d) return;
      const key = (l.innerText || '').replace(/\\s+/g, '').trim();
      const val = (d.innerText || '').replace(/\\s+/g, ' ').trim();
      if (key) kv[key] = val;
    });
    out.push({ code: code, name: name, total_shares: totalShares, kv: kv });
  });
  return out;
}
"""


JS_RECORDS = """
() => {
  const rows = [];
  document.querySelectorAll('table tr').forEach(tr => {
    const tds = Array.from(tr.querySelectorAll('td,th'))
      .map(td => (td.textContent || '').replace(/\\s+/g, ' ').trim());
    if (tds.length >= 3) rows.push(tds);
  });
  if (!rows.length) {
    // 非表格结构: 抓所有含日期的行
    document.querySelectorAll('li,div[class*=record],div[class*=list-item]').forEach(n => {
      const t = (n.textContent || '').replace(/\\s+/g, ' ').trim();
      if (/20\\d{2}-\\d{2}-\\d{2}/.test(t) && t.length < 300) rows.push([t]);
    });
  }
  return rows.slice(0, 60);
}
"""


def parse_number(s):
    """从字符串里抽数字: 1,234.56 -> 1234.56"""
    if s is None:
        return None
    m = re.search(r"-?[\d,]+\.?\d*", str(s).replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def parse_rows(rows):
    """从每只基金的 label/data 键值对里抽取字段"""
    result = []
    for r in rows:
        kv = r.get("kv") or {}
        shares = parse_number(r.get("total_shares")) or parse_number(kv.get("持有份额"))
        result.append({
            "fund_code": r["code"],
            "name": r["name"],
            "shares": shares,
            "available_shares": parse_number(kv.get("可用份额")),
            "market_value": parse_number(kv.get("最新市值") or kv.get("参考市值")),
            "nav": parse_number(kv.get("最新净值") or kv.get("单位净值")),
            "nav_date": kv.get("净值日期"),
            "profit": parse_number(kv.get("累计收益")),
            "day_profit": parse_number(kv.get("当日收益")),
            "profit_pct": parse_number(kv.get("当日涨跌")),
            "ratio": parse_number(kv.get("资产占比")),
            "raw": json.dumps(kv, ensure_ascii=False)[:400],
        })
    return result


def load_local():
    """读取本地持仓"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT fund_code, name, shares, cost_nav, current_nav,
               total_cost, market_value, profit_pct, nav_date, status
        FROM portfolio
        WHERE status IN ('holding','pending_buy')
    """).fetchall()
    conn.close()
    return {r["fund_code"]: dict(r) for r in rows}


def load_local_trades():
    """读取本地交易流水"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, fund_code, trade_type, amount, shares, nav,
               submit_date, confirm_date, status, remark
        FROM trades
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def build_name_map():
    """系统基金名 -> 代码, 并抽出用于模糊匹配的关键词"""
    import config
    mapping = []
    seen = set()
    pools = []
    for attr in ("CORE_ETFS", "SATELLITE_ETFS"):
        pools.extend(getattr(config, attr, []) or [])
    for e in pools:
        code = str(e.get("fund_code", ""))
        name = e.get("name", "")
        if not code or not name or code in seen:
            continue
        seen.add(code)
        # 关键词: 去掉 ETF联接A / ETF / 联接 等通用后缀
        key = re.sub(r"(ETF联接[ABCDE]?|ETF|联接[ABCDE]?|发起式)", "", name).strip()
        mapping.append({"code": code, "name": name, "key": key or name})
    # 低费率替代份额: 关键词更长, match_fund_code 取最长匹配,
    # 平台全称带「联接C/联接E」后缀时优先命中新代码, 否则会错配回老 A 类代码
    for alt_code, alias in getattr(config, "BUY_FUND_ALIASES", {}).items():
        if alt_code not in seen:
            seen.add(alt_code)
            mapping.append({"code": alt_code, "name": alias["name"], "key": alias["key"]})
    return mapping


def match_fund_code(platform_name, name_map):
    """平台全称 -> 系统代码 (关键词包含匹配, 取最长匹配)"""
    best, best_len = None, 0
    for m in name_map:
        if m["key"] and m["key"] in platform_name and len(m["key"]) > best_len:
            best, best_len = m["code"], len(m["key"])
    return best


def parse_records(recs):
    """平台交易记录行 -> 结构化"""
    out = []
    for cells in recs:
        if len(cells) < 6:
            continue
        if cells[0] in ("名称", "基金名称") or "交易类型" in cells:
            continue
        name, ctime, ttype = cells[0], cells[1], cells[2]
        shares_s, amount_s = cells[3], cells[4]
        status = cells[5] if len(cells) > 5 else ""
        if not re.search(r"20\d{2}-\d{2}-\d{2}", ctime or ""):
            continue
        out.append({
            "name": name.strip(),
            "date": ctime.strip()[:10].replace("-", ""),
            "datetime": ctime.strip(),
            "type": ttype.strip(),
            "shares": parse_number(shares_s),
            "amount": parse_number(amount_s),
            "status": status.strip(),
        })
    return out


TYPE_MAP = {"申购": "buy", "赎回": "sell", "红利再投资": "dividend"}


# 对账容差(自然日)。平台日期是提交日, 系统有提交日/确认日两个口径。
DATE_TOLERANCE_DAYS = 2
# 平台「已撤销」绝不能入账(2026-10-13 半导体 30 万已撤销, 照抄会凭空多记成本)
INVALID_STATUS_KW = ("撤销", "失败", "废单", "已取消")
# 份额/金额尚未确认, 不参与匹配, 单独列出提醒
PENDING_STATUS_KW = ("处理中", "确认中", "已受理", "待确认", "受理中")


def _to_date(s):
    """'20261111' / '2026-11-11 14:40' -> date; 解析失败返回 None"""
    s = str(s or "").strip()[:10].replace("-", "").replace("/", "")
    if len(s) < 8:
        return None
    try:
        return datetime.strptime(s[:8], "%Y%m%d").date()
    except ValueError:
        return None


def _positive(v):
    if isinstance(v, str):
        v = v.replace(",", "").strip()
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _value_match(r, t, sys_type):
    """
    金额/份额二次校验, 返回 True=吻合 / False=明确不吻合 / None=平台无可比数据。
    平台赎回「金额」列常为 --, 只能比份额; 平台申购「份额」列填的是金额, 只能比金额。
    故按交易类型选主校验维度, 避免拿毛额/净额口径差异制造假警报。
    """
    checks = []
    if sys_type == "buy":
        pa, pb = _positive(r.get("amount")), _positive(t.get("amount"))
        if pa and pb:
            checks.append(abs(pa - pb) <= max(0.02, abs(pb) * 0.0005))
    else:
        ps, ts = _positive(r.get("shares")), _positive(t.get("shares"))
        if ps and ts:
            checks.append(abs(ps - ts) <= max(0.02, abs(ts) * 0.0005))
        # 赎回若平台给了金额, 补一次宽松校验(兼容毛额/净额差异)
        pa, pb = _positive(r.get("amount")), _positive(t.get("amount"))
        if pa and pb:
            checks.append(abs(pa - pb) <= pb * 0.02)
    if not checks:
        return None
    return any(checks)


def reconcile_trades(records, name_map, local_trades):
    """
    平台流水 vs 系统流水: 找出系统漏记的交易。

    2026-11-12 重写。旧实现三个缺陷:
      1. abs(int(YYYYMMDD) - int(YYYYMMDD)) <= 2 把日期当整数减 → 跨月(10-31 vs 11-01=70)/跨年全失效
      2. 同一条系统流水可被多笔平台记录重复认领 → 11-11 沪深300赎回被误匹配到
         id=103(confirm 11-09, 差 2 天), 对账报「漏记 0 笔」, 实际漏 1 笔
      3. 只看日期, 同代码同类型多笔挤在 2 天内会错配
    现改: 真日期差 + 一对一认领(used) + 金额/份额二次校验 + 状态分流。
    """
    missing, cash_moves, pending, weak = [], [], [], []
    matched = 0
    used = set()
    for r in records:
        status = (r.get("status") or "").strip()
        if any(k in status for k in INVALID_STATUS_KW):
            continue
        if any(k in status for k in PENDING_STATUS_KW):
            pending.append(r)
            continue
        # 货币基金每日红利再投资: 系统不跟踪, 静音跳过
        if "货币" in r["name"] and r["type"] == "红利再投资":
            continue
        sys_type = TYPE_MAP.get(r["type"])
        if sys_type is None:
            if r["type"] in ("转出", "转入", "快速取现", "普通取现"):
                cash_moves.append(r)
            continue
        code = match_fund_code(r["name"], name_map)
        if code is None:
            missing.append({**r, "code": "?", "reason": "系统无此基金(未纳入候选池)"})
            continue

        rdate = _to_date(r.get("date"))
        candidates = []
        for t in local_trades:
            if t.get("id") in used or t["fund_code"] != code or t["trade_type"] != sys_type:
                continue
            diffs = []
            for dfield in ("submit_date", "confirm_date"):
                td = _to_date(t.get(dfield))
                if td and rdate:
                    diffs.append(abs((rdate - td).days))
            if not diffs or min(diffs) > DATE_TOLERANCE_DAYS:
                continue
            v = _value_match(r, t, sys_type)
            # 优先:「金额/份额吻合」>「仅日期近」; 其次日期差更小
            candidates.append((0 if v is not False else 1, min(diffs), t.get("id") or 0, t, v))

        if not candidates:
            missing.append({**r, "code": code, "reason": f"系统无对应{trade_type_cn(sys_type)}记录"})
            continue
        candidates.sort(key=lambda x: (x[0], x[1], x[2]))
        rank, dd, _, best, v = candidates[0]
        if rank == 1:
            missing.append({**r, "code": code,
                            "reason": f"日期相近({dd}天)但金额/份额不符 — 疑似漏记, 请人工核对"})
            continue
        used.add(best.get("id"))
        matched += 1
        if v is None:
            weak.append(r)
    return missing, matched, cash_moves, pending, weak


def trade_type_cn(t):
    return {"buy": "申购", "sell": "赎回", "dividend": "分红"}.get(t, t)


def reconcile(remote, local):
    """对账: 返回差异清单"""
    diffs = []
    remote_codes = {r["fund_code"] for r in remote}

    for r in remote:
        code = r["fund_code"]
        loc = local.get(code)
        # 份额为0 = 已清仓残留条目, 系统也没有 -> 噪音, 跳过
        if loc is None and (r.get("shares") or 0) == 0:
            continue
        if loc is None:
            diffs.append({
                "type": "平台有/系统无",
                "code": code, "name": r["name"],
                "remote_shares": r["shares"], "local_shares": None,
                "remote_mv": r["market_value"], "local_mv": None,
                "note": "系统未跟踪(可能是货币基金/养老金或漏记)",
            })
            continue
        rs, ls = r["shares"], loc["shares"]
        rmv, lmv = r["market_value"], loc["market_value"]
        share_diff = (rs - ls) if (rs is not None and ls is not None) else None
        mv_diff = (rmv - lmv) if (rmv is not None and lmv is not None) else None
        # 份额容差 0.02 份 (平台显示2位小数, 四舍五入差异)
        need = share_diff is None or abs(share_diff) > 0.02
        if need:
            diffs.append({
                "type": "份额不符" if share_diff is not None else "无法解析份额",
                "code": code, "name": r["name"],
                "remote_shares": rs, "local_shares": ls,
                "remote_mv": rmv, "local_mv": lmv,
                "share_diff": share_diff, "mv_diff": mv_diff,
            })

    for code, loc in local.items():
        if code not in remote_codes:
            diffs.append({
                "type": "系统有/平台无",
                "code": code, "name": loc["name"],
                "remote_shares": None, "local_shares": loc["shares"],
                "remote_mv": None, "local_mv": loc["market_value"],
                "note": "系统持仓在平台已不存在(可能已清仓未记录)",
            })
    return diffs


def split_ignored(remote):
    """分离: 策略持仓 vs 不追踪账户(货币基金/养老金)"""
    keep, ignored = [], []
    for r in remote:
        code = r["fund_code"]
        nm = r.get("name") or ""
        reason = IGNORE_CODES.get(code)
        if not reason and any(k in nm for k in IGNORE_NAME_KEYS):
            reason = "货币基金/养老金(按名称)"
        if reason or (r.get("market_value") == 0 and r.get("shares") == 0):
            ignored.append({**r, "reason": reason or "已清仓(份额0)"})
        else:
            keep.append(r)
    return keep, ignored


def report(remote, local, diffs, ignored=None):
    print("\n" + "=" * 78)
    print("  持仓对账报告 (策略持仓)")
    print("=" * 78)
    print(f"{'基金':<22}{'代码':<8}{'平台份额':>13}{'系统份额':>13}{'差异':>11}{'平台市值':>13}{'净值日期':>12}")
    print("-" * 92)
    for r in sorted(remote, key=lambda x: -(x["market_value"] or 0)):
        code = r["fund_code"]
        loc = local.get(code)
        rs = f"{r['shares']:,.2f}" if r["shares"] is not None else "?"
        ls = f"{loc['shares']:,.2f}" if loc else "—"
        if loc and r["shares"] is not None:
            d = r["shares"] - loc["shares"]
            ds = f"{d:+,.2f}" if abs(d) > 0.02 else "✓"
        else:
            ds = "—"
        nm = (r["name"] or "?")[:20]
        mv = f"¥{r['market_value']:,.0f}" if r["market_value"] is not None else "—"
        nd = (r.get("nav_date") or "—")[-5:]
        print(f"{nm:<22}{code:<8}{rs:>13}{ls:>13}{ds:>11}{mv:>13}{nd:>12}")
    print("-" * 92)

    if diffs:
        print(f"\n  ⚠️  发现 {len(diffs)} 处差异:")
        for d in diffs:
            print(f"\n   [{d['type']}] {d['name']} ({d['code']})")
            if d["remote_shares"] is not None or d["local_shares"] is not None:
                rs = f"{d['remote_shares']:,.2f}" if d["remote_shares"] is not None else "—"
                ls = f"{d['local_shares']:,.2f}" if d["local_shares"] is not None else "—"
                print(f"      份额: 平台 {rs}  vs  系统 {ls}")
            if d.get("share_diff") is not None:
                print(f"      份额差: {d['share_diff']:+,.2f}")
            if d["remote_mv"] is not None or d["local_mv"] is not None:
                rm = f"¥{d['remote_mv']:,.0f}" if d["remote_mv"] is not None else "—"
                lm = f"¥{d['local_mv']:,.0f}" if d["local_mv"] is not None else "—"
                print(f"      市值: 平台 {rm}  vs  系统 {lm}")
            if d.get("note"):
                print(f"      备注: {d['note']}")
    else:
        print("\n  ✅ 平台持仓与系统完全一致")

    if ignored:
        print("  ⚪ 不追踪账户 (已排除, 大树确认不纳入策略):")
        for g in ignored:
            mv = f"¥{g['market_value']:,.0f}" if g.get("market_value") else "—"
            print(f"      {g['fund_code']}  {mv:>12}  {g.get('reason','')}")
    print()


# ---------------- 抓取 ----------------
def fetch():
    from playwright.sync_api import sync_playwright

    print("=" * 78)
    print("  易方达财富 持仓同步")
    print("  安全模式: 不填表 / 不提交 / 不点交易按钮")
    print("=" * 78)

    # Chromium 在 Windows 上读的是「系统代理设置」, 不受 HTTP_PROXY 环境变量控制。
    # 2026-11-12: 沙箱代理切到 127.0.0.1:55121 后, e.efunds.com.cn 报
    # ERR_PROXY_CONNECTION_FAILED, 而直连实测 HTTP 200, 故默认强制直连。
    # 确实需要走代理时设 EFUNDS_USE_PROXY=1 关闭本开关。
    chrome_args = ["--disable-blink-features=AutomationControlled"]
    if not os.environ.get("EFUNDS_USE_PROXY"):
        chrome_args.append("--no-proxy-server")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            executable_path=CHROME,
            headless=False,
            viewport={"width": 1600, "height": 1000},
            args=chrome_args,
            ignore_https_errors=True,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        print(f"\n  [1] 打开首页")
        try:
            page.goto(HOME_URL, timeout=45000, wait_until="domcontentloaded")
        except Exception as e:
            print(f"  [WARN] {type(e).__name__}: {e}")
        page.wait_for_timeout(2000)

        # 直接试资产页, 未登录会跳转
        print("  [2] 尝试访问资产页...")
        page.goto(ASSETS_URL, timeout=45000, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)

        def logged_in():
            try:
                body = page.inner_text("body")
            except Exception:
                return False
            return any(w in body for w in LOGIN_OK_HINTS) and "请登录" not in body

        if not logged_in():
            print("\n  [3] 需要登录 → 请在弹出的 Chrome 窗口中用微信扫码")
            deadline = time.time() + 300
            ok = False
            while time.time() < deadline:
                time.sleep(3)
                if logged_in():
                    ok = True
                    break
                try:
                    if page.url.startswith(ASSETS_URL) and "我的资产" in (page.title() or ""):
                        ok = True
                        break
                except Exception:
                    pass
            if not ok:
                print("  [TIMEOUT] 未检测到登录, 退出")
                page.screenshot(path=str(LOGDIR / "sync_timeout.png"), full_page=True)
                ctx.close()
                return None
            print("  [OK] 登录成功")
        else:
            print("  [OK] 已有登录态")

        # 确保落在资产页, 并等持仓列表渲染完成
        if not page.url.startswith(ASSETS_URL):
            page.goto(ASSETS_URL, timeout=45000, wait_until="domcontentloaded")
        try:
            page.wait_for_selector("div.fundAssetsInfo[data-fundcodes]", timeout=40000)
            print("  [OK] 持仓列表已渲染")
        except Exception:
            print("  [WARN] 未等到持仓列表元素, 尝试继续")
        page.wait_for_timeout(3000)
        page.screenshot(path=str(LOGDIR / "sync_assets.png"), full_page=True)

        html = page.content()
        HTML_CACHE.write_text(html, encoding="utf-8")
        print(f"  [OK] 页面已保存: {HTML_CACHE} ({len(html)} 字符)")

        rows = page.evaluate(JS_EXTRACT)
        print(f"  [OK] 解析到 {len(rows)} 只基金")

        # 交易记录 (只读, 用于确认赎回/申购日期与金额)
        rec_rows = []
        try:
            page.goto(RECORDS_URL, timeout=45000, wait_until="domcontentloaded")
            page.wait_for_timeout(3500)
            RECORDS_CACHE.write_text(page.content(), encoding="utf-8")
            rec_rows = page.evaluate(JS_RECORDS)
            print(f"  [OK] 交易记录: {len(rec_rows)} 行 -> {RECORDS_CACHE}")
        except Exception as e:
            print(f"  [WARN] 交易记录抓取失败: {type(e).__name__}: {e}")

        # 抓总资产/现金概览
        try:
            body = page.inner_text("body")
        except Exception:
            body = ""
        overview = {}
        for key, pat in [
            ("总资产", r"人民币总资产[（(]元[)）]\s*([\d,]+\.?\d*)"),
            ("现金账户", r"现金账户资产\s*([\d,]+\.?\d*)"),
            ("基金资产", r"基金资产\s*([\d,]+\.?\d*)"),
            ("养老金", r"个人养老金资产\s*([\d,]+\.?\d*)"),
        ]:
            m = re.search(pat, body)
            overview[key] = parse_number(m.group(1)) if m else None

        ctx.close()
        return {"rows": rows, "overview": overview, "records": rec_rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reparse", action="store_true", help="用已保存的HTML重新解析(免登录)")
    args = ap.parse_args()

    if args.reparse:
        print("  --reparse: 用已有HTML离线解析")
        from playwright.sync_api import sync_playwright
        if not HTML_CACHE.exists():
            print(f"  [ERR] 找不到 {HTML_CACHE}")
            return
        html = HTML_CACHE.read_text(encoding="utf-8")
        with sync_playwright() as p:
            b = p.chromium.launch(executable_path=CHROME, headless=True)
            pg = b.new_page()
            pg.set_content(html)
            rows = pg.evaluate(JS_EXTRACT)
            rec_rows = []
            if RECORDS_CACHE.exists():
                try:
                    pg.set_content(RECORDS_CACHE.read_text(encoding="utf-8"))
                    rec_rows = pg.evaluate(JS_RECORDS)
                except Exception as e:
                    print(f"  [WARN] 交易记录解析失败: {e}")
            b.close()
        data = {"rows": rows, "overview": {}, "records": rec_rows}
    else:
        data = fetch()
        if data is None:
            return

    remote = parse_rows(data["rows"])
    remote, ignored = split_ignored(remote)

    # 把货币基金余额(真实现金)写进 data/account_cash.json,
    # 供 portfolio.get_available_cash() 使用 (2026-10-28: 原先按 200万-市值 推算会虚高 40万)
    cash_row = next((r for r in ignored if r["fund_code"] in CASH_ACCOUNT_CODES), None)
    if cash_row:
        try:
            import config
            mv = float(cash_row.get("market_value") or 0)
            Path(config.ACCOUNT_CASH_FILE).write_text(json.dumps({
                "cash": round(mv, 2),
                "fund_code": cash_row["fund_code"],
                "name": cash_row.get("name"),
                "source": "efunds_sync",
                "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\n  💰 现金账户(真实可用): ¥{mv:,.2f} → 已写入 {config.ACCOUNT_CASH_FILE}")
        except Exception as e:
            print(f"  [WARN] 写入现金余额失败: {type(e).__name__}: {e}")

    JSON_OUT.write_text(json.dumps({
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "overview": data.get("overview", {}),
        "holdings": remote,
        "records": data.get("records", []),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    if data.get("overview"):
        ov = data["overview"]
        print("\n  账户概览:")
        for k, v in ov.items():
            if v is not None:
                print(f"      {k}: ¥{v:,.2f}")

    local = load_local()
    diffs = reconcile(remote, local)
    report(remote, local, diffs, ignored)

    recs = data.get("records") or []
    if recs:
        parsed = parse_records(recs)
        print("\n  平台交易记录 (最近 %d 条):" % len(parsed))
        for r in parsed[:15]:
            amt = f"¥{r['amount']:,.2f}" if r["amount"] else ("%s份" % f"{r['shares']:,.2f}" if r["shares"] else "--")
            print(f"      {r['datetime'][:16]}  {r['type']:<6} {r['name'][:26]:<28} {amt:>16}  {r['status']}")

        name_map = build_name_map()
        local_trades = load_local_trades()
        missing, matched, cash_moves, pending, weak = reconcile_trades(parsed, name_map, local_trades)
        extra = ""
        if cash_moves:
            extra += f", 现金划转 {len(cash_moves)} 笔"
        if pending:
            extra += f", 处理中 {len(pending)} 笔"
        print(f"\n  流水对账: 已匹配 {matched} 笔, 疑似漏记 {len(missing)} 笔" + extra)
        for m in missing:
            sh = f"{m['shares']:,.2f}份" if m["shares"] else (f"¥{m['amount']:,.0f}" if m["amount"] else "--")
            print(f"     ⚠️  {m['datetime'][:16]} {m['type']} {m['name'][:24]} {sh}  [{m['code']}] — {m['reason']}")
        for p in pending:
            sh = f"{p['shares']:,.2f}份" if p["shares"] else (f"¥{p['amount']:,.0f}" if p["amount"] else "--")
            print(f"     ⏳ {p['datetime'][:16]} {p['type']} {p['name'][:24]} {sh} — 平台处理中, 份额/金额未确认, 不参与对账")
        for w in weak:
            print(f"     ⚠️  {w['datetime'][:16]} {w['type']} {w['name'][:24]} — 仅日期匹配(平台无金额/份额), 建议人工确认")
        for c in cash_moves:
            amt = f"{c['shares']:,.2f}份" if c["shares"] else (f"¥{c['amount']:,.0f}" if c["amount"] else "--")
            print(f"     💰 {c['datetime'][:16]} {c['type']} {c['name'][:24]} {amt} — 现金划转, 不影响持仓但影响可用资金")
        if not missing and not cash_moves and not pending and not weak:
            print("     ✅ 平台流水与系统流水一致")

    print(f"  平台数据已保存: {JSON_OUT}")
    print(f"  原始HTML: {HTML_CACHE}")
    print("\n  (本脚本不修改数据库。如需同步份额, 请用 main.py update 命令)")


if __name__ == "__main__":
    main()

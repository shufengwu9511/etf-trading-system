# -*- coding: utf-8 -*-
"""
易方达财富 申赎填单机器人 (阶段B)

流程: 盘中信号 → 生成操作清单 → 打开浏览器 → 扫码登录 → 自动填单 → 停在提交前

==================== 安全设计 ====================
1. 默认只填单不提交 (--submit 才提交, 且需 --i-know 且受护栏校验)
2. 单笔/单日金额上限, 基金白名单校验
3. 赎回份额不得超过数据库持仓份额
4. 交易时段外禁止提交
5. 每一步都截图留证, 写入 logs/trade_bot_*.png
==================================================

用法:
    # 1) 只生成操作清单, 不启动浏览器
    .venv/Scripts/python.exe scripts/efunds_trade.py --plan

    # 2) 填单(默认, 不提交) —— 推荐
    .venv/Scripts/python.exe scripts/efunds_trade.py --fill

    # 3) 只做一笔
    .venv/Scripts/python.exe scripts/efunds_trade.py --fill --only 021893

    # 4) 提交 (危险, 需双重确认)
    .venv/Scripts/python.exe scripts/efunds_trade.py --fill --submit --i-know
"""
import sys
import time
import json
import argparse
from pathlib import Path
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
LOGDIR = ROOT / "logs"
PROFILE = ROOT / "data" / "chrome_auto_profile"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
BASE = "https://e.efunds.com.cn"

# ---------------- 护栏参数 ----------------
MAX_PER_ORDER = 300_000        # 单笔申购上限
MAX_TOTAL_PER_DAY = 600_000    # 单日申购总额上限
TRADE_WINDOW = ("09:30", "15:00")
LOGIN_OK_HINTS = ["安全退出", "我的资产", "总资产"]
LOG_DIR_NAME = "trade_bot"

LOGDIR.mkdir(exist_ok=True)


# ============================================================
# 操作清单生成 (信号 + 持仓)
# ============================================================
def build_plan(buy_amount_per_fund=None):
    """从盘中信号与数据库持仓生成操作清单"""
    import intraday_signal
    from db import get_connection

    sig = intraday_signal.generate_signals()
    if not sig.get("ok"):
        return [], [], f"信号生成失败: {sig.get('error')}"

    conn = get_connection()
    conn.row_factory = None
    rows = conn.execute(
        "SELECT fund_code, name, shares, status FROM portfolio WHERE status='holding'"
    ).fetchall()
    conn.close()
    holding = {r[0]: {"name": r[1], "shares": float(r[2] or 0)} for r in rows}

    sells, buys = [], []
    for s in sig.get("sell", []):
        fc = s["fund_code"]
        h = holding.get(fc)
        if not h:
            continue
        sells.append({
            "action": "redeem", "fund_code": fc, "name": h["name"],
            "shares": round(h["shares"], 2),
            "reason": f"动量第{s.get('rank')}名, 跌出Top3",
        })
    for b in sig.get("buy", []):
        fc = b.get("fund_code")
        buys.append({
            "action": "subscribe", "fund_code": fc, "name": b.get("name"),
            "amount": buy_amount_per_fund,
            "reason": f"动量第{b.get('rank')}名, MA20上方",
        })
    return sells, buys, sig


def allowed_fund(fund_code):
    """白名单校验: 必须在配置的ETF池内"""
    try:
        import config
        pool = list(getattr(config, "SATELLITE_ETFS", [])) + list(getattr(config, "CORE_ETFS", []))
        for e in pool:
            if isinstance(e, dict) and e.get("fund_code") == fund_code:
                return True, e.get("name", "")
        # 自定义池
        p = ROOT / "data" / "etf_pool.json"
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            for e in (data if isinstance(data, list) else data.get("etfs", [])):
                if isinstance(e, dict) and e.get("fund_code") == fund_code:
                    return True, e.get("name", "")
    except Exception:
        pass
    return False, ""


def in_trade_window():
    now = datetime.now().strftime("%H:%M")
    return TRADE_WINDOW[0] <= now <= TRADE_WINDOW[1]


# ============================================================
# 浏览器机器人
# ============================================================
class EfundsBot:
    def __init__(self):
        self.ctx = None
        self.page = None
        self.pw = None

    def start(self):
        from playwright.sync_api import sync_playwright
        self.pw = sync_playwright().start()
        PROFILE.mkdir(parents=True, exist_ok=True)
        self.ctx = self.pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            executable_path=CHROME,
            headless=False,
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
            ignore_https_errors=True,
        )
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()

    def wait_login(self, timeout=300):
        print(f"\n  请在浏览器窗口中扫码登录 (等待 {timeout} 秒)")
        self.page.goto(BASE + "/", timeout=45000, wait_until="domcontentloaded")
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(3)
            try:
                body = self.page.inner_text("body")
            except Exception:
                continue
            if any(w in body for w in LOGIN_OK_HINTS) and self.page.url.rstrip("/") != BASE:
                print(f"  [OK] 登录成功: {self.page.url}")
                return True
        print("  [TIMEOUT] 未检测到登录")
        return False

    def visible_inputs(self):
        js = """() => Array.from(document.querySelectorAll('input')).map(e => ({
            type:e.type||'', id:e.id||'', name:e.name||'',
            ph:(e.placeholder||'').slice(0,30),
            label:(e.closest('label')?.innerText||'').slice(0,30),
            readonly:e.readOnly||false,
            visible:!!e.offsetParent
        })).filter(x=>x.visible)"""
        try:
            return self.page.evaluate(js)
        except Exception:
            return []

    def pick(self, inputs, keys, exclude=("checkbox", "radio", "submit", "button", "hidden")):
        for i in inputs:
            if i["type"] in exclude or i["readonly"]:
                continue
            blob = f"{i['id']} {i['name']} {i['ph']} {i['label']}".lower()
            if any(k in blob for k in keys):
                return i
        return None

    def shot(self, tag):
        p = LOGDIR / f"{LOG_DIR_NAME}_{tag}.png"
        try:
            self.page.screenshot(path=str(p), full_page=True)
        except Exception:
            return None
        return p

    # ---------- 赎回 ----------
    def fill_redeem(self, item, submit=False):
        code, shares = item["fund_code"], item["shares"]
        url = f"{BASE}/payment/redemptions/{code}?form"
        print(f"\n  [赎回] {item['name']} ({code}) 份额 {shares:,.2f}")
        self.page.goto(url, timeout=40000, wait_until="domcontentloaded")
        self.page.wait_for_timeout(2500)
        if "登录" in (self.page.title() or ""):
            print("    [FAIL] 被踢回登录页, 跳过")
            return False
        inputs = self.visible_inputs()
        print(f"    表单字段: {[(i['type'], i['id'] or i['name'], i['ph']) for i in inputs][:8]}")

        # 优先找"全部赎回"选项
        all_opt = None
        try:
            cands = self.page.evaluate(
                """() => Array.from(document.querySelectorAll('input[type=radio],input[type=checkbox]'))
                    .map(e=>({id:e.id,name:e.name,txt:(e.closest('label')?.innerText||e.parentElement?.innerText||'').trim().slice(0,20)}))"""
            )
            all_opt = next((c for c in cands if "全部" in c["txt"]), None)
        except Exception:
            pass

        # 优先按"份额"匹配(moneyAmount框label是'卖出份额'), 再退回amount匹配
        target = self.pick(inputs, ["份额", "share"]) or self.pick(inputs, ["amount", "金额"])
        if all_opt and all_opt.get("id"):
            print(f"    发现[全部赎回]选项: {all_opt['txt']} → 勾选 (不填份额)")
            self.page.check(f"#{all_opt['id']}")
        elif target:
            sel = f"#{target['id']}" if target["id"] else f"input[name='{target['name']}']"
            print(f"    填入份额输入框: {sel}")
            self.page.fill(sel, f"{shares:.2f}")
        else:
            print("    [WARN] 未找到份额输入框, 仅截图, 需人工填写")
            self.shot(f"redeem_{code}_manual")
            return False
        self.page.wait_for_timeout(500)
        print(f"    截图: {self.shot(f'redeem_{code}_filled')}")
        if submit:
            self._click_submit(code, "redeem")
        return True

    # ---------- 申购 ----------
    def fill_subscribe(self, item, submit=False):
        code, amount = item["fund_code"], item["amount"]
        if not amount:
            print(f"\n  [跳过] {item['name']} ({code}) 未指定申购金额")
            return False
        url = f"{BASE}/cart/subscriptions?form&fundCode={code}"
        print(f"\n  [申购] {item['name']} ({code}) 金额 ¥{amount:,.0f}")
        self.page.goto(url, timeout=40000, wait_until="domcontentloaded")
        self.page.wait_for_timeout(2500)
        if "登录" in (self.page.title() or ""):
            print("    [FAIL] 被踢回登录页, 跳过")
            return False
        inputs = self.visible_inputs()
        target = self.pick(inputs, ["amount", "金额"]) or \
            next((i for i in inputs if i["id"] == "amount"), None)
        if not target:
            print("    [WARN] 未找到金额输入框, 仅截图")
            self.shot(f"sub_{code}_manual")
            return False
        sel = f"#{target['id']}" if target["id"] else f"input[name='{target['name']}']"
        self.page.fill(sel, f"{amount:.0f}")
        # 勾选协议
        try:
            boxes = self.page.evaluate(
                """() => Array.from(document.querySelectorAll('input[type=checkbox]'))
                    .map(e=>e.id).filter(Boolean)"""
            )
            for b in boxes:
                try:
                    self.page.check(f"#{b}")
                except Exception:
                    pass
            if boxes:
                print(f"    已勾选协议 {len(boxes)} 项")
        except Exception:
            pass
        self.page.wait_for_timeout(500)
        print(f"    截图: {self.shot(f'sub_{code}_filled')}")
        if submit:
            self._click_submit(code, "sub")
        return True

    def _click_submit(self, code, kind):
        for sel in ["#submitBtn", "input[type=submit]", "button:has-text('提交')",
                    "a:has-text('提交')"]:
            try:
                if self.page.locator(sel).count() > 0:
                    self.page.click(sel)
                    print(f"    [SUBMIT] 已点击提交 ({sel})")
                    self.page.wait_for_timeout(3000)
                    self.shot(f"{kind}_{code}_submitted")
                    return True
            except Exception:
                continue
        print("    [WARN] 未找到提交按钮")
        return False

    def close(self):
        try:
            self.ctx.close()
        except Exception:
            pass
        try:
            self.pw.stop()
        except Exception:
            pass


# ============================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true", help="只生成操作清单")
    ap.add_argument("--fill", action="store_true", help="启动浏览器填单")
    ap.add_argument("--submit", action="store_true", help="填单后提交(危险)")
    ap.add_argument("--i-know", action="store_true", help="提交二次确认")
    ap.add_argument("--only", default="", help="只处理指定基金代码")
    ap.add_argument("--buy-amount", type=float, default=0, help="每只申购金额")
    ap.add_argument("--wait-login", type=int, default=300)
    args = ap.parse_args()

    print("=" * 62)
    print("  易方达财富 申赎填单机器人")
    print(f"  模式: {'填单并提交' if args.submit else '只填单不提交'}")
    print("=" * 62)

    ok, name = True, ""
    if args.only:
        ok, name = allowed_fund(args.only)
        if not ok:
            print(f"  [ERROR] {args.only} 不在候选池白名单内")
            return

    if args.plan and not args.fill:
        sells, buys, sig = build_plan(args.buy_amount or None)
        # 打印判定基准: 真正的Top3(得分>阈值的前N名), 避免"第3名却跌出Top3"的歧义
        if isinstance(sig, dict) and sig.get("ok"):
            print("\n  判定基准 Top3 (综合得分>阈值的前N名):")
            for r in sig.get("top_n", []):
                print(f"    #{r['rank']} {r['name']:<16} 综合{r['composite_score']:+.2f}% "
                      f"MA20上方={r['above_ma20']}")
            print("    注: 综合得分为负的基金即使排名靠前也不进Top3, 持仓仍会被判定赎回")
        print("\n  赎回清单:")
        for s in sells:
            print(f"    🔴 {s['name']} ({s['fund_code']})  {s['shares']:,.2f}份  {s['reason']}")
        print("\n  申购清单:")
        if buys:
            for b in buys:
                amt = f"¥{b['amount']:,.0f}" if b["amount"] else "金额未指定"
                print(f"    🟢 {b['name']} ({b['fund_code']})  {amt}  {b['reason']}")
        else:
            print("    (无)")
        return

    if not args.fill:
        print("  请指定 --plan 或 --fill")
        return

    # 护栏
    if args.submit:
        if not args.i_know:
            print("  [ERROR] --submit 必须配合 --i-know")
            return
        if not in_trade_window():
            print(f"  [ERROR] 不在交易时段 {TRADE_WINDOW}, 禁止提交")
            return

    sells, buys, sig = build_plan(args.buy_amount or None)
    items = sells + [b for b in buys if b.get("amount")]
    if args.only:
        items = [i for i in items if i["fund_code"] == args.only]

    if not items:
        print("  无可执行的申赎操作")
        return

    total_buy = sum(i.get("amount", 0) for i in items if i["action"] == "subscribe")
    if total_buy > MAX_TOTAL_PER_DAY:
        print(f"  [ERROR] 单日申购总额 ¥{total_buy:,.0f} 超过上限 ¥{MAX_TOTAL_PER_DAY:,.0f}")
        return
    for i in items:
        if i["action"] == "subscribe" and i.get("amount", 0) > MAX_PER_ORDER:
            print(f"  [ERROR] 单笔申购 ¥{i['amount']:,.0f} 超过上限")
            return

    print(f"\n  待处理 {len(items)} 笔:")
    for i in items:
        if i["action"] == "redeem":
            print(f"    🔴 赎回 {i['name']} ({i['fund_code']}) {i['shares']:,.2f}份")
        else:
            print(f"    🟢 申购 {i['name']} ({i['fund_code']}) ¥{i['amount']:,.0f}")

    bot = EfundsBot()
    try:
        bot.start()
        if not bot.wait_login(args.wait_login):
            return
        for i in items:
            if i["action"] == "redeem":
                bot.fill_redeem(i, submit=args.submit)
            else:
                bot.fill_subscribe(i, submit=args.submit)
        print("\n" + "=" * 62)
        if args.submit:
            print("  已提交, 请到浏览器核对结果")
        else:
            print("  填单完成, 未提交。请在浏览器窗口中核对数字后自行点提交")
        print("=" * 62)
        try:
            input("  回车键关闭浏览器...")
        except (EOFError, KeyboardInterrupt):
            print("  (无交互环境, 保持打开 120 秒后自动关闭)")
            time.sleep(120)
    finally:
        bot.close()


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""
登录态分层探测: 确认哪些页面只需扫码登录, 哪些需要二次认证(交易密码/短信)

只读: 只 goto, 不填写不提交
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
LOGDIR = ROOT / "logs"
PROFILE = ROOT / "data" / "chrome_auto_profile"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

STEPS = [
    ("/assets", "资产总览"),
    ("/assets/funds/021893", "持仓明细"),
    ("/payment/redemptions/021893?form", "赎回表单"),
    ("/transaction/records", "交易记录"),
    ("/cart/subscriptions?form&fundCode=021893", "申购表单"),
]


def main():
    print("=" * 62)
    print("  登录态分层探测 (只读)")
    print("=" * 62)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            executable_path=CHROME,
            headless=False,
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
            ignore_https_errors=True,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        for path, desc in STEPS:
            url = "https://e.efunds.com.cn" + path
            try:
                page.goto(url, timeout=40000, wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
            except Exception as e:
                print(f"  [ERR ] {desc:<8} {type(e).__name__}: {e}")
                continue
            title = ""
            try:
                title = page.title()
            except Exception:
                pass
            body = ""
            try:
                body = page.inner_text("body") or ""
            except Exception:
                pass
            landed = page.url.replace("https://e.efunds.com.cn", "")
            ok = "登录" not in title
            flag = "OK  " if ok else "KICK"
            print(f"  [{flag}] {desc:<8} -> {landed[:70]}")
            print(f"          title={title}")
            hints = [w for w in ["交易密码", "软键盘", "安全键盘", "验证码",
                                 "短信", "动态码", "风险揭示", "请重新登录"] if w in body]
            if hints:
                print(f"          页面含: {hints}")
            if ok and desc in ("持仓明细", "赎回表单", "交易记录"):
                print(f"          文本片段: {body[:300].replace(chr(10), ' | ')}")
        print("\n  保持打开 60 秒")
        page.wait_for_timeout(60000)
        ctx.close()


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""
易方达财富 交易页结构侦察 (阶段B)

目的: 摸清申购/赎回表单的字段结构, 确认自动化填单的可行性与风险点。
      重点回答: 份额输入框能否程序化填写? 交易密码是不是软键盘? 确认流程几步?

==================== 安全铁律 (禁止修改) ====================
1. 只做: 打开页面 / 读取DOM / 截图
2. 禁止: fill() / type() / click() 任何元素 —— 本脚本不导入任何写入类操作
3. 不使用任何密码, 登录态复用已保存的 profile
============================================================

用法:
    .venv/Scripts/python.exe scripts/efunds_probe_trade.py
"""
import sys
import json
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
LOGDIR = ROOT / "logs"
PROFILE = ROOT / "data" / "chrome_auto_profile"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

TARGETS = [
    ("detail_021893", "/assets/funds/021893", "半导体持仓明细"),
    ("redeem_021893", "/payment/redemptions/021893?form", "赎回表单(半导体)"),
    ("subscribe_021893", "/cart/subscriptions?form&fundCode=021893", "申购表单(半导体)"),
    ("records", "/transaction/records", "交易记录"),
]

LOGDIR.mkdir(exist_ok=True)

JS_INPUTS = """() => Array.from(document.querySelectorAll('input')).map(e => ({
    type: e.type || '', id: e.id || '', name: e.name || '',
    cls: (e.className||'').toString().slice(0,40),
    ph: (e.placeholder||'').slice(0,40),
    maxlen: e.maxLength || 0,
    readonly: e.readOnly || false,
    visible: !!(e.offsetParent)
}))"""

JS_BUTTONS = """() => Array.from(document.querySelectorAll('button,a.btn,a[class*=btn],input[type=button],input[type=submit]'))
    .map(e => ({tag: e.tagName, text: (e.innerText||e.value||'').trim().slice(0,24), cls:(e.className||'').toString().slice(0,40)}))
    .filter(x => x.text)"""

JS_KEYWORDS = """(kws) => {
    const t = document.body.innerText || '';
    const hits = {};
    for (const k of kws) { if (t.includes(k)) hits[k] = true; }
    return hits;
}"""


def dump(page, tag, desc):
    out = {"tag": tag, "desc": desc, "url": page.url}
    try:
        out["title"] = page.title()
    except Exception:
        out["title"] = ""
    try:
        out["inputs"] = page.evaluate(JS_INPUTS)
    except Exception as e:
        out["inputs"] = f"ERR {e}"
    try:
        out["buttons"] = page.evaluate(JS_BUTTONS)
    except Exception as e:
        out["buttons"] = f"ERR {e}"
    try:
        out["keywords"] = page.evaluate(
            JS_KEYWORDS,
            ["交易密码", "软键盘", "安全键盘", "验证码", "份额", "可用份额", "确认",
             "下一步", "大额", "巨额赎回", "顺延", "取消", "风险", "手续费", "到账"],
        )
    except Exception:
        out["keywords"] = {}
    try:
        out["text"] = (page.inner_text("body") or "")[:1200]
    except Exception:
        out["text"] = ""

    shot = LOGDIR / f"trade_{tag}.png"
    try:
        page.screenshot(path=str(shot), full_page=True)
    except Exception:
        pass

    (LOGDIR / f"trade_{tag}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\n  [{tag}] {desc}")
    print(f"    url   : {out['url']}")
    print(f"    title : {out['title']}")
    print(f"    关键词: {sorted(out['keywords'].keys())}")
    if isinstance(out["inputs"], list):
        vis = [i for i in out["inputs"] if i["visible"]]
        print(f"    input 共{len(out['inputs'])}个, 可见{len(vis)}个:")
        for i in vis[:14]:
            print(f"      - {i['type']:<9} id={i['id']:<22} name={i['name']:<16} ph={i['ph']}")
    if isinstance(out["buttons"], list):
        print(f"    按钮/链接 {len(out['buttons'])}个: {[b['text'] for b in out['buttons'][:10]]}")
    print(f"    截图: {shot.name}")
    return out


def main():
    print("=" * 62)
    print("  交易页结构侦察 (阶段B) —— 只读, 不填不点")
    print("=" * 62)
    if not PROFILE.exists():
        print(f"  [ERROR] 登录态目录不存在: {PROFILE}")
        print("          请先运行 scripts/efunds_probe.py 扫码登录")
        return

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

        results = {}
        for tag, path, desc in TARGETS:
            url = "https://e.efunds.com.cn" + path
            try:
                page.goto(url, timeout=40000, wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
            except Exception as e:
                print(f"\n  [{tag}] 打开失败: {type(e).__name__}: {e}")
                continue
            # 若被踢回登录页, 说明登录态失效
            if "登录" in (page.title() or "") and "/assets" not in page.url:
                print(f"\n  [WARN] {tag} 似乎跳回登录页, 登录态可能已失效: {page.url}")
            results[tag] = dump(page, tag, desc)

        print("\n  [DONE] 结构已存 logs/trade_*.json, 截图 logs/trade_*.png")
        print("         浏览器将保持打开 90 秒供人工查看")
        page.wait_for_timeout(90000)
        ctx.close()


if __name__ == "__main__":
    main()

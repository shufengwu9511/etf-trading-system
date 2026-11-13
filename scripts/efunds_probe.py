# -*- coding: utf-8 -*-
"""
易方达财富 只读侦察 (阶段A)

目的: 验证平台能否被浏览器自动化驱动, 摸清登录关卡与页面结构。

==================== 安全铁律 (禁止修改) ====================
1. 只做: 打开页面 / 读取DOM / 截图
2. 禁止: 填写任何输入框 / 提交任何表单 / 点击任何交易类按钮
3. 不使用任何密码凭据, 登录由用户本人在浏览器窗口内手动完成
4. 独立 profile 目录, 不触碰用户日常 Chrome 配置
============================================================

用法:
    .venv/Scripts/python.exe scripts/efunds_probe.py            # 打开登录页并等待登录
    .venv/Scripts/python.exe scripts/efunds_probe.py --no-wait   # 只截图不等待
"""
import sys
import time
import argparse
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
LOGDIR = ROOT / "logs"
PROFILE = ROOT / "data" / "chrome_auto_profile"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
HOME_URL = "https://e.efunds.com.cn/"

# 登录后页面通常出现的关键词
LOGIN_OK_HINTS = ["退出", "我的资产", "总资产", "持仓", "安全退出", "欢迎"]
# 交易类按钮关键词 (绝不点击)
DANGER_WORDS = ["申购", "赎回", "确认", "提交", "支付", "转换", "撤单"]

LOGDIR.mkdir(exist_ok=True)
PROFILE.mkdir(parents=True, exist_ok=True)


def dump_page(page, tag):
    """只读: 截图 + 提取 input/link 结构"""
    shot = LOGDIR / f"probe_{tag}.png"
    page.screenshot(path=str(shot), full_page=True)

    info = {"tag": tag, "url": page.url, "title": "", "inputs": [], "links": []}
    try:
        info["title"] = page.title()
    except Exception:
        pass

    try:
        info["inputs"] = page.eval_on_selector_all(
            "input",
            """els => els.map(e => ({
                type: e.type || '', id: e.id || '', name: e.name || '',
                placeholder: (e.placeholder||'').slice(0,40),
                maxlength: e.maxLength || 0
            }))""",
        )
    except Exception as e:
        info["inputs"] = f"ERR {e}"

    try:
        info["links"] = page.eval_on_selector_all(
            "a",
            """els => els.map(e => ({
                text: (e.innerText||'').trim().slice(0,30), href: (e.getAttribute('href')||'').slice(0,80)
            })).filter(x => x.text)""",
        )
    except Exception as e:
        info["links"] = f"ERR {e}"

    # 页面是否包含可疑验证元素
    try:
        body = page.inner_text("body")
    except Exception:
        body = ""
    info["hints"] = [w for w in ["验证码", "滑块", "微信", "扫码", "快捷登录"] if w in body]
    info["login_ok_hint"] = [w for w in LOGIN_OK_HINTS if w in body]
    info["text_sample"] = body[:600]

    out = LOGDIR / f"probe_{tag}.json"
    import json
    out.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"  [dump] {tag}: url={info['url']}")
    print(f"         title={info['title']}")
    print(f"         inputs={len(info['inputs']) if isinstance(info['inputs'], list) else info['inputs']}")
    if isinstance(info["inputs"], list):
        for i in info["inputs"][:12]:
            print(f"           - type={i['type']} id={i['id']} name={i['name']} ph={i['placeholder']}")
    print(f"         hints={info['hints']}  登录特征={info['login_ok_hint']}")
    print(f"         截图: {shot}")
    return info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-wait", action="store_true", help="只截图, 不等待登录")
    ap.add_argument("--wait", type=int, default=240, help="等待登录秒数")
    args = ap.parse_args()

    print("=" * 60)
    print("  易方达财富 只读侦察 (阶段A)")
    print("  安全模式: 不填表 / 不提交 / 不点交易按钮")
    print("=" * 60)
    print(f"  Chrome: {CHROME}")
    print(f"  Profile: {PROFILE}  (独立目录, 不影响日常浏览器)")

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

        print(f"\n  [1] 打开首页: {HOME_URL}")
        try:
            page.goto(HOME_URL, timeout=45000, wait_until="domcontentloaded")
        except Exception as e:
            print(f"  [WARN] 首页加载异常: {type(e).__name__}: {e}")
        page.wait_for_timeout(2500)
        dump_page(page, "01_home")

        if args.no_wait:
            print("\n  --no-wait 模式, 保持浏览器打开 30 秒后退出")
            page.wait_for_timeout(30000)
            ctx.close()
            return

        print(f"\n  [2] 请在弹出的浏览器窗口中手动登录 (最多等待 {args.wait} 秒)")
        print("      推荐: 微信快捷登录扫码, 密码无需提供给我")
        print("      登录后脚本会自动检测并继续, 全程不碰任何输入")

        deadline = time.time() + args.wait
        logged = False
        last_url = page.url
        while time.time() < deadline:
            time.sleep(3)
            try:
                cur = page.url
            except Exception:
                break
            body = ""
            try:
                body = page.inner_text("body")
            except Exception:
                pass
            hits = [w for w in LOGIN_OK_HINTS if w in body]
            if hits and cur != HOME_URL:
                print(f"  [OK] 检测到登录特征: {hits}")
                print(f"       url: {cur}")
                logged = True
                break
            if cur != last_url:
                print(f"  ... url 变化: {cur}")
                last_url = cur

        if not logged:
            print("  [TIMEOUT] 未检测到登录, 保存当前状态后退出")
            dump_page(page, "02_timeout")
            ctx.close()
            return

        page.wait_for_timeout(1500)
        dump_page(page, "02_logged_in")

        print("\n  [3] 查找持仓/资产相关入口 (只读取链接, 不点击)")
        try:
            cands = page.eval_on_selector_all(
                "a,button,li,span,div",
                """els => els.map(e => ({
                    tag: e.tagName, text: (e.innerText||'').trim().slice(0,20),
                    href: (e.getAttribute('href')||'').slice(0,100)
                })).filter(x => x.text && x.text.length <= 20)""",
            )
        except Exception as e:
            cands = []
            print(f"  [WARN] 读取入口失败: {e}")

        keys = ["资产", "持仓", "我的", "在途", "交易", "查询", "账户", "余额"]
        seen = set()
        for c in cands:
            if any(k in c["text"] for k in keys) and c["text"] not in seen:
                seen.add(c["text"])
                print(f"       - {c['text']}  ({c['href'] or c['tag']})")

        print("\n  [4] 保持浏览器打开 60 秒, 便于人工查看")
        print("       期间不做任何操作")
        page.wait_for_timeout(60000)

        ctx.close()
        print("\n  [DONE] 侦察结束, 结果在 logs/probe_*.json 与 probe_*.png")


if __name__ == "__main__":
    main()

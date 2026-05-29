#!/usr/bin/env python3
# ============================================================
# 动量参数对比回测
# 调用 backtest_runner.py (子进程, 每次独立参数)
# ============================================================
import sys
import os
import subprocess
import json

PYTHON = "C:/Users/sfwu/AppData/Local/Programs/Python/Python314/python.exe"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RUNNER = os.path.join(SCRIPT_DIR, "backtest_runner.py")

# 需要对比的参数组
CONFIGS = [
    {"label": "当前(20/60,w0.4/0.6)", "short": 20, "medium": 60, "w_short": 0.4, "w_medium": 0.6},
    {"label": "提议A(10/30,w0.4/0.6)", "short": 10, "medium": 30, "w_short": 0.4, "w_medium": 0.6},
    {"label": "提议B(10/30,w0.5/0.5)", "short": 10, "medium": 30, "w_short": 0.5, "w_medium": 0.5},
    {"label": "提议C(10/30,w0.6/0.4)", "short": 10, "medium": 30, "w_short": 0.6, "w_medium": 0.4},
    {"label": "探索(15/45,w0.4/0.6)", "short": 15, "medium": 45, "w_short": 0.4, "w_medium": 0.6},
]


def run_one(cfg):
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["MOM_SHORT"] = str(cfg["short"])
    env["MOM_MEDIUM"] = str(cfg["medium"])
    env["MOM_W_SHORT"] = str(cfg["w_short"])
    env["MOM_W_MEDIUM"] = str(cfg["w_medium"])

    result = subprocess.run(
        [PYTHON, "-u", RUNNER],
        capture_output=True, text=True, encoding="utf-8",
        cwd=SCRIPT_DIR, env=env, timeout=300,
    )

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()

    # 找最后一行 JSON
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                r = json.loads(line)
                r["label"] = cfg["label"]
                return r, None
            except json.JSONDecodeError:
                pass

    return None, stdout[-500:] + (stderr[-500:] if stderr else "")


if __name__ == "__main__":
    print("=" * 72)
    print("  动量参数对比回测")
    print("  回测区间: 2024-05-06 ~ 2026-04-28")
    print("=" * 72)

    results = []
    for cfg in CONFIGS:
        label = cfg["label"]
        print(f"\n>>> 回测: {label} ...", flush=True)
        r, err = run_one(cfg)
        if r:
            results.append(r)
            print(f"    完成: 年化={r['annual']:+.2f}%, 回撤={r['max_dd']:.2f}%, 交易={r['trades']}次")
        else:
            print(f"    !! 失败, 输出: {err}")

    if not results:
        print("\n所有回测均失败!")
        sys.exit(1)

    # ---- 打印对比表 ----
    print("\n")
    print("=" * 76)
    print("  对比结果汇总")
    print("=" * 76)

    # 表头
    cols = "  " + " ".join(f"{r['label']:>16}" for r in results)
    print(f"  {'指标':<14} {cols}")
    print("  " + "-" * 72)

    metrics = [
        ("年化收益率",   "annual",        True,  False),
        ("总收益率",     "total_ret",     True,  False),
        ("最大回撤",     "max_dd",        True,  False),
        ("交易次数",     "trades",        False, False),
        ("卖出次数",     "sells",         False, False),
        ("胜率",         "win_rate",      True,  False),
        ("平均盈利",     "avg_win",       True,  False),
        ("平均亏损",     "avg_loss",      True,  False),
        ("盈亏比",       "profit_factor", False, False),
        ("最终市值",     "end_val",       False, True),
    ]

    for name, key, is_pct, is_money in metrics:
        parts = []
        for r in results:
            v = r[key]
            if key == "profit_factor":
                parts.append(f"{v:>16.2f}")
            elif is_money:
                parts.append(f"¥{v:>10,.0f}".rjust(16))
            elif is_pct:
                parts.append(f"{v:>+14.2f}%".rjust(16))
            else:
                parts.append(f"{v:>16}")
        print(f"  {name:<14} " + " ".join(parts))

    # 超额年化
    parts = []
    for r in results:
        v = r["annual"] - r["bm_annual"]
        parts.append(f"{v:>+14.2f}%".rjust(16))
    print(f"  {'超额年化(vs300)':<14} " + " ".join(parts))

    print("=" * 76)

    best_annual = max(results, key=lambda x: x["annual"])
    best_dd = min(results, key=lambda x: x["max_dd"])
    best_pf = max(results, key=lambda x: x["profit_factor"] if x["profit_factor"] < 999 else -999)
    print(f"\n  年化最高 : {best_annual['label']} ({best_annual['annual']:+.2f}%)")
    print(f"  回撤最小 : {best_dd['label']} ({best_dd['max_dd']:.2f}%)")
    print(f"  盈亏比最高: {best_pf['label']} ({best_pf['profit_factor']:.2f})")
    print()

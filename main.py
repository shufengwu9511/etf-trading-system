# ============================================================
# ETF联接基金交易系统 - 主程序入口
# 每日运行: python main.py
# ============================================================
import sys
import os
import io
import time
from datetime import datetime

# 修复 Windows 控制台编码问题
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# 确保项目根目录在 Python 路径中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import init_db, get_connection
from data_fetcher import run_full_data_update
from strategy import check_panic_alert, check_core_etf_signals, check_rotation_signals, check_stop_signals
from portfolio import (
    get_current_holdings, generate_action_sheet,
    save_signals_to_db, calc_position_sizes,
    import_holdings_from_csv, update_holdings_nav,
    sell_holding, sell_holding_partial, update_holding, show_holdings,
    buy_new_holding, confirm_buy, show_trades,
    add_etf_to_pool, show_etf_pool
)
from dashboard import generate_dashboard
from config import LOG_DIR


def run_daily():
    """每日主流程"""
    start_time = time.time()
    print("")
    print("╔" + "═" * 58 + "╗")
    print("║   ETF联接基金交易系统 - 每日信号运行                    ║")
    print(f"║   运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}                      ║")
    print("╚" + "═" * 58 + "╝")
    print("")

    os.makedirs(LOG_DIR, exist_ok=True)

    # Step 1: 更新数据
    print("[Step 1/6] 更新市场数据...")
    try:
        run_full_data_update()
    except Exception as e:
        print(f"  [ERROR] 数据更新失败: {e}")
        return

    # Step 1.5: 刷新持仓净值
    print("\n[Step 2/6] 刷新持仓净值...")
    try:
        update_holdings_nav()
    except Exception as e:
        print(f"  [WARN] 净值刷新失败 (不影响后续流程): {e}")

    # Step 2: 恐慌指数检测
    print("\n[Step 3/6] 检测市场恐慌指数...")
    try:
        panic_alert = check_panic_alert()
        if panic_alert["is_panic"]:
            print("  🚨 恐慌预警! 触发指标:", ", ".join(panic_alert["triggered"]))
        elif panic_alert["trigger_count"] > 0:
            print(f"  ⚠️ {panic_alert['trigger_count']}项波动提醒:", ", ".join(panic_alert["triggered"]))
        else:
            print("  ✅ 市场正常")
    except Exception as e:
        print(f"  [WARN] 恐慌检测异常: {e}")
        panic_alert = {"is_panic": False, "triggered": [], "trigger_count": 0}

    # Step 3: 策略计算
    print("\n[Step 4/6] 运行策略引擎...")

    # 3a: 宽基PE择时
    print("  [3a] 宽基PE估值择时...")
    try:
        core_signals = check_core_etf_signals()
        for sig in core_signals:
            d = {"buy": "🟢买入", "sell": "🔴卖出", "reduce": "🟡减仓", "hold": "⚪持有"}
            print(f"    {d.get(sig['direction'], sig['direction'])} {sig['name']}: {sig['reason']}")
    except Exception as e:
        print(f"  [ERROR] 宽基信号失败: {e}")
        core_signals = []

    # 3b: 行业动量轮动
    print("  [3b] 行业ETF动量轮动...")
    try:
        holdings = get_current_holdings()
        rotation_result = check_rotation_signals(holdings)
        buy_list = rotation_result.get("buy", [])
        sell_list = rotation_result.get("sell", [])
        if buy_list:
            print(f"    🟢 建议申购: {', '.join(s['name'] for s in buy_list)}")
        if sell_list:
            print(f"    🔴 建议赎回: {', '.join(s['name'] for s in sell_list)}")
        if not buy_list and not sell_list:
            print("    ⚪ 无轮动信号")
    except Exception as e:
        print(f"  [ERROR] 轮动信号失败: {e}")
        rotation_result = {"buy": [], "sell": [], "hold": []}

    # 3c: 止盈止损
    print("  [3c] 止盈止损检查...")
    try:
        # 传入动量排名前N的ETF代码, 行业ETF在其中的跳过止盈(让利润奔跑)
        momentum_top_codes = rotation_result.get("top_codes", set())
        stop_signals = check_stop_signals(holdings, is_panic=panic_alert.get("is_panic", False),
                                          momentum_top_codes=momentum_top_codes)
        if stop_signals:
            for sig in stop_signals:
                print(f"    {'🚨' if sig['priority']=='urgent' else '🔴'} {sig['name']}: {sig['reason']}")
        else:
            print("    ✅ 无止盈止损触发")
    except Exception as e:
        print(f"  [ERROR] 止盈止损检查失败: {e}")
        stop_signals = []

    # Step 4: 生成操作提示单
    print("\n[Step 5/6] 生成操作提示单...")
    signals_result = {
        "core": core_signals,
        "rotation": rotation_result,
        "stop": stop_signals
    }
    position_sizes = calc_position_sizes(signals_result, panic_alert)

    # 将计算好的仓位金额合并回信号
    for i, core_pos in enumerate(position_sizes.get("core", [])):
        if i < len(signals_result["core"]):
            signals_result["core"][i]["target_amount"] = core_pos["target_amount"]
    for i, sat_pos in enumerate(position_sizes.get("satellite_buy", [])):
        buy_sigs = signals_result.get("rotation", {}).get("buy", [])
        for j, sig in enumerate(buy_sigs):
            if sig.get("etf_code") == sat_pos.get("etf_code"):
                signals_result["rotation"]["buy"][j]["target_amount"] = sat_pos["target_amount"]

    action_sheet = generate_action_sheet(signals_result, panic_alert, position_sizes)

    # 保存到文件
    sheet_filename = f"action_sheet_{datetime.now().strftime('%Y%m%d_%H%M')}.txt"
    sheet_path = os.path.join(LOG_DIR, sheet_filename)
    with open(sheet_path, "w", encoding="utf-8") as f:
        f.write(action_sheet)
    print(f"  [OK] 操作提示单已保存: {sheet_path}")

    # 保存信号到数据库
    save_signals_to_db(signals_result, panic_alert)

    # Step 5: 输出操作提示单
    print("\n[Step 6/6] ============ 操作提示单 ============")
    print(action_sheet)

    # Step 6.5: 生成可视化看板
    try:
        dashboard_path = generate_dashboard()
        print(f"\n  📊 可视化看板: {dashboard_path}")
    except Exception as e:
        print(f"\n  [WARN] 看板生成失败: {e}")

    # 记录运行日志
    elapsed = time.time() - start_time
    conn = get_connection()
    conn.execute("""
        INSERT INTO system_logs (log_date, run_type, status, details, duration_seconds)
        VALUES (?, ?, ?, ?, ?)
    """, (datetime.now().strftime("%Y%m%d"), "daily",
          "success", f"恐慌={'是' if panic_alert['is_panic'] else '否'}, "
                     f"宽基信号{len(core_signals)}条, 轮动买{len(rotation_result.get('buy',[]))}条"
                     f"卖{len(rotation_result.get('sell',[]))}条, 止盈止损{len(stop_signals)}条",
          round(elapsed, 2)))
    conn.commit()
    conn.close()

    print(f"\n✅ 运行完成, 耗时 {elapsed:.1f} 秒")


def run_init():
    """首次初始化: 建表 + 拉取历史数据"""
    print("=" * 60)
    print("  首次初始化: 建表 + 拉取历史数据")
    print("=" * 60)

    print("\n[1/2] 初始化数据库...")
    init_db()

    print("\n[2/2] 拉取历史数据 (可能需要几分钟)...")
    run_full_data_update()

    print("\n✅ 初始化完成! 每日运行 python main.py 即可获取信号")


def run_sell(fund_code, args):
    """
    标记基金已赎回:
      python main.py sell <fund_code>              # 全部赎回
      python main.py sell <fund_code> --ratio 0.5  # 赎回50%
      python main.py sell <fund_code> --shares 5000 # 赎回5000份
      python main.py sell <fund_code> --submit-date 20260618 --confirm-date 20260619  # 指定日期
    """
    print("=" * 60)
    print("  标记赎回")
    print("=" * 60)
    print("")

    if not fund_code:
        print("  [ERROR] 请指定基金代码")
        print("  用法: python main.py sell <fund_code> [--ratio N | --shares N] [--submit-date YYYYMMDD] [--confirm-date YYYYMMDD]")
        print("  示例: python main.py sell 110020              # 全部赎回")
        print("        python main.py sell 110020 --ratio 0.5  # 赎回50%")
        print("        python main.py sell 110020 --shares 5000 # 赎回5000份")
        print("        python main.py sell 110020 --submit-date 20260618 --confirm-date 20260619")
        return

    # 解析参数
    ratio = None
    shares = None
    submit_date = None
    confirm_date = None
    i = 0
    while i < len(args):
        if args[i] == "--ratio" and i + 1 < len(args):
            ratio = float(args[i + 1])
            i += 2
        elif args[i] == "--shares" and i + 1 < len(args):
            shares = float(args[i + 1])
            i += 2
        elif args[i] == "--submit-date" and i + 1 < len(args):
            submit_date = args[i + 1]
            i += 2
        elif args[i] == "--confirm-date" and i + 1 < len(args):
            confirm_date = args[i + 1]
            i += 2
        else:
            i += 1

    sell_holding_partial(fund_code, ratio=ratio, shares=shares, submit_date=submit_date, confirm_date=confirm_date)


def run_update(fund_code, args):
    """
    更新持仓: python main.py update <fund_code> [--shares N] [--cost_nav N]
                                                 [--add-shares N] [--add-amount N]
                                                 [--submit-date YYYYMMDD] [--confirm-date YYYYMMDD]
    """
    print("=" * 60)
    print("  更新持仓")
    print("=" * 60)
    print("")

    if not fund_code:
        print("  [ERROR] 请指定基金代码")
        print("  用法:")
        print("    替换模式: python main.py update <fund_code> --shares 8000 --cost_nav 0.65")
        print("    追加模式: python main.py update <fund_code> --add-shares 3000 --add-amount 2000")
        print("    追加(历史日期): python main.py update <fund_code> --add-shares 32677 --add-amount 100000 --submit-date 20260512 --confirm-date 20260513")
        return

    new_shares = None
    new_cost_nav = None
    add_shares = None
    add_amount = None
    submit_date = None
    confirm_date = None

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--shares" and i + 1 < len(args):
            new_shares = float(args[i + 1])
            i += 2
        elif arg == "--cost_nav" and i + 1 < len(args):
            new_cost_nav = float(args[i + 1])
            i += 2
        elif arg == "--add-shares" and i + 1 < len(args):
            add_shares = float(args[i + 1])
            i += 2
        elif arg == "--add-amount" and i + 1 < len(args):
            add_amount = float(args[i + 1])
            i += 2
        elif arg == "--submit-date" and i + 1 < len(args):
            submit_date = args[i + 1]
            i += 2
        elif arg == "--confirm-date" and i + 1 < len(args):
            confirm_date = args[i + 1]
            i += 2
        else:
            i += 1

    update_holding(fund_code, new_shares=new_shares, new_cost_nav=new_cost_nav,
                   add_shares=add_shares, add_amount=add_amount,
                   submit_date=submit_date, confirm_date=confirm_date)


def run_holdings():
    """查看持仓概览: python main.py holdings"""
    show_holdings()


def run_dashboard():
    """生成可视化看板: python main.py dashboard"""
    print("=" * 60)
    print("  生成可视化看板")
    print("=" * 60)
    print("")
    path = generate_dashboard()
    print(f"\n✅ 用浏览器打开 {path} 即可查看")


def run_buy(fund_code, args):
    """
    新增建仓: python main.py buy <fund_code> --amount N [--submit-date YYYYMMDD]
    """
    print("=" * 60)
    print("  新增建仓")
    print("=" * 60)
    print("")

    if not fund_code:
        print("  [ERROR] 请指定基金代码")
        print("  用法: python main.py buy <fund_code> --amount N [--name XXX] [--etf-code XXX] [--submit-date YYYYMMDD]")
        print("  示例: python main.py buy 012345 --amount 50000")
        print("        python main.py buy 012345 --amount 50000 --submit-date 20260512  # 补录历史申购")
        return

    amount = None
    etf_code = None
    name = None
    submit_date = None

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--amount" and i + 1 < len(args):
            amount = float(args[i + 1])
            i += 2
        elif arg == "--etf-code" and i + 1 < len(args):
            etf_code = args[i + 1]
            i += 2
        elif arg == "--name" and i + 1 < len(args):
            name = args[i + 1]
            i += 2
        elif arg == "--submit-date" and i + 1 < len(args):
            submit_date = args[i + 1]
            i += 2
        else:
            i += 1

    if not amount or amount <= 0:
        print("  [ERROR] 请指定有效的申购金额 (--amount N)")
        return

    buy_new_holding(fund_code, amount, etf_code=etf_code, name=name, submit_date=submit_date)


def run_confirm(fund_code, args):
    """
    确认在途申购: python main.py confirm <fund_code> --shares N --nav N [--confirm-date YYYYMMDD]
    """
    print("=" * 60)
    print("  确认申购")
    print("=" * 60)
    print("")

    if not fund_code:
        print("  [ERROR] 请指定基金代码")
        print("  用法: python main.py confirm <fund_code> --shares N --nav N [--confirm-date YYYYMMDD]")
        return

    shares = None
    nav = None
    confirm_date = None

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--shares" and i + 1 < len(args):
            shares = float(args[i + 1])
            i += 2
        elif arg == "--nav" and i + 1 < len(args):
            nav = float(args[i + 1])
            i += 2
        elif arg == "--confirm-date" and i + 1 < len(args):
            confirm_date = args[i + 1]
            i += 2
        else:
            i += 1

    if shares is None or nav is None:
        print("  [ERROR] 请同时指定 --shares 和 --nav")
        return

    confirm_buy(fund_code, shares, nav, confirm_date=confirm_date)


def run_trades(args):
    """
    查看交易历史: python main.py trades [--fund-code XXX] [--type buy|sell] [--limit N]
    """
    fund_code = None
    trade_type = None
    limit = 20

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--fund-code" and i + 1 < len(args):
            fund_code = args[i + 1]
            i += 2
        elif arg == "--type" and i + 1 < len(args):
            trade_type = args[i + 1]
            i += 2
        elif arg == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])
            i += 2
        else:
            i += 1

    show_trades(fund_code=fund_code, trade_type=trade_type, limit=limit)


def run_add_etf(args):
    """
    添加自定义ETF到候选池: python main.py add-etf --etf-code XXX --fund-code XXX --name XXX [--category satellite]
    """
    etf_code = None
    fund_code = None
    name = None
    category = "satellite"

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--etf-code" and i + 1 < len(args):
            etf_code = args[i + 1]
            i += 2
        elif arg == "--fund-code" and i + 1 < len(args):
            fund_code = args[i + 1]
            i += 2
        elif arg == "--name" and i + 1 < len(args):
            name = args[i + 1]
            i += 2
        elif arg == "--category" and i + 1 < len(args):
            category = args[i + 1]
            i += 2
        else:
            i += 1

    if not fund_code or not name:
        print("  [ERROR] --fund-code 和 --name 为必填项")
        print("  用法: python main.py add-etf --fund-code 012345 --name 某某ETF [--etf-code 159XXX.SZ] [--category satellite]")
        return

    add_etf_to_pool(etf_code=etf_code, fund_code=fund_code, name=name, category=category)


def run_etf_pool():
    """列出候选池ETF: python main.py pool"""
    show_etf_pool()


def run_import(csv_path):
    """导入现有持仓: python main.py import <csv文件路径>"""
    print("=" * 60)
    print("  导入现有持仓")
    print("=" * 60)
    print(f"  CSV文件: {csv_path}")
    print("")

    if not csv_path:
        print("  [ERROR] 请指定CSV文件路径")
        print("  用法: python main.py import <文件路径.csv>")
        print("  示例: python main.py import my_holdings.csv")
        return

    import_holdings_from_csv(csv_path)

    # 导入后自动刷新一次净值
    print("\n[POST] 刷新导入持仓的净值...")
    update_holdings_nav()

    print("\n✅ 持仓导入完成! 运行 python main.py 即可查看基于当前持仓的信号")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()
        if cmd == "init":
            run_init()
        elif cmd == "import":
            csv_path = sys.argv[2] if len(sys.argv) > 2 else None
            run_import(csv_path)
        elif cmd == "buy":
            fund_code = sys.argv[2] if len(sys.argv) > 2 else None
            run_buy(fund_code, sys.argv[3:])
        elif cmd == "confirm":
            fund_code = sys.argv[2] if len(sys.argv) > 2 else None
            run_confirm(fund_code, sys.argv[3:])
        elif cmd == "sell":
            fund_code = sys.argv[2] if len(sys.argv) > 2 else None
            run_sell(fund_code, sys.argv[3:])
        elif cmd == "update":
            fund_code = sys.argv[2] if len(sys.argv) > 2 else None
            run_update(fund_code, sys.argv[3:])
        elif cmd == "holdings":
            run_holdings()
        elif cmd == "trades":
            run_trades(sys.argv[2:])
        elif cmd == "add-etf":
            run_add_etf(sys.argv[2:])
        elif cmd == "pool":
            run_etf_pool()
        elif cmd == "dashboard":
            run_dashboard()
        else:
            print(f"未知命令: {sys.argv[1]}")
            print("可用命令:")
            print("  python main.py              每日运行 (更新数据+计算信号+生成看板)")
            print("  python main.py init         首次初始化 (建表+拉历史数据)")
            print("  python main.py import <csv> 导入现有持仓")
            print("  python main.py buy <code> --amount N [--submit-date YYYYMMDD]   新增建仓(支持历史日期)")
            print("  python main.py confirm <code> --shares N --nav N [--confirm-date YYYYMMDD]  确认在途申购")
            print("  python main.py sell <code> [--ratio 0.5]          赎回(支持部分赎回)")
            print("                                [--submit-date YYYYMMDD]")
            print("                                [--confirm-date YYYYMMDD]")
            print("  python main.py update <code> [--shares N] [--cost_nav N]  更新持仓")
            print("                           [--add-shares N] [--add-amount N]  追加加仓")
            print("                           [--submit-date YYYYMMDD] [--confirm-date YYYYMMDD]  指定历史日期")
            print("  python main.py holdings     查看持仓概览")
            print("  python main.py trades       查看交易历史 [--fund-code X] [--type buy|sell] [--limit N]")
            print("  python main.py add-etf --fund-code X --name X     添加自定义ETF到候选池")
            print("  python main.py pool         列出候选池ETF")
            print("  python main.py dashboard    生成可视化看板")
    else:
        run_daily()

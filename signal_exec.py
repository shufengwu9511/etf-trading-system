# ============================================================
# 信号执行闭环 (2026-11-18)
#
# 背景: signals.is_executed / executed_at 两个字段自建表起就从未被写入过,
#       逾期告警只能绕开它、从 trades 反推「连续报警段之后有没有卖出成交」。
#       用户口径: 「信号执行闭环应该在持仓更新时就记好」。
#
# 设计: 在 buy / sell / confirm / update 写入 trades 的同一个 conn 里,
#       由 attach_trade_to_signal() 把命中的信号标记为已执行并双向关联。
#       与交易写入共用同一事务 → commit 一次, 不会出现「交易记了、信号没记」。
#
# 匹配口径 (务求保守, 宁可漏认领也不误认领):
#   1. 方向: buy 只认领 direction='buy'; sell 只认领 'sell' (含 stop 三类)
#      和宽基 'reduce'。direction='hold' 不认领。
#   2. 标的: 先按 etf_code 匹配 (同标的老仓 110020 / 新份额 007339 归并),
#      再按 fund_code 兜底。
#   3. 时间: 信号日期 <= 交易提交日, 且相差不超过 SIGNAL_EXEC_LOOKBACK_DAYS
#      个自然日。场外 T 日 15:00 前提交按 T 日净值, 允许隔夜/隔周末执行。
#   4. 排除: 已执行信号、reason 含「已作废」的假信号、观察仓。
#   5. 多候选时取最近一条 (同日多条则取 id 最大), 避免把天天报的
#      宽基 reduce 信号一次性全部认领。
# ============================================================
from datetime import datetime, timedelta

from config import SIGNAL_EXEC_LOOKBACK_DAYS, SIGNAL_EXEC_ENABLED


# 交易类型 → 允许认领的信号方向
_EXEC_DIRECTIONS = {
    "buy": ("buy",),
    "sell": ("sell", "reduce"),
}


def link_trade_to_signal(conn, trade_id, trade_type, fund_code, etf_code,
                         submit_date, verbose=True):
    """
    把一笔刚写入的 trades 记录与它响应的信号关联起来。

    必须在 INSERT INTO trades 之后、conn.commit() 之前调用, 共用同一连接/事务。
    返回命中的信号 dict (含 id / signal_date / signal_type), 未命中返回 None。

    未命中是正常情况 —— 绝大多数历史交易是脱离信号手动下单的, 不该硬凑关联。
    """
    if not SIGNAL_EXEC_ENABLED or not trade_id:
        return None

    directions = _EXEC_DIRECTIONS.get(trade_type)
    if not directions or not submit_date:
        return None

    try:
        submit_dt = datetime.strptime(str(submit_date), "%Y%m%d")
    except (ValueError, TypeError):
        return None
    earliest = (submit_dt - timedelta(days=SIGNAL_EXEC_LOOKBACK_DAYS)).strftime("%Y%m%d")
    submit_date = str(submit_date)

    placeholders = ",".join("?" for _ in directions)
    base_sql = f"""
        SELECT id, signal_date, signal_type, direction, name, reason
        FROM signals
        WHERE is_executed = 0
          AND direction IN ({placeholders})
          AND signal_date >= ? AND signal_date <= ?
          AND (reason IS NULL OR reason NOT LIKE '%已作废%')
          AND {{match}}
        ORDER BY signal_date DESC, id DESC
        LIMIT 1
    """
    base_params = [*directions, earliest, submit_date]

    row = None
    # 优先按场内 ETF 代码匹配 (兼容同标的不同份额)
    if etf_code:
        row = conn.execute(
            base_sql.format(match="etf_code = ?"),
            (*base_params, etf_code),
        ).fetchone()
    # 兜底按场外基金代码匹配
    if row is None and fund_code:
        row = conn.execute(
            base_sql.format(match="fund_code = ?"),
            (*base_params, str(fund_code)),
        ).fetchone()

    if row is None:
        if verbose:
            print(f"  [闭环] 无对应待执行信号, 记为手动交易 (trade#{trade_id})")
        return None

    sig = dict(row)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        UPDATE signals
        SET is_executed = 1, executed_at = ?, executed_trade_id = ?
        WHERE id = ?
    """, (now, trade_id, sig["id"]))
    conn.execute("""
        UPDATE trades SET signal_source = ? WHERE id = ?
    """, (f"signal:{sig['id']}", trade_id))

    if verbose:
        print(f"  [闭环] 已关联信号 #{sig['id']} "
              f"({sig['signal_date']} {sig['signal_type']} {sig['direction']}) "
              f"→ trade#{trade_id}")
    return sig


def backfill_execution_links(conn, dry_run=True, verbose=True):
    """
    历史数据回填: 把已有 trades 与 signals 按同样口径补上关联。

    仅用于核对/补账, 默认 dry_run=True 只报告不改库。历史交易绝大多数是
    脱离信号的手动操作, 漏认领属正常; 必须在人工确认后才执行 dry_run=False。
    返回 (linked, skipped)。
    """
    rows = conn.execute("""
        SELECT id, trade_type, fund_code, etf_code, submit_date
        FROM trades
        WHERE trade_type IN ('buy', 'sell') AND status IN ('done', 'confirmed')
          -- 幂等保护: 已关联过的交易不再二次认领, 重复执行 --apply 也不污染数据
          AND (signal_source IS NULL OR signal_source NOT LIKE 'signal:%')
        ORDER BY submit_date, id
    """).fetchall()

    linked, skipped = [], []
    for r in rows:
        t = dict(r)
        sig = link_trade_to_signal(
            conn, t["id"], t["trade_type"], t["fund_code"], t["etf_code"],
            t["submit_date"], verbose=False,
        )
        if sig:
            linked.append((t["id"], t["submit_date"], t["trade_type"],
                           t["fund_code"], sig["id"], sig["signal_date"]))
        else:
            skipped.append(t["id"])

    if dry_run:
        conn.rollback()  # dry-run 绝不落库
    elif verbose:
        print(f"  [闭环] 历史回填完成: 关联 {len(linked)} 笔, 无对应信号 {len(skipped)} 笔")
    return linked, skipped

"""动量持续性过滤 (2026-11-17 新增, 2026-11-18 完善)

背景: 原买入三重闸门 = 「动量Top3 + 综合动量>0 + 收盘>MA20」, 全部只看**当日快照**,
本质是追过去 10/30 日涨幅。实测机器人 020972 在 2026-11-04~11-13 期间综合动量
+7.29 → +9.23 → +0.34 → +2.89 → +7.43 → +5.23 → +1.94 → +3.71 单日跳 8.9pp,
11-10 在反弹尖峰 +7.43/#1 发出买入信号, 买入后 6 天转为 -0.60 并浮亏 -7.61%。
故加一层「动量是否持续」的校验, 只约束**新买入**, 不改变已持仓的卖出判定
(与 MA20 闸门同原则: 只做买入侧独立闸门, 不提前过滤 TopN, 避免误伤持仓)。

2026-11-18 完善: 原 improve 只比较窗口两端, 会**跳过中间深坑**。实证半导体 021893
综合动量 11-13 +0.49% → 11-16 -6.81% → 11-17 +1.52%, 回看2日只比 11-17 与 11-13,
V型反弹被判「持续走强」, 主流程据此给出 ¥240,000 申购信号(盘中口径同期却是 -3.54%)。
故新增硬约束 require_positive: 窗口内**每一个交易日**的综合动量都必须 >0。

三处共用本模块, 口径必须一致:
  - strategy.py          收盘 K 线 (index_daily)
  - intraday_signal.py   收盘序列 + 盘中现价代理当日收盘
  - backtest.py          历史价格序列

参数见 config.MOMENTUM_PERSIST_*。
"""
from config import (
    MOMENTUM_SHORT,
    MOMENTUM_MEDIUM,
    MOMENTUM_WEIGHT_SHORT,
    MOMENTUM_WEIGHT_MEDIUM,
    MOMENTUM_MIN_THRESHOLD,
)


def composite_at(closes, end_idx):
    """计算以 end_idx 为「当日」的综合动量 (价格比法), 与三处主计算完全同口径。

    closes: 升序收盘价序列; end_idx: 当日索引(含)。
    10日动量 = closes[end_idx] / closes[end_idx-10] - 1
    30日动量 = closes[end_idx] / closes[end_idx-30] - 1
    数据不足返回 None。
    """
    if closes is None or end_idx < MOMENTUM_MEDIUM or end_idx >= len(closes):
        return None
    try:
        s = float(closes[end_idx])
        if s <= 0:
            return None
        m_short = (s / float(closes[end_idx - MOMENTUM_SHORT]) - 1) * 100.0
        m_medium = (s / float(closes[end_idx - MOMENTUM_MEDIUM]) - 1) * 100.0
        return m_short * MOMENTUM_WEIGHT_SHORT + m_medium * MOMENTUM_WEIGHT_MEDIUM
    except (TypeError, ValueError, ZeroDivisionError, IndexError):
        return None


def check_persistence(closes, enabled=True, mode="improve", lookback=3, min_delta=0.0,
                      require_positive=True):
    """判断动量是否具备持续性。

    closes   : 升序收盘价序列, 最后一个元素 = 当日收盘 (盘中口径为「现价代理收盘」)
    enabled  : 总开关, False 时恒通过 (用于 A/B 回测与紧急回退)
    mode     :
        "improve"     — 当日综合动量 >= lookback 个交易日前 + min_delta,
                        拦住「从高位崩下来、小幅反弹又被当成强势」的假信号
        "consecutive" — 最近 lookback 个交易日综合动量持续 > MOMENTUM_MIN_THRESHOLD,
                        拦住单日跳变刚转正的标的
    lookback : 回看交易日数
    min_delta: improve 模式要求的最小改善幅度 (百分点)
    require_positive : (2026-11-18) 硬约束 — 窗口 [当日-lookback, 当日] 内
                       每一个交易日的综合动量都必须 > MOMENTUM_MIN_THRESHOLD。
                       与 mode 叠加生效, 堵住 improve「只比端点跳过中间深坑」的漏洞。
                       设 False 可退回 11-17 旧口径做 A/B。

    返回 (ok: bool, info: dict)。历史数据不足时不拦截 (避免开局无信号)。
    """
    info = {"enabled": bool(enabled), "mode": mode, "lookback": lookback,
            "require_positive": bool(require_positive),
            "today": None, "past": None, "window": None,
            "ok": True, "reason": ""}
    if not enabled or mode == "off" or not closes:
        info["reason"] = "未启用"
        return True, info

    n = len(closes) - 1
    today = composite_at(closes, n)
    info["today"] = round(today, 4) if today is not None else None
    if today is None:
        info["reason"] = "历史数据不足, 不拦截"
        return True, info

    # 2026-11-18 硬约束: 窗口内每日动量必须为正 (拦 V 型反弹 / 两负也判「走强」)
    if require_positive:
        window_vals = [composite_at(closes, i) for i in range(n - lookback, n + 1)]
        info["window"] = [round(v, 4) if v is not None else None for v in window_vals]
        bad = [v for v in window_vals if v is not None and v <= MOMENTUM_MIN_THRESHOLD]
        if bad:
            info["ok"] = False
            window_txt = ", ".join(f"{v:+.2f}%" if v is not None else "N/A"
                                   for v in window_vals)
            info["reason"] = (f"回看{lookback}日窗口 [{window_txt}] 内有"
                              f"{len(bad)}日动量≤{MOMENTUM_MIN_THRESHOLD:.0f}% "
                              f"(最低{min(bad):+.2f}%), 不满足全窗口为正")
            return False, info

    if mode == "consecutive":
        vals = [composite_at(closes, i) for i in range(n - lookback + 1, n + 1)]
        if any(v is None for v in vals):
            info["reason"] = "回看窗口数据不足, 不拦截"
            return True, info
        ok = all(v > MOMENTUM_MIN_THRESHOLD for v in vals)
        info["past"] = round(min(vals), 4)
        info["reason"] = (f"近{lookback}日综合动量区间 "
                          f"[{min(vals):+.2f}%, {max(vals):+.2f}%] "
                          f"{'持续为正' if ok else '曾转负'}")
        info["ok"] = ok
        return ok, info

    # 默认 improve
    past = composite_at(closes, n - lookback)
    if past is None:
        info["reason"] = "回看窗口数据不足, 不拦截"
        return True, info
    info["past"] = round(past, 4)
    ok = today >= past + min_delta
    info["ok"] = ok
    info["reason"] = (f"当日{today:+.2f}% vs {lookback}日前{past:+.2f}% "
                      f"(Δ{today - past:+.2f}pp, 需≥{min_delta:+.2f}pp) "
                      f"{'持续走强' if ok else '动量转弱'}")
    return ok, info

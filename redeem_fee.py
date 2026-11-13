# -*- coding: utf-8 -*-
"""
场外 ETF 联接基金赎回费率表与费用计算。

2026-11-12 建立。背景: 系统长期把 trades.amount 按「份额×净值」毛额记账、
fee 恒为 0, 但平台净到账实测少 0.5%(110020 两笔精确到分), 已实现盈亏被
系统性高估。

🔴 各只 ETF 联接基金的赎回费率并不统一, 不能套「通用阶梯」。2026-11-12 已把
基金池 22 只逐只核实并全量登记(来源: AkShare fund_fee_em / 天天基金 F10 费率页,
易方达、富国官网基金页面与证监会产品资料概要交叉验证):
  - 16 只 : 0-6天1.5% / ≥7天 0            (本来就没有常规赎回费)
  - 110020 / 008975 : 7-364天仍收 0.5%    (最重, 前者已实证两笔)
  - 007028 / 007379 / 013304 : 7-29天0.5% / 30-179天0.1% / ≥180天0
  - 000307 黄金 : 7-364天 0.20%           (较轻)
  - 019071 / 022350 : 7-29天0.1% / ≥30天0
未登记的基金仍不猜费率: fee 记 0 且返回 verified=False, 由 sell 路径打印告警、
在 remark 标记「费率未核实」, 等拿到平台净到账后人工补录(--fee)。宁可留白,
不用假设覆盖真值。
"""
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")

# tiers: (持有天数下界, 上界(含, None=无穷), 费率)
def _ladder(*rows):
    return tuple((lo, hi, Decimal(rate)) for lo, hi, rate in rows)


# 五类真实阶梯, 逐只核实后引用; 新增基金必须先查费率再归类
L_730 = _ladder((0, 6, "0.015"), (7, 364, "0.005"),
                (365, 729, "0.0025"), (730, None, "0"))
L_180 = _ladder((0, 6, "0.015"), (7, 29, "0.005"),
                (30, 179, "0.001"), (180, None, "0"))
L_30 = _ladder((0, 6, "0.015"), (7, 29, "0.001"), (30, None, "0"))
L_7 = _ladder((0, 6, "0.015"), (7, None, "0"))
L_GOLD = _ladder((0, 6, "0.015"), (7, 364, "0.002"),
                 (365, 729, "0.0005"), (730, None, "0"))
L_C50 = _ladder((0, 6, "0.015"), (7, 29, "0.005"), (30, None, "0"))

_SRC = "2026-11-12 全池核查: 天天基金F10费率页(fund_fee_em) + 基金公司官网交叉验证"

REDEEM_FEE_RULES = {
    # ---------- 宽基 ----------
    "110020": {"name": "易方达沪深300ETF联接A", "source": _SRC + "; 平台净额实证两笔精确0.5%", "tiers": L_730},
    "007028": {"name": "易方达中证500ETF联接A", "source": _SRC, "tiers": L_180},
    "007379": {"name": "易方达上证50ETF联接A", "source": _SRC, "tiers": L_180},
    # ---------- 卫星: 7-364天仍收费 ----------
    "008975": {"name": "富国中证消费50ETF联接A", "source": _SRC + "; 富国官网确认", "tiers": L_730},
    "000307": {"name": "易方达黄金ETF联接A", "source": _SRC + "; 易方达官网确认", "tiers": L_GOLD},
    "013304": {"name": "易方达科创创业50ETF联接A", "source": _SRC, "tiers": L_180},
    # ---------- 卫星: 7-29天轻收费 ----------
    "019071": {"name": "嘉实通信ETF联接", "source": _SRC, "tiers": L_30},
    "022350": {"name": "嘉实集成电路ETF联接", "source": _SRC, "tiers": L_30},
    # ---------- 卫星: ≥7天全免 ----------
    "021893": {"name": "易方达半导体ETF联接", "source": _SRC, "tiers": L_7},
    "017853": {"name": "易方达云计算ETF联接", "source": _SRC, "tiers": L_7},
    "024194": {"name": "永赢卫星ETF联接", "source": _SRC, "tiers": L_7},
    "018315": {"name": "易方达高端制造ETF联接", "source": _SRC, "tiers": L_7},
    "012862": {"name": "汇添富电池ETF联接", "source": _SRC, "tiers": L_7},
    "021033": {"name": "易方达储能电池ETF联接", "source": _SRC, "tiers": L_7},
    "020602": {"name": "易方达红利低波ETF联接", "source": _SRC, "tiers": L_7},
    "017646": {"name": "易方达光伏ETF联接", "source": _SRC, "tiers": L_7},
    "025856": {"name": "华夏电网设备ETF联接", "source": _SRC, "tiers": L_7},
    "020972": {"name": "易方达机器人ETF联接", "source": _SRC, "tiers": L_7},
    "018896": {"name": "易方达消费电子ETF联接A", "source": _SRC, "tiers": L_7},
    "020685": {"name": "南方科创新材料ETF联接A", "source": _SRC, "tiers": L_7},
    "019666": {"name": "易方达创新药ETF联接A", "source": _SRC, "tiers": L_7},
    "021822": {"name": "景顺长城石油天然气ETF联接A", "source": _SRC, "tiers": L_7},
    # ---------- 新钱用的低费率份额(2026-11-12 起) ----------
    "007339": {"name": "易方达沪深300ETF联接C", "source": "易方达官网+证监会概要; ≥7天0, 销售服务费0.20%/年", "tiers": L_7},
    "022055": {"name": "富国中证消费50ETF联接E", "source": "富国官网; ≥7天0, 销售服务费0.10%/年", "tiers": L_7},
    "008976": {"name": "富国中证消费50ETF联接C", "source": "富国官网; 7-29天0.5%, 销售服务费0.40%/年", "tiers": L_C50},
}

# 已核实「免赎回费」的份额类别(可在此登记, 但优先用带完整阶梯的 RULES)
FEE_FREE_CODES = set()


def _dec(v):
    return Decimal(str(v if v is not None else 0))


def get_rate(fund_code, hold_days):
    """返回 (费率, 是否已核实)。未登记的基金返回 (None, False)。"""
    code = str(fund_code or "").strip()
    if code in FEE_FREE_CODES:
        return Decimal("0"), True
    rule = REDEEM_FEE_RULES.get(code)
    if rule is None:
        return None, False
    d = int(hold_days)
    for lo, hi, rate in rule["tiers"]:
        if d >= lo and (hi is None or d <= hi):
            return rate, True
    return rule["tiers"][-1][2], True


def calc_redeem_fee(fund_code, hold_days, gross_amount, fee_override=None):
    """
    计算赎回费, 返回 dict: gross / fee / net / rate / verified / note。

    fee_override: 平台实际赎回费(优先使用), 用于事后按平台净额补录。
    约定: trades.amount 存「净到账金额」, trades.fee 存赎回费, 毛额 = 份额×净值。
    """
    gross = _dec(gross_amount).quantize(CENT, rounding=ROUND_HALF_UP)
    if fee_override is not None:
        fee = _dec(fee_override).quantize(CENT, rounding=ROUND_HALF_UP)
        return {"gross": gross, "fee": fee, "net": gross - fee,
                "rate": None, "verified": True, "note": "按平台实际赎回费录入"}
    rate, verified = get_rate(fund_code, hold_days)
    if not verified:
        return {"gross": gross, "fee": Decimal("0"), "net": gross,
                "rate": None, "verified": False,
                "note": f"⚠️ {fund_code} 赎回费率未核实, 本次按 0 记账; 请查平台净额后用 --fee 补录"}
    fee = (gross * rate).quantize(CENT, rounding=ROUND_HALF_UP)
    return {"gross": gross, "fee": fee, "net": gross - fee,
            "rate": rate, "verified": True,
            "note": f"持有{int(hold_days)}天 × {rate * 100}%"}


def unverified_codes():
    """基金池中尚未登记费率的基金代码(用于生成待核台账)。"""
    try:
        import config
        pools = list(getattr(config, "CORE_ETFS", []) or []) + list(getattr(config, "SATELLITE_ETFS", []) or [])
        known = set(REDEEM_FEE_RULES) | FEE_FREE_CODES
        return sorted({str(e["fund_code"]) for e in pools
                       if e.get("fund_code") and str(e["fund_code"]) not in known})
    except Exception:
        return []

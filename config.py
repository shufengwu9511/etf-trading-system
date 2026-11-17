# ============================================================
# ETF联接基金交易系统 - 全局配置
# 交易模式: 纯场外ETF联接基金申赎 (易方达财富账户)
# ============================================================
import json
import os

# ---- Tushare 数据源 ----
try:
    from local_secrets import TUSHARE_TOKEN
except ImportError:
    TUSHARE_TOKEN = None  # 用户需从 local_secrets.example.py 复制为 local_secrets.py 并填入真实Token

# ---- 资金配置 (单位: 元) ----
PRINCIPAL = 2_000_000              # 本金 200万 (仅作盈亏参考基准)
TOTAL_CAPITAL = 2_000_000          # 总资金 200万 (仓位/单笔上限的计算基数)

# 账户真实现金 (货币基金 000621 余额), 由 scripts/efunds_sync.py 每次同步后自动写入
# 存在该文件时, "可用现金"以它为准; 否则回落到 TOTAL_CAPITAL - 持仓市值
# 背景(2026-10-28): 原先用 TOTAL_CAPITAL - 市值 推算现金, 会把累计亏损当成可用现金,
#   实测虚高 ¥406,668 (系统 ¥1,715,522 vs 实际 ¥1,308,854), 导致建议金额超出真实购买力
ACCOUNT_CASH_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "account_cash.json"
)
CORE_RATIO = 0.40                  # 宽基底仓占比 40% (2026-11-04 由 60% 下调, 提升动量轮动占比)
SATELLITE_RATIO = 0.60             # 行业卫星占比 60% (2026-11-04 由 40% 上调)
SINGLE_ETF_MAX_RATIO = 0.12        # 单只行业ETF单笔上限 12% (=24万, 2026-11-05 由 8% 上调)
                                   # 依据: 回测 40/60+3只, 单笔 8%→12% 年化 11.10%→15.81%,
                                   #   回撤 -7.41%→-8.52%, Calmar 1.50→1.86 (见 scripts/satellite_quota_study.py)
                                   # 关键: 卫星实际可部署 = min(SATELLITE_RATIO, 单笔上限×持仓只数)
                                   #   8%×3只 = 24% → 60% 的卫星额度有 36% 永远躺在货币基金里
                                   #   12% 后实际可部署 36%; 按真实权益 160.7万 算单票 14.9%, 仍低于 WARN 15%
MIN_TRADE_AMOUNT = 100             # 最小申购金额

# ---- 集中度风控 (2026-09-03新增) ----
# SINGLE_ETF_MAX_RATIO 只约束"单笔买入金额", 对累计持仓无上限,
# 向下摊平会把单只仓位堆到远超设计的比例 (实测半导体曾达59%).
# 这里补上累计持仓的两级预警, 仅提示不强制, 由人工决定.
CONCENTRATION_WARN_RATIO = 0.15    # 单只持仓占总资金 15% → 黄色预警
CONCENTRATION_DANGER_RATIO = 0.25  # 单只持仓占总资金 25% → 红色危险

# ---- 宽基底仓配置 ----
# index_code: 底层指数代码, 用于 index_daily(行情) 和 index_dailybasic(估值)
# fund_code: 易方达财富上可申购的联接基金代码
# 2026-11-04 调整:
#   1) 宽基总占比 60% → 40%, target_weight 按比例缩减, 三者加总 = CORE_RATIO
#   2) 科创50 → 上证50。科创50 年化波动 34.87% / 历史最大回撤 -47.86%,
#      而宽基仓位只有 PE百分位 + MA100 + 兜底-20% 三道保护, 没有个股止损,
#      波动率不匹配。上证50 波动 15.67% / 回撤 -22.54%, 更贴合宽基风控目标。
#      代价: 波动与回撤改善约 10pp, 但 607 日样本年化让出约 3pp (见 scripts/core_ratio_study.py)
CORE_ETFS = [
    {"code": "510310.SH", "name": "沪深300ETF",  "index_code": "000300.SH", "target_weight": 0.17, "fund_code": "110020", "company": "易方达"},
    {"code": "510580.SH", "name": "中证500ETF",  "index_code": "000905.SH", "target_weight": 0.13, "fund_code": "007028", "company": "易方达"},
    {"code": "510100.SH", "name": "上证50ETF",   "index_code": "000016.SH", "target_weight": 0.10, "fund_code": "007379", "company": "易方达"},
]

# ---- 行业卫星ETF候选池 ----
# 行业ETF没有PE估值, 用fund_daily拉取ETF自身的日线行情
# 2026-04-29量化扫描优化: 淘汰创新药(#33)/机器人(#27)/传媒(#18), 新增集成电路(#4)/电池(#7)/高端制造(#16)
# 保留通信/半导体/云计算(AI核心), 储能电池/黄金/红利低波(防守), 卫星(商业航天)
# 注: 光伏暂时保留(仍有持仓待清仓), 清仓后移除
SATELLITE_ETFS = [
    # AI算力方向 (高弹性进攻组)
    {"code": "159695.SZ", "name": "通信ETF",     "category": "ai",      "fund_code": "019071", "company": "嘉实"},
    {"code": "159558.SZ", "name": "半导体ETF",   "category": "ai",      "fund_code": "021893", "company": "易方达"},
    {"code": "516510.SH", "name": "云计算ETF",   "category": "ai",      "fund_code": "017853", "company": "易方达"},
    {"code": "159546.SZ", "name": "集成电路ETF", "category": "ai",      "fund_code": "022350", "company": "嘉实"},
    # 商业航天方向
    {"code": "159206.SZ", "name": "卫星ETF",     "category": "space",   "fund_code": "024194", "company": "永赢"},
    # 制造方向
    {"code": "562910.SH", "name": "高端制造ETF", "category": "manufacture", "fund_code": "018315", "company": "易方达"},
    # 新能源方向
    {"code": "159755.SZ", "name": "电池ETF",     "category": "energy",  "fund_code": "012862", "company": "汇添富"},
    {"code": "159566.SZ", "name": "储能电池ETF", "category": "energy",  "fund_code": "021033", "company": "易方达"},
    # 防御方向
    {"code": "159934.SZ", "name": "黄金ETF",     "category": "defense", "fund_code": "000307", "company": "易方达"},
    {"code": "563020.SH", "name": "红利低波ETF", "category": "defense", "fund_code": "020602", "company": "易方达"},
    # 待清仓 (清仓后移除)
    {"code": "562970.SH", "name": "光伏ETF",     "category": "energy",  "fund_code": "017646", "company": "易方达"},
]

# ---- 数据源: Tushare 接口映射 ----
# 宽基ETF → 用 index_daily + index_dailybasic (底层指数)
# 行业ETF → 用 fund_daily (ETF自身日线)
DATA_SOURCE = "tushare"

# ---- 数据源: ETF 日K线 ----
# 2026-10-23 切换: pytdx 服务端收紧, get_security_bars 全节点被拒(只回条数不给数据),
# 但 get_xdxr_info 仍正常 → 改为「新浪原始价 + pytdx 官方除权记录复权」组合。
#   "sina" = 新浪(ak.fund_etf_hist_sina) 取原始价 + pytdx xdxr 复权  ← 默认
#   "tdx"  = 纯 pytdx get_security_bars (旧行为, 服务端恢复后可用)
#   "auto" = 先新浪, 失败回落 pytdx
KLINE_SOURCE = "sina"

# ---- 数据源: 盘中实时行情 ----
# 2026-10-23 同上背景: pytdx get_security_quotes 也被服务端拒绝, 先试它等于白跑
# 一次全量节点扫描(~10s)。新浪 hq.sinajs.cn 实测 21只/0.3s 且稳定 → 提到首位。
#   "sina"  = 新浪优先, 失败回落 pytdx → AkShare  ← 默认
#   "pytdx" = 旧行为 (pytdx 优先, 挂了三层兜底)
QUOTE_SOURCE = "sina"

# ---- 策略参数 ----
# PE估值择时 (宽基)
PE_LOOKBACK_YEARS = 5                   # PE百分位回溯年限
PE_BUY_THRESHOLD = 30                   # PE百分位 < 30% 可买入
PE_SELL_THRESHOLD = 80                  # PE百分位 > 80% 应减仓
MA_PERIOD = 100                         # 均线周期 (初始化数据有限时降低)

# 动量轮动 (行业)
MOMENTUM_SHORT = 10                     # 短期动量 (10日涨幅)
MOMENTUM_MEDIUM = 30                    # 中期动量 (30日涨幅)
MOMENTUM_WEIGHT_SHORT = 0.6             # 短期权重 (周期缩短后提高短期权重)
MOMENTUM_WEIGHT_MEDIUM = 0.4            # 中期权重
MOMENTUM_MIN_THRESHOLD = 0              # 动量最低门槛: 综合得分<=0的ETF不纳入轮动

# ---- 动量持续性过滤 (2026-11-17 新增, 大树规则) ----
# 背景: 原买入三重闸门(Top3 + 动量>0 + 收盘>MA20)只看当日快照, 等于追涨。
# 机器人 020972 在 11-04~11-13 综合动量单日跳 8.9pp, 11-10 于反弹尖峰 +7.43%/#1
# 发出买入信号, 买后 6 天转负、浮亏 -7.61%。故买入侧再加一层"动量是否持续"。
# ⚠️ 只约束**新买入**, 不提前过滤 TopN / 不改变已持仓卖出判定 (与 MA20 闸门同原则)。
# 三处共用 momentum_persistence.py: strategy / intraday_signal / backtest。
MOMENTUM_PERSIST_ENABLED = True         # 总开关, False 可一键回退做 A/B
MOMENTUM_PERSIST_MODE = "improve"       # "improve" | "consecutive" | "off"
                                        # improve:     当日动量 >= N日前 + MIN_DELTA (拦"高位崩后小反弹")
                                        # consecutive: 最近N日动量持续 > MOMENTUM_MIN_THRESHOLD (拦单日跳变)
# 回看交易日数。2026-11-17 回测 A/B 选定 2 日:
#   2日: 年化27.44% / 回撤9.04% / 142笔 / 胜率42.0%  ← 默认, 几乎不损收益且降回撤
#   3日: 年化26.50% / 回撤9.81% / 150笔 / 胜率43.8%  ← 更保守, 能连申购日一起拦
#   5日: 年化27.05% / 回撤9.01% / 154笔 / 胜率42.7%
#   关闭: 年化27.54% / 回撤9.89% / 166笔 / 胜率39.5%
# 2日已能拦住机器人 11-09 的信号发出日(当日+2.89% vs 2日前+9.23%), 那才是决策点;
# 3日可连 11-10 申购日一起拦, 代价约 1pp 年化。要更严就把这里改成 3。
MOMENTUM_PERSIST_LOOKBACK = 2
MOMENTUM_PERSIST_MIN_DELTA = 0.0        # improve 模式要求的最小改善幅度 (百分点)

ROTATION_HOLD_COUNT = 3                 # 基础目标持仓数 (买入名单 = 动量Top3)
ROTATION_CYCLE_DAYS = 7                 # 轮动周期 (天). 2026-11-05 由 14 改 7:
                                        # 用户决定取消"跌出Top3需持有满14天"的缓冲,
                                        # 只要过了赎回费锁(MIN_HOLD_DAYS=7)就能卖。
                                        # 三处引用自动对齐: strategy 卖出门槛 /
                                        # intraday_signal 卖出门槛 / backtest 轮动节奏

# ---- 排名缓冲区 + 超额持仓 (2026-11-09 新增, 大树规则) ----
# 背景: 11-05 机器人#1 → 11-06 #4 → 11-09 #3, 三天三套Top3, 指令反复横跳。
# 根因是"10日动量 = 今日价/10日前价"的分母每天滚动: 11-06 机器人动量掉11.88pp,
# 其中 8.64pp 来自 10-23 那根 +8.64% 大阳线滚进分母, 与当日行情完全无关。
ROTATION_RANK_BUFFER = 2               # 卖出排名缓冲宽度:
                                        # 不再"跌出Top3就卖", 持仓只要仍在 Top(3+2)=Top5 内
                                        # 且健康(综合动量>0 且 收盘>MA20) 就继续持有, 压住边界抖动
ROTATION_MAX_HOLD_COUNT = 5            # 卫星持仓硬上限: 允许短期超过基础3只, 最多5只。
                                        # 超额前提 = 所有现有卫星持仓都健康(动量>0 且 MA20上方)。
                                        # 解决"满3只 + 7天赎回费锁卖不掉 + Top3有新标的"的死锁,
                                        # 又不至于无限扩张。三处同步: strategy / intraday_signal / backtest

# 移动止盈 + 止损
# 移动止盈: 利润超过激活阈值后跟踪峰值, 从峰值回撤超过阈值则清仓
# 针对场外联接基金T+1执行延迟优化 (盘后申赎, 信号日→执行日隔1天)
TRAILING_STOP_ACTIVATE = 8.0            # 激活阈值: 利润≥+8%开始跟踪峰值 (原6%, 提高以吸收延迟)
TRAILING_STOP_PULLBACK = 6.0            # 回撤阈值: 从峰值回撤6%触发清仓 (原4%, 覆盖1天执行延迟+正常波动)
TRAILING_STOP_PANIC_PULLBACK = 5.0      # 恐慌模式回撤收紧至5% (原3%, 恐慌时波动更大但仍需留余量)
TRAILING_STOP_TOP1_PULLBACK = 8.0       # 动量Top1容限8% (原6%, 让最强趋势多跑, 抵消延迟)
# 止损 (不变)
STOP_LOSS_THRESHOLD = -15.0             # 止损 -15%
STOP_LOSS_PANIC_THRESHOLD = -8.0        # 恐慌预警时收紧止损至 -8%
MIN_HOLD_DAYS = 7                       # 最短持有天数 (自然日, 避免1.5%惩罚赎回费)
                                        # 口径: 申购确认日(含) ~ 赎回确认日前一自然日(含),
                                        # 见 portfolio._calc_hold_days。2026-11-12 修正
                                        # 少算1天的 off-by-one (旧实现把今天当持有期终点)。

# 观察仓阈值 (2026-09-11新增, 大树规则)
# 市值低于此金额的持仓视为"观察仓": 不赎回/不补仓/不参与任何信号计算,
# 仅保留在持仓表里用于查看实际涨跌幅。避免为几百元仓位付出申赎成本与操作精力。
MIN_POSITION_VALUE = 1000               # 市值 < 1000元 → 观察仓, 信号计算中排除

# MA20 趋势闸门 (买入过滤 + 趋势止损)
MA20_STOP_LOSS_RATIO = 0.95             # 跌破MA20的5%触发趋势止损 (场外T+1需更宽缓冲, 原0.98太敏感)

# ---- 宽基底仓专用规则 (2026-10-14新增) ----
# 宽基不走移动止盈/MA20趋势止损 (那套由PE估值+MA100趋势管理),
# 但保留一条"超宽固定止损"兜底, 防止极端单边行情下底仓无底线拖累。
# 阈值明显宽于行业卫星(-15%), 因为宽基波动小、且底仓逻辑容忍更大回撤。
CORE_STOP_LOSS_THRESHOLD = -20.0        # 宽基兜底止损 -20%
CORE_STOP_LOSS_PANIC_THRESHOLD = -15.0  # 恐慌预警时收紧至 -15%
# 宽基"看空"信号的建议减仓比例: 当PE高估 或 MA100向下 且当前有持仓时,
# 建议减掉当前持仓的该比例 (无持仓则仅观望, 不产生方向错误的"减仓"提示)
CORE_REDUCE_RATIO = 0.30                # 建议减仓 30%

# ---- 执行纪律规则 (2026-10-23新增, 针对"信号不执行 + 向下摊平") ----
# 1) 反摊平红线: 已持仓浮亏达到/超过该阈值时, 不再生成任何加仓买入信号。
#    实证: 半导体17笔买入在净值3.10→2.14区间持续加仓, 184万投入浮亏23.7%,
#    摊平只放大仓位敞口, 并未真正降低成本。
ANTI_AVERAGING_LOSS_THRESHOLD = -10.0   # 浮亏 <= -10% → 禁止加仓
# 2) 卖出信号逾期提醒: 回看最近N个有信号的交易日, 期间发出过卖出信号但
#    没有任何对应赎回交易的标的, 在看板置顶标红。
#    实证: 2026-09-14 后26个交易日零交易, 三只持仓拖延成本约32万。
SIGNAL_OVERDUE_LOOKBACK_DAYS = 5        # 回看最近5个发出信号的交易日
SIGNAL_OVERDUE_GRACE_DAYS = 1           # 信号当日及次1日视为"在办理中", 超过才算逾期

# 保留旧参数以兼容 (已弃用, 仅用于向后兼容)
TAKE_PROFIT_THRESHOLD = 5.0
TAKE_PROFIT_TIER1 = 8.0
TAKE_PROFIT_TIER2 = 15.0

# 恐慌指数监控
# index_daily pct_chg: 百分比形式 (如 -2.5 表示跌2.5%)
PANIC_HS300_DROP = -2.5                 # 沪深300单日跌幅阈值 -2.5%
# ⚠️ 2026-09-03 修正: Tushare moneyflow_hsgt 的 north_money 实际 = hgt + sgt,
#    是「北向成交额」而非「净流入」, 恒为正数 (实测142行100%吻合, 范围20~49亿)。
#    且 2024-08 起北向净流入已停止披露, 原 PANIC_NORTH_OUTFLOW 判据
#    (north_money <= -100亿) 永远不可能成立 → 该指标为死代码。
#    改为可计算的「北向成交额异动」: 当日成交额 / 近20日均值 >= 阈值。
PANIC_NORTH_OUTFLOW = 1_000_000         # [已废弃/保留兼容] 原净流出阈值(万元), 不再参与判定
PANIC_NORTH_TURNOVER_RATIO = 1.25      # 北向成交额异动倍数 (较近20日均值)
                                       # 按历史分布标定: 122个样本 P90=1.19 P95=1.25 最大1.40,
                                       # 取P95→触发率约5% (辅助指标, 恐慌需4项中2项)
PANIC_NORTH_MA_DAYS = 20                # 北向成交额均值窗口(交易日)
PANIC_LIMIT_DOWN_COUNT = 100            # 跌停家数阈值
PANIC_VOLUME_RATIO = 1.5                # 成交量放大阈值 (较5日均量)
PANIC_TRIGGER_COUNT = 2                 # 上述指标中几项触发则预警
PANIC_COOLDOWN_DAYS = 2                 # 预警解除需连续N日正常

# ---- 场外基金特有配置 ----
SIGNAL_CUTOFF_TIME = "14:30"            # 信号输出截止时间 (T日净值)
TRADE_EXECUTION_REMINDER = "请在14:30前完成申赎操作，享受当日净值成交"

# ---- 数据存储 ----
DATA_DIR = "data"
DB_NAME = "trading_system.db"
LOG_DIR = "logs"
# ---- 自定义基金候选池 ----
# 用户可通过 `python main.py add-etf` 动态添加基金到 data/etf_pool.json
# 加载时自动合并到 SATELLITE_ETFS，不影响其他模块的引用
def _load_custom_etfs():
    """从 data/etf_pool.json 加载用户自定义的ETF候选池"""
    import os
    pool_path = os.path.join(DATA_DIR, "etf_pool.json")
    if not os.path.exists(pool_path):
        return []
    try:
        with open(pool_path, "r", encoding="utf-8") as f:
            custom = json.load(f)
        if not isinstance(custom, list):
            return []
        # 字段校验: 必须有 code/name/fund_code/category
        valid = []
        for item in custom:
            if all(k in item for k in ("code", "name", "fund_code", "category")):
                # 显示名统一为场内ETF简称: 历史 add-etf 曾手填平台全称"XXETF联接A",
                # 与 config 内置 14 只的"XXETF"口径不一致, 看板实时行情/动量排名会混着显示。
                # 份额类别由 fund_code 区分, 名称不必带后缀; efunds_sync.build_name_map()
                # 本就会剥掉这些后缀做模糊匹配, 对账不受影响。
                _nm = str(item.get("name", ""))
                for _suf in ("联接A", "联接B", "联接C", "联接D", "联接E", "联接"):
                    if _nm.endswith("ETF" + _suf):
                        _nm = _nm[: -len(_suf)]
                        break
                valid.append({**item, "name": _nm})
            else:
                print(f"[CONFIG] 跳过无效自定义ETF: {item}")
        return valid
    except Exception as e:
        print(f"[CONFIG] 加载 etf_pool.json 失败: {e}")
        return []

# 加载并合并自定义ETF到卫星候选池
CUSTOM_ETFS = _load_custom_etfs()
for _etf in CUSTOM_ETFS:
    # 去重: 如果已存在相同 fund_code 则跳过
    if not any(e["fund_code"] == _etf["fund_code"] for e in SATELLITE_ETFS):
        SATELLITE_ETFS.append(_etf)


# ============================================================
# 低费率份额: 新钱用替代代码, 老仓不动 (2026-11-12)
# ============================================================
# 背景: 110020 / 008975 的 A 类持有 7-364 天仍收 0.5% 赎回费,
# 而同基金同标的存在 ≥7 天免赎回费的份额。全池 22 只核查后只有这
# 两只值得切(其余 4 只有常规赎回费但 A 类满 180/730 天归零, 长期更优;
# 另外 16 只本来就是 ≥7 天 0)。切换成本高于收益, 故不改。
#
# 系统按场内 etf_code 聚合市值(check_core_signals 的 cur_map、
# check_rotation_signals 的 current_codes), 同标的两份额可共存;
# 卖出信号始终取持仓表里的实际 fund_code, 买入信号取这里的替代代码。
BUY_FUND_CODE_OVERRIDES = {
    # 老仓A类代码 : 新钱用的低费率份额代码
    "110020": "007339",  # 易方达沪深300ETF联接C: ≥7天赎回费0, 销售服务费0.20%/年
    "008975": "022055",  # 富国消费50ETF联接E : ≥7天赎回费0, 销售服务费0.10%/年
}

# 替代份额的平台全称/匹配关键词(用于 efunds_sync 对账, 长关键词优先于老代码)
BUY_FUND_ALIASES = {
    "007339": {"name": "易方达沪深300ETF联接C", "key": "沪深300ETF联接C"},
    "022055": {"name": "富国中证消费50ETF联接E", "key": "消费50ETF联接E"},
}


def find_etf_by_fund_code(fund_code):
    """按联接基金代码反查配置(含 BUY_FUND_CODE_OVERRIDES 里的低费率替代份额)。"""
    fc = str(fund_code or "").strip()
    for pool, cat in ((CORE_ETFS, "core"), (SATELLITE_ETFS, "satellite")):
        for etf in pool:
            if fc == etf["fund_code"] or fc == BUY_FUND_CODE_OVERRIDES.get(etf["fund_code"]):
                return {**etf, "category": cat}
    return None


def preferred_buy_code(etf):
    """买入信号里应显示的联接基金代码: 新钱用低费率份额, 老仓代码保留给持仓匹配。"""
    return BUY_FUND_CODE_OVERRIDES.get(etf["fund_code"], etf["fund_code"])


def fund_display_name(fund_code):
    """建仓时写入 portfolio.name 的名称: 替代份额用自身全称, 避免显示成老 A 类。"""
    fc = str(fund_code or "").strip()
    if fc in BUY_FUND_ALIASES:
        return BUY_FUND_ALIASES[fc]["name"]
    info = find_etf_by_fund_code(fc)
    return info["name"] if info else f"基金{fc}"

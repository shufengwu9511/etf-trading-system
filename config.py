# ============================================================
# ETF联接基金交易系统 - 全局配置
# 交易模式: 纯场外ETF联接基金申赎 (易方达财富账户)
# ============================================================
import json

# ---- Tushare 数据源 ----
try:
    from local_secrets import TUSHARE_TOKEN
except ImportError:
    TUSHARE_TOKEN = None  # 用户需从 local_secrets.example.py 复制为 local_secrets.py 并填入真实Token

# ---- 资金配置 (单位: 元) ----
TOTAL_CAPITAL = 2_000_000          # 总资金 200万
CORE_RATIO = 0.60                  # 宽基底仓占比 60%
SATELLITE_RATIO = 0.40             # 行业卫星占比 40%
SINGLE_ETF_MAX_RATIO = 0.08        # 单只行业ETF上限 8% (8万)
MIN_TRADE_AMOUNT = 100             # 最小申购金额

# ---- 宽基底仓配置 ----
# index_code: 底层指数代码, 用于 index_daily(行情) 和 index_dailybasic(估值)
# fund_code: 易方达财富上可申购的联接基金代码
CORE_ETFS = [
    {"code": "510310.SH", "name": "沪深300ETF",  "index_code": "000300.SH", "target_weight": 0.25, "fund_code": "110020", "company": "易方达"},
    {"code": "510580.SH", "name": "中证500ETF",  "index_code": "000905.SH", "target_weight": 0.20, "fund_code": "007028", "company": "易方达"},
    {"code": "588080.SH", "name": "科创50ETF",   "index_code": "000688.SH", "target_weight": 0.15, "fund_code": "011608", "company": "易方达"},
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
ROTATION_HOLD_COUNT = 3                 # 同时持有的行业ETF数量
ROTATION_CYCLE_DAYS = 14                # 轮动周期 (天)

# 移动止盈 + 止损
# 移动止盈: 利润超过激活阈值后跟踪峰值, 从峰值回撤超过阈值则清仓
TRAILING_STOP_ACTIVATE = 6.0            # 激活阈值: 利润≥+6%开始跟踪峰值
TRAILING_STOP_PULLBACK = 4.0            # 回撤阈值: 从峰值回撤4%触发清仓
TRAILING_STOP_PANIC_PULLBACK = 3.0      # 恐慌模式回撤收紧至3%
TRAILING_STOP_TOP1_PULLBACK = 6.0       # 动量排名第1的容限6% (让最强趋势多跑)
# 止损 (不变)
STOP_LOSS_THRESHOLD = -15.0             # 止损 -15%
STOP_LOSS_PANIC_THRESHOLD = -8.0        # 恐慌预警时收紧止损至 -8%
MIN_HOLD_DAYS = 7                       # 最短持有天数 (按份额确认日算自然日, 避免赎回费)

# 保留旧参数以兼容 (已弃用, 仅用于向后兼容)
TAKE_PROFIT_THRESHOLD = 5.0
TAKE_PROFIT_TIER1 = 8.0
TAKE_PROFIT_TIER2 = 15.0

# 恐慌指数监控
# index_daily pct_chg: 百分比形式 (如 -2.5 表示跌2.5%)
PANIC_HS300_DROP = -2.5                 # 沪深300单日跌幅阈值 -2.5%
PANIC_NORTH_OUTFLOW = 1_000_000        # 北向资金净流出阈值 100亿 (单位:万元, Tushare moneyflow_hsgt)
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
                valid.append(item)
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

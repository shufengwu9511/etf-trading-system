# ============================================================
# ETF联接基金交易系统 - 数据库初始化模块
# ============================================================
import sqlite3
import os
from config import DB_NAME, DATA_DIR


def get_db_path():
    """获取数据库文件完整路径"""
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, DB_NAME)


def get_connection():
    """获取数据库连接"""
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """初始化数据库表结构"""
    conn = get_connection()
    cursor = conn.cursor()

    # ---- 指数日线行情表 ----
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS index_daily (
            ts_code TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            open REAL,
            close REAL,
            high REAL,
            low REAL,
            vol REAL,
            amount REAL,
            pct_chg REAL,
            PRIMARY KEY (ts_code, trade_date)
        )
    """)

    # ---- 指数估值表 (PE/PB) ----
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS index_valuation (
            ts_code TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            pe REAL,
            pe_ttm REAL,
            pb REAL,
            PRIMARY KEY (ts_code, trade_date)
        )
    """)

    # ---- 北向资金流向表 ----
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS north_money_flow (
            trade_date TEXT PRIMARY KEY,
            north_money REAL,
            ggt_ss REAL,
            ggt_sz REAL,
            hgt REAL,
            sgt REAL
        )
    """)

    # ---- 跌停统计表 ----
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS limit_down_stats (
            trade_date TEXT PRIMARY KEY,
            limit_down_count INTEGER,
            limit_up_count INTEGER
        )
    """)

    # ---- 恐慌预警记录表 ----
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS panic_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            alert_level TEXT NOT NULL,
            trigger_details TEXT,
            hs300_pct_chg REAL,
            north_money REAL,
            limit_down_count INTEGER,
            volume_ratio REAL,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)

    # ---- 持仓表 ----
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS portfolio (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fund_code TEXT NOT NULL,
            etf_code TEXT NOT NULL,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            shares REAL DEFAULT 0,
            cost_nav REAL DEFAULT 0,
            current_nav REAL DEFAULT 0,
            total_cost REAL DEFAULT 0,
            market_value REAL DEFAULT 0,
            profit_loss REAL DEFAULT 0,
            profit_pct REAL DEFAULT 0,
            status TEXT DEFAULT 'holding',
            buy_date TEXT,
            confirm_date TEXT,
            buy_amount REAL DEFAULT 0,
            hold_days INTEGER DEFAULT 0,
            updated_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)

    # ---- 交易记录表 ----
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fund_code TEXT NOT NULL,
            etf_code TEXT NOT NULL,
            name TEXT NOT NULL,
            trade_type TEXT NOT NULL,
            amount REAL NOT NULL,
            shares REAL DEFAULT 0,
            nav REAL DEFAULT 0,
            fee REAL DEFAULT 0,
            status TEXT DEFAULT 'pending',
            submit_date TEXT NOT NULL,
            confirm_date TEXT,
            settle_date TEXT,
            signal_source TEXT,
            remark TEXT,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)

    # ---- 信号记录表 ----
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_date TEXT NOT NULL,
            signal_type TEXT NOT NULL,
            etf_code TEXT NOT NULL,
            fund_code TEXT,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            direction TEXT NOT NULL,
            amount REAL,
            reason TEXT,
            priority TEXT DEFAULT 'normal',
            is_executed INTEGER DEFAULT 0,
            executed_at TEXT,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)

    # ---- 动量得分表 ----
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS momentum_scores (
            trade_date TEXT NOT NULL,
            etf_code TEXT NOT NULL,
            name TEXT,
            category TEXT,
            momentum_20d REAL,
            momentum_60d REAL,
            composite_score REAL,
            rank INTEGER,
            PRIMARY KEY (trade_date, etf_code)
        )
    """)

    # ---- 系统运行日志 ----
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS system_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            log_date TEXT NOT NULL,
            run_type TEXT NOT NULL,
            status TEXT NOT NULL,
            details TEXT,
            duration_seconds REAL,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)

    conn.commit()

    # ---- 兼容旧库: 自动补齐新增列 ----
    cursor.execute("PRAGMA table_info(portfolio)")
    existing_cols = {row[1] for row in cursor.fetchall()}
    if "confirm_date" not in existing_cols:
        cursor.execute("ALTER TABLE portfolio ADD COLUMN confirm_date TEXT")
        conn.commit()
    if "nav_date" not in existing_cols:
        cursor.execute("ALTER TABLE portfolio ADD COLUMN nav_date TEXT")
        conn.commit()

    conn.close()
    print("[DB] 数据库初始化完成:", get_db_path())


if __name__ == "__main__":
    init_db()

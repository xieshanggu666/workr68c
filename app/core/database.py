from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

from app.core.config import DATABASE_URL

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# 账务状态机新增列（create_all 不会变更已存在的表，这里为 SQLite 老库补齐）
_SQLITE_NEW_COLUMNS = [
    ("escrow_accounts", "version", "INTEGER NOT NULL DEFAULT 0"),
    ("escrow_transactions", "from_status", "VARCHAR(16) NOT NULL DEFAULT ''"),
    ("escrow_transactions", "to_status", "VARCHAR(16) NOT NULL DEFAULT ''"),
    ("escrow_transactions", "idempotency_key", "VARCHAR(64)"),
    ("escrow_transactions", "operator_id", "INTEGER"),
    ("escrow_transactions", "biz_type", "VARCHAR(32) NOT NULL DEFAULT 'manual'"),
    ("escrow_transactions", "biz_id", "VARCHAR(64) NOT NULL DEFAULT ''"),
]


def run_migrations():
    """SQLite 轻量迁移：补齐账务状态机新增列、回填幂等键并建立唯一索引。"""
    if not DATABASE_URL.startswith("sqlite"):
        return
    with engine.begin() as conn:
        for table, column, ddl in _SQLITE_NEW_COLUMNS:
            existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
            if column not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
        # 历史流水回填幂等键与状态迁移，保证唯一索引与回溯可用
        conn.execute(
            text("UPDATE escrow_transactions SET idempotency_key = 'legacy:' || id WHERE idempotency_key IS NULL")
        )
        conn.execute(
            text(
                "UPDATE escrow_transactions SET from_status = CASE tx_type "
                "WHEN 'deposit' THEN 'unpaid' ELSE 'paid' END, "
                "to_status = CASE tx_type "
                "WHEN 'deposit' THEN 'paid' WHEN 'return' THEN 'returned' "
                "WHEN 'forfeit' THEN 'forfeited' ELSE 'paid' END "
                "WHERE from_status = ''"
            )
        )
        conn.execute(
            text("CREATE UNIQUE INDEX IF NOT EXISTS ix_escrow_transactions_idempotency_key "
                 "ON escrow_transactions (idempotency_key)")
        )
        duplicate_accounts = conn.execute(
            text(
                "SELECT COUNT(*) FROM (SELECT section_id, bid_document_id FROM escrow_accounts "
                "GROUP BY section_id, bid_document_id HAVING COUNT(*) > 1)"
            )
        ).scalar()
        if not duplicate_accounts:
            conn.execute(
                text("CREATE UNIQUE INDEX IF NOT EXISTS uq_escrow_account_bid "
                     "ON escrow_accounts (section_id, bid_document_id)")
            )

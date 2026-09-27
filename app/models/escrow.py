from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint

from app.core.database import Base


class EscrowAccount(Base):
    """投标保证金账户。

    状态机：unpaid --pay--> paid --return--> returned
                              └--forfeit--> forfeited
    每份投标（section_id + bid_document_id）唯一一个账户。
    """

    __tablename__ = "escrow_accounts"
    __table_args__ = (
        UniqueConstraint("section_id", "bid_document_id", name="uq_escrow_account_bid"),
    )

    id = Column(Integer, primary_key=True)
    section_id = Column(Integer, ForeignKey("tender_sections.id"), nullable=False, index=True)
    bid_document_id = Column(Integer, ForeignKey("bid_documents.id"), nullable=False, index=True)
    bidder_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    amount = Column(Numeric(18, 2), nullable=False, default=0)
    paid_at = Column(DateTime, nullable=True)
    returned_at = Column(DateTime, nullable=True)
    status = Column(String(16), nullable=False, default="unpaid")
    version = Column(Integer, nullable=False, default=0)  # 乐观锁：每次状态迁移 +1
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class EscrowTransaction(Base):
    """保证金流水：账户每次状态迁移/账务事件恰好产生一条，与状态变更同事务提交。

    - idempotency_key：幂等键，重复请求/重试按 key 去重
    - from_status/to_status：该笔流水驱动的状态迁移（事件类流水前后状态相同）
    - biz_type/biz_id：业务来源（clarification_excluded/section_failed/bid_lost/winner_confirmed/manual...）
    """

    __tablename__ = "escrow_transactions"

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("escrow_accounts.id"), nullable=False, index=True)
    tx_type = Column(String(16), nullable=False)  # deposit/return/forfeit/confirm
    amount = Column(Numeric(18, 2), nullable=False, default=0)
    balance_after = Column(Numeric(18, 2), nullable=False, default=0)
    from_status = Column(String(16), nullable=False, default="")
    to_status = Column(String(16), nullable=False, default="")
    idempotency_key = Column(String(64), nullable=True, unique=True, index=True)
    operator_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    biz_type = Column(String(32), nullable=False, default="manual")
    biz_id = Column(String(64), nullable=False, default="")
    remark = Column(String(256), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

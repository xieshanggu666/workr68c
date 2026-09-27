from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint

from app.core.database import Base


class EscrowAccount(Base):
    """投标保证金账户。

    状态机：unpaid → paid → returned / forfeited
    所有迁移必须经由 app.services.escrow_service.apply_escrow_transition，
    通过「状态条件更新（CAS）+ version 乐观锁」保证并发安全。
    """

    __tablename__ = "escrow_accounts"
    __table_args__ = (
        UniqueConstraint("bid_document_id", name="uq_escrow_bid_document"),
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
    """保证金资金流水。

    - idempotency_key 全局唯一：同一业务动作无论重试多少次只记账一次
    - from_status/to_status 记录账户状态迁移，支持状态机回放审计
    - ref_type/ref_id 关联触发该流水的业务事件（澄清/流标/定标/手工）
    """

    __tablename__ = "escrow_transactions"

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("escrow_accounts.id"), nullable=False, index=True)
    tx_type = Column(String(16), nullable=False)  # deposit/return/forfeit
    amount = Column(Numeric(18, 2), nullable=False, default=0)
    balance_after = Column(Numeric(18, 2), nullable=False, default=0)
    from_status = Column(String(16), nullable=False, default="")
    to_status = Column(String(16), nullable=False, default="")
    idempotency_key = Column(String(96), nullable=True, unique=True, index=True)
    ref_type = Column(String(32), nullable=True)   # manual/not_won/clarification/section_failed
    ref_id = Column(Integer, nullable=True)
    operator_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    remark = Column(String(256), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

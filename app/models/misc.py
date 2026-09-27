from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text

from app.core.database import Base


class AuditLog(Base):
    """审计日志：操作人、动作、明细及关联业务实体，支持按实体回溯。"""

    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    action = Column(String(64), nullable=False)
    detail = Column(Text, nullable=False, default="")
    entity_type = Column(String(32), nullable=True, index=True)  # section/escrow_account/winner/clarification...
    entity_id = Column(Integer, nullable=True, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class Setting(Base):
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True)
    key = Column(String(64), nullable=False, unique=True)
    value = Column(String(256), nullable=False, default="")

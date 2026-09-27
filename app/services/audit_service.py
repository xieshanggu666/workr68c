"""审计日志服务。

- add_audit 默认独立提交，兼容简单调用点
- 组合业务流程传 commit=False，与业务数据同一事务提交，保证「业务成功必有审计」
"""

from app.models.misc import AuditLog


def add_audit(db, user_id, action, detail="", *, entity_type=None, entity_id=None, commit=True):
    entry = AuditLog(
        user_id=user_id,
        action=action,
        detail=detail,
        entity_type=entity_type,
        entity_id=entity_id,
    )
    db.add(entry)
    if commit:
        db.commit()
    else:
        db.flush()
    return entry


def list_entity_audits(db, entity_type: str, entity_id: int) -> list[AuditLog]:
    """按业务实体回溯审计轨迹。"""
    return (
        db.query(AuditLog)
        .filter(AuditLog.entity_type == entity_type, AuditLog.entity_id == entity_id)
        .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        .all()
    )

from app.models.misc import AuditLog


def add_audit(db, user_id, action, detail="", *, commit=True):
    """写审计日志。

    commit=False 时仅 flush，由调用方在统一事务中提交，
    用于审计与业务变更（如保证金账务迁移）同库事务落库。
    """
    db.add(AuditLog(user_id=user_id, action=action, detail=detail))
    if commit:
        db.commit()
    else:
        db.flush()

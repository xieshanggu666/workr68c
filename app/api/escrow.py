from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, require_roles
from app.models import EscrowAccount, User
from app.services.escrow_service import (
    EscrowConcurrencyError,
    InvalidTransitionError,
    account_transactions,
    forfeit_deposit,
    pay_deposit,
    return_deposit,
)

router = APIRouter(prefix="/api", tags=["escrow"])


def _tx_dict(t) -> dict:
    return {
        "id": t.id,
        "tx_type": t.tx_type,
        "amount": float(t.amount),
        "balance_after": float(t.balance_after),
        "from_status": t.from_status,
        "to_status": t.to_status,
        "idempotency_key": t.idempotency_key,
        "ref_type": t.ref_type,
        "ref_id": t.ref_id,
        "operator_id": t.operator_id,
        "remark": t.remark,
        "created_at": t.created_at,
    }


@router.get("/sections/{section_id}/escrow")
def list_escrow(section_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    accounts = db.query(EscrowAccount).filter(EscrowAccount.section_id == section_id).all()
    return [
        {
            "id": a.id,
            "bid_document_id": a.bid_document_id,
            "bidder_id": a.bidder_id,
            "amount": float(a.amount),
            "status": a.status,
            "version": a.version,
            "paid_at": a.paid_at,
            "returned_at": a.returned_at,
        }
        for a in accounts
    ]


@router.get("/escrow/{account_id}/transactions")
def list_transactions(account_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """账户资金流水：含状态迁移、业务来源与操作人，支持审计回溯。"""
    return [_tx_dict(t) for t in account_transactions(db, account_id)]


@router.post("/escrow/{account_id}/pay")
def pay(account_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    account = db.get(EscrowAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="保证金账户不存在")
    if user.role != "admin" and account.bidder_id != user.id:
        raise HTTPException(status_code=403, detail="无权操作该账户")
    try:
        pay_deposit(db, account, operator_id=user.id)
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except EscrowConcurrencyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"id": account.id, "status": account.status}


@router.post("/escrow/{account_id}/return")
def do_return(account_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "operator"))):
    account = db.get(EscrowAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="保证金账户不存在")
    try:
        return_deposit(db, account, "招标人主动退还保证金", ref_type="manual", operator_id=user.id)
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except EscrowConcurrencyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"id": account.id, "status": account.status}


@router.post("/escrow/{account_id}/forfeit")
def do_forfeit(account_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "operator"))):
    account = db.get(EscrowAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="保证金账户不存在")
    try:
        forfeit_deposit(db, account, "违规投标", ref_type="manual", operator_id=user.id)
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except EscrowConcurrencyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"id": account.id, "status": account.status}

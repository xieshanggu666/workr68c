from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, require_roles
from app.models import EscrowAccount, EscrowTransaction, User
from app.services.escrow_service import (
    EscrowError,
    EscrowStateConflict,
    forfeit_deposit,
    pay_deposit,
    return_deposit,
    verify_account,
)

router = APIRouter(prefix="/api", tags=["escrow"])


def _get_account(db: Session, account_id: int) -> EscrowAccount:
    account = db.get(EscrowAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="保证金账户不存在")
    return account


def _escrow_errors(exc: EscrowError):
    if isinstance(exc, EscrowStateConflict):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


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
            "paid_at": a.paid_at,
            "returned_at": a.returned_at,
        }
        for a in accounts
    ]


@router.get("/escrow/{account_id}/transactions")
def list_transactions(account_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    txs = db.query(EscrowTransaction).filter(EscrowTransaction.account_id == account_id).order_by(EscrowTransaction.id.asc()).all()
    return [
        {
            "tx_type": t.tx_type,
            "amount": float(t.amount),
            "balance_after": float(t.balance_after),
            "from_status": t.from_status,
            "to_status": t.to_status,
            "idempotency_key": t.idempotency_key,
            "operator_id": t.operator_id,
            "biz_type": t.biz_type,
            "biz_id": t.biz_id,
            "remark": t.remark,
            "created_at": t.created_at,
        }
        for t in txs
    ]


@router.get("/escrow/{account_id}/verify")
def verify(account_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "operator"))):
    """重放账户流水，校验状态与余额链一致性（审计回溯）。"""
    try:
        return verify_account(db, account_id)
    except EscrowError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/escrow/{account_id}/pay")
def pay(
    account_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    account = _get_account(db, account_id)
    if user.role != "admin" and account.bidder_id != user.id:
        raise HTTPException(status_code=403, detail="无权操作该账户")
    try:
        pay_deposit(db, account, operator_id=user.id, idempotency_key=idempotency_key)
    except EscrowError as exc:
        raise _escrow_errors(exc) from exc
    return {"id": account.id, "status": account.status}


@router.post("/escrow/{account_id}/return")
def do_return(
    account_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    account = _get_account(db, account_id)
    try:
        return_deposit(db, account, operator_id=user.id, idempotency_key=idempotency_key)
    except EscrowError as exc:
        raise _escrow_errors(exc) from exc
    return {"id": account.id, "status": account.status}


@router.post("/escrow/{account_id}/forfeit")
def do_forfeit(
    account_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    account = _get_account(db, account_id)
    try:
        forfeit_deposit(db, account, "违规投标", operator_id=user.id, idempotency_key=idempotency_key)
    except EscrowError as exc:
        raise _escrow_errors(exc) from exc
    return {"id": account.id, "status": account.status}

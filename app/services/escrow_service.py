"""投标保证金账务状态机：缴纳 / 退还 / 没收。

设计要点：
- 状态机：unpaid → paid → returned / forfeited，终态不可再迁移
- 并发幂等：账户状态迁移用「条件更新（CAS）」实现，并发下只有一个请求能推进状态；
  每条流水携带全局唯一幂等键，重复请求 / 网络重试 / 并发重放都收敛到同一条流水
- 流水一致性：状态迁移 + 资金流水 + 审计日志在同一事务提交；
  balance_after 由状态机推导（paid=应缴额，returned/forfeited=0），不接受外部传入
- 审计回溯：流水记录 from/to 状态、业务来源（ref_type/ref_id）与操作人，
  每次迁移同步写审计日志，可按账户 / 业务事件双向回溯
- 可重试：独立操作（commit=True）内部自动重试并发冲突；批量流程（commit=False）
  由顶层事务包裹，冲突时整体回滚后重试，确定性幂等键保证重试不产生重复流水
"""

import time
from datetime import datetime

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.models.escrow import EscrowAccount, EscrowTransaction
from app.models.project import Project, TenderSection
from app.services.audit_service import add_audit

# 保证金账户状态机：当前状态 → 允许的目标状态
ESCROW_TRANSITIONS = {
    "unpaid": {"paid"},
    "paid": {"returned", "forfeited"},
    "returned": set(),
    "forfeited": set(),
}

# 状态迁移对应的流水类型
_TX_TYPE_BY_TARGET = {"paid": "deposit", "returned": "return", "forfeited": "forfeit"}

# 并发冲突重试次数（仅 commit=True 的独立操作生效）
_MAX_RETRIES = 5


class EscrowError(Exception):
    """保证金账务错误基类。"""


class InvalidTransitionError(EscrowError):
    """状态机不允许的迁移（如未缴纳就退还、已退还再没收）。"""


class EscrowConcurrencyError(EscrowError):
    """并发冲突且重试后仍未收敛，调用方可稍后整体重试。"""


def _balance_after(to_status: str, amount) -> float:
    """账户余额由状态机推导：在管中（paid）为应缴额，退/没后清零。"""
    return round(float(amount), 2) if to_status == "paid" else 0.0


def _cas_account_status(db, account_id: int, from_status: str, to_status: str) -> tuple[bool, datetime]:
    """原子迁移账户状态（compare-and-swap），并发下仅一个请求成功。"""
    now = datetime.utcnow()
    values = {"status": to_status, "version": EscrowAccount.version + 1}
    if to_status == "paid":
        values["paid_at"] = now
    else:
        values["returned_at"] = now
    result = db.execute(
        update(EscrowAccount)
        .where(EscrowAccount.id == account_id, EscrowAccount.status == from_status)
        .values(**values)
    )
    return result.rowcount == 1, now


def _find_by_key(db, idempotency_key: str | None) -> EscrowTransaction | None:
    if not idempotency_key:
        return None
    return (
        db.query(EscrowTransaction)
        .filter(EscrowTransaction.idempotency_key == idempotency_key)
        .first()
    )


def _sync_account(account: EscrowAccount, to_status: str, now: datetime) -> None:
    """CAS 成功后同步内存对象，避免额外 SELECT。"""
    account.status = to_status
    account.version = (account.version or 0) + 1
    if to_status == "paid":
        account.paid_at = now
    else:
        account.returned_at = now


def apply_escrow_transition(
    db,
    account: EscrowAccount,
    to_status: str,
    *,
    remark: str,
    idempotency_key: str,
    ref_type: str | None = None,
    ref_id: int | None = None,
    operator_id: int | None = None,
    commit: bool = True,
) -> tuple[EscrowTransaction, bool]:
    """推进保证金账户状态机并记账，返回 (流水, 是否本次新建)。

    幂等语义：同一 idempotency_key 无论调用多少次，只产生一条流水，
    后续调用返回原流水（created=False），账户状态保持不变。
    """
    tx_type = _TX_TYPE_BY_TARGET.get(to_status)
    if tx_type is None:
        raise InvalidTransitionError(f"未知的保证金目标状态: {to_status}")

    attempts = _MAX_RETRIES if commit else 1
    for attempt in range(attempts):
        # 1. 幂等重放：该业务动作已记账，直接返回原流水
        existing = _find_by_key(db, idempotency_key)
        if existing is not None:
            db.refresh(account)
            return existing, False

        db.refresh(account)
        from_status = account.status
        if to_status not in ESCROW_TRANSITIONS.get(from_status, set()):
            raise InvalidTransitionError(
                f"保证金账户 {account.id} 不允许从 {from_status} 迁移到 {to_status}"
            )

        # 2. CAS 推进状态；失败说明有并发请求正在处理同一账户
        cas_ok, now = _cas_account_status(db, account.id, from_status, to_status)
        if not cas_ok:
            if not commit:
                # 嵌入外层事务：由顶层回滚整个业务流程后整体重试
                raise EscrowConcurrencyError(
                    f"保证金账户 {account.id} 状态被并发修改，请重试"
                )
            db.rollback()
            time.sleep(0.05 * (attempt + 1))
            continue

        # 3. 同事务记账：流水 + 审计
        _sync_account(account, to_status, now)
        tx = EscrowTransaction(
            account_id=account.id,
            tx_type=tx_type,
            amount=round(float(account.amount), 2),
            balance_after=_balance_after(to_status, account.amount),
            from_status=from_status,
            to_status=to_status,
            idempotency_key=idempotency_key,
            ref_type=ref_type,
            ref_id=ref_id,
            operator_id=operator_id,
            remark=remark,
        )
        db.add(tx)
        db.flush()
        add_audit(
            db,
            operator_id,
            f"ESCROW_{tx_type.upper()}",
            f"保证金账户 {account.id} {from_status}→{to_status}，金额 {tx.amount}，流水 {tx.id}：{remark}",
            entity_type="escrow_account",
            entity_id=account.id,
            commit=False,
        )
        try:
            if commit:
                db.commit()
            else:
                db.flush()
        except IntegrityError:
            # 幂等键唯一冲突：并发请求已记账，回滚后按重放收敛
            db.rollback()
            existing = _find_by_key(db, idempotency_key)
            if existing is not None:
                db.refresh(account)
                return existing, False
            raise
        return tx, True

    raise EscrowConcurrencyError(f"保证金账户 {account.id} 并发冲突，请重试")


def create_account(db, section_id: int, bid_document_id: int, bidder_id: int) -> EscrowAccount:
    section = db.get(TenderSection, section_id)
    project = db.get(Project, section.project_id) if section else None
    budget = float(project.budget or 0) if project else 0
    deposit_amount = float(section.deposit_ratio) * budget if section else 0
    account = EscrowAccount(
        section_id=section_id,
        bid_document_id=bid_document_id,
        bidder_id=bidder_id,
        amount=round(deposit_amount, 2),
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def pay_deposit(
    db,
    account: EscrowAccount,
    *,
    idempotency_key: str | None = None,
    operator_id: int | None = None,
    commit: bool = True,
) -> EscrowAccount:
    """投标人缴纳保证金到账。重复缴纳按幂等重放处理，不会重复记账。"""
    apply_escrow_transition(
        db,
        account,
        "paid",
        remark="保证金缴纳",
        idempotency_key=idempotency_key or f"pay:account:{account.id}",
        ref_type="manual",
        operator_id=operator_id,
        commit=commit,
    )
    return account


def return_deposit(
    db,
    account: EscrowAccount,
    reason: str = "未中标保证金退还",
    *,
    idempotency_key: str | None = None,
    ref_type: str | None = None,
    ref_id: int | None = None,
    operator_id: int | None = None,
    commit: bool = True,
) -> EscrowAccount:
    """足额退还保证金。同一幂等键重复调用只退一次。"""
    apply_escrow_transition(
        db,
        account,
        "returned",
        remark=reason,
        idempotency_key=idempotency_key or f"return:account:{account.id}",
        ref_type=ref_type,
        ref_id=ref_id,
        operator_id=operator_id,
        commit=commit,
    )
    return account


def forfeit_deposit(
    db,
    account: EscrowAccount,
    reason: str,
    *,
    idempotency_key: str | None = None,
    ref_type: str | None = None,
    ref_id: int | None = None,
    operator_id: int | None = None,
    commit: bool = True,
) -> EscrowAccount:
    """没收保证金（中标后放弃 / 违规）。"""
    apply_escrow_transition(
        db,
        account,
        "forfeited",
        remark=f"保证金没收：{reason}",
        idempotency_key=idempotency_key or f"forfeit:account:{account.id}",
        ref_type=ref_type,
        ref_id=ref_id,
        operator_id=operator_id,
        commit=commit,
    )
    return account


def return_bid_deposit(
    db,
    section_id: int,
    bid_document_id: int,
    reason: str = "未中标保证金退还",
    *,
    key_prefix: str | None = None,
    ref_type: str | None = None,
    ref_id: int | None = None,
    operator_id: int | None = None,
    commit: bool = True,
) -> list[EscrowAccount]:
    """退还某份投标对应且已缴纳的保证金。

    幂等键按「业务事件 + 账户」确定，同一业务事件重试不会重复退款。
    """
    accounts = (
        db.query(EscrowAccount)
        .filter(
            EscrowAccount.section_id == section_id,
            EscrowAccount.bid_document_id == bid_document_id,
            EscrowAccount.status == "paid",
        )
        .all()
    )
    prefix = key_prefix or f"return:bid:{bid_document_id}"
    for account in accounts:
        return_deposit(
            db,
            account,
            reason,
            idempotency_key=f"{prefix}:account:{account.id}",
            ref_type=ref_type,
            ref_id=ref_id,
            operator_id=operator_id,
            commit=commit,
        )
    return accounts


def return_section_deposits(
    db,
    section_id: int,
    reason: str = "标段流标，保证金退还",
    *,
    key_prefix: str | None = None,
    ref_type: str | None = None,
    ref_id: int | None = None,
    operator_id: int | None = None,
    commit: bool = True,
) -> list[EscrowAccount]:
    """退还标段下所有仍处于已缴纳状态的保证金（流标场景，可整体重试）。"""
    accounts = (
        db.query(EscrowAccount)
        .filter(EscrowAccount.section_id == section_id, EscrowAccount.status == "paid")
        .all()
    )
    prefix = key_prefix or f"return:section:{section_id}"
    for account in accounts:
        return_deposit(
            db,
            account,
            reason,
            idempotency_key=f"{prefix}:account:{account.id}",
            ref_type=ref_type,
            ref_id=ref_id,
            operator_id=operator_id,
            commit=commit,
        )
    return accounts


def account_transactions(db, account_id: int) -> list[EscrowTransaction]:
    """账户完整资金流水（按时间升序），用于对账与审计回溯。"""
    return (
        db.query(EscrowTransaction)
        .filter(EscrowTransaction.account_id == account_id)
        .order_by(EscrowTransaction.created_at.asc(), EscrowTransaction.id.asc())
        .all()
    )

"""投标保证金账务状态机：缴纳 / 退还 / 没收 / 中标确认锁定的统一入口。

状态机：
    unpaid --pay--> paid --return-->  returned
                       └--forfeit--> forfeited
    paid   --confirm--> paid          （账务事件：中标确认后保证金继续锁定，仅留痕不改状态）

设计约束：
- 可重试：每次迁移携带幂等键；命中幂等键或账户已达目标状态时直接返回原流水，
  任意环节失败后均可安全重试，不会产生重复资金变动
- 并发幂等：状态迁移使用条件更新（CAS，UPDATE ... WHERE status=期望值），
  并发下只有一方生效，另一方退化为幂等重放；每份投标的账户全局唯一
- 流水一致：状态迁移 + 资金流水 + 审计日志在同一数据库事务提交，
  每笔迁移恰好一条流水，balance_after 由迁移类型确定性推导
- 审计回溯：流水记录操作人、业务来源（biz_type/biz_id）、迁移前后状态与幂等键；
  verify_account 可重放流水校验账户一致性
"""

from datetime import datetime

from sqlalchemy import update

from app.models.escrow import EscrowAccount, EscrowTransaction
from app.models.project import Project, TenderSection
from app.services.audit_service import add_audit


class EscrowError(Exception):
    """保证金账务基础异常。"""


class EscrowStateConflict(EscrowError):
    """账户当前状态不允许该操作（或已被并发修改且无法重放）。"""


# 状态迁移：action -> (源状态, 目标状态, 流水类型)
ESCROW_TRANSITIONS = {
    "pay": ("unpaid", "paid", "deposit"),
    "return": ("paid", "returned", "return"),
    "forfeit": ("paid", "forfeited", "forfeit"),
}

# 账务事件：不改变账户状态，仅记录流水留痕（如中标确认后保证金继续锁定）
ESCROW_EVENTS = {
    "confirm": ("paid", "confirm"),
}

_AUDIT_ACTIONS = {
    "pay": "ESCROW_PAY",
    "return": "ESCROW_RETURN",
    "forfeit": "ESCROW_FORFEIT",
    "confirm": "ESCROW_CONFIRM",
}


def _derive_key(action: str, account_id: int, biz_type: str, biz_id: str) -> str:
    """业务触发的账务迁移使用确定性幂等键：同一业务事件重试不会重复记账。"""
    return f"{biz_type}:{biz_id or 'manual'}:{action}:acct{account_id}"[:64]


def _find_tx_by_key(db, idempotency_key: str | None) -> EscrowTransaction | None:
    if not idempotency_key:
        return None
    return (
        db.query(EscrowTransaction)
        .filter(EscrowTransaction.idempotency_key == idempotency_key)
        .first()
    )


def _find_transition_tx(db, account_id: int, tx_type: str, to_status: str) -> EscrowTransaction | None:
    return (
        db.query(EscrowTransaction)
        .filter(
            EscrowTransaction.account_id == account_id,
            EscrowTransaction.tx_type == tx_type,
            EscrowTransaction.to_status == to_status,
        )
        .order_by(EscrowTransaction.id.desc())
        .first()
    )


def apply_transition(
    db,
    account_id: int,
    action: str,
    *,
    reason: str = "",
    operator_id: int | None = None,
    biz_type: str = "manual",
    biz_id: str = "",
    idempotency_key: str | None = None,
    commit: bool = True,
) -> EscrowTransaction:
    """对保证金账户执行一次账务迁移/事件，返回对应流水。

    幂等：同一 idempotency_key 或账户已处于目标状态时，返回既有流水而不重复记账。
    并发：状态迁移通过 CAS 条件更新生效，并发失败者按重试语义返回既有流水。
    事务：状态、流水、审计单事务提交；commit=False 时仅 flush，由调用方统一提交。
    """
    transition = ESCROW_TRANSITIONS.get(action)
    event = ESCROW_EVENTS.get(action)
    if transition is None and event is None:
        raise EscrowError(f"未知的保证金账务动作: {action}")

    key = idempotency_key or _derive_key(action, account_id, biz_type, biz_id)
    existing = _find_tx_by_key(db, key)
    if existing is not None:
        if idempotency_key is not None:
            return existing  # 显式幂等键：同键即同一操作，直接重放
        # 派生键：仅当账户仍停留在该流水终态时才视为同一操作的重试；
        # 状态已推进（如退还后再次缴纳）属于新的冲突请求，落入下方状态校验
        current = db.get(EscrowAccount, account_id)
        if current is not None and current.status == existing.to_status:
            return existing

    account = db.get(EscrowAccount, account_id)
    if account is None:
        raise EscrowError(f"保证金账户不存在: {account_id}")

    now = datetime.utcnow()
    if transition is not None:
        from_status, to_status, tx_type = transition
        if account.status == to_status:
            # 幂等重放：账户已达目标状态，返回完成该迁移的既有流水
            replayed = _find_transition_tx(db, account_id, tx_type, to_status)
            if replayed is not None:
                return replayed
            raise EscrowStateConflict(f"账户 {account_id} 已处于 {to_status}，但缺少对应流水，数据不一致")
        if account.status != from_status:
            raise EscrowStateConflict(
                f"账户 {account_id} 当前状态 {account.status} 不允许执行 {action}（要求 {from_status}）"
            )
        values = {"status": to_status, "version": EscrowAccount.version + 1}
        if action == "pay":
            values["paid_at"] = now
        elif action == "return":
            values["returned_at"] = now
        result = db.execute(
            update(EscrowAccount)
            .where(EscrowAccount.id == account_id, EscrowAccount.status == from_status)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            # 并发下已被其他请求迁移：按重试语义返回既有流水
            db.refresh(account)
            replayed = _find_transition_tx(db, account_id, tx_type, to_status)
            if account.status == to_status and replayed is not None:
                return replayed
            raise EscrowStateConflict(f"账户 {account_id} 状态并发变更，{action} 未生效，请重试")
        amount = float(account.amount)
        balance_after = amount if action == "pay" else 0.0
    else:
        required_status, tx_type = event
        from_status = to_status = required_status
        if account.status != required_status:
            raise EscrowStateConflict(
                f"账户 {account_id} 当前状态 {account.status} 不允许记录 {action} 事件（要求 {required_status}）"
            )
        amount = 0.0
        balance_after = float(account.amount)

    tx = EscrowTransaction(
        account_id=account_id,
        tx_type=tx_type,
        amount=round(amount, 2),
        balance_after=round(balance_after, 2),
        from_status=from_status,
        to_status=to_status,
        idempotency_key=key,
        operator_id=operator_id,
        biz_type=biz_type,
        biz_id=str(biz_id or ""),
        remark=reason,
    )
    db.add(tx)
    db.flush()
    add_audit(
        db,
        operator_id,
        _AUDIT_ACTIONS[action],
        f"账户 {account_id} {from_status}->{to_status} 金额 {tx.amount} 来源 {biz_type}:{biz_id or '-'} 幂等键 {key}",
        commit=False,
    )
    if commit:
        db.commit()
    else:
        db.flush()
    db.refresh(account)
    return tx


def verify_account(db, account_id: int) -> dict:
    """重放账户流水，校验账户状态与余额链是否一致（审计回溯用）。"""
    account = db.get(EscrowAccount, account_id)
    if account is None:
        raise EscrowError(f"保证金账户不存在: {account_id}")
    txs = (
        db.query(EscrowTransaction)
        .filter(EscrowTransaction.account_id == account_id)
        .order_by(EscrowTransaction.id.asc())
        .all()
    )
    issues: list[str] = []
    status = "unpaid"
    balance = 0.0
    for tx in txs:
        if tx.from_status != status:
            issues.append(f"流水 {tx.id} 起始状态 {tx.from_status} 与上一笔终态 {status} 不一致")
        if tx.tx_type == "deposit":
            balance += float(tx.amount)
        elif tx.tx_type in {"return", "forfeit"}:
            balance -= float(tx.amount)
        if abs(float(tx.balance_after) - balance) > 0.005:
            issues.append(f"流水 {tx.id} 余额 {tx.balance_after} 与重放余额 {round(balance, 2)} 不一致")
        status = tx.to_status
    if status != account.status:
        issues.append(f"流水终态 {status} 与账户状态 {account.status} 不一致")
    return {
        "account_id": account_id,
        "status": account.status,
        "expected_status": status,
        "balance": round(balance, 2),
        "tx_count": len(txs),
        "consistent": not issues,
        "issues": issues,
    }


def create_account(db, section_id: int, bid_document_id: int, bidder_id: int) -> EscrowAccount:
    """创建保证金账户；同一投标重复提交时返回既有账户（唯一约束兜底并发）。"""
    existing = (
        db.query(EscrowAccount)
        .filter(EscrowAccount.section_id == section_id, EscrowAccount.bid_document_id == bid_document_id)
        .first()
    )
    if existing is not None:
        return existing
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
    try:
        db.commit()
    except Exception:
        db.rollback()
        raced = (
            db.query(EscrowAccount)
            .filter(EscrowAccount.section_id == section_id, EscrowAccount.bid_document_id == bid_document_id)
            .first()
        )
        if raced is not None:
            return raced
        raise
    db.refresh(account)
    return account


def pay_deposit(
    db,
    account: EscrowAccount,
    *,
    reason: str = "保证金缴纳",
    operator_id: int | None = None,
    idempotency_key: str | None = None,
) -> EscrowAccount:
    """投标人缴纳保证金到账。"""
    apply_transition(
        db, account.id, "pay",
        reason=reason, operator_id=operator_id, idempotency_key=idempotency_key,
    )
    return db.get(EscrowAccount, account.id)


def return_deposit(
    db,
    account: EscrowAccount,
    reason: str = "未中标保证金退还",
    *,
    operator_id: int | None = None,
    biz_type: str = "manual",
    biz_id: str = "",
    idempotency_key: str | None = None,
    commit: bool = True,
) -> EscrowAccount:
    """足额退还保证金。"""
    apply_transition(
        db, account.id, "return",
        reason=reason, operator_id=operator_id, biz_type=biz_type, biz_id=biz_id,
        idempotency_key=idempotency_key, commit=commit,
    )
    return db.get(EscrowAccount, account.id)


def return_bid_deposit(
    db,
    section_id: int,
    bid_document_id: int,
    reason: str = "未中标保证金退还",
    *,
    operator_id: int | None = None,
    biz_type: str = "manual",
    biz_id: str = "",
    commit: bool = True,
) -> list[EscrowAccount]:
    """退还某份投标对应且已缴纳的保证金（按业务来源幂等，可安全重试）。"""
    accounts = (
        db.query(EscrowAccount)
        .filter(
            EscrowAccount.section_id == section_id,
            EscrowAccount.bid_document_id == bid_document_id,
            EscrowAccount.status == "paid",
        )
        .all()
    )
    return [
        return_deposit(
            db, account, reason,
            operator_id=operator_id, biz_type=biz_type, biz_id=biz_id, commit=commit,
        )
        for account in accounts
    ]


def return_section_deposits(
    db,
    section_id: int,
    reason: str = "标段流标，保证金退还",
    *,
    operator_id: int | None = None,
    biz_type: str = "section_failed",
    biz_id: str | None = None,
    commit: bool = True,
) -> list[EscrowAccount]:
    """退还标段下所有仍处于已缴纳状态的保证金（流标等场景，可整体重试）。"""
    accounts = (
        db.query(EscrowAccount)
        .filter(EscrowAccount.section_id == section_id, EscrowAccount.status == "paid")
        .all()
    )
    return [
        return_deposit(
            db, account, reason,
            operator_id=operator_id, biz_type=biz_type,
            biz_id=biz_id if biz_id is not None else f"section:{section_id}",
            commit=commit,
        )
        for account in accounts
    ]


def forfeit_deposit(
    db,
    account: EscrowAccount,
    reason: str,
    *,
    operator_id: int | None = None,
    biz_type: str = "manual",
    biz_id: str = "",
    idempotency_key: str | None = None,
) -> EscrowAccount:
    """没收保证金（中标后放弃 / 违规）。"""
    apply_transition(
        db, account.id, "forfeit",
        reason=f"保证金没收：{reason}", operator_id=operator_id,
        biz_type=biz_type, biz_id=biz_id, idempotency_key=idempotency_key,
    )
    return db.get(EscrowAccount, account.id)

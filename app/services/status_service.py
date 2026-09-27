"""标段状态机与中标公示生效。"""

from datetime import date, datetime, timedelta

from sqlalchemy import update

from app.models.bid import BidDocument, Winner
from app.models.escrow import EscrowAccount
from app.models.evaluation import AbnormalPriceClarification
from app.models.project import TenderSection, TenderStatusLog
from app.services.audit_service import add_audit
from app.services.escrow_service import apply_transition

ALLOWED = {
    "draft": {"announced", "closed"},
    "announced": {"bidding", "closed"},
    "bidding": {"evaluating", "failed", "closed"},
    "evaluating": {"awarded", "failed", "closed"},
    "awarded": {"closed"},
    "failed": {"announced", "closed"},
    "closed": set(),
}


def can_transition(from_status: str, to_status: str) -> bool:
    return to_status in ALLOWED.get(from_status, set())


def _has_unresolved_abnormal_price(db, section_id: int) -> bool:
    return (
        db.query(AbnormalPriceClarification)
        .filter(
            AbnormalPriceClarification.section_id == section_id,
            AbnormalPriceClarification.status.in_(["pending", "responded"]),
        )
        .first()
        is not None
    )


def transition(
    db,
    section: TenderSection,
    to_status: str,
    operator_id: int | None,
    remark: str = "",
    *,
    commit: bool = True,
) -> bool:
    """标段状态流转并记录轨迹；commit=False 时由调用方统一提交（与账务迁移同事务）。"""
    if section.status == to_status:
        return True  # 幂等：已处于目标状态视为成功，便于整体重试
    if to_status == "awarded" and _has_unresolved_abnormal_price(db, section.id):
        return False
    if not can_transition(section.status, to_status):
        return False
    db.add(
        TenderStatusLog(
            section_id=section.id,
            from_status=section.status,
            to_status=to_status,
            operator_id=operator_id,
            remark=remark,
        )
    )
    section.status = to_status
    if commit:
        db.commit()
    else:
        db.flush()
    return True


def start_publicity(db, section: TenderSection, winner: Winner, *, commit: bool = True) -> Winner:
    """发布中标公示：公示期从今日起 notice_days 天（重复调用不重置公示期）。"""
    if winner.publish_start and winner.publish_end:
        return winner
    winner.publish_start = datetime.now()
    winner.publish_end = datetime.now() + timedelta(days=section.notice_days)
    if commit:
        db.commit()
    else:
        db.flush()
    db.refresh(winner)
    return winner


def winner_bid_eligible(db, winner: Winner) -> bool:
    """中标候选人对应的投标仍须有效，且不存在未完成的异常低价澄清。"""
    bid = db.get(BidDocument, winner.bid_document_id)
    if not bid or bid.section_id != winner.section_id or bid.status in {"abn_excluded", "disqualified", "submitted", "clarifying"}:
        return False
    active = (
        db.query(AbnormalPriceClarification)
        .filter(
            AbnormalPriceClarification.section_id == winner.section_id,
            AbnormalPriceClarification.bid_document_id == winner.bid_document_id,
            AbnormalPriceClarification.status.in_(["pending", "responded", "excluded"]),
        )
        .first()
    )
    return active is None


def confirm_expired_publicity(db, winner: Winner, operator_id: int | None = None) -> bool:
    """公示到期后确认中标生效。

    - 已确认的中标记录重复确认返回成功（幂等重试安全）
    - 状态迁移使用 CAS 条件更新，并发确认只有一方生效
    - 确认与保证金锁定留痕、审计日志在同一事务提交
    """
    if winner.status == "confirmed":
        return True
    if winner.status != "pending":
        return False
    if not winner.publish_end:
        return False
    if not winner_bid_eligible(db, winner):
        db.execute(
            update(Winner)
            .where(Winner.id == winner.id, Winner.status == "pending")
            .values(status="cancelled")
            .execution_options(synchronize_session=False)
        )
        db.commit()
        db.refresh(winner)
        return False
    today = date.today()
    end_date = winner.publish_end.date()
    if today < end_date:
        return False
    result = db.execute(
        update(Winner)
        .where(Winner.id == winner.id, Winner.status == "pending")
        .values(status="confirmed")
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        # 并发下已被确认/取消：按重试语义返回最终结果
        db.refresh(winner)
        return winner.status == "confirmed"

    # 账务留痕：中标确认后保证金继续锁定（待合同签订后退还或转履约），与确认同事务
    account = (
        db.query(EscrowAccount)
        .filter(
            EscrowAccount.section_id == winner.section_id,
            EscrowAccount.bid_document_id == winner.bid_document_id,
            EscrowAccount.status == "paid",
        )
        .first()
    )
    if account is not None:
        apply_transition(
            db,
            account.id,
            "confirm",
            reason="中标确认，保证金继续锁定待后续处理",
            operator_id=operator_id,
            biz_type="winner_confirmed",
            biz_id=f"winner:{winner.id}",
            commit=False,
        )
    add_audit(db, operator_id, "WINNER_CONFIRM", f"中标记录 {winner.id} 公示到期确认中标", commit=False)
    db.commit()
    db.refresh(winner)
    return True

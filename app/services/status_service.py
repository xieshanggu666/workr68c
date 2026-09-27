"""标段状态机、流标处理与中标公示生效。

- transition：状态流转用条件更新（CAS）实现，并发下仅一个请求能推进状态，
  流转日志与状态变更同事务写入
- fail_section：流标 = 状态流转 + 全部保证金退还 + 审计，单事务提交、可整体重试
- confirm_expired_publicity：中标确认幂等，重复确认返回既有结果，不重复处理
"""

from datetime import date, datetime, timedelta

from sqlalchemy import update

from app.models.bid import BidDocument, Winner
from app.models.evaluation import AbnormalPriceClarification
from app.models.project import TenderSection, TenderStatusLog
from app.services.audit_service import add_audit
from app.services.escrow_service import return_section_deposits

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
    """标段状态流转（CAS 原子推进）。

    并发或重试场景下，只有第一个请求能完成流转，其余返回 False；
    commit=False 时嵌入外层事务，由调用方统一提交。
    """
    if to_status == "awarded" and _has_unresolved_abnormal_price(db, section.id):
        return False
    if not can_transition(section.status, to_status):
        return False
    result = db.execute(
        update(TenderSection)
        .where(TenderSection.id == section.id, TenderSection.status == section.status)
        .values(status=to_status)
    )
    if result.rowcount != 1:
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


def fail_section(db, section: TenderSection, operator_id: int | None, remark: str = "标段流标") -> bool:
    """流标：状态流转 + 退还全部已缴保证金 + 审计，单事务、可整体重试。

    重试安全：状态已流转则跳过流转；保证金按确定性幂等键退还，
    已退账户自动跳过，未退账户继续退还；无任何变更时不重复写审计。
    """
    transitioned = False
    if section.status != "failed":
        if not transition(db, section, "failed", operator_id, remark, commit=False):
            db.rollback()
            return False
        transitioned = True
    refunded = return_section_deposits(
        db,
        section.id,
        "标段流标，保证金退还",
        key_prefix=f"return:section:{section.id}:failed",
        ref_type="section_failed",
        ref_id=section.id,
        operator_id=operator_id,
        commit=False,
    )
    if transitioned or refunded:
        add_audit(
            db,
            operator_id,
            "SECTION_FAILED",
            f"标段 {section.code} 流标：{remark}",
            entity_type="section",
            entity_id=section.id,
            commit=False,
        )
    db.commit()
    return True


def start_publicity(db, section: TenderSection, winner: Winner, *, commit: bool = True) -> Winner:
    """发布中标公示：公示期从今日起 notice_days 天。"""
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
    """公示到期后确认中标生效（幂等：重复确认返回既有结果，不重复处理）。"""
    if winner.status == "confirmed":
        return True  # 幂等重放：已确认
    if winner.status != "pending":
        return False
    if not winner.publish_end:
        return False
    if not winner_bid_eligible(db, winner):
        result = db.execute(
            update(Winner)
            .where(Winner.id == winner.id, Winner.status == "pending")
            .values(status="cancelled")
        )
        if result.rowcount == 1:
            winner.status = "cancelled"
            add_audit(
                db,
                operator_id,
                "WINNER_CANCELLED",
                f"中标记录 {winner.id} 对应投标已失效，取消候选资格",
                entity_type="winner",
                entity_id=winner.id,
                commit=False,
            )
            db.commit()
        return False
    today = date.today()
    end_date = winner.publish_end.date()
    if today < end_date:
        return False
    # CAS 推进：并发确认只有一个生效
    result = db.execute(
        update(Winner)
        .where(Winner.id == winner.id, Winner.status == "pending")
        .values(status="confirmed")
    )
    if result.rowcount != 1:
        db.rollback()
        db.refresh(winner)
        return winner.status == "confirmed"
    winner.status = "confirmed"
    add_audit(
        db,
        operator_id,
        "WINNER_CONFIRMED",
        f"中标记录 {winner.id} 公示期满，确认中标生效",
        entity_type="winner",
        entity_id=winner.id,
        commit=False,
    )
    db.commit()
    return True

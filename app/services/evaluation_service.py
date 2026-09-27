"""评标引擎：综合评分法 / 最低价法。

- 综合评分法：总分 = 价格分 + 技术商务主观分
  价格分依据最低有效报价与该投标报价的比值计算
  主观分 = 评委对评分项打分的均值 × 权重
- 最低价法：合规通过中报价最低者中标，低于平均价阈值判异常低价

账务一致性约定：
- 澄清排除 / 流标 / 未中标的保证金退还统一走保证金状态机（escrow_service），
  幂等键按业务事件确定（澄清按 clarification id，流标按 section id，未中标按 bid id），
  同一事件重试或并发不会重复退款
- 定标（中标人 + 未中标退款 + 公示 + 标段状态流转）在单个事务内提交，
  并以 awarded 状态流转作为并发闸门：重复开标 / 并发定标自动收敛为一次
"""

import json
from datetime import datetime, timedelta
from statistics import mean

from sqlalchemy import update

from app.models.bid import BidDocument, Winner
from app.models.evaluation import AbnormalPriceClarification, BidScore, EvaluationItem, EvaluationRule
from app.services.escrow_service import return_bid_deposit
from app.services.status_service import fail_section, start_publicity, transition


def compute_price_score(rule: EvaluationRule, bid_price: float, min_price: float) -> float:
    """综合评分法下的价格分。"""
    if min_price <= 0 or bid_price <= 0:
        return 0.0
    price_score = (min_price / bid_price) * float(rule.price_full_score)
    return round(price_score, 2)


def detect_abnormal_low(rule: EvaluationRule, prices: list[float]) -> list[float]:
    """检测异常低价：报价低于平均价 * 阈值的报价。"""
    avg = mean(prices)
    threshold = avg * float(rule.abnormal_price_ratio)
    return [p for p in prices if p < threshold]


def _aggregate_item_score(item: EvaluationItem, judge_scores: list[float], drop_extreme: bool) -> float:
    """聚合评委打分，取均值并加权。"""
    if not judge_scores:
        return 0.0
    if drop_extreme and len(judge_scores) >= 3:
        valid = sorted(judge_scores)[1:-1]
    else:
        valid = judge_scores
    avg = mean(valid)
    return round(avg * float(item.weight), 4)


def _compliance_passed(bid: BidDocument) -> bool:
    try:
        data = json.loads(bid.compliance_json or "{}")
        return bool(data.get("passed", False))
    except (TypeError, ValueError, AttributeError):
        return False


ACTIVE_CLARIFICATION_STATUSES = {"pending", "responded"}
CLARIFICATION_DEADLINE_HOURS = 24


def clarification_dict(item: AbnormalPriceClarification) -> dict:
    return {
        "id": item.id,
        "section_id": item.section_id,
        "bid_document_id": item.bid_document_id,
        "bidder_id": item.bidder_id,
        "suspected_price": float(item.suspected_price),
        "threshold_price": float(item.threshold_price),
        "status": item.status,
        "request_remark": item.request_remark,
        "response_content": item.response_content,
        "response_at": item.response_at,
        "review_remark": item.review_remark,
        "reviewed_by": item.reviewed_by,
        "reviewed_at": item.reviewed_at,
        "deadline": item.deadline,
        "created_at": item.created_at,
    }


def list_section_clarifications(db, section_id: int) -> list[AbnormalPriceClarification]:
    return (
        db.query(AbnormalPriceClarification)
        .filter(AbnormalPriceClarification.section_id == section_id)
        .order_by(AbnormalPriceClarification.created_at.asc())
        .all()
    )


def _rank_lowest_candidates(bids: list[BidDocument]) -> list[dict]:
    ranked = [
        {
            "bid_document_id": bid.id,
            "company": bid.company,
            "price": float(bid.price),
            "price_score": 0,
            "total": 0.0,
            "status": bid.status,
        }
        for bid in bids
    ]
    return sorted(ranked, key=lambda r: r["price"])


def _find_abnormal_low_bids(rule: EvaluationRule, bids: list[BidDocument]) -> list[tuple[BidDocument, float]]:
    """返回低于平均价阈值的投标及对应阈值。"""
    if not bids:
        return []
    avg = mean(float(b.price) for b in bids)
    threshold = avg * float(rule.abnormal_price_ratio)
    return [(bid, threshold) for bid in bids if float(bid.price) < threshold]


def _base_result(clarifications: list[AbnormalPriceClarification], section) -> dict:
    """开标/定标结果的统一结构（含定标字段缺省值）。"""
    active = [item for item in clarifications if item.status in ACTIVE_CLARIFICATION_STATUSES]
    return {
        "ranked": [],
        "clarifications": [clarification_dict(item) for item in clarifications],
        "active_clarifications": [clarification_dict(item) for item in active],
        "abnormal_prices": [float(item.suspected_price) for item in clarifications],
        "active_abnormal_prices": [float(item.suspected_price) for item in active],
        "needs_clarification": bool(active),
        "section_status": section.status,
        "winner_id": None,
        "winner_bid_id": None,
        "winner_company": None,
        "winner_price": None,
        "publish_end": None,
        "awarded": False,
        "failed": False,
    }


def _awarded_result(db, winner: Winner, clarifications: list[AbnormalPriceClarification], section) -> dict:
    """已定标的幂等结果：重复定标 / 重试时返回当前中标信息。"""
    result = _base_result(clarifications, section)
    bid = db.get(BidDocument, winner.bid_document_id)
    result.update(
        {
            "winner_id": winner.id,
            "winner_bid_id": winner.bid_document_id,
            "winner_company": bid.company if bid else None,
            "winner_price": float(winner.win_price),
            "publish_end": winner.publish_end,
            "awarded": True,
        }
    )
    return result


def _refund_clarification_exclusion(db, item: AbnormalPriceClarification, reason: str, operator_id: int | None) -> None:
    """异常低价排除的保证金退还：按澄清记录幂等，人工审核与逾期殊途同幂。"""
    return_bid_deposit(
        db,
        item.section_id,
        item.bid_document_id,
        reason,
        key_prefix=f"return:clarification:{item.id}",
        ref_type="clarification",
        ref_id=item.id,
        operator_id=operator_id,
        commit=False,
    )


def expire_pending_clarifications(db, section_id: int, *, commit: bool = True) -> list[AbnormalPriceClarification]:
    """澄清期限届满仍未说明的，视为不能合理说明并排除。"""
    now = datetime.utcnow()
    expired = (
        db.query(AbnormalPriceClarification)
        .filter(
            AbnormalPriceClarification.section_id == section_id,
            AbnormalPriceClarification.status == "pending",
            AbnormalPriceClarification.deadline < now,
        )
        .all()
    )
    for item in expired:
        item.status = "excluded"
        item.review_remark = "澄清期限届满未作出说明"
        item.reviewed_at = now
        bid = db.get(BidDocument, item.bid_document_id)
        if bid:
            bid.status = "abn_excluded"
        _refund_clarification_exclusion(db, item, "异常低价未在期限内澄清，保证金退还", None)
    if expired and commit:
        db.commit()
    return expired


def prepare_lowest_price_evaluation(db, section, rule: EvaluationRule, bids: list[BidDocument], *, commit: bool = True) -> dict:
    """发起最低价法澄清；待澄清/已排除报价均不进入排名和定标。"""
    expire_pending_clarifications(db, section.id, commit=False)
    clarifications = list_section_clarifications(db, section.id)
    records_by_bid = {item.bid_document_id: item for item in clarifications}
    active_by_bid = {bid_id: item for bid_id, item in records_by_bid.items() if item.status in ACTIVE_CLARIFICATION_STATUSES}

    qualified = []
    for bid in bids:
        if not _compliance_passed(bid) or bid.status == "abn_excluded" or bid.id in active_by_bid:
            continue
        record = records_by_bid.get(bid.id)
        if record is None or record.status == "accepted":
            qualified.append(bid)

    flagged = _find_abnormal_low_bids(rule, qualified)
    now = datetime.utcnow()
    created = False
    for bid, threshold in flagged:
        if bid.id in records_by_bid:
            continue
        item = AbnormalPriceClarification(
            section_id=section.id,
            bid_document_id=bid.id,
            bidder_id=bid.bidder_id,
            suspected_price=bid.price,
            threshold_price=round(threshold, 2),
            status="pending",
            request_remark="报价低于平均价阈值，需书面说明报价合理性",
            deadline=now + timedelta(hours=CLARIFICATION_DEADLINE_HOURS),
        )
        db.add(item)
        bid.status = "clarifying"
        created = True
    if created:
        db.flush()

    clarifications = list_section_clarifications(db, section.id)
    active = [item for item in clarifications if item.status in ACTIVE_CLARIFICATION_STATUSES]
    accepted_ids = {item.bid_document_id for item in clarifications if item.status == "accepted"}
    excluded_ids = {item.bid_document_id for item in clarifications if item.status == "excluded"}
    clarification_ids = {item.bid_document_id for item in clarifications}
    candidates = [
        bid
        for bid in bids
        if _compliance_passed(bid)
        and bid.id not in {item.bid_document_id for item in active}
        and bid.id not in excluded_ids
        and (bid.id in accepted_ids or bid.id not in clarification_ids)
    ]
    if commit:
        db.commit()
    result = _base_result(clarifications, section)
    result.update(
        {
            "ranked": _rank_lowest_candidates(candidates),
            "candidate_bids": candidates,
        }
    )
    return result


def _active_winner(db, section_id: int) -> Winner | None:
    return (
        db.query(Winner)
        .filter(Winner.section_id == section_id, Winner.status != "cancelled")
        .order_by(Winner.created_at.desc())
        .first()
    )


def finalize_lowest_price_evaluation(db, section, user, bids: list[BidDocument] | None = None, *, commit: bool = True) -> dict:
    """全部异常低价处理完成后才允许最低价排名定标和公示。

    幂等：标段已有有效中标记录时直接返回当前结果，重复调用不产生重复定标。
    原子性：中标人、未中标退款、公示、标段状态流转在同一事务提交。
    """
    if bids is None:
        bids = db.query(BidDocument).filter(BidDocument.section_id == section.id).all()
    expire_pending_clarifications(db, section.id, commit=False)
    clarifications = list_section_clarifications(db, section.id)
    active = [item for item in clarifications if item.status in ACTIVE_CLARIFICATION_STATUSES]
    if active:
        if commit:
            db.commit()
        return _base_result(clarifications, section)

    # 幂等闸门：已存在有效中标记录，直接返回当前结果
    existing = _active_winner(db, section.id)
    if existing is not None:
        if commit:
            db.commit()
        return _awarded_result(db, existing, clarifications, section)

    accepted_ids = {item.bid_document_id for item in clarifications if item.status == "accepted"}
    excluded_ids = {item.bid_document_id for item in clarifications if item.status == "excluded"}
    candidates = [
        bid
        for bid in bids
        if _compliance_passed(bid)
        and bid.id not in excluded_ids
        and (bid.id in accepted_ids or bid.id not in {item.bid_document_id for item in clarifications})
    ]
    ranked = _rank_lowest_candidates(candidates)
    if not candidates:
        # 流标：状态流转 + 全部保证金退还 + 审计，单事务可重试
        if not fail_section(db, section, user.id, "异常低价排除后无有效投标，标段流标"):
            raise ValueError("当前标段状态不允许流标")
        result = _base_result(list_section_clarifications(db, section.id), section)
        result.update(
            {
                "ranked": ranked,
                "failed": True,
                "message": "异常低价排除后无有效投标，已流转为流标",
            }
        )
        if commit:
            db.commit()
        return result

    winner_bid = min(candidates, key=lambda bid: float(bid.price))
    # 并发闸门：awarded 状态流转（CAS）仅允许一个请求进入定标
    if not transition(db, section, "awarded", user.id, "异常低价澄清完成，开标并产生中标候选人", commit=False):
        db.rollback()
        existing = _active_winner(db, section.id)
        if existing is not None:
            result = _awarded_result(db, existing, list_section_clarifications(db, section.id), section)
            if commit:
                db.commit()
            return result
        raise ValueError("标段状态被并发修改，定标失败，请重试")

    winner = Winner(
        section_id=section.id,
        bid_document_id=winner_bid.id,
        bidder_id=winner_bid.bidder_id,
        win_price=float(winner_bid.price),
        status="pending",
    )
    db.add(winner)
    db.flush()

    winner_bid.status = "won"
    for bid in candidates:
        if bid.id != winner_bid.id:
            bid.status = "lost"
            return_bid_deposit(
                db,
                section.id,
                bid.id,
                "未中标保证金退还",
                key_prefix=f"return:bid:{bid.id}:not_won",
                ref_type="not_won",
                ref_id=section.id,
                operator_id=user.id,
                commit=False,
            )

    start_publicity(db, section, winner, commit=False)
    if commit:
        db.commit()
    else:
        db.flush()
    db.refresh(winner)
    result = _base_result(clarifications, section)
    result.update(
        {
            "ranked": ranked,
            "winner_id": winner.id,
            "winner_bid_id": winner_bid.id,
            "winner_company": winner_bid.company,
            "winner_price": float(winner_bid.price),
            "publish_end": winner.publish_end,
            "awarded": True,
        }
    )
    return result


def submit_clarification(db, item: AbnormalPriceClarification, content: str) -> AbnormalPriceClarification:
    """投标人提交异常低价澄清说明（CAS 防并发重复提交）。"""
    if item.status != "pending":
        raise ValueError("该澄清已处理，不能重复提交")
    if item.deadline < datetime.utcnow():
        raise ValueError("澄清期限已届满，不能提交说明")
    now = datetime.utcnow()
    result = db.execute(
        update(AbnormalPriceClarification)
        .where(
            AbnormalPriceClarification.id == item.id,
            AbnormalPriceClarification.status == "pending",
        )
        .values(response_content=content, status="responded", response_at=now)
    )
    if result.rowcount != 1:
        db.rollback()
        raise ValueError("该澄清已处理，不能重复提交")
    item.response_content = content
    item.status = "responded"
    item.response_at = now
    db.commit()
    db.refresh(item)
    return item


def review_clarification(db, section, item: AbnormalPriceClarification, action: str, reviewer, remark: str = "") -> dict:
    """评审澄清结果；accepted 恢复有效性，excluded 永久排除出定标链路。

    审核结论 + 保证金退还 + 后续定标在同一事务提交；并发审核由 CAS 收敛。
    """
    if action not in {"accepted", "excluded"}:
        raise ValueError("澄清处理结果无效")
    if item.status not in ACTIVE_CLARIFICATION_STATUSES:
        raise ValueError("该澄清已处理")

    now = datetime.utcnow()
    result = db.execute(
        update(AbnormalPriceClarification)
        .where(
            AbnormalPriceClarification.id == item.id,
            AbnormalPriceClarification.status.in_(list(ACTIVE_CLARIFICATION_STATUSES)),
        )
        .values(status=action, review_remark=remark, reviewed_by=reviewer.id, reviewed_at=now)
    )
    if result.rowcount != 1:
        db.rollback()
        raise ValueError("该澄清已处理")
    item.status = action
    item.review_remark = remark
    item.reviewed_by = reviewer.id
    item.reviewed_at = now

    bid = db.get(BidDocument, item.bid_document_id)
    if action == "accepted":
        if bid:
            bid.status = "qualified"
    else:
        if bid:
            bid.status = "abn_excluded"
        _refund_clarification_exclusion(db, item, "异常低价澄清不成立，保证金退还", reviewer.id)

    final = finalize_lowest_price_evaluation(db, section, reviewer, commit=False)
    db.commit()
    return final


def evaluate_section(db, section_id: int, rule: EvaluationRule, bids: list[BidDocument]) -> list[dict]:
    """对合规通过的投标进行综合评分，返回按总分排序的排名列表。"""
    items = (
        db.query(EvaluationItem)
        .filter(EvaluationItem.section_id == section_id)
        .all()
    )
    valid_prices = [float(b.price) for b in bids if _compliance_passed(b)]
    if not valid_prices:
        return []
    min_price = min(valid_prices)

    results: list[dict] = []
    for bid in bids:
        if not _compliance_passed(bid):
            continue
        total = compute_price_score(rule, float(bid.price), min_price)
        breakdown = {"price": compute_price_score(rule, float(bid.price), min_price)}
        for item in items:
            scores = []
            for score in db.query(BidScore).filter(
                BidScore.bid_document_id == bid.id, BidScore.section_id == section_id
            ):
                item_map = json.loads(score.scores_json or "{}")
                if str(item.id) in item_map:
                    scores.append(float(item_map[str(item.id)]))
            item_score = _aggregate_item_score(item, scores, bool(rule.drop_highest_lowest))
            breakdown[f"item_{item.id}"] = item_score
            total += item_score
        results.append(
            {
                "bid_document_id": bid.id,
                "company": bid.company,
                "price": float(bid.price),
                "price_score": breakdown["price"],
                "total": round(total, 2),
            }
        )
    results.sort(key=lambda r: r["total"], reverse=True)
    return results

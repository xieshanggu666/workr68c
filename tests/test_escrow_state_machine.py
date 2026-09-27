"""保证金账务状态机专项测试：幂等、并发、流水一致性、审计回溯与可重试流程。"""

import asyncio
import json
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import update

from app.main import app
from app.models import (
    AbnormalPriceClarification,
    AuditLog,
    BidDocument,
    EscrowAccount,
    EscrowTransaction,
    EvaluationRule,
    Project,
    TenderSection,
    TenderStatusLog,
    User,
    Winner,
)
from app.services.escrow_service import (
    EscrowStateConflict,
    apply_transition,
    create_account,
    forfeit_deposit,
    pay_deposit,
    return_deposit,
    verify_account,
)
from app.services.status_service import confirm_expired_publicity
from app.core.security import hash_password


def _user(db, username, role="bidder", company="测试公司"):
    pw, salt = hash_password("123456")
    u = User(
        username=username,
        display_name=username,
        email=f"{username}@example.com",
        company=company,
        password_hash=pw,
        salt=salt,
        role=role,
    )
    db.add(u)
    db.commit()
    return u


def _project(db, code="ZB-SM-001"):
    p = Project(code=code, name="状态机测试项目", category="货物", budget=2_000_000, status="published")
    db.add(p)
    db.commit()
    return p


def _section(db, project, method="comprehensive", status="bidding", code="BD-SM-001"):
    s = TenderSection(
        project_id=project.id,
        code=code,
        name="状态机测试标段",
        status=status,
        method=method,
        deposit_ratio=0.02,
        notice_days=3,
    )
    db.add(s)
    db.commit()
    return s


def _bid(db, section, bidder, price, passed=True):
    bid = BidDocument(
        section_id=section.id,
        bidder_id=bidder.id,
        company=bidder.company,
        price=price,
        license_expiry="2030-01-01",
        tech_material="技术方案材料齐全",
        status="qualified" if passed else "submitted",
        compliance_json=json.dumps({"passed": passed, "errors": []}),
    )
    db.add(bid)
    db.commit()
    return bid


def _escrow(db, section, bid, bidder, amount=200_000, status="paid"):
    """创建保证金账户；status="paid" 时通过状态机缴纳，保证流水链完整。"""
    account = EscrowAccount(
        section_id=section.id,
        bid_document_id=bid.id,
        bidder_id=bidder.id,
        amount=amount,
        status="unpaid",
    )
    db.add(account)
    db.commit()
    if status != "unpaid":
        pay_deposit(db, account)
    return account


def _txs(db, account_id, tx_type=None):
    q = db.query(EscrowTransaction).filter(EscrowTransaction.account_id == account_id)
    if tx_type:
        q = q.filter(EscrowTransaction.tx_type == tx_type)
    return q.order_by(EscrowTransaction.id.asc()).all()


def _login_and_post(username, path, payload=None):
    async def _run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/api/auth/login", json={"username": username, "password": "123456"})
            return await client.post(path, json=payload or {})

    return asyncio.run(_run())


# ---------- 状态机：幂等与冲突 ----------


def test_pay_is_idempotent_and_replays_original_tx(db):
    section = _section(db, _project(db))
    bidder = _user(db, "sm_b1")
    bid = _bid(db, section, bidder, 100_000)
    account = _escrow(db, section, bid, bidder, status="unpaid")

    pay_deposit(db, account, operator_id=bidder.id)
    pay_deposit(db, account, operator_id=bidder.id)  # 重试：不重复记账

    deposits = _txs(db, account.id, "deposit")
    assert len(deposits) == 1, f"重复缴纳应只有一条缴纳流水，实际 {len(deposits)}"
    assert deposits[0].from_status == "unpaid" and deposits[0].to_status == "paid"
    assert float(deposits[0].balance_after) == 200_000
    db.refresh(account)
    assert account.status == "paid" and account.version == 1


def test_same_idempotency_key_returns_same_tx(db):
    section = _section(db, _project(db))
    bidder = _user(db, "sm_b2")
    bid = _bid(db, section, bidder, 100_000)
    account = _escrow(db, section, bid, bidder, status="unpaid")

    tx1 = apply_transition(db, account.id, "pay", reason="缴纳", idempotency_key="pay-key-1")
    tx2 = apply_transition(db, account.id, "pay", reason="缴纳", idempotency_key="pay-key-1")
    assert tx1.id == tx2.id
    assert len(_txs(db, account.id)) == 1


def test_conflicting_transitions_raise(db):
    section = _section(db, _project(db))
    bidder = _user(db, "sm_b3")
    bid = _bid(db, section, bidder, 100_000)
    account = _escrow(db, section, bid, bidder, status="unpaid")

    with pytest.raises(EscrowStateConflict):
        return_deposit(db, account)  # 未缴纳不能退还
    pay_deposit(db, account)
    return_deposit(db, account)
    with pytest.raises(EscrowStateConflict):
        forfeit_deposit(db, account, "违规")  # 已退还不能没收
    with pytest.raises(EscrowStateConflict):
        pay_deposit(db, account)  # 已退还的账户不能重新缴纳（终态）


def test_forfeit_does_not_set_returned_at(db):
    """没收不应写入退还时间（回归：旧实现错写 returned_at）。"""
    section = _section(db, _project(db))
    bidder = _user(db, "sm_b4")
    bid = _bid(db, section, bidder, 100_000)
    account = _escrow(db, section, bid, bidder)
    forfeit_deposit(db, account, "放弃中标")
    db.refresh(account)
    assert account.status == "forfeited"
    assert account.returned_at is None
    tx = _txs(db, account.id, "forfeit")[0]
    assert tx.from_status == "paid" and tx.to_status == "forfeited"
    assert float(tx.balance_after) == 0


def test_concurrent_cas_failure_replays_existing_tx(db):
    """并发下 CAS 失败的一方按重试语义拿到既有流水，不产生重复退款。"""
    section = _section(db, _project(db))
    bidder = _user(db, "sm_b5")
    bid = _bid(db, section, bidder, 100_000)
    account = _escrow(db, section, bid, bidder)

    # 模拟并发：另一事务在本会话读取后已先行完成退还（绕过 identity map）
    db.execute(
        update(EscrowAccount)
        .where(EscrowAccount.id == account.id)
        .values(status="returned", returned_at=datetime.utcnow())
        .execution_options(synchronize_session=False)
    )
    raced_tx = EscrowTransaction(
        account_id=account.id,
        tx_type="return",
        amount=200_000,
        balance_after=0,
        from_status="paid",
        to_status="returned",
        idempotency_key="raced-key",
        biz_type="manual",
    )
    db.add(raced_tx)
    db.flush()
    # 本会话内存中的 account 仍是 paid（未 refresh），CAS 将匹配 0 行
    assert account.status == "paid"

    tx = apply_transition(db, account.id, "return", reason="并发重试退还")
    assert tx.id == raced_tx.id, "CAS 失败应重放到既有流水"
    assert len(_txs(db, account.id, "return")) == 1
    db.refresh(account)
    assert account.status == "returned"


def test_create_account_idempotent_per_bid(db):
    project = _project(db)
    section = _section(db, project)
    bidder = _user(db, "sm_b6")
    bid = _bid(db, section, bidder, 100_000)
    a1 = create_account(db, section.id, bid.id, bidder.id)
    a2 = create_account(db, section.id, bid.id, bidder.id)
    assert a1.id == a2.id
    assert db.query(EscrowAccount).filter(EscrowAccount.bid_document_id == bid.id).count() == 1
    assert float(a1.amount) == 2_000_000 * 0.02


# ---------- 流水一致性与审计回溯 ----------


def test_verify_account_consistent_after_full_lifecycle(db):
    section = _section(db, _project(db))
    bidder = _user(db, "sm_b7")
    bid = _bid(db, section, bidder, 100_000)
    account = _escrow(db, section, bid, bidder, status="unpaid")
    pay_deposit(db, account)
    return_deposit(db, account, "未中标保证金退还", biz_type="bid_lost", biz_id=f"bid:{bid.id}")

    result = verify_account(db, account.id)
    assert result["consistent"] is True, f"流水重放应一致: {result['issues']}"
    assert result["expected_status"] == "returned"
    assert result["balance"] == 0
    assert result["tx_count"] == 2


def test_verify_account_detects_tampered_balance(db):
    section = _section(db, _project(db))
    bidder = _user(db, "sm_b8")
    bid = _bid(db, section, bidder, 100_000)
    account = _escrow(db, section, bid, bidder, status="unpaid")
    pay_deposit(db, account)
    tx = _txs(db, account.id, "deposit")[0]
    tx.balance_after = 1  # 篡改余额
    db.commit()
    result = verify_account(db, account.id)
    assert result["consistent"] is False
    assert result["issues"]


def test_transition_writes_audit_with_biz_context(db):
    """账务迁移与审计日志同事务：可追溯操作人、业务来源与幂等键。"""
    section = _section(db, _project(db))
    bidder = _user(db, "sm_b9")
    operator = _user(db, "sm_op1", role="operator")
    bid = _bid(db, section, bidder, 100_000)
    account = _escrow(db, section, bid, bidder)
    return_deposit(
        db, account, "异常低价澄清不成立，保证金退还",
        operator_id=operator.id, biz_type="clarification_excluded", biz_id="clarification:9",
    )
    audits = db.query(AuditLog).filter(AuditLog.action == "ESCROW_RETURN").all()
    assert len(audits) == 1
    assert audits[0].user_id == operator.id
    assert "clarification_excluded" in audits[0].detail
    tx = _txs(db, account.id, "return")[0]
    assert tx.operator_id == operator.id
    assert tx.biz_type == "clarification_excluded"
    assert tx.biz_id == "clarification:9"
    assert tx.idempotency_key


# ---------- 流标：单事务与可重试 ----------


def test_section_failed_transition_is_atomic_and_retryable(db):
    """流标流转与保证金退还整体生效；重复调用不重复退款、不重复记轨迹。"""
    _user(db, "sm_admin", role="admin")
    project = _project(db)
    section = _section(db, project)
    bidder1 = _user(db, "sm_f1")
    bidder2 = _user(db, "sm_f2")
    bid1 = _bid(db, section, bidder1, 100_000)
    bid2 = _bid(db, section, bidder2, 120_000)
    acc1 = _escrow(db, section, bid1, bidder1)
    acc2 = _escrow(db, section, bid2, bidder2)

    r1 = _login_and_post("sm_admin", f"/api/sections/{section.id}/transition", {"to_status": "failed", "remark": "有效投标不足"})
    assert r1.status_code == 200
    r2 = _login_and_post("sm_admin", f"/api/sections/{section.id}/transition", {"to_status": "failed", "remark": "重试"})
    assert r2.status_code == 200, f"流标重试应幂等成功，实际 {r2.status_code}: {r2.text}"

    db.refresh(section)
    db.refresh(acc1)
    db.refresh(acc2)
    assert section.status == "failed"
    assert acc1.status == "returned" and acc2.status == "returned"
    assert len(_txs(db, acc1.id, "return")) == 1, "重试不得重复退款"
    assert len(_txs(db, acc2.id, "return")) == 1
    tx = _txs(db, acc1.id, "return")[0]
    assert tx.biz_type == "section_failed"
    failed_logs = db.query(TenderStatusLog).filter(
        TenderStatusLog.section_id == section.id, TenderStatusLog.to_status == "failed"
    ).count()
    assert failed_logs == 1, "重复流标不应重复记录状态轨迹"
    for acc in (acc1, acc2):
        assert verify_account(db, acc.id)["consistent"] is True


# ---------- 中标确认：CAS 与账务留痕 ----------


def test_confirm_winner_records_escrow_event_and_is_idempotent(db):
    section = _section(db, _project(db), status="awarded")
    bidder = _user(db, "sm_w1")
    bid = _bid(db, section, bidder, 100_000)
    bid.status = "won"
    db.commit()
    account = _escrow(db, section, bid, bidder)
    winner = Winner(
        section_id=section.id, bid_document_id=bid.id, bidder_id=bidder.id,
        win_price=100_000, status="pending",
        publish_start=datetime.now() - timedelta(days=3), publish_end=datetime.now(),
    )
    db.add(winner)
    db.commit()

    assert confirm_expired_publicity(db, winner, operator_id=bidder.id) is True
    db.refresh(winner)
    assert winner.status == "confirmed"
    confirms = _txs(db, account.id, "confirm")
    assert len(confirms) == 1, "中标确认应在保证金流水留痕"
    assert confirms[0].biz_type == "winner_confirmed"
    assert confirms[0].from_status == confirms[0].to_status == "paid"
    db.refresh(account)
    assert account.status == "paid", "确认后保证金继续锁定"

    assert confirm_expired_publicity(db, winner, operator_id=bidder.id) is True, "重复确认应幂等成功"
    assert len(_txs(db, account.id, "confirm")) == 1, "重复确认不得重复留痕"
    audits = db.query(AuditLog).filter(AuditLog.action == "WINNER_CONFIRM").all()
    assert len(audits) == 1


# ---------- 异常低价澄清：排除退款幂等 ----------


def test_clarification_exclusion_refund_is_idempotent_across_reviews(db):
    """澄清不成立排除后，重复触发定标流程不重复退还保证金。"""
    _user(db, "sm_admin2", role="admin")
    project = _project(db, code="ZB-SM-002")
    section = _section(db, project, method="lowest_price", status="evaluating", code="BD-SM-002")
    db.add(EvaluationRule(section_id=section.id, method="lowest_price", abnormal_price_ratio=0.6))
    db.commit()
    normal_bidder = _user(db, "sm_c1")
    abnormal_bidder = _user(db, "sm_c2")
    normal = _bid(db, section, normal_bidder, 1_000_000)
    abnormal = _bid(db, section, abnormal_bidder, 300_000)
    normal_acc = _escrow(db, section, normal, normal_bidder)
    abnormal_acc = _escrow(db, section, abnormal, abnormal_bidder)

    r = _login_and_post("sm_admin2", f"/api/sections/{section.id}/evaluation/open")
    assert r.json()["needs_clarification"] is True
    clarification = db.query(AbnormalPriceClarification).filter(
        AbnormalPriceClarification.bid_document_id == abnormal.id
    ).one()
    _login_and_post("sm_c2", f"/api/clarifications/{clarification.id}/response", {"content": "无法合理说明"})

    review = _login_and_post(
        "sm_admin2", f"/api/clarifications/{clarification.id}/review",
        {"action": "excluded", "remark": "不能证明可履约"},
    )
    assert review.json()["awarded"] is True
    # 重复审核同一澄清（已排除分支会再次走定标流程）
    review2 = _login_and_post(
        "sm_admin2", f"/api/clarifications/{clarification.id}/review",
        {"action": "excluded", "remark": "重复操作"},
    )
    assert review2.status_code == 200

    db.refresh(abnormal_acc)
    db.refresh(normal_acc)
    assert abnormal_acc.status == "returned"
    assert len(_txs(db, abnormal_acc.id, "return")) == 1, "澄清排除退款应幂等"
    tx = _txs(db, abnormal_acc.id, "return")[0]
    assert tx.biz_type == "clarification_excluded"
    assert tx.biz_id == f"clarification:{clarification.id}"
    assert normal_acc.status == "paid", "中标人保证金应保持锁定"
    for acc in (abnormal_acc, normal_acc):
        assert verify_account(db, acc.id)["consistent"] is True

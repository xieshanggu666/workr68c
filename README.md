# 招投标管理系统

基于 **FastAPI + SQLAlchemy + SQLite** 后端与 **Vue 3（本地运行时，离线可用）** 前端的企业招投标管理系统。

## 功能模块

- **招标项目管理**：项目建档、预算、类别、状态管理（草稿/已发布/招标中/评标中/已定标/已关闭）
- **标段管理**：多标段、评标方法（综合评分法 / 最低价法）、保证金比例、公示天数、资格要求、状态机流转与轨迹记录
- **投标管理**：投标文件提交、报价、证照有效期、技术方案，自动合规校验（必填/日期/范围规则）
- **评标引擎**：
  - 综合评分法：价格分（最低价基准）+ 技术/商务主观分（评委打分、加权）
  - 最低价法：合规通过中最低报价中标；异常低价自动发起澄清，澄清期间不得排名、定标或公示；澄清成立才恢复有效，澄清不成立或逾期则排除并退还保证金
- **中标公示**：公示期管理、到期确认中标（确认与保证金锁定留痕同事务，重复确认幂等）
- **保证金账务**：统一的可重试账务状态机（缴纳 / 退还 / 没收 / 中标确认锁定），完整资金流水
- **审计日志**：全操作留痕，账务迁移与审计同事务落库

## 保证金账务状态机

所有保证金变动（含异常低价排除退款、流标退款、未中标退款、中标确认）统一经由
`escrow_service.apply_transition` 状态机入口：

```
unpaid --pay--> paid --return-->  returned
                   └--forfeit--> forfeited
paid   --confirm--> paid          （中标确认：仅留痕，保证金继续锁定）
```

- **可重试**：每次迁移携带幂等键（业务事件自动派生，手工操作可传 `Idempotency-Key` 请求头）；
  命中幂等键或账户已达目标状态时直接返回原流水，任意环节失败后可安全重试
- **并发幂等**：状态迁移使用 CAS 条件更新（`UPDATE ... WHERE status=期望值`），并发下只有一方生效；
  每份投标的保证金账户全局唯一（`section_id + bid_document_id` 唯一约束）
- **流水一致**：状态迁移 + 资金流水 + 审计日志在同一数据库事务提交，每次迁移恰好一条流水；
  `GET /api/escrow/{id}/verify` 可重放流水校验账户状态与余额链一致性
- **审计回溯**：流水记录操作人、业务来源（`biz_type`/`biz_id`）、迁移前后状态与幂等键，
  流标 / 定标 / 中标确认等多步操作整体单事务提交
- 非法迁移（如未缴纳退还、已退还没收）返回 **409 冲突**；老库启动时自动补列与唯一索引

## 技术栈

| 层 | 技术 |
|----|------|
| 后端 | Python 3.10+ / FastAPI / SQLAlchemy 2.0 / SQLite |
| 认证 | JWT（HttpOnly Cookie） |
| 前端 | Vue 3 单页应用（hash 路由，vue.global.js 本地运行时，无需构建工具） |
| 测试 | pytest + httpx |

## 快速开始

```bash
pip install -r requirements.txt
python scripts/init_db.py
uvicorn app.main:app --reload
```

访问 http://127.0.0.1:8000 ，登录账号（密码均为 `123456`）：

| 账号 | 角色 | 说明 |
|------|------|------|
| admin | 管理员 | 全部操作权限 |
| operator | 招标经办 | 项目/标段/评标管理 |
| bidder1 / bidder2 / bidder3 | 投标人 | 提交投标、缴纳保证金 |
| judge1 / judge2 / judge3 | 评委 | 评分项打分 |

## 演示数据

初始化脚本预置 2 个项目、3 个标段、3 份已提交的投标文件（标段 BD-2026-001），可直接体验：
发布公告 → 提交投标 → 合规校验 → 缴纳保证金 → 配置评标规则 → 评委打分 → 开标排名 → 公示确认 → 保证金退还。

## 目录结构

```
bid_system/
├── app/
│   ├── main.py            # 应用入口，托管前端静态资源与 API
│   ├── core/              # 配置 / 数据库 / 安全 / 依赖
│   ├── models/            # 17 张数据表
│   ├── schemas/           # Pydantic 校验模型
│   ├── services/          # 业务逻辑（合规校验 / 评标引擎 / 保证金 / 状态机）
│   └── api/               # REST 接口
├── static/
│   ├── index.html         # Vue 单页应用入口
│   ├── css/               # 样式
│   ├── js/                # 路由与视图组件
│   └── vendor/            # Vue 3 本地运行时（离线可用）
├── scripts/init_db.py     # 数据库初始化与演示数据
├── tests/                 # 单元测试
├── data/                  # SQLite 数据库文件
└── requirements.txt
```

## 主要 API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | /api/auth/login | 登录（写 Cookie） |
| GET/POST | /api/projects | 项目列表 / 新建 |
| POST | /api/projects/{id}/sections | 新建标段 |
| GET | /api/sections/{id} | 标段详情（投标/保证金/公示） |
| POST | /api/sections/{id}/transition | 状态流转 |
| POST | /api/sections/{id}/bids | 提交投标（自动合规校验） |
| GET | /api/sections/{id}/evaluation | 评标配置 |
| POST | /api/sections/{id}/evaluation/open | 开标（算分/排名/异常低价澄清/中标候选人） |
| GET | /api/sections/{id}/evaluation/clarifications | 查询异常低价澄清记录 |
| POST | /api/clarifications/{id}/response | 投标人提交异常低价澄清说明 |
| POST | /api/clarifications/{id}/review | 审核澄清：成立恢复有效 / 不成立排除报价 |
| POST | /api/escrow/{id}/return | 退还保证金 |
| GET | /api/escrow/{id}/transactions | 保证金流水（含状态迁移/业务来源/幂等键） |
| GET | /api/escrow/{id}/verify | 重放流水校验账户一致性（审计回溯） |
| GET | /api/dashboard | 统计总览 |
| GET | /api/audit | 审计日志 |

## 测试

```bash
python -m pytest tests/ -v
```

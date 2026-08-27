# harden-enterprise-rag-production-readiness 发布证据

本文件记录本次变更的可复现检查、已验证边界和仍需外部环境证明的项目。所有证据均不包含
数据库密码、模型密钥、访问令牌、租户正文或索引正文。

## 已验证的本地证据

在仓库根目录执行：

```powershell
uv run pytest -q
uv run ruff check backend tests alembic
uv run mypy backend
```

在 `web` 目录执行：

```powershell
npm run check
npm run build
```

实施前基线为已有 SQLite/API 路径可通过，但真实队列确认、租约恢复、代次回滚、OCR 策略、完整 Provider
流式能力和 RLS 运行身份没有可复现证据；本次新增契约在实现前预期失败或无法运行。实施后本地套件为
`pytest 100%`、Ruff/MyPy/TypeScript/ESLint/生产构建通过，Compose 配置解析通过，SQLite 迁移升级/回滚通过。

迁移回滚证据使用临时 SQLite 数据库完成：从当前基线升级到 head，再降回上一版本；临时
数据库和缓存已在检查结束后删除。迁移连接与应用运行连接在部署模板中分离，应用启动时会
执行只读 PostgreSQL RLS 预检。

新增契约夹具覆盖：

- `queued` 上传提交、显式 ACK/NACK、Outbox 租户范围、条件任务租约和过期恢复；
- 任务处理快照、`512/64` 兼容默认值、OCR disabled/best-effort/required 和失败保留；
- 双索引代次元数据、旧代次回切、陈旧 ACL/版本/代次候选和不含正文的对账指纹；
- Provider 首 delta 前后故障、真实 SSE Unicode delta、最终 usage、buffered 交付和流取消；
- RLS 受保护表清单与 ORM 一致性、嵌套 Provider 明文密钥拒绝。

## 运行时验收边界

生产必须使用 PostgreSQL、RabbitMQ、`queued` 入库、非特权非 owner 运行角色和显式生成式
Provider Profile。PostgreSQL Worker 必须配置完整的 `RAG_WORKER_TENANT_IDS`，Outbox 和
租约恢复逐租户设置事务上下文；缺少租户轮询范围时拒绝启动。上传响应只代表源对象、版本、
任务、幂等记录和 Outbox 在同一提交中成功，不代表文档已经可检索。

代次发布顺序为：解析/OCR/切分 → PostgreSQL 分块和双索引暂存 → 数量与 metadata 校验 →
两个投影发布 → 原子切换活动版本与活动代次。任一阶段失败都保留最后有效代次；重建和对账
修复复用普通队列。索引检查只比较租户、空间、文档、内容版本、处理代次、ACL 和 published
等 metadata，不导出正文。

## 尚需真实外部环境证明

以下项目不能由本地 SQLite、内存索引或静态检查替代，发布前必须在不暴露凭证的受控环境补齐：

- PostgreSQL 非特权运行角色的跨租户读写、无上下文 fail-closed、连接池复用和 RLS 门禁；
- RabbitMQ publisher confirm、ACK/NACK、重连、预取、重复投递和多 Worker 重启收敛；
- Compose 延迟健康检查、API 快速返回 queued、Worker 实际消费，以及 Milvus/OpenSearch
  外部索引故障和单路受控降级；
- 扫描型中文 PDF 的原始页面引用、OCR 低置信度/部分失败、外联禁止和成本/配额限制；
- 生产 OIDC、显式远程生成式 Provider、真实首 delta 延迟、断连取消和端到端负载基线。

未完成这些外部证据前，不得把本变更标记为生产发布完成，也不得归档 OpenSpec 变更。

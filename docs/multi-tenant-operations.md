# 多租户启用与运维手册

本手册用于把既有 Enterprise RAG 安装安全地升级为持久化授权的多租户模式。平台角色只由可信 OIDC 身份提供方签发；租户角色只从应用数据库中的成员关系与用户组计算，二者不可互相替代。

## 首次启用

1. 备份 PostgreSQL，并用数据库拥有者/迁移账号在维护窗口执行 `uv run alembic upgrade head`。API 与 worker 必须改用不具备 `SUPERUSER`、`BYPASSRLS` 和表所有者身份的运行账号；Compose 示例为 `enterprise_rag_app`。若继续使用 `POSTGRES_USER` 启动应用，PostgreSQL 会绕过 RLS。
2. 在 Keycloak 中把首位平台运维账号加入 `platform_administrator` realm role。开发验收 realm 已包含 `rag-admin`，生产环境必须替换示例密码和账号。
3. 从已验证的访问令牌读取稳定的 OIDC `sub`，不要使用邮箱或显示名称作为外部主体标识。
4. 先执行只读演练：

   `uv run enterprise-rag-bootstrap --dry-run --administrator tenant-demo=<OIDC_SUB>`

5. 确认报告中的 `missing_administrator_tenant_ids` 为空后，去掉 `--dry-run` 再执行一次。该命令可重复执行，只补齐生命周期、配置版本、用量基线和缺失管理员。
6. 启动 API 与 worker，检查 `/health/ready`。若报告缺少租户管理员、活动配置或生产认证前置条件，不得启用流量。

## 分阶段强制执行

1. 在开发/测试环境验证租户发现与 `X-Tenant-ID` 切换。
2. 确保所有非归档租户至少有一位活动管理员，并核对配置版本 1 和六类用量记录。
3. 在预发布环境启用 OIDC，保持 `RAG_CLAIM_ONLY_TENANT_AUTHORIZATION=false`，运行完整生产烟测。
4. 最后在生产环境启用新版本。生产启动校验会拒绝仅依赖令牌中租户角色的配置。

## 日常操作

- 租户生命周期：平台管理员通过“平台租户控制台”创建、激活、暂停、恢复或归档；每次状态变更都必须填写原因，并使用最新 revision。
- 权限恢复：仅在租户没有可用管理员时使用平台恢复操作。恢复会写入审计日志，不会授予平台账号读取租户内容的权限。
- 配置回滚：回滚会复制历史版本并创建新的活动版本，不会修改历史记录。密钥只保存绑定标识，禁止把原始密钥写入配置。
- 配额核对：租户管理员可调用 `/api/v1/tenant/usage/reconcile`；出现漂移或阈值预警时先核对源数据，再调整配置。
- 暂停语义：暂停后 API 数据面拒绝访问，worker 会拒绝或隔离缺少上下文及过期快照的任务；恢复后必须以新授权纪元和新配置快照重新提交。

## 故障检查

- `Tenant context is unavailable`：确认所选租户、主体状态、成员状态及 OIDC `sub` 是否一致。
- `If-Match revision is required` 或冲突：重新读取租户/成员/配置详情后再提交，禁止盲目覆盖。
- `quota_exceeded`：查看“设置与配额”的已用量和预留量；失败或取消的昂贵操作应释放预留。
- readiness 不就绪：运行 bootstrap 的 `--dry-run`，检查 Keycloak issuer/JWKS/audience、活动配置、数据库迁移和至少一位租户管理员。

## 验收与恢复

先通过标准浏览器登录流程取得短生命周期访问令牌，并仅通过当前进程的
`RAG_SMOKE_ACCESS_TOKEN` 环境变量注入，再执行 `uv run python scripts/production_smoke.py`，
验收租户创建、成员、切换、跨租户拒绝、配置激活、配额、暂停/恢复和引用隔离。公共
Web 客户端禁用密码直授，不再接受用户名和密码参数。若升级失败，先停止新流量，修正
缺失的租户控制面数据，再重新执行可幂等 bootstrap；不要删除租户数据或直接改写历史配置版本。

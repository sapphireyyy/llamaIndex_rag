# enhance-multi-tenant-administration 发布证据

验证日期：2026-08-25（Asia/Shanghai）

## 结论

变更已在 SQLite 测试路径、PostgreSQL 17 非超级运行角色、Keycloak 26.3、MinIO、RabbitMQ、Redis、Qdrant、OpenSearch 和浏览器生产构建路径完成验收。平台角色与租户角色分离，跨租户标识访问不枚举，暂停租户同时阻断 API 和 worker。

## 数据库与引导

- SQLite：全量升级成功；`downgrade -1` 后再次升级成功；`alembic check` 无漂移。
- PostgreSQL 17：全量升级成功；回退当前变更后再次升级成功；`alembic check` 无漂移。
- PostgreSQL RLS：使用无 `SUPERUSER`/`BYPASSRLS` 权限的运行角色验证，租户 A/B 查询各只返回本租户记录；连接复用后上下文正确重置；无租户上下文返回空集。
- Bootstrap：`enterprise-rag-bootstrap --dry-run` 返回 `ready: true`，无缺失管理员租户。
- 迁移账号与运行账号已分离；Compose 初始化脚本会创建非超级运行角色并设置默认表/序列权限。

## 自动化验证

- 后端：共收集 74 项；默认套件 68 项通过、6 项生产/PostgreSQL 条件测试按设计跳过。
- 生产 provider 套件：5 项通过（Keycloak、MinIO、RabbitMQ、Qdrant/OpenSearch、生产容器适配器）。
- PostgreSQL RLS 专项：1 项通过。
- 前端：TypeScript + ESLint 通过；Vitest 2 项租户边界/切换交互测试通过；Vite 生产构建通过。
- 静态质量：Ruff 全项目通过；mypy strict 检查 57 个源码文件通过。
- OpenSpec：`openspec validate enhance-multi-tenant-administration --strict` 通过。
- Docker 完整栈：API、worker、Web、PostgreSQL、Keycloak、MinIO、RabbitMQ、Redis、Qdrant、OpenSearch 共 10 个服务运行；API 健康检查通过，worker 输出 `worker_ready`，Web 首页与 `/health/ready` 反向代理均返回 200。

## 生产式烟测

在真实 Docker 依赖和 OIDC 登录下通过以下完整流程：

1. 匿名租户发现被拒绝；平台管理员创建并激活两个租户。
2. 同一已验证主体在持久化成员关系基础上切换两个租户。
3. 租户 A 分配成员，激活新的不可变配置版本，并触发成员配额 429。
4. 租户 A 上传并摄取文档、激活助手、完成有引用的问答。
5. 租户 B 访问租户 A 文档预览和引用均返回 404。
6. 暂停租户 A 后 API 返回 423，worker 返回 `tenant_suspended_rejected`。
7. 重新激活后租户上下文恢复可用。
8. 遥测在请求结束阶段以显式租户会话写入，验收日志无跨租户或写入失败告警。

Windows 本机的 8000/8010 处于系统保留端口段，因此临时宿主机验收 API 使用 8100，最终 Docker API 使用 8101；这是主机端口策略，不是应用故障。

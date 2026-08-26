# 企业级 RAG 知识助手

这是一个具备多租户隔离能力的企业知识助手。它以明确的领域契约、权限过滤的混合检索、
基于证据的引用回答、版本化配置和可重复评测为核心；LlamaIndex 被隔离在项目自有的适配器之后。

## 快速启动

1. 将 `.env.example` 复制为 `.env`，并按本地环境修改配置。
2. 使用 `uv sync --all-groups` 安装后端依赖。
3. 使用 `uv run alembic upgrade head` 执行数据库迁移。
4. 使用 `uv run uvicorn enterprise_rag.main:app --app-dir backend --reload` 启动 API。
5. 使用 `uv run python -m enterprise_rag.worker` 启动摄取任务工作进程。
6. Windows 环境中，使用 `npm.cmd --prefix web install` 安装前端依赖，再使用
   `npm.cmd --prefix web run dev` 启动前端。

Vite 开发页面默认位于 <http://localhost:5173>；Docker Compose 页面位于
<http://localhost:3000>，并通过 Keycloak Authorization Code + PKCE 登录。前端只接收公开
OIDC 配置，不配置客户端密钥。

默认开发配置使用 SQLite 和进程内适配器，因此运行测试与基础本地体验无需准备外部凭证。
`compose.yaml` 提供了用于集成测试和生产近似验证的企业级依赖基线。

## 验证命令

- 后端测试：`uv run pytest`
- 代码规范：`uv run ruff check .`
- 类型检查：`uv run mypy backend`
- 前端检查：`npm.cmd --prefix web run check`
- 前端测试：`npm.cmd --prefix web test`
- Keycloak 浏览器验收：通过秘密环境变量提供两个测试主体后运行
  `npm.cmd --prefix web run test:e2e:keycloak`
- OpenSpec 校验：`openspec validate build-general-enterprise-rag-assistant --type change --strict`

更多架构决策、运维说明和使用流程见 `docs/`。

远端模型、密钥引用和 DeepSeek 等 OpenAI 兼容服务的配置见
[`docs/operations/model-configuration.md`](docs/operations/model-configuration.md)。

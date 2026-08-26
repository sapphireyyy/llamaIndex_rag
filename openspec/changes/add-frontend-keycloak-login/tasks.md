## 1. 认证依赖与公开配置

- [ ] 1.1 在 Web 工程中加入并锁定 `keycloak-js` 依赖，确认 TypeScript 与 Vite 可以使用其公开类型和 ESM 导出。
- [ ] 1.2 定义前端公开运行时配置模型和启动时校验，覆盖认证模式、Keycloak URL、realm、clientId、回调地址、退出回调、scopes 和续期保护窗口，并保证错误信息不回显敏感值。
- [ ] 1.3 为 Vite 开发模式提供与 Docker 相同的配置接口和显式 development 认证开关，缺失 staging/production OIDC 配置时必须失败关闭。
- [ ] 1.4 增加 Docker 运行时配置模板、非 root 容器启动渲染脚本和 Nginx `no-store` 响应配置，验证同一 Web 镜像可在不重新构建的情况下切换公开 OIDC 地址。

## 2. Keycloak 认证边界

- [ ] 2.1 创建项目自有 `AuthClient` 接口、认证快照和状态枚举，使 React 与 API 层不直接依赖 Keycloak 适配器对象。
- [ ] 2.2 使用 `keycloak-js` 实现 standard flow、PKCE S256、nonce、`check-sso` 和受限浏览器回退，并保证回调处理发生在受保护应用初始化之前。
- [ ] 2.3 实现显式登录、相对应用路径恢复、RP-Initiated Logout 和认证错误归一化，拒绝外部返回地址和不匹配的回调状态。
- [ ] 2.4 实现内存 Token 管理和 single-flight `updateToken`，禁止把访问令牌、刷新令牌、ID Token 或授权码写入 Web Storage、URL 日志或遥测。
- [ ] 2.5 实现认证会话版本和在途请求取消注册表，使退出、续期失败或 subject 变化后旧响应不能写回页面。

## 3. React 启动门禁与登录体验

- [ ] 3.1 增加 `AuthProvider` 和 `AuthGate`，实现 initializing、unauthenticated、authenticated、recoverable_error 互斥状态，并确保未认证时只允许公开健康检查。
- [ ] 3.2 增加中文登录、认证中、会话过期、Keycloak 不可用、配置错误和重试界面，提供键盘焦点、辅助技术标签与稳定状态反馈。
- [ ] 3.3 在应用顶栏增加当前登录主体摘要和退出入口，退出时立即卸载受保护视图并清除通知、问答会话和页面缓存。
- [ ] 3.4 将租户发现移动到认证成功之后，并将记忆租户键绑定到当前 subject；恢复前重新验证租户仍可发现且可用。
- [ ] 3.5 提供统一 `resetTenantBoundary`，在退出、认证失败和 subject 变化时清除当前租户、租户上下文、业务缓存、请求控制器和主体相关本地状态。

## 4. 统一认证请求层

- [ ] 4.1 将现有请求函数重构为显式 `publicFetch` 和 `authenticatedFetch`，后者统一添加 Bearer Token、关联 ID 与适用的租户边界头。
- [ ] 4.2 将所有 JSON、FormData、管理、摄取、质量和反馈接口迁移到认证传输层，并验证公开调用方无法通过参数绕过认证。
- [ ] 4.3 将流式问答迁移到同一 Token 续期、租户边界、会话版本和取消机制，验证整个 SSE 读取周期绑定原认证主体。
- [ ] 4.4 将受保护预览和下载改为认证 Blob 请求与短生命周期 object URL，并在完成、错误或取消后撤销 URL。
- [ ] 4.5 实现请求前续期、401 强制续期后最多一次重试、续期失败退出和 403 保持会话的集中错误策略，防止重试循环。

## 5. Keycloak 与部署联动

- [ ] 5.1 更新本地 Keycloak 公共客户端：保持 standard flow，启用 PKCE S256，禁用 Direct Access Grants，并保留 `enterprise-rag` audience mapper。
- [ ] 5.2 为 Docker `http://localhost:3000`、实际 Vite 开发地址及其退出回调精确配置 Valid Redirect URIs、Post Logout Redirect URIs 和 Web Origins，避免宽泛跨域通配。
- [ ] 5.3 在 Compose 和部署示例中配置 Web 公开 OIDC 环境变量，保持 API issuer 与浏览器签发者一致、JWKS 使用容器内部地址，且不向前端注入任何客户端密钥或测试密码。
- [ ] 5.4 增加 silent check SSO 静态回调资源及其部署路由，并验证第三方 Cookie 受限时可降级到常规会话检查或显式登录。

## 6. 自动化测试

- [ ] 6.1 为 `AuthClient` 增加单元测试，覆盖初始化、已有 SSO、登录、有效/无效回调、single-flight 续期、续期失败、退出和 Token 不落盘。
- [ ] 6.2 为认证传输层增加测试，覆盖 Bearer 与租户头、并发续期、401 单次重试、第二次 401 退出、403 保持会话和旧会话响应丢弃。
- [ ] 6.3 为上传、流式问答和受保护 Blob 下载增加测试，验证三种通道都携带 Token、支持取消且不泄漏 object URL。
- [ ] 6.4 为 React 认证门禁增加组件测试，验证未认证不加载业务数据、认证中不渲染旧内容、中文错误状态、键盘可访问性和退出清理。
- [ ] 6.5 增加主体切换与租户恢复测试，验证不同 subject 不复用租户选择、查询历史或业务缓存，失效成员关系不会向旧租户发请求。
- [ ] 6.6 增加真实 Keycloak 浏览器验收测试，覆盖登录回调、刷新恢复、租户发现、普通 API、流式问答、Token 到期、退出和第二主体登录，并确保报告不保存凭据。

## 7. 安全、质量与回归检查

- [ ] 7.1 增加静态或测试断言，阻止 Token、授权码和敏感声明写入 `localStorage`、日志、错误提示、遥测或测试快照。
- [ ] 7.2 收紧 Web CSP 和认证相关资源来源，确认没有新增不受控 HTML 注入路径，且认证适配器生产调试日志关闭。
- [ ] 7.3 运行 Web TypeScript、ESLint、Vitest 和生产构建，修复认证改动引入的类型、Hook 生命周期、可访问性和打包问题。
- [ ] 7.4 运行受影响后端认证、租户隔离与 API 合同测试，确认前端联动没有要求后端回退到 Token 角色授权或开发身份。
- [ ] 7.5 运行 OpenSpec 主规格与 `add-frontend-keycloak-login` 严格校验，确认实现与 10 项要求、25 个场景保持一致。

## 8. Docker 验收与文档

- [ ] 8.1 按部署顺序先更新 Keycloak 客户端，再重新构建并启动 Web；验证 API、Keycloak、Web 健康且浏览器不再以匿名请求触发页面 401。
- [ ] 8.2 使用真实 Keycloak 测试主体完成登录、租户发现、租户切换、知识访问、流式问答、预览下载、续期和退出的端到端验收。
- [ ] 8.3 验证无 Token 返回 401、有效 Token 可访问、无权限返回 403、成员撤销立即生效，并确认退出或主体切换后旧租户数据不可见。
- [ ] 8.4 验证 Keycloak 暂时不可用、回调地址错误、Token 过期和第三方 Cookie 受限时的失败关闭与恢复行为。
- [ ] 8.5 更新根级页面操作指南和运维 runbook，记录登录/退出步骤、公开配置、开发认证边界、常见 401/403/回调错误及安全注意事项。
- [ ] 8.6 记录验收证据、已知浏览器限制和回滚结果；确认未在日志、截图、报告或仓库中留下测试密码、Token 或其他凭据。

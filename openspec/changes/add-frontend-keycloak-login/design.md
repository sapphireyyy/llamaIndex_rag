## Context

参见 [proposal.md](./proposal.md) 的 Why。当前 React/Vite 前端在模块加载后直接调用租户发现接口，统一请求函数和流式问答函数都没有认证上下文；租户选择仅以一个全局键写入 `localStorage`。Docker API 运行于 staging OIDC 模式，而本地 Keycloak 客户端仍只登记旧的前端 Origin，因此页面没有完成登录、回调、令牌传播和租户边界联动的路径。

后端已经验证 Bearer Token 的 issuer、audience、签名和有效期，并从持久化主体、成员关系与角色解析租户权限。本变更应复用该边界，不把租户角色授权复制到浏览器。需求合同见 [frontend-oidc-authentication spec](./specs/frontend-oidc-authentication/spec.md)。

Keycloak 官方 JavaScript 适配器支持公共 SPA 客户端、Authorization Code Flow、PKCE、`check-sso`、内存令牌、`updateToken()` 和 RP-Initiated Logout，因此可以覆盖本变更而无需自建 OIDC 协议实现。

## Goals / Non-Goals

**Goals:**

- 在 React 渲染受保护内容前建立明确的认证状态机，并支持显式登录、会话恢复和退出。
- 为普通请求、上传、受保护文件和流式问答提供同一个令牌获取、续期、401 恢复和取消边界。
- 保持 Token 仅驻留内存；把租户选择、缓存和请求生命周期绑定到当前 OIDC subject。
- 允许同一 Web 镜像通过非敏感运行时配置部署到本地、staging 和 production。
- 提供可自动化验证的单元、组件和真实 Keycloak 浏览器验收路径。

**Non-Goals:**

- 不改变后端 Token 校验、租户成员关系、权限矩阵或数据库模型。
- 不在前端根据 Token 角色声明直接授予租户权限；页面权限仍以后端返回的平台角色和租户上下文为准。
- 不实现自定义 Keycloak 登录主题、用户注册、找回密码或身份生命周期管理。
- 不引入 BFF、服务端会话 Cookie 或刷新令牌数据库；若未来安全基线要求 BFF，应作为独立变更。
- 不支持 implicit flow、hybrid flow、资源所有者密码模式或浏览器客户端密钥。

## Decisions

### 1. 使用官方 `keycloak-js` 适配器

前端新增一个项目自有的 `AuthClient` 边界，内部封装 `keycloak-js`，React 和 API 模块只依赖项目接口，不直接散布适配器调用。适配器使用 standard flow、PKCE S256、nonce 校验和 `check-sso` 初始化；未认证时显示应用自己的登录入口，而不是应用启动即强制跳转。

选择官方适配器是因为它与当前 Keycloak 部署一致，负责协议参数、回调解析、令牌续期和登出 URL，降低自建协议实现的安全风险。通用 OIDC 客户端可降低供应商耦合，但本变更明确以 Keycloak 联动为目标，且当前部署、测试 realm 和运维手册都以 Keycloak 为验收基线。手写 OAuth/OIDC 流程不予采用。

`AuthClient` 至少暴露：初始化、读取不可变认证快照、订阅状态、登录、退出、取得经过续期的访问令牌和清除会话。访问令牌、刷新令牌与 ID Token 只保存在适配器内存中，不写入 Web Storage。

### 2. 认证状态机先于受保护应用数据

应用根节点使用 `AuthProvider` 和 `AuthGate` 管理以下互斥状态：`initializing`、`unauthenticated`、`authenticated`、`recoverable_error`。健康检查可在未认证状态执行；租户发现及所有业务视图只有在 `authenticated` 后挂载。

OIDC 回调必须在任何客户端路由或业务请求之前处理。登录前位置以仅含应用内相对路径的临时状态保存，回调成功后恢复；外部 URL、协议相对 URL 和不受信任回调参数不得用于跳转，以避免开放重定向。

相比在现有 `App` 的请求失败处理中临时捕获 401，根级状态机可以防止页面先发出匿名请求、先渲染旧主体数据再跳登录，以及多个视图各自实现不同认证行为。

### 3. 统一认证传输层覆盖所有请求类型

`api.ts` 保留领域 API 外观，但底层改为一个 `authenticatedFetch`。该传输层从 `AuthClient` 获取至少仍有效指定秒数的 Token，附加 Bearer 头和现有租户头，并生成关联 ID。普通 JSON、FormData、流式问答、预览和下载都必须经过同一边界。

文件下载不得继续依赖无法附加 Header 的直接导航。前端通过认证请求取得 Blob，创建短生命周期 object URL 触发下载，并在完成或取消后撤销 URL。流式问答在建立连接前完成续期，并将读取循环绑定到当前认证会话版本。

公开端点使用单独的 `publicFetch`，初始仅允许健康检查和公开运行时配置；调用方不能通过任意布尔参数绕过认证。

### 4. 续期使用 single-flight，401 只恢复一次

每个受保护请求发送前调用 `updateToken(minValidity)`。认证模块维护单个进行中的续期 Promise；并发请求共享结果，避免刷新令牌竞争和重复请求。

如果 API 返回 401，传输层强制执行一次续期并仅重试原请求一次。重试仍为 401、续期失败或 Keycloak 会话失效时，认证模块清除 Token、增加会话版本、取消当前主体的在途请求并进入 `unauthenticated` 或可恢复错误状态。403 作为授权失败直接交给页面显示，不刷新 Token、不退出登录。

每个请求捕获发起时的会话版本；响应到达时若版本已改变，结果被丢弃。这一机制同时防止退出或切换主体后旧响应写回新页面。

### 5. 租户和页面状态按 OIDC subject 隔离

认证成功后先读取适配器确认的 `subject`，再执行租户发现。租户选择允许持久化，但键名包含不可逆的主体派生标识，且恢复前必须再次出现在最新租户发现结果中。退出、认证失败或 subject 变化时调用统一 `resetTenantBoundary()`，清除当前租户、租户上下文、业务缓存、会话内问答记录和请求控制器。

Token 声明中的 `tenant_id` 或角色仅用于认证诊断和后端校验，不直接驱动浏览器授权。平台角色、租户角色和租户状态继续以后端租户发现与上下文接口为准。

### 6. 使用非敏感运行时配置复用同一 Web 镜像

前端定义经运行时校验的公开配置：认证模式、Keycloak 公共 URL、realm、clientId、redirect URI、post-logout redirect URI、scopes、续期保护窗口。配置不包含客户端密钥、测试密码或 Token。

Vite 开发服务器从 `VITE_*` 值构造同一配置接口。Docker Web 容器启动时将环境变量渲染到 `/tmp` 下的只读响应文件，由 Nginx 以 `Cache-Control: no-store` 提供；页面在应用入口前加载该文件。这样无需为环境地址重新构建镜像，也避免在不可写的 Nginx静态目录中生成文件。

配置校验失败时只渲染认证配置错误页，不初始化 Keycloak、不调用受保护 API。生产构建不提供 development 认证的隐式默认值。

### 7. Keycloak 客户端保持公共客户端并收紧浏览器授权面

本地 realm 中的 `enterprise-rag` 客户端保持 public client 和 standard flow，禁用 Direct Access Grants，并显式启用 PKCE S256。Valid Redirect URIs、Post Logout Redirect URIs 和 Web Origins 使用实际公开前端 Origin；Docker 的 `http://localhost:3000` 与开发服务器地址分别精确登记，不使用宽泛的任意域通配。

访问令牌 audience 保持 `enterprise-rag`，issuer 保持浏览器和 API 都可验证的一致公开值；API 容器可继续使用内部 JWKS 地址取密钥。Keycloak 配置必须先于启用新 Web 登录流程部署，否则回调会因 Origin 不匹配失败。

### 8. 会话恢复对第三方 Cookie 限制采用降级策略

优先使用 `check-sso` 和 silent check SSO 页面减少刷新跳转；保留适配器对受限浏览器的常规 `check-sso` 回退。不得把 silent iframe 视为唯一正确路径，因为现代浏览器可能阻止第三方 Cookie。

若 silent check 不受支持，应用允许一次顶层会话检查或回到显式登录入口，不通过持久化 Token 绕过限制。跨标签页的登出最终由 Keycloak 会话检查或下一次 Token 续期收敛；安全边界不依赖即时 iframe 通知。

### 9. 错误模型与可观测性不记录凭据

认证错误归一为：配置错误、登录取消/失败、回调校验失败、会话过期、身份提供方不可用、API 未认证和 API 无权限。页面显示中文动作建议；仅展示后端返回的稳定类别和关联 ID。

日志与遥测记录认证阶段、结果、耗时和无敏感值的错误类别，不记录授权码、Token、完整声明、登录 URL 查询串或个人资料。生产默认关闭适配器调试日志。

### 10. 测试分层覆盖协议边界和真实浏览器路径

单元测试使用伪 `AuthClient` 验证状态机、single-flight、401 单次重试、403 保持会话、会话版本丢弃旧响应和 Blob URL 清理。React 组件测试验证未认证时不请求业务 API、认证中不泄露旧内容、退出清理和主体隔离的租户恢复。

生产近似验收增加真实 Keycloak 浏览器测试，覆盖登录、回调、租户发现、普通请求、流式请求、刷新页面恢复、Token 到期、退出和第二主体登录。测试不得把测试用户密码、Token 或回调参数写入报告；凭据从验收环境秘密注入。

## Risks / Trade-offs

- [XSS 仍可能读取内存 Token] → 保持 Token 不落盘，收紧 CSP，避免不受控 HTML 注入，固定并审计认证依赖，确保日志与错误边界不复制 Token。
- [第三方 Cookie 策略限制 silent check SSO 和会话 iframe] → 允许顶层 `check-sso` 或显式登录降级，并以短生命周期 Token 加下一次续期检测远端登出。
- [并发请求在续期、退出时发生竞态] → 使用 single-flight 续期、会话版本和集中 AbortController 注册表。
- [运行时配置与 Keycloak 客户端不一致造成重定向循环] → 启动时验证配置，精确登记 Origin，在进入循环前显示稳定配置错误，并在部署验收中校验回调地址。
- [把 401 与 403 混淆导致反复登录或错误退出] → 传输层只对 401 执行一次认证恢复，403 保持会话并由权限页面处理。
- [Keycloak 专用适配器增加供应商耦合] → 用项目自有 `AuthClient` 隔离适配器；未来更换 IdP 时保持 React 与 API 层接口不变。
- [真实浏览器验收增加执行时间和环境依赖] → 单元/组件测试覆盖大部分分支，真实 Keycloak 测试仅覆盖关键集成路径并复用现有 Compose 验收环境。

## Migration Plan

1. 先更新 Keycloak 客户端的标准流程、PKCE、精确 Redirect URI、Post Logout URI 和 Web Origin；保留现有 audience mapper。
2. 发布支持公开运行时认证配置的 Web 镜像，但在 development/test 可显式保持开发认证，验证回滚路径。
3. 在 staging 使用真实 Keycloak 测试账号执行浏览器验收，确认普通 API、流式问答、文件请求、续期、退出和跨主体租户清理。
4. 验收通过后启用 staging/production OIDC 模式；监控 401、回调失败、续期失败和登录耗时，不记录凭据。
5. 回滚时恢复上一版 Web 镜像和运行时配置。Keycloak 中新增的精确回调地址可暂时保留；若移除，先确认没有客户端仍使用。此变更无数据库迁移，后端 Bearer 校验保持兼容。

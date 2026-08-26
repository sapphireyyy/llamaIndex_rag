## Why

当前生产近似 Docker 环境启用了 Keycloak OIDC，但前端没有登录、回调处理或访问令牌注入能力，导致页面加载后所有受保护 API 请求稳定返回 401。需要补齐浏览器端认证边界，使用户能够通过 Keycloak 登录并在保持租户隔离的前提下使用现有页面。

## What Changes

- 增加前端 Keycloak/OIDC Authorization Code + PKCE 登录、回调恢复和退出登录行为。
- 在认证状态明确后再加载租户与业务数据，并为所有受保护 HTTP、上传下载和流式问答请求统一附加 Bearer 访问令牌。
- 增加令牌到期、静默续期失败、API 401、无权限 403、回调校验失败等可恢复且不泄密的页面状态。
- 登录主体变化或退出登录时清除租户边界和主体相关缓存，防止浏览器会话中的跨主体数据残留。
- 约束 Keycloak 公共客户端、重定向地址、Web Origin、issuer、audience 和前端公开配置；前端不得持有客户端密钥。
- 保留仅限 development/test 的开发身份模式，生产近似环境必须走 OIDC。

## Capabilities

### New Capabilities

- `frontend-oidc-authentication`: 定义前端与 Keycloak 的登录、会话、令牌传播、退出、错误恢复和租户边界联动行为。

### Modified Capabilities

无。

## Impact

后续实现将影响 React 应用启动流程、统一 API 客户端、流式问答与受保护资源请求、前端路由和认证状态展示，以及 Keycloak 本地 realm/Compose 的公开客户端重定向配置。后端现有 Bearer Token 校验和持久化租户授权模型保持不变。

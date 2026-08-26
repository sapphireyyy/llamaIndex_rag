import { useEffect, useState, type ReactNode } from "react";
import { useAuth } from "./auth-context";
import { publicJson } from "./transport";
import type { HealthResponse } from "./api";

const failureMessages = {
  configuration_error: ["认证配置错误", "请检查公开 OIDC 配置后重新部署或刷新页面。"],
  login_failed: ["登录未完成", "登录已取消或请求无效，请重新发起登录。"],
  callback_invalid: ["登录回调校验失败", "本次回调未通过安全校验，请重新登录。"],
  session_expired: ["会话已过期", "登录会话已经失效，请重新登录。"],
  provider_unavailable: ["身份服务暂不可用", "暂时无法连接 Keycloak，请稍后重试。"],
  api_unauthenticated: ["认证已失效", "服务拒绝了当前会话，请重新登录。"],
} as const;

function PublicHealth() {
  const [message, setMessage] = useState("正在检查服务状态…");
  useEffect(() => {
    void publicJson<HealthResponse>("/health/ready")
      .then((health) => setMessage(health.ready ? `API ${health.version} 已就绪` : "API 当前处于受限状态"))
      .catch(() => setMessage("暂时无法连接 API"));
  }, []);
  return <p className="auth-health" role="status">{message}</p>;
}

export default function AuthGate({ children }: { children: ReactNode }) {
  const { snapshot, login, retry } = useAuth();
  if (snapshot.status === "authenticated") return <>{children}</>;
  if (snapshot.status === "initializing") {
    return <main className="auth-page" aria-busy="true"><section className="auth-card"><span className="auth-kicker">Enterprise RAG</span><h1>正在恢复登录会话</h1><p role="status">正在安全检查 Keycloak 会话，请稍候。</p><PublicHealth /></section></main>;
  }
  if (snapshot.status === "recoverable_error") {
    const [title, description] = failureMessages[snapshot.failure?.code ?? "provider_unavailable"];
    const configurationError = snapshot.failure?.code === "configuration_error";
    return <main className="auth-page"><section className="auth-card" role="alert"><span className="auth-kicker">认证受限</span><h1>{title}</h1><p>{description}</p><div className="auth-actions">{!configurationError && <button className="primary" autoFocus onClick={() => void login()} aria-label="重新登录">重新登录</button>}<button onClick={retry} aria-label="重新检查认证状态">重新检查</button></div><PublicHealth /></section></main>;
  }
  return <main className="auth-page"><section className="auth-card"><span className="auth-kicker">可信企业知识助手</span><h1>使用企业账号登录</h1><p>登录后只会加载当前账号有权访问的租户和知识。</p><button className="primary auth-login" autoFocus onClick={() => void login()} aria-label="使用 Keycloak 登录">使用 Keycloak 登录</button><PublicHealth /></section></main>;
}

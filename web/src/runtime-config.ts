export type AppEnvironment = "development" | "test" | "staging" | "production";
export type AuthMode = "development" | "oidc";

export type PublicRuntimeConfig = Readonly<{
  environment: AppEnvironment;
  authMode: AuthMode;
  oidcUrl: string;
  realm: string;
  clientId: string;
  redirectUri: string;
  postLogoutRedirectUri: string;
  silentCheckSsoRedirectUri: string;
  scopes: readonly string[];
  minTokenValiditySeconds: number;
}>;

export type PublicRuntimeConfigInput = Partial<{
  environment: string;
  authMode: string;
  oidcUrl: string;
  realm: string;
  clientId: string;
  redirectUri: string;
  postLogoutRedirectUri: string;
  silentCheckSsoRedirectUri: string;
  scopes: string | string[];
  minTokenValiditySeconds: string | number;
}>;

export class RuntimeConfigError extends Error {
  readonly code = "configuration_error";

  constructor(readonly fields: readonly string[]) {
    super(`认证公开配置无效：${fields.join("、")}`);
    this.name = "RuntimeConfigError";
  }
}

function nonEmpty(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function parseScopes(value: unknown): string[] {
  const values = Array.isArray(value) ? value : nonEmpty(value).split(/[\s,]+/);
  return [...new Set(values.map((item) => nonEmpty(item)).filter(Boolean))];
}

function isAbsoluteHttpUrl(value: string): boolean {
  try {
    const parsed = new URL(value);
    return parsed.protocol === "https:" || parsed.protocol === "http:";
  } catch {
    return false;
  }
}

function readViteConfig(): PublicRuntimeConfigInput {
  const env = import.meta.env;
  return {
    environment: env.VITE_APP_ENVIRONMENT,
    authMode: env.VITE_AUTH_MODE,
    oidcUrl: env.VITE_OIDC_URL,
    realm: env.VITE_OIDC_REALM,
    clientId: env.VITE_OIDC_CLIENT_ID,
    redirectUri: env.VITE_OIDC_REDIRECT_URI,
    postLogoutRedirectUri: env.VITE_OIDC_POST_LOGOUT_REDIRECT_URI,
    silentCheckSsoRedirectUri: env.VITE_OIDC_SILENT_CHECK_SSO_REDIRECT_URI,
    scopes: env.VITE_OIDC_SCOPES,
    minTokenValiditySeconds: env.VITE_OIDC_MIN_VALIDITY_SECONDS,
  };
}

export function loadPublicRuntimeConfig(
  source: PublicRuntimeConfigInput = {
    ...readViteConfig(),
    ...(window.__ENTERPRISE_RAG_CONFIG__ ?? {}),
  },
): PublicRuntimeConfig {
  const invalid: string[] = [];
  const environment = nonEmpty(source.environment) as AppEnvironment;
  const authMode = nonEmpty(source.authMode) as AuthMode;
  const allowedEnvironments: AppEnvironment[] = ["development", "test", "staging", "production"];

  if (!allowedEnvironments.includes(environment)) invalid.push("environment");
  if (!(["development", "oidc"] as string[]).includes(authMode)) invalid.push("authMode");
  if (authMode === "development" && !(["development", "test"] as string[]).includes(environment)) {
    invalid.push("developmentAuthBoundary");
  }

  const oidcUrl = nonEmpty(source.oidcUrl).replace(/\/$/, "");
  const realm = nonEmpty(source.realm);
  const clientId = nonEmpty(source.clientId);
  const redirectUri = nonEmpty(source.redirectUri);
  const postLogoutRedirectUri = nonEmpty(source.postLogoutRedirectUri);
  const silentCheckSsoRedirectUri = nonEmpty(source.silentCheckSsoRedirectUri);
  const scopes = parseScopes(source.scopes);
  const minTokenValiditySeconds = Number(source.minTokenValiditySeconds ?? 30);

  if (authMode === "oidc") {
    if (!isAbsoluteHttpUrl(oidcUrl)) invalid.push("oidcUrl");
    if (!realm) invalid.push("realm");
    if (!clientId) invalid.push("clientId");
    if (!isAbsoluteHttpUrl(redirectUri)) invalid.push("redirectUri");
    if (!isAbsoluteHttpUrl(postLogoutRedirectUri)) invalid.push("postLogoutRedirectUri");
    if (!isAbsoluteHttpUrl(silentCheckSsoRedirectUri)) invalid.push("silentCheckSsoRedirectUri");
    if (scopes.length === 0) invalid.push("scopes");
  }
  if (!Number.isInteger(minTokenValiditySeconds) || minTokenValiditySeconds < 5 || minTokenValiditySeconds > 300) {
    invalid.push("minTokenValiditySeconds");
  }

  if (invalid.length > 0) throw new RuntimeConfigError([...new Set(invalid)]);
  return Object.freeze({
    environment,
    authMode,
    oidcUrl,
    realm,
    clientId,
    redirectUri,
    postLogoutRedirectUri,
    silentCheckSsoRedirectUri,
    scopes: Object.freeze(scopes),
    minTokenValiditySeconds,
  });
}

import Keycloak, { type KeycloakTokenParsed } from "keycloak-js";
import type { AuthMode, PublicRuntimeConfig } from "./runtime-config";

export type AuthStatus = "initializing" | "unauthenticated" | "authenticated" | "recoverable_error";
export type AuthFailureCode =
  | "configuration_error"
  | "login_failed"
  | "callback_invalid"
  | "session_expired"
  | "provider_unavailable"
  | "api_unauthenticated";

export type AuthFailure = Readonly<{ code: AuthFailureCode; correlationId?: string }>;
export type AuthSnapshot = Readonly<{
  status: AuthStatus;
  mode: AuthMode;
  subject: string | null;
  displayName: string | null;
  sessionVersion: number;
  failure: AuthFailure | null;
}>;

export interface AuthClient {
  initialize(): Promise<AuthSnapshot>;
  getSnapshot(): AuthSnapshot;
  subscribe(listener: (snapshot: AuthSnapshot) => void): () => void;
  login(returnPath?: string): Promise<void>;
  logout(): Promise<void>;
  getAccessToken(options?: { forceRefresh?: boolean }): Promise<string | null>;
  invalidate(failure: AuthFailure): void;
  registerRequest(controller: AbortController): () => void;
  cancelRequests(): void;
}

export type KeycloakAdapter = Pick<
  Keycloak,
  | "authenticated"
  | "subject"
  | "token"
  | "tokenParsed"
  | "init"
  | "login"
  | "logout"
  | "updateToken"
  | "clearToken"
> & {
  onAuthLogout?: () => void;
  onAuthRefreshError?: () => void;
  onTokenExpired?: () => void;
};

const RETURN_PATH_KEY = "enterprise-rag.auth.return-path";

function frozenSnapshot(values: AuthSnapshot): AuthSnapshot {
  return Object.freeze({ ...values, failure: values.failure ? Object.freeze({ ...values.failure }) : null });
}

export function safeApplicationPath(candidate: string): string | null {
  if (!candidate.startsWith("/") || candidate.startsWith("//")) return null;
  try {
    const parsed = new URL(candidate, window.location.origin);
    return parsed.origin === window.location.origin
      ? `${parsed.pathname}${parsed.search}${parsed.hash}`
      : null;
  } catch {
    return null;
  }
}

function hasOidcCallback(): boolean {
  const parameters = `${window.location.search}&${window.location.hash}`;
  return /(?:^|[&#?])(code|state|error|session_state)=/.test(parameters);
}

function audienceContains(parsed: KeycloakTokenParsed, clientId: string): boolean {
  const audience = parsed.aud;
  return audience === clientId || (Array.isArray(audience) && audience.includes(clientId));
}

function validateClaims(parsed: KeycloakTokenParsed | undefined, config: PublicRuntimeConfig): boolean {
  if (!parsed || !parsed.sub || typeof parsed.iat !== "number" || typeof parsed.exp !== "number") return false;
  const expectedIssuer = `${config.oidcUrl}/realms/${encodeURIComponent(config.realm)}`;
  const now = Math.floor(Date.now() / 1000);
  return parsed.iss === expectedIssuer && audienceContains(parsed, config.clientId) && parsed.iat <= now + 60 && parsed.exp > now;
}

export class KeycloakAuthClient implements AuthClient {
  private snapshot: AuthSnapshot;
  private readonly listeners = new Set<(snapshot: AuthSnapshot) => void>();
  private readonly requests = new Set<AbortController>();
  private initializePromise: Promise<AuthSnapshot> | null = null;
  private refreshPromise: Promise<string> | null = null;

  constructor(
    private readonly config: PublicRuntimeConfig,
    private readonly adapter: KeycloakAdapter = new Keycloak({
      url: config.oidcUrl,
      realm: config.realm,
      clientId: config.clientId,
    }),
  ) {
    this.snapshot = frozenSnapshot({
      status: "initializing",
      mode: "oidc",
      subject: null,
      displayName: null,
      sessionVersion: 0,
      failure: null,
    });
    this.adapter.onAuthLogout = () => this.invalidate({ code: "session_expired" });
    this.adapter.onAuthRefreshError = () => this.invalidate({ code: "session_expired" });
    this.adapter.onTokenExpired = () => { void this.getAccessToken().catch(() => undefined); };
  }

  getSnapshot(): AuthSnapshot {
    return this.snapshot;
  }

  subscribe(listener: (snapshot: AuthSnapshot) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private publish(next: Omit<AuthSnapshot, "mode">): AuthSnapshot {
    this.snapshot = frozenSnapshot({ ...next, mode: "oidc" });
    for (const listener of this.listeners) listener(this.snapshot);
    return this.snapshot;
  }

  private authenticatedSnapshot(): AuthSnapshot {
    const parsed = this.adapter.tokenParsed;
    if (!this.adapter.authenticated || !this.adapter.token || !validateClaims(parsed, this.config)) {
      this.adapter.clearToken();
      return this.publish({
        status: "recoverable_error",
        subject: null,
        displayName: null,
        sessionVersion: this.snapshot.sessionVersion + 1,
        failure: { code: "callback_invalid" },
      });
    }
    const subject = String(parsed?.sub);
    const displayName = typeof parsed?.name === "string"
      ? parsed.name
      : typeof parsed?.preferred_username === "string" ? parsed.preferred_username : subject;
    return this.publish({
      status: "authenticated",
      subject,
      displayName,
      sessionVersion: this.snapshot.sessionVersion + (this.snapshot.subject && this.snapshot.subject !== subject ? 1 : 0),
      failure: null,
    });
  }

  initialize(): Promise<AuthSnapshot> {
    if (this.initializePromise) return this.initializePromise;
    this.publish({ ...this.snapshot, status: "initializing", failure: null });
    this.initializePromise = this.adapter.init({
      onLoad: "check-sso",
      flow: "standard",
      pkceMethod: "S256",
      useNonce: true,
      responseMode: "fragment",
      redirectUri: this.config.redirectUri,
      silentCheckSsoRedirectUri: this.config.silentCheckSsoRedirectUri,
      silentCheckSsoFallback: true,
      checkLoginIframe: true,
      scope: this.config.scopes.join(" "),
      enableLogging: false,
    }).then((authenticated) => {
      if (!authenticated) {
        sessionStorage.removeItem(RETURN_PATH_KEY);
        return this.publish({
          status: "unauthenticated",
          subject: null,
          displayName: null,
          sessionVersion: this.snapshot.sessionVersion,
          failure: null,
        });
      }
      const next = this.authenticatedSnapshot();
      if (next.status === "authenticated") {
        const returnPath = safeApplicationPath(sessionStorage.getItem(RETURN_PATH_KEY) ?? "");
        sessionStorage.removeItem(RETURN_PATH_KEY);
        if (returnPath && returnPath !== `${location.pathname}${location.search}${location.hash}`) {
          history.replaceState(null, "", returnPath);
        }
      }
      return next;
    }).catch(() => {
      const failure: AuthFailure = { code: hasOidcCallback() ? "callback_invalid" : "provider_unavailable" };
      return this.publish({
        status: "recoverable_error",
        subject: null,
        displayName: null,
        sessionVersion: this.snapshot.sessionVersion + 1,
        failure,
      });
    }).finally(() => {
      this.initializePromise = null;
    });
    return this.initializePromise;
  }

  async login(returnPath = `${location.pathname}${location.search}${location.hash}`): Promise<void> {
    const safePath = safeApplicationPath(returnPath);
    if (!safePath) {
      this.invalidate({ code: "login_failed" });
      return;
    }
    sessionStorage.setItem(RETURN_PATH_KEY, safePath);
    try {
      await this.adapter.login({ redirectUri: this.config.redirectUri, scope: this.config.scopes.join(" ") });
    } catch {
      sessionStorage.removeItem(RETURN_PATH_KEY);
      this.invalidate({ code: "provider_unavailable" });
    }
  }

  async logout(): Promise<void> {
    this.cancelRequests();
    this.publish({
      status: "unauthenticated",
      subject: null,
      displayName: null,
      sessionVersion: this.snapshot.sessionVersion + 1,
      failure: null,
    });
    try {
      await this.adapter.logout({ redirectUri: this.config.postLogoutRedirectUri, logoutMethod: "GET" });
    } catch {
      this.adapter.clearToken();
      this.publish({ ...this.snapshot, status: "recoverable_error", failure: { code: "provider_unavailable" } });
    }
  }

  getAccessToken(options: { forceRefresh?: boolean } = {}): Promise<string | null> {
    if (this.snapshot.status !== "authenticated") return Promise.reject(new Error("Authentication required"));
    if (this.refreshPromise) return this.refreshPromise;
    const minValidity = options.forceRefresh ? -1 : this.config.minTokenValiditySeconds;
    this.refreshPromise = this.adapter.updateToken(minValidity).then(() => {
      const next = this.authenticatedSnapshot();
      if (next.status !== "authenticated" || !this.adapter.token) throw new Error("Session expired");
      return this.adapter.token;
    }).catch((error) => {
      this.invalidate({ code: "session_expired" });
      throw error;
    }).finally(() => {
      this.refreshPromise = null;
    });
    return this.refreshPromise;
  }

  invalidate(failure: AuthFailure): void {
    this.cancelRequests();
    this.adapter.clearToken();
    this.publish({
      status: "recoverable_error",
      subject: null,
      displayName: null,
      sessionVersion: this.snapshot.sessionVersion + 1,
      failure,
    });
  }

  registerRequest(controller: AbortController): () => void {
    this.requests.add(controller);
    return () => this.requests.delete(controller);
  }

  cancelRequests(): void {
    for (const controller of this.requests) controller.abort();
    this.requests.clear();
  }
}

export class DevelopmentAuthClient implements AuthClient {
  private readonly listeners = new Set<(snapshot: AuthSnapshot) => void>();
  private readonly requests = new Set<AbortController>();
  private snapshot = frozenSnapshot({
    status: "initializing",
    mode: "development",
    subject: null,
    displayName: null,
    sessionVersion: 0,
    failure: null,
  } as AuthSnapshot);

  async initialize(): Promise<AuthSnapshot> {
    return this.publish("authenticated");
  }

  getSnapshot(): AuthSnapshot { return this.snapshot; }
  subscribe(listener: (snapshot: AuthSnapshot) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
  async login(): Promise<void> { this.publish("authenticated"); }
  async logout(): Promise<void> { this.cancelRequests(); this.publish("unauthenticated", true); }
  async getAccessToken(): Promise<null> {
    if (this.snapshot.status !== "authenticated") throw new Error("Authentication required");
    return null;
  }
  invalidate(failure: AuthFailure): void {
    this.cancelRequests();
    this.snapshot = frozenSnapshot({ ...this.snapshot, status: "recoverable_error", subject: null, displayName: null, sessionVersion: this.snapshot.sessionVersion + 1, failure });
    this.emit();
  }
  registerRequest(controller: AbortController): () => void { this.requests.add(controller); return () => this.requests.delete(controller); }
  cancelRequests(): void { for (const controller of this.requests) controller.abort(); this.requests.clear(); }

  private publish(status: AuthStatus, increment = false): AuthSnapshot {
    this.snapshot = frozenSnapshot({
      status,
      mode: "development",
      subject: status === "authenticated" ? "development-user" : null,
      displayName: status === "authenticated" ? "本地开发身份" : null,
      sessionVersion: this.snapshot.sessionVersion + (increment ? 1 : 0),
      failure: null,
    });
    this.emit();
    return this.snapshot;
  }
  private emit(): void { for (const listener of this.listeners) listener(this.snapshot); }
}

export function createAuthClient(config: PublicRuntimeConfig): AuthClient {
  return config.authMode === "development" ? new DevelopmentAuthClient() : new KeycloakAuthClient(config);
}

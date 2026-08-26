import { beforeEach, describe, expect, it, vi } from "vitest";
import { KeycloakAuthClient, safeApplicationPath, type KeycloakAdapter } from "../auth";
import type { PublicRuntimeConfig } from "../runtime-config";

const config: PublicRuntimeConfig = Object.freeze({
  environment: "staging",
  authMode: "oidc",
  oidcUrl: "http://localhost:8080",
  realm: "enterprise-rag",
  clientId: "enterprise-rag",
  redirectUri: "http://localhost:3000/",
  postLogoutRedirectUri: "http://localhost:3000/",
  silentCheckSsoRedirectUri: "http://localhost:3000/silent-check-sso.html",
  scopes: Object.freeze(["profile", "email"]),
  minTokenValiditySeconds: 30,
});

function adapter(overrides: Partial<KeycloakAdapter> = {}): KeycloakAdapter {
  const now = Math.floor(Date.now() / 1000);
  return {
    authenticated: true,
    subject: "subject-a",
    token: "memory-only-access-token",
    tokenParsed: {
      sub: "subject-a",
      preferred_username: "测试用户",
      iss: "http://localhost:8080/realms/enterprise-rag",
      aud: "enterprise-rag",
      iat: now - 10,
      exp: now + 300,
    },
    init: vi.fn().mockResolvedValue(true),
    login: vi.fn().mockResolvedValue(undefined),
    logout: vi.fn().mockResolvedValue(undefined),
    updateToken: vi.fn().mockResolvedValue(false),
    clearToken: vi.fn(),
    ...overrides,
  };
}

describe("Keycloak AuthClient", () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    history.replaceState(null, "", "/");
  });

  it("initializes standard flow with PKCE, nonce and silent SSO", async () => {
    const fake = adapter();
    const client = new KeycloakAuthClient(config, fake);
    const snapshot = await client.initialize();
    expect(snapshot.status).toBe("authenticated");
    expect(snapshot.subject).toBe("subject-a");
    expect(fake.init).toHaveBeenCalledWith(expect.objectContaining({
      onLoad: "check-sso",
      flow: "standard",
      pkceMethod: "S256",
      useNonce: true,
      silentCheckSsoFallback: true,
      enableLogging: false,
    }));
    expect(Object.keys(localStorage)).toEqual([]);
  });

  it("returns unauthenticated when no SSO session exists", async () => {
    const client = new KeycloakAuthClient(config, adapter({ authenticated: false, token: undefined, init: vi.fn().mockResolvedValue(false) }));
    expect((await client.initialize()).status).toBe("unauthenticated");
  });

  it("fails closed with distinct provider and callback errors", async () => {
    const unavailable = new KeycloakAuthClient(config, adapter({ init: vi.fn().mockRejectedValue(new Error("offline")) }));
    expect((await unavailable.initialize()).failure?.code).toBe("provider_unavailable");

    history.replaceState(null, "", "/?code=invalid&state=mismatch");
    const callback = new KeycloakAuthClient(config, adapter({ init: vi.fn().mockRejectedValue(new Error("invalid callback")) }));
    expect((await callback.initialize()).failure?.code).toBe("callback_invalid");
  });

  it("rejects invalid claims and external return paths", async () => {
    const fake = adapter({ tokenParsed: { sub: "subject-a", iat: 1, exp: 2, iss: "wrong", aud: "wrong" } });
    const client = new KeycloakAuthClient(config, fake);
    expect((await client.initialize()).failure?.code).toBe("callback_invalid");
    await client.login("https://attacker.invalid/return");
    expect(fake.login).not.toHaveBeenCalled();
    expect(safeApplicationPath("//attacker.invalid/path")).toBeNull();
  });

  it("shares one token refresh across concurrent requests", async () => {
    let resolveRefresh!: (value: boolean) => void;
    const refresh = new Promise<boolean>((resolve) => { resolveRefresh = resolve; });
    const fake = adapter({ updateToken: vi.fn().mockReturnValue(refresh) });
    const client = new KeycloakAuthClient(config, fake);
    await client.initialize();
    const first = client.getAccessToken();
    const second = client.getAccessToken();
    expect(fake.updateToken).toHaveBeenCalledTimes(1);
    resolveRefresh(true);
    await expect(Promise.all([first, second])).resolves.toEqual([
      "memory-only-access-token",
      "memory-only-access-token",
    ]);
  });

  it("invalidates the session, aborts requests and ends the Keycloak session", async () => {
    const fake = adapter({ updateToken: vi.fn().mockRejectedValue(new Error("offline")) });
    const client = new KeycloakAuthClient(config, fake);
    await client.initialize();
    const controller = new AbortController();
    client.registerRequest(controller);
    await expect(client.getAccessToken()).rejects.toThrow();
    expect(controller.signal.aborted).toBe(true);
    expect(client.getSnapshot().failure?.code).toBe("session_expired");

    const secondAdapter = adapter();
    const second = new KeycloakAuthClient(config, secondAdapter);
    await second.initialize();
    const logoutController = new AbortController();
    second.registerRequest(logoutController);
    await second.logout();
    expect(logoutController.signal.aborted).toBe(true);
    expect(second.getSnapshot().status).toBe("unauthenticated");
    expect(secondAdapter.logout).toHaveBeenCalledWith({
      redirectUri: config.postLogoutRedirectUri,
      logoutMethod: "GET",
    });
  });

  it("stores only a safe temporary return path and never tokens", async () => {
    const fake = adapter();
    const client = new KeycloakAuthClient(config, fake);
    await client.login("/knowledge?tab=active#top");
    expect(sessionStorage.getItem("enterprise-rag.auth.return-path")).toBe("/knowledge?tab=active#top");
    expect(JSON.stringify(localStorage)).not.toContain("memory-only-access-token");
    expect(JSON.stringify(sessionStorage)).not.toContain("memory-only-access-token");
  });
});

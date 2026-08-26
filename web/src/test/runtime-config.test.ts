import { describe, expect, it } from "vitest";
import { loadPublicRuntimeConfig, RuntimeConfigError } from "../runtime-config";

const oidcConfig = {
  environment: "staging",
  authMode: "oidc",
  oidcUrl: "http://localhost:8080",
  realm: "enterprise-rag",
  clientId: "enterprise-rag",
  redirectUri: "http://localhost:3000/",
  postLogoutRedirectUri: "http://localhost:3000/",
  silentCheckSsoRedirectUri: "http://localhost:3000/silent-check-sso.html",
  scopes: "profile,email",
  minTokenValiditySeconds: "30",
};

describe("public runtime configuration", () => {
  it("validates and normalizes the public OIDC model", () => {
    const config = loadPublicRuntimeConfig(oidcConfig);
    expect(config.scopes).toEqual(["profile", "email"]);
    expect(config.oidcUrl).toBe("http://localhost:8080");
    expect(config.minTokenValiditySeconds).toBe(30);
  });

  it("fails closed outside development without exposing provided values", () => {
    let captured: RuntimeConfigError | null = null;
    try {
      loadPublicRuntimeConfig({
        environment: "production",
        authMode: "development",
        oidcUrl: "https://secret-idp.invalid/private",
      });
    } catch (error) {
      captured = error as RuntimeConfigError;
    }
    expect(captured).toBeInstanceOf(RuntimeConfigError);
    expect(captured?.fields).toContain("developmentAuthBoundary");
    expect(captured?.message).not.toContain("secret-idp");
  });

  it("allows development authentication only when explicitly selected", () => {
    expect(loadPublicRuntimeConfig({
      environment: "development",
      authMode: "development",
      minTokenValiditySeconds: 30,
    }).authMode).toBe("development");
    expect(() => loadPublicRuntimeConfig({ environment: "development" })).toThrow(RuntimeConfigError);
  });
});

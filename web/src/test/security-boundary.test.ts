import { describe, expect, it } from "vitest";
import authSource from "../auth.ts?raw";
import transportSource from "../transport.ts?raw";

describe("browser credential safety boundary", () => {
  it("does not persist tokens or authorization codes and keeps adapter logging disabled", () => {
    expect(authSource).not.toMatch(/localStorage\.(?:setItem|getItem)\s*\([^)]*(?:token|authorization|code)/i);
    expect(transportSource).not.toMatch(/localStorage\.setItem\s*\([^)]*(?:token|authorization|code)/i);
    expect(authSource).toContain("enableLogging: false");
    expect(authSource).not.toMatch(/console\.(?:log|debug|info)\s*\(/);
    expect(transportSource).not.toMatch(/console\.(?:log|debug|info)\s*\(/);
  });

  it("does not expose a boolean authentication bypass on protected transport", () => {
    expect(transportSource).toContain("export async function publicFetch");
    expect(transportSource).toContain("export async function authenticatedFetch");
    expect(transportSource).not.toMatch(/authenticated\s*\?\s*fetch|skipAuth|public\s*:\s*boolean/i);
  });
});

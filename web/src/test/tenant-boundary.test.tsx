import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { api, setTenantBoundary } from "../api";

const activeTenant = {
  id: "tenant-a", slug: "tenant-a", name: "甲租户", state: "active",
  revision: 1, membership_state: "active", roles: ["administrator"],
};
const suspendedTenant = {
  id: "tenant-b", slug: "tenant-b", name: "乙租户", state: "suspended",
  revision: 2, membership_state: "active", roles: ["administrator"],
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status, headers: { "Content-Type": "application/json" },
  });
}

describe("tenant application boundary", () => {
  beforeEach(() => {
    window.localStorage.clear();
    setTenantBoundary("");
  });
  afterEach(() => vi.unstubAllGlobals());

  it("adds exactly the selected tenant at the centralized request boundary", async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(json({ items: [] })));
    vi.stubGlobal("fetch", fetchMock);
    setTenantBoundary("tenant-a");
    await api.spaces();
    const firstHeaders = new Headers(fetchMock.mock.calls[0][1]?.headers);
    expect(firstHeaders.get("X-Tenant-ID")).toBe("tenant-a");
    setTenantBoundary("tenant-b");
    await api.spaces();
    const secondHeaders = new Headers(fetchMock.mock.calls[1][1]?.headers);
    expect(secondHeaders.get("X-Tenant-ID")).toBe("tenant-b");
    await api.discoverTenants();
    const discoveryHeaders = new Headers(fetchMock.mock.calls[2][1]?.headers);
    expect(discoveryHeaders.has("X-Tenant-ID")).toBe(false);
  });

  it("clears the active data view and renders a suspended state after switching", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/health/ready")) return json({ status: "ready", ready: true, checks: {}, configuration_errors: [], version: "test" });
      if (path.endsWith("/api/v1/tenants")) return json({ platform_roles: [], items: [activeTenant, suspendedTenant] });
      if (path.endsWith("/api/v1/tenant/context")) {
        expect(new Headers(init?.headers).get("X-Tenant-ID")).toBe("tenant-a");
        return json({ tenant_id: "tenant-a", tenant_state: "active", principal_id: "principal-a", membership_id: "membership-a", roles: ["administrator"], groups: [], authorization_epoch: 1, configuration_version_id: "config-a" });
      }
      if (path.endsWith("/api/v1/assistants")) return json({ items: [] });
      return json({ items: [] });
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);
    await screen.findByText("甲租户 · active");
    expect(screen.getByRole("button", { name: /租户管理/ })).toBeVisible();
    fireEvent.change(screen.getByLabelText("当前租户"), { target: { value: "tenant-b" } });
    await screen.findByText("乙租户 当前不可访问");
    expect(screen.getByText("租户已暂停，数据面请求会被拒绝。")).toBeVisible();
    await waitFor(() => expect(window.localStorage.getItem("enterprise-rag.selected-tenant")).toBe("tenant-b"));
  });
});

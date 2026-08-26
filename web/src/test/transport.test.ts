import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, streamQuery } from "../api";
import type { AuthClient, AuthSnapshot } from "../auth";
import {
  ApiError,
  StaleSessionError,
  authenticatedFetch,
  downloadProtectedResource,
  setAuthClient,
  setTenantBoundary,
} from "../transport";

function response(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(typeof body === "string" ? body : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

function fakeAuth() {
  const snapshot: AuthSnapshot = Object.freeze({
    status: "authenticated",
    mode: "oidc",
    subject: "subject-a",
    displayName: "测试用户",
    sessionVersion: 1,
    failure: null,
  });
  const requests = new Set<AbortController>();
  return {
    initialize: vi.fn().mockResolvedValue(snapshot),
    getSnapshot: vi.fn(() => snapshot),
    subscribe: vi.fn(() => () => undefined),
    login: vi.fn().mockResolvedValue(undefined),
    logout: vi.fn().mockResolvedValue(undefined),
    getAccessToken: vi.fn().mockResolvedValue("access-token"),
    invalidate: vi.fn(),
    registerRequest: vi.fn((controller: AbortController) => { requests.add(controller); return () => requests.delete(controller); }),
    cancelRequests: vi.fn(() => { for (const controller of requests) controller.abort(); requests.clear(); }),
  } satisfies AuthClient;
}

describe("authenticated transport", () => {
  beforeEach(() => {
    setTenantBoundary("");
    setAuthClient(fakeAuth());
    vi.restoreAllMocks();
  });

  it("adds Bearer, tenant and correlation headers to JSON and FormData", async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(response({ items: [] })));
    vi.stubGlobal("fetch", fetchMock);
    setTenantBoundary("tenant-a");
    await api.spaces();
    await api.upload("source-a", new File(["content"], "example.txt"));
    for (const call of fetchMock.mock.calls) {
      const headers = new Headers(call[1]?.headers);
      expect(headers.get("Authorization")).toBe("Bearer access-token");
      expect(headers.get("X-Tenant-ID")).toBe("tenant-a");
      expect(headers.get("X-Correlation-ID")).toBeTruthy();
    }
    expect(new Headers(fetchMock.mock.calls[1][1]?.headers).has("Content-Type")).toBe(false);
  });

  it("refreshes once for 401 and never refreshes for 403", async () => {
    const auth = fakeAuth();
    setAuthClient(auth);
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(response({}, 401))
      .mockResolvedValueOnce(response({ items: [] }, 200))
      .mockResolvedValueOnce(response({}, 403)));
    await expect(api.spaces()).resolves.toEqual({ items: [] });
    await expect(api.spaces()).rejects.toMatchObject({ status: 403 });
    expect(auth.getAccessToken.mock.calls.map((call) => call[0])).toEqual([
      { forceRefresh: false },
      { forceRefresh: true },
      { forceRefresh: false },
    ]);
    expect(auth.invalidate).not.toHaveBeenCalled();
  });

  it("invalidates after the second 401 and discards old tenant responses", async () => {
    const auth = fakeAuth();
    setAuthClient(auth);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({}, 401)));
    await expect(api.spaces()).rejects.toBeInstanceOf(ApiError);
    expect(auth.invalidate).toHaveBeenCalledWith(expect.objectContaining({ code: "api_unauthenticated" }));

    let resolveFetch!: (value: Response) => void;
    vi.stubGlobal("fetch", vi.fn().mockReturnValue(new Promise<Response>((resolve) => { resolveFetch = resolve; })));
    setTenantBoundary("tenant-a");
    const pending = authenticatedFetch("/api/v1/knowledge-spaces");
    setTenantBoundary("tenant-b");
    resolveFetch(response({ items: [] }));
    await expect(pending).rejects.toBeInstanceOf(StaleSessionError);
  });

  it("keeps streaming bound to the authenticated request and supports cancellation", async () => {
    const encoder = new TextEncoder();
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode('event: answer\ndata: {"delta":"你好"}\n\n'));
        controller.close();
      },
    });
    const fetchMock = vi.fn().mockResolvedValue(new Response(body, { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const events: string[] = [];
    await streamQuery("assistant-a", "问题", null, (_, data) => events.push(String(data.delta)));
    expect(events).toEqual(["你好"]);
    expect(new Headers(fetchMock.mock.calls[0][1]?.headers).get("Authorization")).toBe("Bearer access-token");

    vi.stubGlobal("fetch", vi.fn((_input: RequestInfo | URL, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
      if (init?.signal?.aborted) {
        reject(new DOMException("Aborted", "AbortError"));
        return;
      }
      init?.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
    })));
    const controller = new AbortController();
    const cancelled = streamQuery("assistant-a", "取消问题", null, vi.fn(), controller.signal);
    controller.abort();
    await expect(cancelled).rejects.toMatchObject({ name: "AbortError" });
  });

  it("uses a short-lived object URL for protected downloads", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response("file", 200, {
      "Content-Type": "application/octet-stream",
      "Content-Disposition": 'attachment; filename="safe.txt"',
    })));
    const create = vi.fn(() => "blob:protected");
    const revoke = vi.fn();
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: create });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: revoke });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    await downloadProtectedResource("/api/v1/documents/document-a/download", "fallback.txt");
    expect(create).toHaveBeenCalledTimes(1);
    expect(revoke).toHaveBeenCalledWith("blob:protected");
  });
});

import type { AuthClient } from "./auth";

const PUBLIC_PATHS = new Set(["/health/live", "/health/ready"]);
const SUBJECT_TENANT_PREFIX = "enterprise-rag.selected-tenant.";
let authClient: AuthClient | null = null;
let selectedTenantId = "";
let boundaryVersion = 0;

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly correlationId?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export class StaleSessionError extends Error {
  constructor() {
    super("认证或租户边界已变化，旧请求结果已丢弃");
    this.name = "StaleSessionError";
  }
}

export function setAuthClient(client: AuthClient): void {
  authClient = client;
}

export function getAuthClient(): AuthClient {
  if (!authClient) throw new Error("Authentication client is not configured");
  return authClient;
}

export function setTenantBoundary(tenantId: string): void {
  if (selectedTenantId === tenantId) return;
  selectedTenantId = tenantId;
  boundaryVersion += 1;
  authClient?.cancelRequests();
}

export function getTenantBoundary(): string {
  return selectedTenantId;
}

async function subjectStorageKey(subject: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(subject));
  const suffix = Array.from(new Uint8Array(digest).slice(0, 12), (value) => value.toString(16).padStart(2, "0")).join("");
  return `${SUBJECT_TENANT_PREFIX}${suffix}`;
}

export async function rememberTenantForSubject(subject: string, tenantId: string): Promise<void> {
  const key = await subjectStorageKey(subject);
  if (tenantId) localStorage.setItem(key, tenantId);
  else localStorage.removeItem(key);
}

export async function rememberedTenantForSubject(subject: string): Promise<string> {
  return localStorage.getItem(await subjectStorageKey(subject)) ?? "";
}

export async function resetTenantBoundary(subject?: string | null): Promise<void> {
  setTenantBoundary("");
  if (subject) localStorage.removeItem(await subjectStorageKey(subject));
}

function resolveApiUrl(path: string): string {
  if (!path.startsWith("/") || path.startsWith("//")) throw new Error("Only application API paths are allowed");
  return `${import.meta.env.VITE_API_BASE ?? ""}${path}`;
}

function combineSignal(controller: AbortController, external?: AbortSignal | null): () => void {
  if (!external) return () => undefined;
  if (external.aborted) controller.abort(external.reason);
  const abort = () => controller.abort(external.reason);
  external.addEventListener("abort", abort, { once: true });
  return () => external.removeEventListener("abort", abort);
}

function requestHeaders(path: string, init: RequestInit, token: string | null): Headers {
  const headers = new Headers(init.headers);
  headers.set("X-Correlation-ID", crypto.randomUUID());
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (selectedTenantId && !path.startsWith("/api/v1/tenants")) headers.set("X-Tenant-ID", selectedTenantId);
  if (init.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  return headers;
}

async function errorFromResponse(response: Response): Promise<ApiError> {
  const body = await response.clone().json().catch(() => ({})) as { message?: string; detail?: string };
  const correlationId = response.headers.get("X-Correlation-ID") ?? undefined;
  const fallback = response.status === 403 ? "权限不足（403）" : `请求失败（${response.status}）`;
  return new ApiError(body.message ?? body.detail ?? fallback, response.status, correlationId);
}

export async function publicFetch(path: string, init: RequestInit = {}): Promise<Response> {
  if (!PUBLIC_PATHS.has(path)) throw new Error("Endpoint is not public");
  const headers = new Headers(init.headers);
  headers.set("X-Correlation-ID", crypto.randomUUID());
  return fetch(resolveApiUrl(path), { ...init, headers });
}

export async function authenticatedFetch(path: string, init: RequestInit = {}): Promise<Response> {
  if (!path.startsWith("/api/")) throw new Error("Protected endpoint must use the API namespace");
  const client = getAuthClient();
  const controller = new AbortController();
  const removeExternalSignal = combineSignal(controller, init.signal);
  const unregister = client.registerRequest(controller);
  const sessionVersion = client.getSnapshot().sessionVersion;
  const requestBoundaryVersion = boundaryVersion;

  const send = async (forceRefresh: boolean): Promise<Response> => {
    const token = await client.getAccessToken({ forceRefresh });
    if (client.getSnapshot().mode === "oidc" && !token) throw new Error("Authentication required");
    return fetch(resolveApiUrl(path), {
      ...init,
      signal: controller.signal,
      headers: requestHeaders(path, init, token),
    });
  };

  try {
    let response = await send(false);
    if (response.status === 401) response = await send(true);
    if (response.status === 401) {
      const correlationId = response.headers.get("X-Correlation-ID") ?? undefined;
      client.invalidate({ code: "api_unauthenticated", correlationId });
      throw new ApiError("会话已失效，请重新登录", 401, correlationId);
    }
    if (client.getSnapshot().sessionVersion !== sessionVersion || boundaryVersion !== requestBoundaryVersion) throw new StaleSessionError();
    return response;
  } catch (error) {
    if (!(error instanceof StaleSessionError) && !controller.signal.aborted && client.getSnapshot().status !== "authenticated") {
      throw new ApiError("会话已失效，请重新登录", 401);
    }
    throw error;
  } finally {
    unregister();
    removeExternalSignal();
  }
}

export async function publicJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await publicFetch(path, init);
  if (!response.ok) throw await errorFromResponse(response);
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export async function authenticatedJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await authenticatedFetch(path, init);
  if (!response.ok) throw await errorFromResponse(response);
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export async function authenticatedBlob(path: string, init?: RequestInit): Promise<{ blob: Blob; filename: string | null }> {
  const response = await authenticatedFetch(path, init);
  if (!response.ok) throw await errorFromResponse(response);
  const disposition = response.headers.get("Content-Disposition") ?? "";
  const filename = disposition.match(/filename="?([^";]+)"?/i)?.[1] ?? null;
  return { blob: await response.blob(), filename };
}

export async function downloadProtectedResource(path: string, fallbackFilename: string): Promise<void> {
  const { blob, filename } = await authenticatedBlob(path);
  const objectUrl = URL.createObjectURL(blob);
  try {
    const anchor = document.createElement("a");
    anchor.href = objectUrl;
    anchor.download = filename ?? fallbackFilename;
    anchor.rel = "noopener";
    anchor.click();
  } finally {
    URL.revokeObjectURL(objectUrl);
  }
}

export function captureRequestBoundary(): { sessionVersion: number; boundaryVersion: number } {
  return { sessionVersion: getAuthClient().getSnapshot().sessionVersion, boundaryVersion };
}

export function assertRequestBoundary(captured: { sessionVersion: number; boundaryVersion: number }): void {
  if (getAuthClient().getSnapshot().sessionVersion !== captured.sessionVersion || boundaryVersion !== captured.boundaryVersion) {
    throw new StaleSessionError();
  }
}

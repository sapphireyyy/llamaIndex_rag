export type HealthResponse = {
  status: "ready" | "degraded";
  ready: boolean;
  checks: Record<string, boolean>;
  configuration_errors: string[];
  version: string;
};

export type Space = {
  id: string;
  name: string;
  description: string;
  state: "active" | "archived";
  default_policy_ids: Record<string, string>;
};

export type Source = { id: string; kind: string; external_id: string; enabled: boolean };
export type DocumentItem = {
  id: string;
  title: string;
  state: string;
  knowledge_space_id: string;
  active_version_id: string | null;
};
export type Job = {
  id: string;
  document_id: string | null;
  document_version_id: string | null;
  status: string;
  stage: string;
  attempt_count: number;
  max_attempts: number;
  error_message: string | null;
};
export type Assistant = { id: string; name: string; active_version_id: string | null };
export type AssistantVersion = {
  id: string;
  version: number;
  state: string;
  knowledge_space_ids: string[];
  validation_errors: string[];
  activated_at: string | null;
};
export type ProviderProfile = {
  id: string;
  name: string;
  kind: string;
  adapter: string;
  capabilities: string[];
  secret_bindings: Array<{ id: string; name: string; valid: boolean }>;
};
export type Policy = { id: string; kind: string; version: number; state: string };
export type Citation = {
  id: string;
  label: string;
  page: number | null;
  section: string | null;
  source_url: string;
};
export type EvaluationDataset = { id: string; name: string; active_version_id: string };
export type EvaluationRun = {
  id: string;
  status: string;
  dataset_version_id: string;
  assistant_version_id: string;
  aggregates: Record<string, number>;
};

export type TenantState = "provisioning" | "active" | "suspended" | "archived";
export type TenantSummary = {
  id: string;
  slug: string;
  name: string;
  state: TenantState;
  revision: number;
  membership_state?: string;
  roles?: string[];
  state_reason?: string;
  authorization_epoch?: number;
  active_configuration_version_id?: string | null;
  metadata?: Record<string, unknown>;
};
export type TenantContext = {
  tenant_id: string;
  tenant_state: TenantState;
  principal_id: string;
  membership_id: string;
  roles: string[];
  groups: string[];
  authorization_epoch: number;
  configuration_version_id: string;
};
export type TenantMember = {
  id: string;
  principal_id: string;
  external_id: string;
  display_name: string;
  state: string;
  direct_roles: string[];
  effective_roles: string[];
  groups: string[];
  revision: number;
};
export type TenantGroup = {
  id: string;
  external_id: string;
  display_name: string;
  state: string;
  revision: number;
};
export type TenantInvitation = {
  id: string;
  destination: string;
  roles: string[];
  state: string;
  revision: number;
  expires_at: string;
  token?: string;
};
export type TenantConfigurationVersion = {
  id: string;
  version: number;
  state: string;
  config: Record<string, unknown>;
  config_hash: string;
  validation_errors: string[];
  activated_at: string | null;
  created_at: string;
};
export type TenantEffectiveSettings = {
  tenant_id: string;
  tenant_revision: number;
  configuration_version_id: string;
  version: number;
  config_hash: string;
  config: Record<string, unknown>;
};
export type TenantUsage = {
  resource: string;
  used: number;
  reserved: number;
  limit: number;
  revision: number;
  warning: boolean;
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  return authenticatedJson<T>(path, init);
}

export const api = {
  health: () => publicJson<HealthResponse>("/health/ready"),
  discoverTenants: () =>
    request<{ platform_roles: string[]; items: TenantSummary[] }>("/api/v1/tenants"),
  tenantContext: () => request<TenantContext>("/api/v1/tenant/context"),
  platformTenants: (search = "", state = "") => {
    const query = new URLSearchParams({ search });
    if (state) query.set("state", state);
    return request<{ items: TenantSummary[] }>(`/api/v1/platform/tenants?${query}`);
  },
  createPlatformTenant: (values: {
    name: string;
    slug?: string;
    administrator_external_id?: string;
  }) => request<TenantSummary>("/api/v1/platform/tenants", {
    method: "POST",
    body: JSON.stringify({ ...values, metadata: {} }),
    headers: { "Idempotency-Key": crypto.randomUUID() },
  }),
  updatePlatformTenant: (tenant: TenantSummary, values: { name?: string; metadata?: Record<string, unknown> }) =>
    request<TenantSummary>(`/api/v1/platform/tenants/${tenant.id}`, {
      method: "PATCH",
      body: JSON.stringify(values),
      headers: { "If-Match": String(tenant.revision) },
    }),
  transitionPlatformTenant: (tenant: TenantSummary, action: string, reason: string) =>
    request<TenantSummary>(`/api/v1/platform/tenants/${tenant.id}/actions/${action}`, {
      method: "POST",
      body: JSON.stringify({ reason }),
      headers: { "If-Match": String(tenant.revision) },
    }),
  fleetSummary: () => request<{ items: Array<Record<string, unknown>> }>("/api/v1/platform/fleet-summary"),
  tenantMembers: () => request<{ items: TenantMember[] }>("/api/v1/tenant/members"),
  assignTenantMember: (external_id: string, display_name: string, roles: string[]) =>
    request<TenantMember>("/api/v1/tenant/members", {
      method: "POST",
      body: JSON.stringify({ external_id, display_name, roles }),
      headers: { "Idempotency-Key": crypto.randomUUID() },
    }),
  updateTenantMember: (member: TenantMember, values: { roles?: string[]; state?: string; reason?: string }) =>
    request<TenantMember>(`/api/v1/tenant/members/${member.id}`, {
      method: "PATCH",
      body: JSON.stringify(values),
      headers: { "If-Match": String(member.revision) },
    }),
  tenantGroups: () => request<{ items: TenantGroup[] }>("/api/v1/tenant/groups"),
  createTenantGroup: (external_id: string, display_name: string) =>
    request<TenantGroup>("/api/v1/tenant/groups", {
      method: "POST",
      body: JSON.stringify({ external_id, display_name }),
      headers: { "Idempotency-Key": crypto.randomUUID() },
    }),
  setTenantGroupMember: (groupId: string, principalId: string, active: boolean) =>
    request(`/api/v1/tenant/groups/${groupId}/members/${principalId}`, {
      method: "PUT", body: JSON.stringify({ active }),
    }),
  setTenantGroupRole: (groupId: string, role: string, active: boolean) =>
    request(`/api/v1/tenant/groups/${groupId}/roles/${role}`, {
      method: "PUT", body: JSON.stringify({ active }),
    }),
  tenantInvitations: () => request<{ items: TenantInvitation[] }>("/api/v1/tenant/invitations"),
  createTenantInvitation: (destination: string, roles: string[], expires_in_hours: number) =>
    request<TenantInvitation>("/api/v1/tenant/invitations", {
      method: "POST", body: JSON.stringify({ destination, roles, expires_in_hours }),
    }),
  revokeTenantInvitation: (invitation: TenantInvitation) =>
    request<TenantInvitation>(`/api/v1/tenant/invitations/${invitation.id}/revoke`, {
      method: "POST", headers: { "If-Match": String(invitation.revision) },
    }),
  recoverTenantAdministrator: (tenantId: string, external_id: string, display_name: string) =>
    request<TenantMember>(`/api/v1/platform/tenants/${tenantId}/recovery/administrator`, {
      method: "POST", body: JSON.stringify({ external_id, display_name }),
    }),
  tenantEffectiveSettings: () =>
    request<TenantEffectiveSettings>("/api/v1/tenant/settings/effective"),
  tenantConfigurationVersions: () =>
    request<{ items: TenantConfigurationVersion[] }>("/api/v1/tenant/settings/versions"),
  createTenantConfigurationVersion: (
    config: Record<string, unknown>, tenantRevision: number, activate: boolean,
  ) => request<TenantConfigurationVersion>("/api/v1/tenant/settings/versions", {
    method: "POST",
    body: JSON.stringify({ config, activate }),
    headers: { "If-Match": String(tenantRevision) },
  }),
  rollbackTenantConfiguration: (sourceVersionId: string, tenantRevision: number) =>
    request<TenantConfigurationVersion>("/api/v1/tenant/settings/rollback", {
      method: "POST",
      body: JSON.stringify({ source_version_id: sourceVersionId }),
      headers: { "If-Match": String(tenantRevision) },
    }),
  tenantUsage: () => request<{ items: TenantUsage[] }>("/api/v1/tenant/usage"),
  reconcileTenantUsage: () => request<{ items: Array<Record<string, unknown>> }>(
    "/api/v1/tenant/usage/reconcile", { method: "POST" },
  ),
  spaces: () => request<{ items: Space[] }>("/api/v1/knowledge-spaces"),
  createSpace: (name: string, description: string) =>
    request<Space>("/api/v1/knowledge-spaces", {
      method: "POST",
      body: JSON.stringify({ name, description }),
      headers: { "Idempotency-Key": crypto.randomUUID() },
    }),
  updateSpace: (id: string, values: Partial<Pick<Space, "name" | "description">>) =>
    request<Space>(`/api/v1/knowledge-spaces/${id}`, {
      method: "PATCH",
      body: JSON.stringify(values),
    }),
  spaceAction: (id: string, action: "archive" | "restore") =>
    request<Space>(`/api/v1/knowledge-spaces/${id}/actions/${action}`, { method: "POST" }),
  memberships: (id: string) =>
    request<{ items: Array<{ id: string; principal_token: string; role: string }> }>(
      `/api/v1/knowledge-spaces/${id}/memberships`,
    ),
  setMembership: (id: string, principal_token: string, role: string | null) =>
    request(`/api/v1/knowledge-spaces/${id}/memberships`, {
      method: "PUT",
      body: JSON.stringify({ principal_token, role }),
    }),
  sources: (id: string) =>
    request<{ items: Source[] }>(`/api/v1/knowledge-spaces/${id}/data-sources`),
  createSource: (id: string, external_id: string) =>
    request<Source>(`/api/v1/knowledge-spaces/${id}/data-sources`, {
      method: "POST",
      body: JSON.stringify({ kind: "upload", external_id, config: {} }),
    }),
  upload: (sourceId: string, file: File) => {
    const body = new FormData();
    body.set("source_id", sourceId);
    body.set("file", file);
    return request<Job>("/api/v1/documents/upload", {
      method: "POST",
      body,
      headers: { "Idempotency-Key": crypto.randomUUID() },
    });
  },
  documents: () => request<{ items: DocumentItem[] }>("/api/v1/documents"),
  jobs: () => request<{ items: Job[] }>("/api/v1/ingestion-jobs"),
  retryJob: (id: string) => request<Job>(`/api/v1/ingestion-jobs/${id}/retry`, { method: "POST" }),
  cancelJob: (id: string) => request<Job>(`/api/v1/ingestion-jobs/${id}/cancel`, { method: "POST" }),
  deleteDocument: (id: string) => request<void>(`/api/v1/documents/${id}`, { method: "DELETE" }),
  previewDocument: (id: string) =>
    request<{ items: Array<{ id: string; text: string; page: number | null; section: string | null }> }>(
      `/api/v1/documents/${id}/preview`,
    ),
  downloadDocument: (id: string, fallbackFilename: string) =>
    downloadProtectedResource(`/api/v1/documents/${id}/download`, fallbackFilename),
  documentVersions: (id: string) =>
    request<{ items: Array<{ id: string; state: string; active: boolean; created_at: string }> }>(
      `/api/v1/documents/${id}/versions`,
    ),
  assistants: () => request<{ items: Assistant[] }>("/api/v1/assistants"),
  assistantHistory: (id: string) =>
    request<{ items: AssistantVersion[] }>(`/api/v1/assistants/${id}/versions`),
  createAssistant: (name: string, spaceId: string) =>
    request<{ id: string; draft: AssistantVersion }>("/api/v1/assistants", {
      method: "POST",
      body: JSON.stringify({
        name,
        draft: { knowledge_space_ids: [spaceId], policy_version_ids: {}, provider_profile_ids: {} },
      }),
    }),
  createAssistantVersion: (id: string, spaceIds: string[]) =>
    request<AssistantVersion>(`/api/v1/assistants/${id}/versions`, {
      method: "POST",
      body: JSON.stringify({
        knowledge_space_ids: spaceIds,
        policy_version_ids: {},
        provider_profile_ids: {},
      }),
    }),
  validateAssistant: (assistantId: string, versionId: string) =>
    request<{ valid: boolean; errors: string[] }>(
      `/api/v1/assistants/${assistantId}/versions/${versionId}/validate`,
      { method: "POST" },
    ),
  activateAssistant: (assistantId: string, versionId: string) =>
    request<AssistantVersion>(
      `/api/v1/assistants/${assistantId}/versions/${versionId}/activate`,
      { method: "POST" },
    ),
  providers: () => request<{ items: ProviderProfile[] }>("/api/v1/provider-profiles"),
  policies: () => request<{ items: Policy[] }>("/api/v1/policies"),
  source: (url: string) =>
    request<{
      title: string;
      document_id: string;
      page: number | null;
      section: string | null;
      preview_url: string;
      download_url: string;
    }>(url),
  downloadSource: (url: string, fallbackFilename: string) =>
    downloadProtectedResource(url, fallbackFilename),
  datasets: () => request<{ items: EvaluationDataset[] }>("/api/v1/evaluation/datasets"),
  createDataset: (name: string, items: unknown[]) =>
    request<{ id: string; version_id: string }>("/api/v1/evaluation/datasets", {
      method: "POST",
      body: JSON.stringify({ name, items }),
    }),
  runs: () => request<{ items: EvaluationRun[] }>("/api/v1/evaluation/runs"),
  createRun: (datasetVersionId: string, assistantVersionId: string) =>
    request<EvaluationRun>("/api/v1/evaluation/runs", {
      method: "POST",
      body: JSON.stringify({
        dataset_version_id: datasetVersionId,
        assistant_version_id: assistantVersionId,
      }),
    }),
  run: (id: string) => request<Record<string, unknown>>(`/api/v1/evaluation/runs/${id}`),
  createGate: (metric: string, threshold: number) =>
    request<{ id: string }>("/api/v1/evaluation/gates", {
      method: "POST",
      body: JSON.stringify({
        name: `${metric} gate`, metric, operator: ">=", threshold, mandatory: true,
      }),
    }),
  overrideGate: (gateId: string, runId: string, reason: string) =>
    request<{ id: string }>(`/api/v1/evaluation/gates/${gateId}/override`, {
      method: "POST",
      body: JSON.stringify({ run_id: runId, reason }),
    }),
  feedback: (answerId: string, rating: number, comment: string) =>
    request<{ id: string }>("/api/v1/feedback", {
      method: "POST",
      body: JSON.stringify({ answer_id: answerId, category: "answer", rating, comment }),
    }),
  feedbackList: () => request<{ items: Array<Record<string, unknown>> }>("/api/v1/feedback"),
};

export async function streamQuery(
  assistantId: string,
  question: string,
  conversationId: string | null,
  onEvent: (event: string, data: Record<string, unknown>) => void,
  signal?: AbortSignal,
): Promise<void> {
  const client = getAuthClient();
  const streamController = new AbortController();
  const abortStream = () => streamController.abort(signal?.reason);
  if (signal?.aborted) abortStream();
  else signal?.addEventListener("abort", abortStream, { once: true });
  const unregister = client.registerRequest(streamController);
  const captured = captureRequestBoundary();
  let reader: ReadableStreamDefaultReader<Uint8Array> | null = null;
  try {
    const response = await authenticatedFetch("/api/v1/chat/query", {
      method: "POST",
      signal: streamController.signal,
      body: JSON.stringify({ assistant_id: assistantId, question, conversation_id: conversationId }),
    });
    if (!response.ok || !response.body) throw new Error(`问答请求失败（${response.status}）`);
    reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      assertRequestBoundary(captured);
      buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done });
      const blocks = buffer.split("\n\n");
      buffer = blocks.pop() ?? "";
      for (const block of blocks) {
        const event = block.match(/^event: (.+)$/m)?.[1];
        const data = block.match(/^data: (.+)$/m)?.[1];
        if (event && data) onEvent(event, JSON.parse(data) as Record<string, unknown>);
      }
      if (done) break;
    }
  } finally {
    signal?.removeEventListener("abort", abortStream);
    if (streamController.signal.aborted) await reader?.cancel().catch(() => undefined);
    unregister();
  }
}
import {
  assertRequestBoundary,
  authenticatedFetch,
  authenticatedJson,
  captureRequestBoundary,
  downloadProtectedResource,
  getAuthClient,
  getTenantBoundary,
  publicJson,
  rememberedTenantForSubject,
  rememberTenantForSubject,
  resetTenantBoundary,
  setTenantBoundary,
} from "./transport";

export {
  getTenantBoundary,
  rememberedTenantForSubject,
  rememberTenantForSubject,
  resetTenantBoundary,
  setTenantBoundary,
};

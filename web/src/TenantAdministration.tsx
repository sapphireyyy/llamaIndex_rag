import { FormEvent, type ReactNode, useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  type TenantConfigurationVersion,
  type TenantContext,
  type TenantEffectiveSettings,
  type TenantGroup,
  type TenantInvitation,
  type TenantMember,
  type TenantSummary,
  type TenantUsage,
} from "./api";

type Notice = { tone: "ok" | "error"; text: string } | null;
type Props = {
  tenant: TenantSummary | null;
  context: TenantContext | null;
  platformAdministrator: boolean;
  notify: (notice: Notice) => void;
  refreshDiscovery: () => Promise<void>;
};
type Section = "members" | "settings" | "platform";

function Status({ value }: { value: string }) {
  return <span className={`pill pill-${value}`}>{value}</span>;
}

function ErrorBoundary({ children, message }: { children: ReactNode; message: string }) {
  return children ?? <div className="empty-state">{message}</div>;
}

function PlatformConsole({ notify, refreshDiscovery }: Pick<Props, "notify" | "refreshDiscovery">) {
  const [tenants, setTenants] = useState<TenantSummary[]>([]);
  const [fleet, setFleet] = useState<Array<Record<string, unknown>>>([]);
  const [search, setSearch] = useState("");
  const [state, setState] = useState("");
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [administrator, setAdministrator] = useState("");

  const refresh = useCallback(async () => {
    const [tenantResult, fleetResult] = await Promise.all([
      api.platformTenants(search, state), api.fleetSummary(),
    ]);
    setTenants(tenantResult.items);
    setFleet(fleetResult.items);
  }, [search, state]);
  useEffect(() => {
    const timer = window.setTimeout(() => {
      void refresh().catch((error: Error) => notify({ tone: "error", text: error.message }));
    }, 0);
    return () => window.clearTimeout(timer);
  }, [refresh, notify]);

  async function create(event: FormEvent) {
    event.preventDefault();
    try {
      await api.createPlatformTenant({
        name, slug: slug || undefined,
        administrator_external_id: administrator || undefined,
      });
      setName(""); setSlug(""); setAdministrator("");
      await Promise.all([refresh(), refreshDiscovery()]);
      notify({ tone: "ok", text: "租户已创建，当前处于配置开通状态。" });
    } catch (error) { notify({ tone: "error", text: (error as Error).message }); }
  }

  async function transition(tenant: TenantSummary, action: string) {
    const reason = window.prompt("请输入本次状态变更原因（将写入审计记录）");
    if (reason === null) return;
    if (!window.confirm(`确认对“${tenant.name}”执行 ${action}？`)) return;
    try {
      await api.transitionPlatformTenant(tenant, action, reason);
      await Promise.all([refresh(), refreshDiscovery()]);
      notify({ tone: "ok", text: "租户状态已更新。" });
    } catch (error) { notify({ tone: "error", text: (error as Error).message }); }
  }

  async function edit(tenant: TenantSummary) {
    const nextName = window.prompt("租户显示名称", tenant.name);
    if (!nextName || nextName === tenant.name) return;
    try {
      await api.updatePlatformTenant(tenant, { name: nextName });
      await Promise.all([refresh(), refreshDiscovery()]);
      notify({ tone: "ok", text: "租户资料已更新。" });
    } catch (error) { notify({ tone: "error", text: (error as Error).message }); }
  }

  async function recover(tenant: TenantSummary) {
    const externalId = window.prompt("恢复管理员的登录标识");
    if (!externalId) return;
    const displayName = window.prompt("显示名称", externalId) ?? externalId;
    try {
      await api.recoverTenantAdministrator(tenant.id, externalId, displayName);
      notify({ tone: "ok", text: "平台恢复操作已完成并写入审计记录。" });
    } catch (error) { notify({ tone: "error", text: (error as Error).message }); }
  }

  const fleetByTenant = useMemo(
    () => new Map(fleet.map((item) => [String(item.tenant_id), item])), [fleet],
  );
  return <div className="content-stack">
    <section className="panel">
      <div className="panel-title"><div><h2>平台租户控制台</h2><p>管理租户生命周期和安全的汇总用量，不展示租户内容。</p></div></div>
      <form className="admin-create-grid" onSubmit={create}>
        <label>租户名称<input value={name} onChange={(event) => setName(event.target.value)} required /></label>
        <label>短标识<input value={slug} onChange={(event) => setSlug(event.target.value)} placeholder="可自动生成" /></label>
        <label>首位管理员<input value={administrator} onChange={(event) => setAdministrator(event.target.value)} placeholder="可稍后恢复" /></label>
        <button className="primary" type="submit">创建租户</button>
      </form>
    </section>
    <section className="panel">
      <div className="filter-row">
        <label>搜索<input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="名称或短标识" /></label>
        <label>状态<select value={state} onChange={(event) => setState(event.target.value)}><option value="">全部</option><option value="provisioning">开通中</option><option value="active">正常</option><option value="suspended">已暂停</option><option value="archived">已归档</option></select></label>
      </div>
      <div className="table-wrap"><table><thead><tr><th>租户</th><th>状态</th><th>版本</th><th>安全汇总</th><th>操作</th></tr></thead><tbody>{tenants.map((tenant) => {
        const summary = fleetByTenant.get(tenant.id);
        const actions = tenant.state === "provisioning" ? ["activate"] : tenant.state === "active" ? ["suspend", "archive"] : tenant.state === "suspended" ? ["reactivate", "archive"] : ["restore"];
        return <tr key={tenant.id}><td><strong>{tenant.name}</strong><small className="block mono">{tenant.slug}</small></td><td><Status value={tenant.state} />{tenant.state_reason && <small className="block">{tenant.state_reason}</small>}</td><td>r{tenant.revision}</td><td>{summary ? `${String(summary.member_count ?? 0)} 成员 · ${String(summary.document_count ?? 0)} 文档` : "—"}</td><td className="actions"><button onClick={() => void edit(tenant)}>编辑</button>{actions.map((action) => <button className={action === "suspend" || action === "archive" ? "danger" : ""} key={action} onClick={() => void transition(tenant, action)}>{action}</button>)}<button onClick={() => void recover(tenant)}>恢复管理员</button></td></tr>;
      })}</tbody></table></div>
    </section>
  </div>;
}

function MembersConsole({ tenant, notify }: { tenant: TenantSummary; notify: Props["notify"] }) {
  const [members, setMembers] = useState<TenantMember[]>([]);
  const [groups, setGroups] = useState<TenantGroup[]>([]);
  const [invitations, setInvitations] = useState<TenantInvitation[]>([]);
  const [externalId, setExternalId] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [memberRole, setMemberRole] = useState("reader");
  const [groupName, setGroupName] = useState("");
  const [destination, setDestination] = useState("");
  const [invitationToken, setInvitationToken] = useState("");

  const refresh = useCallback(async () => {
    const [memberResult, groupResult, invitationResult] = await Promise.all([
      api.tenantMembers(), api.tenantGroups(), api.tenantInvitations(),
    ]);
    setMembers(memberResult.items); setGroups(groupResult.items); setInvitations(invitationResult.items);
  }, []);
  useEffect(() => {
    const timer = window.setTimeout(() => {
      void refresh().catch((error: Error) => notify({ tone: "error", text: error.message }));
    }, 0);
    return () => window.clearTimeout(timer);
  }, [refresh, notify, tenant.id]);

  async function addMember(event: FormEvent) {
    event.preventDefault();
    try { await api.assignTenantMember(externalId, displayName, [memberRole]); setExternalId(""); setDisplayName(""); await refresh(); notify({ tone: "ok", text: "成员已分配。" }); }
    catch (error) { notify({ tone: "error", text: (error as Error).message }); }
  }
  async function updateMember(member: TenantMember, values: { roles?: string[]; state?: string }) {
    const destructive = values.state === "suspended" || values.state === "removed" || (values.roles && !values.roles.includes("administrator"));
    if (destructive && !window.confirm("此操作会立即改变访问权限，确定继续？")) return;
    try { await api.updateTenantMember(member, { ...values, reason: "由租户管理界面更新" }); await refresh(); notify({ tone: "ok", text: "成员权限已更新。" }); }
    catch (error) { notify({ tone: "error", text: `${(error as Error).message}；系统会阻止移除最后一位管理员。` }); }
  }
  async function addGroup(event: FormEvent) {
    event.preventDefault();
    try { await api.createTenantGroup(groupName.toLowerCase().replace(/\s+/g, "-"), groupName); setGroupName(""); await refresh(); notify({ tone: "ok", text: "用户组已创建。" }); }
    catch (error) { notify({ tone: "error", text: (error as Error).message }); }
  }
  async function invite(event: FormEvent) {
    event.preventDefault();
    try { const created = await api.createTenantInvitation(destination, [memberRole], 72); setDestination(""); setInvitationToken(created.token ?? ""); await refresh(); notify({ tone: "ok", text: "邀请已创建，令牌仅显示这一次。" }); }
    catch (error) { notify({ tone: "error", text: (error as Error).message }); }
  }
  async function assignGroup(groupId: string) {
    const member = members[0];
    if (!member) return;
    try { await api.setTenantGroupMember(groupId, member.principal_id, true); await api.setTenantGroupRole(groupId, "reader", true); await refresh(); notify({ tone: "ok", text: "已将首位成员加入用户组并赋予读取角色。" }); }
    catch (error) { notify({ tone: "error", text: (error as Error).message }); }
  }

  return <div className="content-stack">
    <section className="panel"><div className="panel-title"><div><h2>成员与角色</h2><p>直接角色与用户组角色共同决定有效权限，权限变更立即刷新授权纪元。</p></div></div>
      <form className="admin-create-grid" onSubmit={addMember}><label>登录标识<input value={externalId} onChange={(event) => setExternalId(event.target.value)} required /></label><label>显示名称<input value={displayName} onChange={(event) => setDisplayName(event.target.value)} /></label><label>租户角色<select value={memberRole} onChange={(event) => setMemberRole(event.target.value)}><option value="reader">读取者</option><option value="editor">编辑者</option><option value="administrator">管理员</option></select></label><button className="primary">分配成员</button></form>
      <div className="table-wrap"><table><thead><tr><th>成员</th><th>状态</th><th>直接角色</th><th>有效权限</th><th>操作</th></tr></thead><tbody>{members.map((member) => <tr key={member.id}><td>{member.display_name || member.external_id}<small className="block mono">{member.external_id}</small></td><td><Status value={member.state} /></td><td><select aria-label={`${member.external_id} 角色`} value={member.direct_roles[0] ?? "reader"} onChange={(event) => void updateMember(member, { roles: [event.target.value] })}><option value="reader">读取者</option><option value="editor">编辑者</option><option value="administrator">管理员</option></select></td><td>{member.effective_roles.join("、")}<small className="block">组：{member.groups.join("、") || "无"}</small></td><td>{member.state === "active" ? <><button onClick={() => void updateMember(member, { state: "suspended" })}>暂停</button><button className="danger" onClick={() => void updateMember(member, { state: "removed" })}>移除</button></> : <button onClick={() => void updateMember(member, { state: "active" })}>重新启用</button>}</td></tr>)}</tbody></table></div>
    </section>
    <div className="admin-two-column"><section className="panel"><div className="panel-title"><h2>用户组</h2></div><form className="form-row compact-form" onSubmit={addGroup}><input value={groupName} onChange={(event) => setGroupName(event.target.value)} placeholder="用户组名称" required /><button className="primary">创建</button></form><div className="item-list">{groups.map((group) => <div className="list-item static-item" key={group.id}><strong>{group.display_name}</strong><Status value={group.state} /><small>{group.external_id}</small><button onClick={() => void assignGroup(group.id)}>分配首位成员 + 读取角色</button></div>)}</div></section>
      <section className="panel"><div className="panel-title"><h2>邀请</h2></div><form className="form-row compact-form" onSubmit={invite}><input type="email" value={destination} onChange={(event) => setDestination(event.target.value)} placeholder="name@example.com" required /><button className="primary">发出邀请</button></form>{invitationToken && <div className="one-time-secret"><strong>一次性邀请令牌</strong><code>{invitationToken}</code><button onClick={() => { void navigator.clipboard.writeText(invitationToken); setInvitationToken(""); }}>复制并隐藏</button></div>}<div className="item-list">{invitations.map((item) => <div className="list-item static-item" key={item.id}><strong>{item.destination}</strong><Status value={item.state} /><small>{new Date(item.expires_at).toLocaleString()}</small>{item.state === "pending" && <button className="danger" onClick={() => void api.revokeTenantInvitation(item).then(refresh)}>撤销</button>}</div>)}</div></section></div>
  </div>;
}

function SettingsConsole({ tenant, notify }: { tenant: TenantSummary; notify: Props["notify"] }) {
  const [effective, setEffective] = useState<TenantEffectiveSettings | null>(null);
  const [versions, setVersions] = useState<TenantConfigurationVersion[]>([]);
  const [usage, setUsage] = useState<TenantUsage[]>([]);
  const [locale, setLocale] = useState("zh-CN");
  const [timeZone, setTimeZone] = useState("Asia/Shanghai");
  const [retentionDays, setRetentionDays] = useState(365);
  const [warningPercent, setWarningPercent] = useState(80);
  const [chunkSize, setChunkSize] = useState(512);
  const [chunkOverlap, setChunkOverlap] = useState(64);
  const [ocrMode, setOcrMode] = useState("disabled");
  const [ocrMinTextChars, setOcrMinTextChars] = useState(32);
  const [ocrProviderId, setOcrProviderId] = useState("");

  const refresh = useCallback(async () => {
    const [settingsResult, versionResult, usageResult] = await Promise.all([
      api.tenantEffectiveSettings(), api.tenantConfigurationVersions(), api.tenantUsage(),
    ]);
    setEffective(settingsResult); setVersions(versionResult.items); setUsage(usageResult.items);
    setLocale(String(settingsResult.config.locale ?? "zh-CN"));
    setTimeZone(String(settingsResult.config.time_zone ?? "Asia/Shanghai"));
    const retention = settingsResult.config.retention as Record<string, unknown> | undefined;
    const quotas = settingsResult.config.quotas as Record<string, unknown> | undefined;
    const ingestion = settingsResult.config.ingestion as Record<string, unknown> | undefined;
    setRetentionDays(Number(retention?.content_days ?? 365));
    setWarningPercent(Number(quotas?.warning_percent ?? 80));
    setChunkSize(Number(ingestion?.chunk_size ?? 512));
    setChunkOverlap(Number(ingestion?.chunk_overlap ?? 64));
    setOcrMode(String(ingestion?.ocr_mode ?? "disabled"));
    setOcrMinTextChars(Number(ingestion?.ocr_min_text_chars ?? 32));
    setOcrProviderId(String(ingestion?.ocr_provider_id ?? ""));
  }, []);
  useEffect(() => {
    const timer = window.setTimeout(() => {
      void refresh().catch((error: Error) => notify({ tone: "error", text: error.message }));
    }, 0);
    return () => window.clearTimeout(timer);
  }, [refresh, notify, tenant.id]);

  async function save(event: FormEvent) {
    event.preventDefault(); if (!effective) return;
    const retention = { ...(effective.config.retention as Record<string, unknown>), content_days: retentionDays };
    const quotas = { ...(effective.config.quotas as Record<string, unknown>), warning_percent: warningPercent };
    const ingestion = {
      ...(effective.config.ingestion as Record<string, unknown>),
      chunk_size: chunkSize,
      chunk_overlap: chunkOverlap,
      ocr_mode: ocrMode,
      ocr_min_text_chars: ocrMinTextChars,
      ocr_provider_id: ocrProviderId || null,
    };
    try { await api.createTenantConfigurationVersion({ ...effective.config, locale, time_zone: timeZone, retention, quotas, ingestion }, effective.tenant_revision, true); await refresh(); notify({ tone: "ok", text: "新配置版本已验证并激活。" }); }
    catch (error) { notify({ tone: "error", text: `配置校验失败：${(error as Error).message}` }); }
  }
  async function rollback(version: TenantConfigurationVersion) {
    if (!effective || !window.confirm(`将版本 ${version.version} 复制为新的活动版本？`)) return;
    try { await api.rollbackTenantConfiguration(version.id, effective.tenant_revision); await refresh(); notify({ tone: "ok", text: "回滚已创建为新的不可变版本。" }); }
    catch (error) { notify({ tone: "error", text: (error as Error).message }); }
  }
  return <div className="content-stack"><section className="panel"><div className="panel-title"><div><h2>租户设置</h2><p>保存会创建完整、不可变的新版本；界面仅显示密钥绑定标识，不显示密钥值。</p></div><span className="mono">{effective?.config_hash.slice(0, 12)}</span></div><form className="settings-grid" onSubmit={save}><label>语言地区<input value={locale} onChange={(event) => setLocale(event.target.value)} required /></label><label>IANA 时区<input value={timeZone} onChange={(event) => setTimeZone(event.target.value)} required /></label><label>内容保留天数<input type="number" min="1" max="3650" value={retentionDays} onChange={(event) => setRetentionDays(Number(event.target.value))} /></label><label>配额预警阈值（%）<input type="number" min="1" max="100" value={warningPercent} onChange={(event) => setWarningPercent(Number(event.target.value))} /></label><label>切分窗口（token）<input type="number" min="32" max="8192" value={chunkSize} onChange={(event) => setChunkSize(Number(event.target.value))} /><small>默认 512；任务提交后固定使用当时版本。</small></label><label>切分重叠（token）<input type="number" min="0" max="2048" value={chunkOverlap} onChange={(event) => setChunkOverlap(Number(event.target.value))} /><small>必须小于切分窗口，默认 64。</small></label><label>扫描页 OCR<select value={ocrMode} onChange={(event) => setOcrMode(event.target.value)}><option value="disabled">关闭</option><option value="best_effort">尽力识别，失败保留原页</option><option value="required">必须成功，否则任务失败</option></select></label><label>OCR 最低文本阈值<input type="number" min="0" max="100000" value={ocrMinTextChars} onChange={(event) => setOcrMinTextChars(Number(event.target.value))} /></label><label>OCR Provider 标识<input value={ocrProviderId} onChange={(event) => setOcrProviderId(event.target.value)} placeholder="required 模式必填" /><small>只填写已注册的 Provider 标识，不填写密钥。</small></label><button className="primary" disabled={!effective}>验证并激活新版本</button></form></section>
    <section className="panel"><div className="panel-title"><div><h2>配额与用量</h2><p>预留量会在昂贵操作开始前计入；黄色项目已达到租户预警阈值。</p></div><button onClick={() => void api.reconcileTenantUsage().then(refresh)}>重新核对</button></div><div className="usage-grid">{usage.map((item) => { const total = item.used + item.reserved; const ratio = item.limit > 0 ? Math.min(100, total * 100 / item.limit) : 0; return <div className={`usage-card ${item.warning ? "warning" : ""}`} key={item.resource}><div><strong>{item.resource}</strong><span>{total.toLocaleString()} / {item.limit.toLocaleString()}</span></div><div className="usage-bar"><i style={{ width: `${ratio}%` }} /></div><small>已用 {item.used.toLocaleString()} · 预留 {item.reserved.toLocaleString()}</small></div>; })}</div></section>
    <section className="panel"><div className="panel-title"><h2>版本历史</h2></div><div className="timeline">{versions.map((version) => <div className="timeline-item" key={version.id}><div className="timeline-dot" /><div><strong>版本 {version.version}</strong> <Status value={version.state} /><p className="mono">{version.config_hash}</p>{version.validation_errors.length > 0 && <p className="error-text">{version.validation_errors.join("；")}</p>}</div><button disabled={version.id === effective?.configuration_version_id} onClick={() => void rollback(version)}>回滚到此版本</button></div>)}</div></section></div>;
}

export default function TenantAdministration(props: Props) {
  const [section, setSection] = useState<Section>(props.platformAdministrator ? "platform" : "members");
  const canAdminister = props.context?.roles.includes("administrator") ?? false;
  const active = props.tenant?.state === "active";
  return <div className="tenant-admin-layout"><div className="admin-tabs" role="tablist">
    {props.platformAdministrator && <button className={section === "platform" ? "active" : ""} onClick={() => setSection("platform")}>平台租户</button>}
    <button className={section === "members" ? "active" : ""} disabled={!canAdminister} title={!canAdminister ? "需要租户管理员角色" : ""} onClick={() => setSection("members")}>成员与权限</button>
    <button className={section === "settings" ? "active" : ""} disabled={!canAdminister} title={!canAdminister ? "需要租户管理员角色" : ""} onClick={() => setSection("settings")}>设置与配额</button>
  </div>
  {section === "platform" && props.platformAdministrator && <PlatformConsole notify={props.notify} refreshDiscovery={props.refreshDiscovery} />}
  {section !== "platform" && !props.tenant && <div className="panel empty-state">当前账号没有可管理的租户。平台管理员仍可在平台控制台创建或恢复租户。</div>}
  {section !== "platform" && props.tenant && !active && <div className={`panel tenant-state-card ${props.tenant.state}`}><Status value={props.tenant.state} /><h2>{props.tenant.name} 当前不可进入租户数据面</h2><p>{props.tenant.state === "provisioning" ? "租户仍在开通中，激活后才能管理数据。" : props.tenant.state === "suspended" ? "租户已暂停，问答、摄取和管理写操作均被拒绝。" : "租户已归档，只可由平台管理员恢复。"}</p></div>}
  {section === "members" && props.tenant && active && canAdminister && <ErrorBoundary message="无法加载成员管理"><MembersConsole tenant={props.tenant} notify={props.notify} /></ErrorBoundary>}
  {section === "settings" && props.tenant && active && canAdminister && <ErrorBoundary message="无法加载租户设置"><SettingsConsole tenant={props.tenant} notify={props.notify} /></ErrorBoundary>}
  </div>;
}

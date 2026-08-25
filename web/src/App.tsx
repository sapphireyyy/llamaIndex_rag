import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  setTenantBoundary,
  streamQuery,
  type Assistant,
  type AssistantVersion,
  type Citation,
  type DocumentItem,
  type EvaluationDataset,
  type EvaluationRun,
  type HealthResponse,
  type Job,
  type Source,
  type Space,
  type TenantContext,
  type TenantSummary,
} from "./api";
import TenantAdministration from "./TenantAdministration";
import "./styles.css";

type View = "chat" | "spaces" | "assistants" | "quality" | "administration";
type Notice = { tone: "ok" | "error"; text: string } | null;

function StatusPill({ value }: { value: string }) {
  return <span className={`pill pill-${value}`}>{value}</span>;
}

function SpacesView({ notify }: { notify: (notice: Notice) => void }) {
  const [spaces, setSpaces] = useState<Space[]>([]);
  const [sources, setSources] = useState<Source[]>([]);
  const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [selected, setSelected] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [principal, setPrincipal] = useState("group:employees");
  const [preview, setPreview] = useState("");

  const refresh = useCallback(async () => {
    const [spaceResult, documentResult, jobResult] = await Promise.all([
      api.spaces(), api.documents(), api.jobs(),
    ]);
    setSpaces(spaceResult.items);
    setDocuments(documentResult.items);
    setJobs(jobResult.items);
    const spaceId = selected || spaceResult.items[0]?.id || "";
    setSelected(spaceId);
    setSources(spaceId ? (await api.sources(spaceId)).items : []);
  }, [selected]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void refresh().catch((error: Error) => notify({ tone: "error", text: error.message }));
    }, 0);
    return () => window.clearTimeout(timer);
  }, [refresh, notify]);

  async function create(event: FormEvent) {
    event.preventDefault();
    try {
      const space = await api.createSpace(name, description);
      setName(""); setDescription(""); setSelected(space.id);
      notify({ tone: "ok", text: "知识空间已创建" });
      await refresh();
    } catch (error) { notify({ tone: "error", text: (error as Error).message }); }
  }

  async function selectSpace(id: string) {
    setSelected(id);
    setSources((await api.sources(id)).items);
  }

  async function ensureUploadSource(): Promise<string> {
    const existing = sources.find((source) => source.kind === "upload" && source.enabled);
    if (existing) return existing.id;
    const created = await api.createSource(selected, `manual-${Date.now()}`);
    setSources((current) => [...current, created]);
    return created.id;
  }

  async function upload(file: File | undefined) {
    if (!file || !selected) return;
    try {
      const sourceId = await ensureUploadSource();
      const job = await api.upload(sourceId, file);
      notify({ tone: job.status === "succeeded" ? "ok" : "error", text: `摄取任务：${job.stage}` });
      await refresh();
    } catch (error) { notify({ tone: "error", text: (error as Error).message }); }
  }

  async function editSelected() {
    if (!selectedSpace) return;
    const nextName = window.prompt("知识空间名称", selectedSpace.name);
    if (!nextName) return;
    const nextDescription = window.prompt("用途说明", selectedSpace.description) ?? "";
    await api.updateSpace(selectedSpace.id, { name: nextName, description: nextDescription });
    notify({ tone: "ok", text: "知识空间已更新" });
    await refresh();
  }

  async function showPreview(item: DocumentItem) {
    const [content, versions] = await Promise.all([
      api.previewDocument(item.id), api.documentVersions(item.id),
    ]);
    setPreview(
      `版本历史\n${versions.items.map((version) => `${version.active ? "●" : "○"} ${version.id} · ${version.state}`).join("\n")}\n\n正文预览\n${content.items.map((chunk) => chunk.text).join("\n\n")}`,
    );
  }

  const selectedSpace = spaces.find((item) => item.id === selected);
  return (
    <div className="page-grid">
      <section className="panel side-panel">
        <div className="panel-title"><h2>知识空间</h2><span>{spaces.length}</span></div>
        <form className="stack" onSubmit={create}>
          <input value={name} onChange={(event) => setName(event.target.value)} placeholder="空间名称" required />
          <textarea value={description} onChange={(event) => setDescription(event.target.value)} placeholder="用途说明" />
          <button className="primary" type="submit">新建空间</button>
        </form>
        <div className="item-list">
          {spaces.map((space) => (
            <button className={`list-item ${space.id === selected ? "selected" : ""}`} key={space.id} onClick={() => void selectSpace(space.id)}>
              <strong>{space.name}</strong><StatusPill value={space.state} />
              <small>{space.description || "暂无说明"}</small>
            </button>
          ))}
        </div>
      </section>
      <div className="content-stack">
        <section className="panel">
          <div className="panel-title">
            <div><h2>{selectedSpace?.name ?? "请选择知识空间"}</h2><p>成员变化即时刷新授权纪元，归档后不参与检索。</p></div>
            {selectedSpace && <div className="actions"><button onClick={() => void editSelected()}>编辑</button><button onClick={() => void api.spaceAction(selectedSpace.id, selectedSpace.state === "active" ? "archive" : "restore").then(refresh)}>{selectedSpace.state === "active" ? "归档" : "恢复"}</button></div>}
          </div>
          {selectedSpace && (
            <div className="form-row">
              <input value={principal} onChange={(event) => setPrincipal(event.target.value)} aria-label="成员主体" />
              <select id="member-role" defaultValue="reader"><option value="reader">读取者</option><option value="editor">编辑者</option><option value="administrator">管理员</option></select>
              <button onClick={() => void api.setMembership(selected, principal, (document.getElementById("member-role") as HTMLSelectElement).value).then(() => notify({ tone: "ok", text: "成员权限已更新" }))}>分配成员</button>
            </div>
          )}
        </section>
        <section className="panel">
          <div className="panel-title"><div><h2>文档与摄取</h2><p>先暂存并验证双索引，成功后才切换活动版本。</p></div><label className="upload-button">上传文档<input type="file" onChange={(event) => void upload(event.target.files?.[0])} /></label></div>
          <div className="table-wrap"><table><thead><tr><th>文档</th><th>状态</th><th>活动版本</th><th>操作</th></tr></thead><tbody>
            {documents.filter((item) => !selected || item.knowledge_space_id === selected).map((item) => <tr key={item.id}><td>{item.title}</td><td><StatusPill value={item.state} /></td><td className="mono">{item.active_version_id?.slice(-10) ?? "—"}</td><td className="actions"><button onClick={() => void showPreview(item)}>版本与预览</button><button className="danger" onClick={() => void api.deleteDocument(item.id).then(refresh)}>删除</button></td></tr>)}
          </tbody></table></div>
          {preview && <div className="preview"><button className="close" onClick={() => setPreview("")}>×</button><pre>{preview}</pre></div>}
        </section>
        <section className="panel compact"><div className="panel-title"><h2>最近任务</h2></div><div className="job-grid">{jobs.slice(0, 6).map((job) => <div className="job-card" key={job.id}><StatusPill value={job.status} /><strong>{job.stage}</strong><small>尝试 {job.attempt_count}/{job.max_attempts ?? 3}</small>{job.error_message && <p>{job.error_message}</p>}{job.status === "failed" && <button onClick={() => void api.retryJob(job.id).then(refresh)}>重试</button>}{["queued", "running"].includes(job.status) && <button onClick={() => void api.cancelJob(job.id).then(refresh)}>取消</button>}</div>)}</div></section>
      </div>
    </div>
  );
}

function AssistantsView({ notify }: { notify: (notice: Notice) => void }) {
  const [assistants, setAssistants] = useState<Assistant[]>([]);
  const [spaces, setSpaces] = useState<Space[]>([]);
  const [history, setHistory] = useState<AssistantVersion[]>([]);
  const [selected, setSelected] = useState("");
  const [spaceId, setSpaceId] = useState("");
  const [name, setName] = useState("");

  const refresh = useCallback(async () => {
    const [assistantResult, spaceResult] = await Promise.all([api.assistants(), api.spaces()]);
    setAssistants(assistantResult.items); setSpaces(spaceResult.items);
    const assistantId = selected || assistantResult.items[0]?.id || "";
    setSelected(assistantId); setSpaceId((current) => current || spaceResult.items[0]?.id || "");
    setHistory(assistantId ? (await api.assistantHistory(assistantId)).items : []);
  }, [selected]);
  useEffect(() => {
    const timer = window.setTimeout(() => {
      void refresh().catch((error: Error) => notify({ tone: "error", text: error.message }));
    }, 0);
    return () => window.clearTimeout(timer);
  }, [refresh, notify]);

  async function create(event: FormEvent) {
    event.preventDefault();
    try {
      const result = await api.createAssistant(name, spaceId);
      const validation = await api.validateAssistant(result.id, result.draft.id);
      if (!validation.valid) throw new Error(validation.errors.join("；"));
      await api.activateAssistant(result.id, result.draft.id);
      setName(""); setSelected(result.id); notify({ tone: "ok", text: "助手版本已校验并激活" }); await refresh();
    } catch (error) { notify({ tone: "error", text: (error as Error).message }); }
  }

  async function newDraft() {
    const draft = await api.createAssistantVersion(selected, [spaceId]);
    const validation = await api.validateAssistant(selected, draft.id);
    notify({ tone: validation.valid ? "ok" : "error", text: validation.valid ? "草稿校验通过，可激活" : validation.errors.join("；") });
    await refresh();
  }

  async function activate(versionId: string) {
    try { await api.activateAssistant(selected, versionId); notify({ tone: "ok", text: "版本已激活；历史版本仍可审计与回滚" }); await refresh(); }
    catch (error) { notify({ tone: "error", text: (error as Error).message }); }
  }

  return <div className="page-grid">
    <section className="panel side-panel"><div className="panel-title"><h2>助手</h2><span>{assistants.length}</span></div><form className="stack" onSubmit={create}><input value={name} onChange={(event) => setName(event.target.value)} placeholder="助手名称" required /><select value={spaceId} onChange={(event) => setSpaceId(event.target.value)}>{spaces.filter((space) => space.state === "active").map((space) => <option value={space.id} key={space.id}>{space.name}</option>)}</select><button className="primary" type="submit">创建并激活</button></form><div className="item-list">{assistants.map((assistant) => <button className={`list-item ${selected === assistant.id ? "selected" : ""}`} key={assistant.id} onClick={() => { setSelected(assistant.id); void api.assistantHistory(assistant.id).then((result) => setHistory(result.items)); }}><strong>{assistant.name}</strong><small className="mono">{assistant.active_version_id?.slice(-10) ?? "未激活"}</small></button>)}</div></section>
    <div className="content-stack"><section className="panel"><div className="panel-title"><div><h2>组件化配置</h2><p>提示词、检索、引用、拒答、护栏和供应商都以不可变版本快照绑定。</p></div><button disabled={!selected || !spaceId} onClick={() => void newDraft()}>新建草稿</button></div><div className="component-grid">{["提示词", "模型", "检索", "引用", "拒答", "护栏"].map((label) => <div className="component-card" key={label}><small>{label}</small><strong>租户默认配置</strong><span>版本化 · 已校验</span></div>)}</div></section><section className="panel"><div className="panel-title"><h2>版本历史与回滚</h2></div><div className="timeline">{history.map((version) => <div className="timeline-item" key={version.id}><div className="timeline-dot" /><div><strong>版本 {version.version}</strong> <StatusPill value={version.state} /><p className="mono">{version.id}</p>{version.validation_errors.length > 0 && <p className="error-text">{version.validation_errors.join("；")}</p>}</div><button onClick={() => void activate(version.id)}>{version.state === "active" ? "重新校验并回滚" : "激活"}</button></div>)}</div></section></div>
  </div>;
}

type ChatMessage = { role: "user" | "assistant"; text: string; status?: string; citations?: Citation[] };

function ChatView({ notify }: { notify: (notice: Notice) => void }) {
  const [assistants, setAssistants] = useState<Assistant[]>([]);
  const [assistantId, setAssistantId] = useState("");
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [answerId, setAnswerId] = useState("");
  const [question, setQuestion] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [sourcePreview, setSourcePreview] = useState("");
  useEffect(() => { void api.assistants().then((result) => { setAssistants(result.items); setAssistantId(result.items[0]?.id ?? ""); }); }, []);

  async function ask(event: FormEvent) {
    event.preventDefault(); if (!assistantId || !question.trim() || streaming) return;
    const userText = question.trim(); setQuestion(""); setStreaming(true);
    setMessages((current) => [...current, { role: "user", text: userText }, { role: "assistant", text: "", citations: [] }]);
    try {
      await streamQuery(assistantId, userText, conversationId, (eventName, data) => {
        if (eventName === "query") { setConversationId(String(data.conversation_id)); setAnswerId(String(data.answer_id)); }
        if (eventName === "answer") setMessages((current) => current.map((item, index) => index === current.length - 1 ? { ...item, text: item.text + String(data.delta) } : item));
        if (eventName === "citation") setMessages((current) => current.map((item, index) => index === current.length - 1 ? { ...item, citations: [...(item.citations ?? []), data as unknown as Citation] } : item));
        if (eventName === "terminal") setMessages((current) => current.map((item, index) => index === current.length - 1 ? { ...item, status: String(data.status) } : item));
      });
    } catch (error) { notify({ tone: "error", text: (error as Error).message }); }
    finally { setStreaming(false); }
  }

  async function openCitation(citation: Citation) {
    const source = await api.source(citation.source_url);
    const preview = await api.previewDocument(source.document_id);
    setSourcePreview(`${source.title}${source.page ? ` · 第 ${source.page} 页` : ""}\n\n${preview.items.map((item) => item.text).join("\n\n")}`);
  }

  return <div className="chat-layout"><section className="chat-panel"><div className="chat-toolbar"><select value={assistantId} onChange={(event) => { setAssistantId(event.target.value); setConversationId(null); setMessages([]); }}>{assistants.map((assistant) => <option key={assistant.id} value={assistant.id}>{assistant.name}</option>)}</select><span>{conversationId ? "连续对话 · 每轮重新授权检索" : "新对话"}</span></div><div className="messages">{messages.length === 0 && <div className="welcome"><div className="welcome-icon">⌁</div><h2>从授权知识中获得可追溯答案</h2><p>答案没有足够证据时会明确拒答；来源冲突时会并列展示。</p></div>}{messages.map((message, index) => <article className={`message ${message.role}`} key={index}><div className="avatar">{message.role === "user" ? "你" : "AI"}</div><div className="bubble"><p>{message.text || (streaming ? "正在检索与核验…" : "")}</p>{message.status && <StatusPill value={message.status} />}<div className="citations">{message.citations?.map((citation) => <button key={citation.id} onClick={() => void openCitation(citation)}>{citation.label}{citation.page ? ` · p.${citation.page}` : ""}</button>)}</div></div></article>)}</div><form className="composer" onSubmit={ask}><textarea value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="询问企业政策、IT 帮助或产品资料…" onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} /><button className="primary" disabled={streaming || !assistantId}>{streaming ? "生成中" : "发送"}</button></form>{answerId && <div className="feedback-bar">这个答案有帮助吗？<button onClick={() => void api.feedback(answerId, 5, "helpful").then(() => notify({ tone: "ok", text: "反馈已记录" }))}>有帮助</button><button onClick={() => void api.feedback(answerId, 1, "needs improvement").then(() => notify({ tone: "ok", text: "反馈已记录" }))}>需改进</button></div>}</section>{sourcePreview && <aside className="source-drawer"><button className="close" onClick={() => setSourcePreview("")}>×</button><pre>{sourcePreview}</pre></aside>}</div>;
}

function QualityView({ notify }: { notify: (notice: Notice) => void }) {
  const [datasets, setDatasets] = useState<EvaluationDataset[]>([]);
  const [runs, setRuns] = useState<EvaluationRun[]>([]);
  const [assistants, setAssistants] = useState<Assistant[]>([]);
  const [detail, setDetail] = useState<Record<string, unknown> | null>(null);
  const [selectedRun, setSelectedRun] = useState("");
  const [feedback, setFeedback] = useState<Array<Record<string, unknown>>>([]);
  const sample = useMemo(() => JSON.stringify([{ id: "sample-1", question: "示例问题", expected_source_scope: [], expected_answerability: false, access_context: { groups: ["administrators"], roles: ["administrator"] } }], null, 2), []);
  const [items, setItems] = useState(sample);
  const refresh = useCallback(async () => { const [datasetResult, runResult, assistantResult, feedbackResult] = await Promise.all([api.datasets(), api.runs(), api.assistants(), api.feedbackList()]); setDatasets(datasetResult.items); setRuns(runResult.items); setAssistants(assistantResult.items); setFeedback(feedbackResult.items); }, []);
  useEffect(() => {
    const timer = window.setTimeout(() => { void refresh(); }, 0);
    return () => window.clearTimeout(timer);
  }, [refresh]);
  async function createDataset() { try { await api.createDataset(`通用评测集 ${new Date().toLocaleDateString()}`, JSON.parse(items) as unknown[]); notify({ tone: "ok", text: "不可变评测集版本已创建" }); await refresh(); } catch (error) { notify({ tone: "error", text: (error as Error).message }); } }
  async function runEvaluation() { const dataset = datasets[0]; const assistant = assistants.find((item) => item.active_version_id); if (!dataset || !assistant?.active_version_id) return; try { const run = await api.createRun(dataset.active_version_id, assistant.active_version_id); notify({ tone: "ok", text: `评测完成：${run.status}` }); await refresh(); } catch (error) { notify({ tone: "error", text: (error as Error).message }); } }
  const gateContainer = detail?.gates as { gates?: Array<{ gate_id: string; metric: string; passed: boolean; overridden: boolean }> } | undefined;
  const failedGates = (gateContainer?.gates ?? []).filter((gate) => !gate.passed && !gate.overridden);
  async function override(gateId: string) { const reason = window.prompt("请输入覆盖原因（会写入审计记录）"); if (!reason || !selectedRun) return; await api.overrideGate(gateId, selectedRun, reason); setDetail(await api.run(selectedRun)); notify({ tone: "ok", text: "门禁覆盖已审计" }); }
  return <div className="quality-grid"><section className="panel"><div className="panel-title"><div><h2>评测集</h2><p>题目、预期来源、可回答性和访问上下文按版本冻结。</p></div><button className="primary" onClick={() => void createDataset()}>保存新版本</button></div><textarea className="code-editor" value={items} onChange={(event) => setItems(event.target.value)} /><div className="summary-row"><span>{datasets.length} 个评测集</span><button disabled={!datasets.length || !assistants.some((item) => item.active_version_id)} onClick={() => void runEvaluation()}>运行当前候选</button></div></section><section className="panel"><div className="panel-title"><div><h2>运行与门禁</h2><p>质量、延迟和访问控制分别评分。</p></div><button onClick={() => void api.createGate("access_control", 1).then(() => notify({ tone: "ok", text: "强制访问控制门禁已创建" }))}>新增安全门禁</button></div><div className="run-list">{runs.map((run) => <button key={run.id} onClick={() => { setSelectedRun(run.id); void api.run(run.id).then(setDetail); }}><div><strong className="mono">{run.id.slice(-10)}</strong><StatusPill value={run.status} /></div><div className="metrics">{Object.entries(run.aggregates).slice(0, 4).map(([key, value]) => <span key={key}>{key}<b>{Number(value).toFixed(2)}</b></span>)}</div></button>)}</div><div className="feedback-review"><h3>用户反馈</h3>{feedback.slice(0, 5).map((item) => <p key={String(item.id)}><strong>{String(item.rating ?? "—")} / 5</strong> {String(item.comment ?? item.category)}</p>)}</div></section>{detail && <section className="panel detail-panel"><button className="close" onClick={() => setDetail(null)}>×</button><h2>失败分析与门禁详情</h2>{failedGates.map((gate) => <button className="danger" key={gate.gate_id} onClick={() => void override(gate.gate_id)}>覆盖 {gate.metric} 门禁</button>)}<pre>{JSON.stringify(detail, null, 2)}</pre></section>}</div>;
}

export function LegacyApp() {
  const [view, setView] = useState<Exclude<View, "administration">>("chat");
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [notice, setNotice] = useState<Notice>(null);
  const notify = useCallback((value: Notice) => { setNotice(value); if (value) window.setTimeout(() => setNotice(null), 4200); }, []);
  useEffect(() => { void api.health().then(setHealth).catch((error: Error) => notify({ tone: "error", text: error.message })); }, [notify]);
  const titles: Record<Exclude<View, "administration">, [string, string]> = { chat: ["可追溯智能问答", "基于当前授权范围检索，每个事实答案都附带可访问来源。"], spaces: ["知识空间与文档", "管理内容边界、成员权限、数据来源和摄取版本。"], assistants: ["助手配置", "组合策略和供应商组件，校验后以不可变版本激活。"], quality: ["质量评测与发布门禁", "用固定语料比较候选版本，并阻止质量或安全回退。"] };
  return <div className="app-shell"><aside className="sidebar"><div className="brand"><span className="brand-mark">ER</span><div><strong>Enterprise RAG</strong><small>可信知识助手</small></div></div><nav>{([ ["chat", "问答", "⌁"], ["spaces", "知识", "▤"], ["assistants", "配置", "◈"], ["quality", "质量", "◎"] ] as const).map(([key, label, icon]) => <button key={key} className={view === key ? "active" : ""} onClick={() => setView(key)}><span>{icon}</span>{label}</button>)}</nav><div className={`system-status ${health?.ready ? "ready" : "degraded"}`}><i /><div><strong>{health?.ready ? "系统就绪" : "状态检查中"}</strong><small>{health ? `API ${health.version}` : "正在连接服务"}</small></div></div></aside><main><header className="topbar"><div><h1>{titles[view][0]}</h1><p>{titles[view][1]}</p></div><div className="tenant-chip"><span>开发租户</span><strong>admin-demo</strong></div></header><div className="workspace">{view === "chat" && <ChatView notify={notify} />}{view === "spaces" && <SpacesView notify={notify} />}{view === "assistants" && <AssistantsView notify={notify} />}{view === "quality" && <QualityView notify={notify} />}</div></main>{notice && <div className={`toast ${notice.tone}`}>{notice.text}</div>}</div>;
}

export default function App() {
  const [view, setView] = useState<View>("chat");
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [notice, setNotice] = useState<Notice>(null);
  const [tenants, setTenants] = useState<TenantSummary[]>([]);
  const [selectedTenantId, setSelectedTenantId] = useState("");
  const [tenantContext, setTenantContext] = useState<TenantContext | null>(null);
  const [platformRoles, setPlatformRoles] = useState<string[]>([]);
  const [tenantGeneration, setTenantGeneration] = useState(0);
  const notify = useCallback((value: Notice) => {
    setNotice(value);
    if (value) window.setTimeout(() => setNotice(null), 4200);
  }, []);

  const selectTenant = useCallback(async (tenantId: string, available: TenantSummary[]) => {
    setTenantBoundary(tenantId);
    setSelectedTenantId(tenantId);
    window.localStorage.setItem("enterprise-rag.selected-tenant", tenantId);
    setTenantContext(null);
    setTenantGeneration((current) => current + 1);
    const tenant = available.find((item) => item.id === tenantId);
    if (tenant?.state === "active" && tenant.membership_state === "active") {
      try { setTenantContext(await api.tenantContext()); }
      catch (error) { notify({ tone: "error", text: (error as Error).message }); }
    }
  }, [notify]);

  const refreshDiscovery = useCallback(async () => {
    const result = await api.discoverTenants();
    setTenants(result.items);
    setPlatformRoles(result.platform_roles);
    const remembered = window.localStorage.getItem("enterprise-rag.selected-tenant") ?? "";
    const next = result.items.some((item) => item.id === remembered)
      ? remembered
      : result.items.find((item) => item.state === "active")?.id ?? result.items[0]?.id ?? "";
    await selectTenant(next, result.items);
  }, [selectTenant]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void Promise.all([
        api.health().then(setHealth),
        refreshDiscovery(),
      ]).catch((error: Error) => notify({ tone: "error", text: error.message }));
    }, 0);
    return () => window.clearTimeout(timer);
  }, [notify, refreshDiscovery]);

  const selectedTenant = tenants.find((tenant) => tenant.id === selectedTenantId) ?? null;
  const platformAdministrator = platformRoles.includes("platform_administrator");
  const activeTenant = selectedTenant?.state === "active" && tenantContext !== null;
  const tenantAdministrator = tenantContext?.roles.includes("administrator") ?? false;
  const titles: Record<View, [string, string]> = {
    chat: ["可追溯智能问答", "只从当前租户与当前权限可访问的知识中生成答案。"],
    spaces: ["知识空间与文档", "管理内容边界、数据来源、摄取版本和空间权限。"],
    assistants: ["助手配置", "组合策略、供应商和知识空间，并以不可变版本激活。"],
    quality: ["质量评测与发布门禁", "用固定语料验证质量、安全性和访问控制。"],
    administration: ["多租户管理", "管理租户生命周期、成员权限、配置版本和配额。"],
  };
  const navItems: Array<[View, string, string, boolean]> = [
    ["chat", "问答", "问", activeTenant],
    ["spaces", "知识", "知", activeTenant],
    ["assistants", "助手", "助", activeTenant && tenantAdministrator],
    ["quality", "质量", "质", activeTenant && tenantAdministrator],
    ["administration", "租户管理", "管", platformAdministrator || tenantAdministrator],
  ];

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><span className="brand-mark">ER</span><div><strong>Enterprise RAG</strong><small>可信企业知识助手</small></div></div>
      <nav>{navItems.filter(([, , , visible]) => visible).map(([key, label, icon]) => <button key={key} className={view === key ? "active" : ""} onClick={() => setView(key)}><span>{icon}</span>{label}</button>)}</nav>
      <div className={`system-status ${health?.ready ? "ready" : "degraded"}`}><i /><div><strong>{health?.ready ? "系统就绪" : "系统受限"}</strong><small>{health ? `API ${health.version}` : "正在连接服务"}</small></div></div>
    </aside>
    <main><header className="topbar"><div><h1>{titles[view][0]}</h1><p>{titles[view][1]}</p></div><div className="tenant-switcher"><label htmlFor="tenant-selector">当前租户</label><select id="tenant-selector" value={selectedTenantId} onChange={(event) => void selectTenant(event.target.value, tenants)} disabled={tenants.length === 0}><option value="">{tenants.length ? "请选择租户" : "无可用租户"}</option>{tenants.map((tenant) => <option key={tenant.id} value={tenant.id}>{tenant.name} · {tenant.state}</option>)}</select>{selectedTenant && <div><StatusPill value={selectedTenant.state} /><span>{tenantContext?.roles.join(" / ") ?? selectedTenant.membership_state ?? "无成员关系"}</span></div>}</div></header>
      <div className="workspace" key={tenantGeneration}>
        {!activeTenant && view !== "administration" && <section className={`panel tenant-state-card ${selectedTenant?.state ?? "unauthorized"}`}><StatusPill value={selectedTenant?.state ?? "no-membership"} /><h2>{selectedTenant ? `${selectedTenant.name} 当前不可访问` : "当前账号没有可访问的租户"}</h2><p>{selectedTenant?.state === "provisioning" ? "租户仍在开通中。" : selectedTenant?.state === "suspended" ? "租户已暂停，数据面请求会被拒绝。" : selectedTenant?.state === "archived" ? "租户已归档。" : "请联系平台管理员分配有效的租户成员关系。"}</p>{platformAdministrator && <button className="primary" onClick={() => setView("administration")}>进入平台租户控制台</button>}</section>}
        {activeTenant && view === "chat" && <ChatView notify={notify} />}
        {activeTenant && view === "spaces" && <SpacesView notify={notify} />}
        {activeTenant && view === "assistants" && tenantAdministrator && <AssistantsView notify={notify} />}
        {activeTenant && view === "quality" && tenantAdministrator && <QualityView notify={notify} />}
        {view === "administration" && <TenantAdministration tenant={selectedTenant} context={tenantContext} platformAdministrator={platformAdministrator} notify={notify} refreshDiscovery={refreshDiscovery} />}
      </div>
    </main>
    {notice && <div className={`toast ${notice.tone}`} role="status">{notice.text}</div>}
  </div>;
}

import { useState, type FormEvent } from "react";
import { ArrowUpRight, Check, CircleAlert, Coins, Eye, FlaskConical, GitBranch, Globe2,
  History, ListChecks, Play, ReceiptText, RefreshCw, Square, type LucideIcon } from "lucide-react";
import { fmt, type ResearchCallReceipt, type ResearchEvent, type ResearchModules,
  type ResearchPlanResponse, type ResearchRun } from "../api";
import { useResearchRun, type ResearchSnapshot } from "../hooks/useResearchRun";
import { isResearchActive, RESEARCH_MACRO_SERIES, RESEARCH_QUESTIONS, RESEARCH_STATUS, researchDraft, researchPlanIssues,
  researchPlanRequest, researchResultKind, researchValue, type ResearchDraft, type ResearchMode } from "./researchModel";
import "./research.css";

const MODES: { id: ResearchMode; label: string; icon: LucideIcon }[] = [
  { id: "gold", label: "黄金", icon: Coins }, { id: "macro", label: "宏观", icon: Globe2 },
  { id: "factor", label: "因子", icon: FlaskConical }, { id: "combined", label: "联合", icon: GitBranch },
];

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function sourceUrl(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  try { return ["https:", "http:"].includes(new URL(value).protocol) ? value : undefined; }
  catch { return undefined; }
}

function Fields({ values }: { values: Record<string, unknown> }) {
  return <dl className="research-fields">{Object.entries(values).map(([key, value]) =>
    <div key={key}><dt>{key}</dt><dd>{researchValue(value)}</dd></div>)}</dl>;
}

export function ResearchBudget({ run }: { run: ResearchRun }) {
  return <dl className="research-budget" aria-label="运行预算">
    <div><dt>调用</dt><dd>{researchValue(run.usage.calls_used)} / {researchValue(run.usage.max_calls)}</dd></div>
    <div><dt>Token</dt><dd>{researchValue(run.usage.tokens_used)} / {researchValue(run.usage.max_tokens)}</dd></div>
    <div><dt>预留 Token</dt><dd>{researchValue(run.usage.tokens_reserved)}</dd></div>
  </dl>;
}

export function ResearchCalls({ calls }: { calls: ResearchCallReceipt[] }) {
  if (!calls.length) return <p className="research-empty">暂无调用收据</p>;
  return <div className="research-receipts">{[...calls].sort((left, right) => left.sequence - right.sequence).map((call) =>
    <details key={call.call_id} className="research-receipt">
      <summary><span className="research-mono">#{call.sequence} {call.role}</span><span>{call.provider} / {call.model}</span><span>{call.status}</span></summary>
      <Fields values={{ call_id: call.call_id, purpose: call.purpose, used_tokens: call.used_tokens,
        reservation_tokens: call.reservation_tokens, input_tokens: call.response?.input_tokens,
        output_tokens: call.response?.output_tokens, raw_request_id: call.response?.raw_request_id,
        error: call.error, parse_error: call.parse_error }} />
      <h4>请求</h4><pre>{researchValue(call.messages)}</pre>
      <h4>响应</h4><pre>{researchValue(call.response?.content)}</pre>
    </details>)}</div>;
}

export function ResearchEvents({ events }: { events: ResearchEvent[] }) {
  if (!events.length) return <p className="research-empty">暂无服务器事件</p>;
  return <ol className="research-events">{[...events].sort((left, right) => Date.parse(left.at) - Date.parse(right.at)
    || left.run_id.localeCompare(right.run_id) || left.sequence - right.sequence).map((event) =>
    <li key={`${event.run_id}-${event.sequence}`}><span className="research-event-sequence">{event.sequence}</span><div>
      <strong>{event.event_type}</strong><span className="research-stage">{event.stage}</span>
      <div className="research-mono">{event.run_id}</div>
      <time dateTime={event.at}>{event.at}</time>
      <details><summary>事件数据</summary><pre>{researchValue(event.data)}</pre></details>
    </div></li>)}</ol>;
}

export function ResearchPlanPreview({ response, issues = [], disabled = false, submitted = false, onRun }: {
  response: ResearchPlanResponse; issues?: string[]; disabled?: boolean; submitted?: boolean; onRun: () => void;
}) {
  const { plan } = response;
  return <section className="research-plan" aria-label="计划预览">
    <div className="research-section-head"><h2><ListChecks size={17} />计划预览</h2><span>{plan.source} / {plan.mode}</span></div>
    <p className="research-mono">{plan.plan_id}</p>
    <Fields values={{ 问题: plan.question, 知识截止: plan.knowledge_cutoff, 调用上限: plan.budget.max_calls,
      Token上限: plan.budget.max_tokens, 超时秒数: plan.budget.timeout_seconds, 调用并行上限: plan.budget.max_parallel_calls }} />
    <ol className="research-plan-tasks">{plan.tasks.map((task) => <li key={task.task_id}>
      <div><strong className="research-mono">{task.task_type}</strong><span>{task.required ? "必需" : "可选"}</span></div>
      <small>{task.task_id}</small>
      <p>依赖：{task.depends_on.length ? task.depends_on.join(", ") : "无"}</p>
      {Object.entries(task.inputs).map(([field, input]) => <p className="research-mono" key={field}>{field} = {input.task_id}.{input.output_key}</p>)}
      <p>{task.budget.max_calls} calls / {task.budget.max_tokens} tokens</p>
      <details><summary>任务参数</summary><pre>{researchValue(task.payload)}</pre></details>
    </li>)}</ol>
    {response.artifact_id && <p className="research-mono">{response.artifact_id}</p>}
    {!!response.receipts?.length && <details><summary>规划收据</summary><ResearchCalls calls={response.receipts} /></details>}
    {issues.map((issue) => <p className="research-notice" key={issue}>{issue}</p>)}
    <button type="button" className="research-primary" disabled={disabled || submitted || issues.length > 0} onClick={onRun}>
      <Play size={15} />{submitted ? "已提交本计划" : "确认运行"}
    </button>
  </section>;
}

export function ResearchResult({ run, children = [] }: { run: ResearchRun; children?: ResearchRun[] }) {
  const result = run.result;
  if (!result) return <p className="research-empty">{isResearchActive(run) ? "结果尚未返回" : "未生成结果"}</p>;
  const kind = researchResultKind(run);
  if (!kind) return <div className="research-notice" role="status">结果不可用：不支持的结果版本 <code>{run.task_type} / {run.result_schema_version}</code></div>;
  const payload = result.payload;
  const publication = result.publication;
  const withheld = publication.decision === "WITHHELD";
  const candidate = record(payload.candidate);
  const evaluation = record(payload.evaluation);
  const metrics = record(record(payload.factor_run).metrics);
  const facts = kind === "macro_research" ? record(payload.facts) : payload;
  const indicators = record(facts.indicators);
  return <div className="research-result">
    <div className="research-section-head"><h3>{result.outcome}</h3><span className="research-publication" data-decision={publication.decision}>{publication.decision}</span></div>
    {!withheld && result.summary && <p>{result.summary}</p>}
    {publication.reason && <p className={withheld ? "research-notice" : "research-muted"}>{publication.reason}</p>}
    {kind === "root" && <>
      <ul className="research-summaries">{(payload.summaries as unknown[]).map((item, index) => {
        const summary = record(item);
        const child = children.find((entry) => summary.run_id ? entry.run_id === summary.run_id : entry.task_id && entry.task_id === summary.task_id);
        const childPublication = child?.result?.publication;
        const canSummarize = !withheld && !run.cancel_requested && child?.status === "COMPLETED" && researchResultKind(child)
          && (childPublication?.decision === "NOT_APPLICABLE" || (childPublication?.decision === "PASS"
            && Boolean(childPublication.content_hash) && childPublication.content_hash === summary.approved_publication_hash));
        return <li key={index}><strong>{researchValue(summary.task_type ?? summary.task_id)}</strong>
          <span>{researchValue(summary.status)} / {researchValue(summary.outcome)}</span>
          {canSummarize && <p>{child?.result?.summary}</p>}
          {summary.error != null && <p className="research-error">{researchValue(summary.error)}</p>}
          {typeof summary.approved_publication_hash === "string" && <p className="research-mono">{summary.approved_publication_hash}</p>}
        </li>;
      })}</ul>
      {Array.isArray(payload.approved_publication_hashes) && <details><summary>approved_publication_hashes</summary>
        <pre>{researchValue(payload.approved_publication_hashes.filter((value) => typeof value === "string"))}</pre></details>}
    </>}
    {kind === "gold" && <>
      <Fields values={{ 标的: evaluation.symbol, 证据日期: evaluation.as_of,
        研究结论: payload.outcome, 快照: record(payload.analysis_context).snapshot_id }} />
      {!withheld && <p>{researchValue(record(payload.proposal).hypothesis)}</p>}
      <a className="research-link" href="/gold">黄金研究<ArrowUpRight size={15} /></a>
    </>}
    {kind === "factor" && <>
      <h3>{researchValue(candidate.name)}</h3><pre>{researchValue(candidate.expression)}</pre>
      {!withheld && <p>{researchValue(candidate.hypothesis)}</p>}
      <Fields values={{ 实验状态: record(payload.experiment).state, "Rank IC": fmt.num(metrics.rank_ic, 4),
        ICIR: fmt.num(metrics.icir, 3), 覆盖率: fmt.pct(metrics.coverage), 换手率: fmt.pct(metrics.turnover) }} />
      <dl className="research-fields">{Object.entries(record(payload.local_gates)).map(([name, passed]) =>
        <div key={name}><dt>{name}</dt><dd>{passed === true ? "PASS" : passed === false ? "FAIL" : "未提供"}</dd></div>)}</dl>
      {!withheld && ["critic", "statistician", "decision"].map((role) => <p key={role}><strong>{role} </strong>{researchValue(record(payload[role]).rationale)}</p>)}
    </>}
    {(kind === "macro" || kind === "macro_research") && <>
      <p className="research-mono">{researchValue(facts.snapshot_id ?? payload.snapshot_id)}</p>
      <div className="research-table-wrap"><table className="research-indicators"><thead><tr><th>序列</th><th>最新值 / 单位</th><th>观测日期</th><th>可用时间</th><th>变化</th><th>来源</th></tr></thead>
        <tbody>{Object.entries(indicators).map(([series, raw]) => {
          const indicator = record(raw);
          const url = sourceUrl(indicator.source_url);
          return <tr key={series}><th scope="row">{researchValue(indicator.title ?? series)}<small>{series}</small></th>
            <td>{researchValue(indicator.latest_value)}<small>{researchValue(indicator.unit)}</small></td>
            <td>{researchValue(indicator.observation_date)}</td><td>{researchValue(indicator.available_at)}</td>
            <td>{Object.entries(indicator).filter(([key]) => key.startsWith("change")).map(([key, value]) => <div key={key}>{key}: {researchValue(value)}</div>)}</td>
            <td>{url ? <a href={url} target="_blank" rel="noreferrer">来源<ArrowUpRight size={13} /></a> : "未提供"}</td></tr>;
        })}</tbody></table></div>
      {!Object.keys(indicators).length && <p className="research-empty">暂无指标</p>}
      <Fields values={{ 覆盖: facts.coverage, 缺失: facts.missing, 限制: facts.limitations }} />
      {facts.observations_metadata != null && <details><summary>观测元数据</summary><pre>{researchValue(facts.observations_metadata)}</pre></details>}
      {kind === "macro_research" && !withheld && <>
        <Fields values={{ 解读状态: payload.interpretation_status, 综合状态: record(payload.synthesis).state }} />
        {!!Object.keys(record(payload.perspectives)).length && <section aria-label="独立观点"><h3>独立观点</h3>
          {Object.entries(record(payload.perspectives)).map(([role, raw]) => {
            const perspective = record(raw);
            return <section key={role} className="research-perspective"><h4>{role}</h4>
              <Fields values={{ 状态: perspective.state }} /><p>{researchValue(perspective.summary)}</p>
              {Array.isArray(perspective.claims) && <ul>{perspective.claims.map((rawClaim, index) => {
                const claim = record(rawClaim);
                return <li key={index}><p>{researchValue(claim.claim)}</p><Fields values={{ 证据引用: claim.evidence_ids }} /></li>;
              })}</ul>}
              <Fields values={{ 缺失: perspective.missing, 风险: perspective.risks }} />
            </section>;
          })}</section>}
      </>}
    </>}
    {kind === "collection" && <details open><summary>采集记录</summary><pre>{researchValue(payload.collection ?? payload)}</pre></details>}
    <Fields values={{ 结果版本: run.result_schema_version, 知识截止: run.knowledge_cutoff,
      报告: result.report_artifact_id, 发布哈希: publication.content_hash }} />
    {!!result.evidence.length && <details><summary>证据引用 ({result.evidence.length})</summary>{result.evidence.map((evidence, index) =>
      <Fields key={`${evidence.artifact_id}-${index}`} values={{ artifact_id: evidence.artifact_id, content_hash: evidence.content_hash,
        namespace: evidence.namespace, schema_version: evidence.schema_version, record_key: evidence.record_key }} />)}</details>}
    {!!result.limitations.length && <ul className="research-limitations">{result.limitations.map((limitation, index) => <li key={index}>{limitation}</li>)}</ul>}
  </div>;
}

export function ResearchRunView({ snapshot, pending, onCancel }: {
  snapshot: ResearchSnapshot; pending: boolean; onCancel: () => void;
}) {
  const [selectedId, setSelectedId] = useState(snapshot.run.run_id);
  const [tab, setTab] = useState("result");
  const runs = [snapshot.run, ...snapshot.children];
  const selected = runs.find((run) => run.run_id === selectedId) ?? snapshot.run;
  const audit = snapshot.audit.find((entry) => entry.run_id === selected.run_id);
  const tabs = [{ id: "result", label: "结果", icon: Eye }, { id: "events", label: "事件", icon: ListChecks }, { id: "calls", label: "收据", icon: ReceiptText }];
  return <section className="research-run" aria-label="研究运行">
    <div className="research-section-head"><h2>运行详情</h2>
      <button className="research-icon-button" type="button" onClick={onCancel} disabled={pending || !isResearchActive(snapshot.run) || snapshot.run.cancel_requested}
        title={snapshot.run.cancel_requested ? "已请求取消" : "取消运行"} aria-label={snapshot.run.cancel_requested ? "已请求取消" : "取消运行"}><Square size={15} /></button>
    </div>
    {snapshot.run.cancel_requested && isResearchActive(snapshot.run) && <p className="research-notice">已请求取消，等待服务器确认</p>}
    <nav className="research-run-tree" aria-label="根任务与子任务">{runs.map((run) => <button type="button" key={run.run_id}
      data-child={Boolean(run.parent_run_id)} aria-pressed={selected.run_id === run.run_id} onClick={() => setSelectedId(run.run_id)}>
      <span><strong>{run.parent_run_id ? run.task_type : "根任务"}</strong><small className="research-mono">{run.task_id ?? run.run_id}</small></span>
      <span className="research-status" data-status={run.status}>{RESEARCH_STATUS[run.status] ?? run.status}</span><span className="research-stage">{run.stage}</span>
    </button>)}</nav>
    <div className="research-run-detail">
      <Fields values={{ run_id: selected.run_id, 任务: selected.task_type, 模块: selected.module_id,
        更新于: selected.updated_at, 修订: selected.revision }} />
      <ResearchBudget run={selected} />
      {selected.error && <p className="research-error" role="alert">{selected.error}</p>}
      <div className="research-tabs" role="tablist" aria-label="运行视图">{tabs.map(({ id, label, icon: Icon }, index) =>
        <button type="button" key={id} data-tab={id} id={`research-tab-${id}`} role="tab" aria-selected={tab === id}
          aria-controls={`research-panel-${id}`} tabIndex={tab === id ? 0 : -1} onClick={() => setTab(id)} onKeyDown={(event) => {
            const next = event.key === "ArrowRight" ? tabs[(index + 1) % tabs.length] : event.key === "ArrowLeft" ? tabs[(index + tabs.length - 1) % tabs.length]
              : event.key === "Home" ? tabs[0] : event.key === "End" ? tabs[tabs.length - 1] : null;
            if (next) { event.preventDefault(); setTab(next.id); event.currentTarget.parentElement?.querySelector<HTMLButtonElement>(`[data-tab="${next.id}"]`)?.focus(); }
          }}><Icon size={14} />{label}{id === "events" ? ` (${audit?.events.length ?? 0})` : id === "calls" ? ` (${audit?.calls.length ?? 0})` : ""}</button>)}</div>
      <div role="tabpanel" id={`research-panel-${tab}`} aria-labelledby={`research-tab-${tab}`}>
        {tab === "result" ? <ResearchResult run={selected} children={snapshot.children} /> : tab === "events" ? <ResearchEvents events={audit?.events ?? []} /> : <ResearchCalls calls={audit?.calls ?? []} />}
      </div>
    </div>
  </section>;
}

function ResearchCatalog({ modules, mode }: { modules: ResearchModules; mode: ResearchMode }) {
  const tasks = modules.modules.filter((task) => mode === "combined" ? ["gold", "macro"].includes(task.module_id) : task.module_id === mode);
  return <details className="research-catalog"><summary>任务目录 ({tasks.length})</summary>
    <Fields values={{ 任务并行上限: modules.max_parallel_tasks, 调用并行上限: modules.max_parallel_calls }} />
    {!tasks.length && <p className="research-notice">模块未注册</p>}
    {tasks.map((task) => <div className="research-catalog-task" key={task.task_type}>
      <div><strong>{task.task_type}</strong><span>{task.ready ? <Check size={13} /> : <CircleAlert size={13} />}{task.ready ? "就绪" : "未就绪"}</span></div>
      <p>{task.title}</p><small>{task.version} / {task.result_schema_version}</small>
      {!task.ready && <p className="research-notice">{task.unavailable_reason || task.missing_roles.join(", ") || "未就绪"}</p>}
      <p>{(Array.isArray(task.roles) ? task.roles : Object.keys(task.roles)).join(", ")}</p>
      <details><summary>请求约束</summary><pre>{researchValue(task.request_schema)}</pre></details>
    </div>)}
  </details>;
}

export default function Research() {
  const research = useResearchRun();
  const [draft, setDraft] = useState<ResearchDraft>(() => researchDraft());
  const [inputError, setInputError] = useState("");
  const locked = Boolean(research.pending);
  const selectedSeries = new Set(draft.seriesIds.split(/[\s,]+/).filter(Boolean));
  const issues = research.plan && research.modules ? researchPlanIssues(research.plan.plan, research.modules) : [];
  function update<Key extends keyof ResearchDraft>(key: Key, value: ResearchDraft[Key]) {
    setDraft((previous) => ({ ...previous, [key]: value }));
    setInputError("");
    research.discardPlan();
  }
  function chooseMode(mode: ResearchMode) {
    setDraft((previous) => ({ ...previous, mode, question: RESEARCH_QUESTIONS[mode] }));
    setInputError("");
    research.discardPlan();
  }
  function preview(event: FormEvent) {
    event.preventDefault();
    try { void research.preview(researchPlanRequest(draft)); }
    catch (caught) { setInputError(caught instanceof Error ? caught.message : String(caught)); }
  }
  return <div className="research-page">
    <header className="research-header"><h1><FlaskConical size={23} />研究</h1>
      <div className="research-header-actions"><span role="status">{research.error ? "同步已停止" : research.loading ? "读取中" : research.polling ? "同步中" : "已同步"}</span>
        <button type="button" className="research-icon-button" title="重新查询" aria-label="重新查询" disabled={locked} onClick={research.reload}><RefreshCw size={16} /></button>
      </div>
    </header>
    {(research.error || research.actionError || inputError) && <div className="research-error" role="alert">{research.error || research.actionError || inputError}</div>}
    {research.modules?.busy && <div className="research-notice research-busy">研究槽位占用中
      {research.modules.active_run_id && <button type="button" onClick={() => research.selectRun(research.modules!.active_run_id!)}><Eye size={14} />当前运行</button>}
    </div>}
    <div className="research-workspace">
      <div className="research-input">
        <form onSubmit={preview}>
          <fieldset disabled={locked} className="research-modes"><legend>模块</legend>{MODES.map(({ id, label, icon: Icon }) =>
            <label key={id}><input type="radio" name="research-mode" checked={draft.mode === id} onChange={() => chooseMode(id)} /><span><Icon size={15} />{label}</span></label>)}</fieldset>
          <fieldset disabled={locked} className="research-form-fields">
            <label>研究问题<textarea required maxLength={1000} value={draft.question} onChange={(event) => update("question", event.target.value)} /></label>
            <label className="research-check" title="模型规划会调用模型并产生费用；关闭时使用规则规划">
              <input type="checkbox" checked={draft.useModel} onChange={(event) => update("useModel", event.target.checked)} />
              <span>模型规划</span><CircleAlert size={14} aria-hidden="true" />
            </label>
            {(draft.mode === "gold" || draft.mode === "combined") && <>
              <div className="research-form-row"><label>标的<select value={draft.symbol} onChange={(event) => update("symbol", event.target.value)}>
                <option value="Au99.99">Au99.99 现货</option>{["518880", "518800", "159934", "159937"].map((symbol) => <option key={symbol} value={symbol}>{symbol} ETF</option>)}</select></label>
                <label>仓位上限 (%)<input type="number" required min="0" max="30" step="0.01" value={draft.maxWeight} onChange={(event) => update("maxWeight", event.target.value)} /></label></div>
              <label>研究重点<textarea required maxLength={1000} value={draft.focus} onChange={(event) => update("focus", event.target.value)} /></label>
            </>}
            {draft.mode !== "gold" && <div className="research-form-row">
              <label>开始日期<input type="date" required value={draft.startDate} max={draft.endDate || undefined} onChange={(event) => update("startDate", event.target.value)} /></label>
              <label>结束日期<input type="date" required value={draft.endDate} min={draft.startDate || undefined} onChange={(event) => update("endDate", event.target.value)} /></label>
            </div>}
            {(draft.mode === "macro" || draft.mode === "combined") && <fieldset className="research-series"><legend>宏观序列</legend>
              {RESEARCH_MACRO_SERIES.map((series) => <label className="research-check" key={series.id}>
                <input type="checkbox" checked={selectedSeries.has(series.id)} onChange={(event) => update("seriesIds", event.target.checked
                  ? [...selectedSeries, series.id].join(" ") : [...selectedSeries].filter((id) => id !== series.id).join(" "))} />
                <span>{series.label}<small className="research-mono">{series.id}</small></span>
              </label>)}
            </fieldset>}
            {draft.mode === "factor" && <>
              <label>因子课题<textarea required maxLength={1000} value={draft.topic} onChange={(event) => update("topic", event.target.value)} /></label>
              <label>预测目标<select value={draft.target} onChange={(event) => update("target", event.target.value)}>
                <option value="NEXT_OPEN_TO_CLOSE">次日开盘至收盘</option><option value="NEXT_CLOSE_TO_CLOSE">收盘至收盘</option><option value="NEXT_OPEN_TO_NEXT_OPEN">开盘至开盘</option>
              </select></label>
              <label>数据模式<select value={draft.dataMode} onChange={(event) => update("dataMode", event.target.value as ResearchDraft["dataMode"])}>
                <option value="fixture">fixture</option><option value="frozen_snapshot">frozen_snapshot</option></select></label>
              {draft.dataMode === "frozen_snapshot" && <label>冻结快照 ID<input required value={draft.snapshotId} onChange={(event) => update("snapshotId", event.target.value)} /></label>}
            </>}
          </fieldset>
          <button type="submit" disabled={locked || !research.modules || Boolean(research.error)}><Eye size={15} />{research.pending === "preview" ? "规划中" : "预览计划"}</button>
        </form>
        {research.modules && <ResearchCatalog modules={research.modules} mode={draft.mode} />}
        {research.plan && <ResearchPlanPreview response={research.plan} issues={issues} submitted={research.submitted}
          disabled={locked || Boolean(research.error) || Boolean(research.modules?.busy || research.modules?.active_run_id)} onRun={() => { void research.start(); }} />}
      </div>
      <div className="research-output" aria-busy={research.loading}>
        {research.snapshot ? <ResearchRunView key={research.snapshot.run.run_id} snapshot={research.snapshot} pending={locked} onCancel={() => { void research.cancel(); }} />
          : <section className="research-empty-state"><ListChecks size={25} /><h2>{research.loading ? "读取运行记录" : "暂无研究运行"}</h2></section>}
      </div>
    </div>
    <section className="research-history" aria-label="运行历史">
      <div className="research-section-head"><h2><History size={16} />运行历史</h2><span>{research.history.length}</span></div>
      {!research.history.length && <p className="research-empty">{research.loading ? "读取中" : "暂无记录"}</p>}
      <div className="research-history-list">{research.history.map((run) => <button key={run.run_id} type="button" disabled={locked}
        aria-pressed={research.snapshot?.run.run_id === run.run_id} onClick={() => research.selectRun(run.run_id)}>
        <span><strong>{researchValue(run.request.question ?? record(run.request.plan).question ?? run.task_type)}</strong><small className="research-mono">{run.run_id}</small></span>
        <span className="research-status" data-status={run.status}>{RESEARCH_STATUS[run.status] ?? run.status}</span><time dateTime={run.created_at}>{run.created_at}</time>
      </button>)}</div>
    </section>
  </div>;
}

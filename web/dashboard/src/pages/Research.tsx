import { FormEvent, useState } from "react";
import {
  Bot,
  BrainCircuit,
  Calculator,
  CheckCircle2,
  CircleAlert,
  FileCheck2,
  FlaskConical,
  GitMerge,
  Lightbulb,
  Play,
  ScanSearch,
  ShieldCheck,
  Sparkles,
  type LucideIcon,
} from "lucide-react";
import {
  AgentResearchRequest,
  AgentResearchResult,
  Artifact,
  Experiment,
  api,
  fmt,
} from "../api";
import DataTable, { Column } from "../components/DataTable";
import { useAsync } from "../hooks";

const ARTIFACT_TYPES = [
  "", "DATA_SNAPSHOT", "UNIVERSE", "FEATURE_SET", "MODEL", "SIGNAL",
  "PORTFOLIO_TARGET", "BACKTEST_SPEC", "BACKTEST_RESULT", "VALIDATION_REPORT",
  "STRATEGY_PACKAGE", "DEPLOYMENT", "RESEARCH_REPORT",
];

type WorkflowStageId =
  | "researcher"
  | "critic"
  | "factor_lab"
  | "statistician"
  | "local_gates"
  | "orchestrator"
  | "report";

type WorkflowStage = {
  id: WorkflowStageId;
  label: string;
  eyebrow: string;
  description: string;
  icon: LucideIcon;
};

const WORKFLOW_STAGES: WorkflowStage[] = [
  { id: "researcher", label: "Researcher", eyebrow: "IDEA", description: "提出结构化因子候选与经济假设", icon: Lightbulb },
  { id: "critic", label: "Critic", eyebrow: "REVIEW", description: "审查 PIT 安全、DSL 风险与过拟合", icon: ScanSearch },
  { id: "factor_lab", label: "FactorLab", eyebrow: "LOCAL", description: "本地确定性计算 IC、覆盖率与换手", icon: Calculator },
  { id: "statistician", label: "Statistician", eyebrow: "EVIDENCE", description: "只解释本地引擎已经产生的指标", icon: FlaskConical },
  { id: "local_gates", label: "Local gates", eyebrow: "HARD GATE", description: "本地门槛拥有最终否决权，LLM 不可覆盖", icon: ShieldCheck },
  { id: "orchestrator", label: "Orchestrator", eyebrow: "DECISION", description: "汇总证据并给出候选晋级建议", icon: GitMerge },
  { id: "report", label: "Research report", eyebrow: "ARTIFACT", description: "固化调用轨迹、结论与可追溯产物", icon: FileCheck2 },
];

export default function Research() {
  const [refresh, setRefresh] = useState(0);
  const { data: experiments, error: expError } = useAsync(() => api.experiments(), [refresh]);
  const { data: agentStatus, error: statusError } = useAsync(() => api.agentResearchStatus(), []);
  const [request, setRequest] = useState<AgentResearchRequest>({
    topic: "寻找一个可解释、低换手且严格避免未来数据的 A 股量价因子",
    universe_id: "cn_a_all",
    start_date: "2024-02-01",
    end_date: "2024-05-31",
    target: "NEXT_OPEN_TO_CLOSE",
  });
  const [agentResult, setAgentResult] = useState<AgentResearchResult | null>(null);
  const [selectedStageId, setSelectedStageId] = useState<WorkflowStageId>("researcher");
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState("");
  const [typeFilter, setTypeFilter] = useState("");
  const { data: artifacts } = useAsync(() => api.artifacts(typeFilter || undefined), [typeFilter]);
  const [lineageId, setLineageId] = useState<string | null>(null);
  const { data: lineage } = useAsync(
    () => (lineageId ? api.lineage(lineageId) : Promise.resolve(null)),
    [lineageId]
  );

  const selectedStage = WORKFLOW_STAGES.find((stage) => stage.id === selectedStageId) ?? WORKFLOW_STAGES[0];
  const selectedRoute = selectedStageId in (agentStatus?.roles ?? {})
    ? agentStatus?.roles[selectedStageId]
    : undefined;
  const selectedCalls = agentResult?.calls.filter((call) => call.role === selectedStageId) ?? [];
  const selectedTokens = selectedCalls.reduce(
    (total, call) => total + (call.input_tokens ?? 0) + (call.output_tokens ?? 0),
    0
  );
  const totalTokens = agentResult?.calls.reduce(
    (total, call) => total + (call.input_tokens ?? 0) + (call.output_tokens ?? 0),
    0
  ) ?? 0;

  function stageStatus(stageId: WorkflowStageId) {
    if (!agentResult) return running ? (stageId === "researcher" ? "active" : "queued") : "ready";
    if (stageId === "critic") return agentResult.critic.approved ? "complete" : "warning";
    if (stageId === "statistician") return agentResult.statistician.robust ? "complete" : "warning";
    if (stageId === "local_gates") return Object.values(agentResult.local_gates).every(Boolean) ? "complete" : "warning";
    if (stageId === "orchestrator") return agentResult.decision.recommend_candidate ? "complete" : "warning";
    return "complete";
  }

  function stageOutcome(stageId: WorkflowStageId) {
    if (!agentResult) return selectedRoute ? `${selectedRoute.provider} · ${selectedRoute.model}` : "等待研究运行";
    if (stageId === "researcher") return agentResult.candidate.hypothesis;
    if (stageId === "critic") return agentResult.critic.rationale;
    if (stageId === "factor_lab") return `Rank IC ${fmt.num(agentResult.factor_run.metrics.rank_ic, 4)} · 覆盖率 ${fmt.pct(agentResult.factor_run.metrics.coverage)}`;
    if (stageId === "statistician") return agentResult.statistician.rationale;
    if (stageId === "local_gates") return Object.entries(agentResult.local_gates).map(([key, passed]) => `${key} ${passed ? "PASS" : "FAIL"}`).join(" · ");
    if (stageId === "orchestrator") return agentResult.decision.rationale;
    return agentResult.report_artifact_id;
  }

  async function runAgents(event: FormEvent) {
    event.preventDefault();
    setRunning(true);
    setRunError("");
    try {
      const result = await api.runAgentResearch(request);
      setAgentResult(result);
      setRefresh((value) => value + 1);
    } catch (caught) {
      setRunError(caught instanceof Error ? caught.message : "多智能体研究运行失败");
    } finally {
      setRunning(false);
    }
  }

  const expCols: Column<Experiment>[] = [
    { key: "experiment_id", title: "ID", render: (r) => <span className="mono">{r.experiment_id}</span> },
    { key: "name", title: "名称" },
    { key: "state", title: "状态", render: (r) => <span className="badge">{r.state}</span> },
    { key: "description", title: "描述", render: (r) => <span className="muted">{r.description || "-"}</span> },
    { key: "updated_at", title: "更新于", render: (r) => (r.updated_at ?? "").slice(0, 19) },
  ];

  const artCols: Column<Artifact>[] = [
    {
      key: "artifact_id",
      title: "Artifact",
      render: (r) => (
        <a className="mono" style={{ color: "var(--accent)", cursor: "pointer" }} onClick={() => setLineageId(r.artifact_id)}>
          {r.artifact_id}
        </a>
      ),
    },
    { key: "artifact_type", title: "类型" },
    { key: "producer", title: "产生者", render: (r) => <span className="muted">{r.producer}</span> },
    { key: "created_at", title: "创建于", render: (r) => (r.created_at ?? "").slice(0, 19) },
    { key: "content_hash", title: "内容哈希", render: (r) => <span className="mono muted">{r.content_hash ? r.content_hash.slice(0, 12) + "…" : "-"}</span> },
    { key: "status", title: "状态" },
  ];

  return (
    <div className="agent-research-page">
      <header className="agent-header">
        <div>
          <span className="eyebrow"><Bot size={13} /> RESEARCH PLANE</span>
          <h1>多智能体研究</h1>
          <p>四角色协作提出与审查因子，本地确定性引擎负责最终验证。</p>
        </div>
        <span className={`agent-ready ${agentStatus?.ready ? "ready" : ""}`}>
          {agentStatus?.ready ? <CheckCircle2 size={15} /> : <CircleAlert size={15} />}
          {agentStatus?.ready ? "模型已就绪" : "等待模型凭据"}
        </span>
      </header>

      {(expError || statusError || runError) && <div className="error-box">{expError || statusError || runError}</div>}

      <section className={`workflow-board ${running ? "is-running" : ""}`} aria-label="多智能体研究工作流">
        <div className="workflow-board-head">
          <div>
            <span className="workflow-kicker"><BrainCircuit size={14} /> AGENT WORKFLOW</span>
            <h2>研究协作拓扑</h2>
          </div>
          <div className="workflow-run-summary">
            <span><i className={agentStatus?.ready ? "online" : ""} />{agentStatus?.ready ? "SYSTEM READY" : "CREDENTIALS MISSING"}</span>
            <span>{agentResult ? `${agentResult.calls.length} CALLS · ${totalTokens.toLocaleString()} TOKENS` : `BUDGET · ${agentStatus?.budget.max_llm_calls_per_run ?? "-"} CALLS`}</span>
          </div>
        </div>

        <div className="workflow-workspace">
          <div className="workflow-map">
            <button className="workflow-origin" type="button" onClick={() => setSelectedStageId("researcher")}>
              <Sparkles size={16} />
              <span><small>RESEARCH BRIEF</small><strong>{request.topic}</strong></span>
              <b>{running ? "RUNNING" : agentResult ? agentResult.experiment.state : "READY"}</b>
            </button>

            <div className="workflow-spine" aria-hidden="true"><span /></div>

            <div className="workflow-agent-row">
              {WORKFLOW_STAGES.slice(0, 4).map((stage, index) => {
                const Icon = stage.icon;
                const route = agentStatus?.roles[stage.id];
                return (
                  <div className="workflow-agent-step" key={stage.id}>
                    <button
                      className={`workflow-node ${stageStatus(stage.id)} ${selectedStageId === stage.id ? "selected" : ""}`}
                      type="button"
                      onClick={() => setSelectedStageId(stage.id)}
                    >
                      <span className="workflow-node-top"><small>{stage.eyebrow}</small><i /></span>
                      <Icon size={18} />
                      <strong>{stage.label}</strong>
                      <span>{route ? `${route.provider} · ${route.model}` : stage.id === "factor_lab" ? "deterministic engine" : "route pending"}</span>
                    </button>
                    {index < 3 && <span className="workflow-arrow" aria-hidden="true">→</span>}
                  </div>
                );
              })}
            </div>

            <div className="workflow-merge" aria-hidden="true"><span /><i /></div>

            <div className="workflow-final-row">
              {WORKFLOW_STAGES.slice(4).map((stage, index) => {
                const Icon = stage.icon;
                const route = agentStatus?.roles[stage.id];
                return (
                  <div className="workflow-final-step" key={stage.id}>
                    <button
                      className={`workflow-node wide ${stageStatus(stage.id)} ${selectedStageId === stage.id ? "selected" : ""}`}
                      type="button"
                      onClick={() => setSelectedStageId(stage.id)}
                    >
                      <span className="workflow-node-top"><small>{stage.eyebrow}</small><i /></span>
                      <Icon size={18} />
                      <strong>{stage.label}</strong>
                      <span>{route ? `${route.provider} · ${route.model}` : stage.id === "report" ? "immutable artifact" : "local authority"}</span>
                    </button>
                    {index < 2 && <span className="workflow-arrow" aria-hidden="true">→</span>}
                  </div>
                );
              })}
            </div>
          </div>

          <aside className="workflow-inspector">
            <div className="workflow-inspector-title">
              <span>{selectedStage.eyebrow}</span>
              <i className={stageStatus(selectedStage.id)}>{stageStatus(selectedStage.id)}</i>
            </div>
            <h3>{selectedStage.label}</h3>
            <p>{selectedStage.description}</p>
            <dl>
              <div><dt>runtime</dt><dd>{selectedRoute ? `${selectedRoute.provider} / ${selectedRoute.model}` : selectedStage.id === "factor_lab" || selectedStage.id === "local_gates" ? "local / deterministic" : "system"}</dd></div>
              <div><dt>calls</dt><dd>{selectedCalls.length || (agentResult && !selectedRoute ? 1 : 0)}</dd></div>
              <div><dt>tokens</dt><dd>{selectedRoute ? selectedTokens.toLocaleString() : "N/A"}</dd></div>
            </dl>
            <div className="workflow-inspector-output">
              <small>LATEST OUTPUT</small>
              <span>{stageOutcome(selectedStage.id)}</span>
            </div>
          </aside>
        </div>

        <div className="workflow-log" aria-label="工作流日志">
          <div><span>session log</span><i /></div>
          <p><time>00:00:01</time><b>system</b><span>role routes loaded · local gates locked</span></p>
          <p><time>00:00:02</time><b>research</b><span>{running ? "workflow in progress · waiting for structured responses" : agentResult ? `${agentResult.trace_id} · ${agentResult.experiment.state}` : "standing by for research brief"}</span></p>
          <p><time>00:00:03</time><b>artifact</b><span>{agentResult ? `${agentResult.report_artifact_id} · persisted` : "report will be written after quality gates"}</span></p>
        </div>
      </section>

      <section className="agent-console">
        <form onSubmit={runAgents}>
          <div className="section-heading">
            <div><h2>研究任务</h2><p>一次运行生成一个 DSL 候选，所有模型调用均受预算限制。</p></div>
            <ShieldCheck size={19} />
          </div>
          <label>研究主题<textarea required minLength={3} maxLength={500} value={request.topic} onChange={(event) => setRequest({ ...request, topic: event.target.value })} /></label>
          <div className="agent-run-fields">
            <label>开始日期<input type="date" value={request.start_date} onChange={(event) => setRequest({ ...request, start_date: event.target.value })} /></label>
            <label>结束日期<input type="date" value={request.end_date} onChange={(event) => setRequest({ ...request, end_date: event.target.value })} /></label>
            <label>预测目标<select value={request.target} onChange={(event) => setRequest({ ...request, target: event.target.value })}><option value="NEXT_OPEN_TO_CLOSE">次日开盘至收盘</option><option value="NEXT_CLOSE_TO_CLOSE">收盘至收盘</option><option value="NEXT_OPEN_TO_NEXT_OPEN">开盘至开盘</option></select></label>
          </div>
          <button className="primary agent-run-button" disabled={running || !agentStatus?.ready}>
            <Play size={15} /> {running ? "四个 Agent 协作中…" : "启动研究闭环"}
          </button>
        </form>
        <div className="agent-role-list">
          {Object.entries(agentStatus?.roles ?? {}).map(([role, route], index) => (
            <div key={role}><span>{index + 1}</span><div><strong>{role}</strong><small>{route.provider} · {route.model}</small></div></div>
          ))}
        </div>
      </section>

      {agentResult && <section className="agent-result">
        <div className="section-heading"><div><h2>{agentResult.candidate.name}</h2><p className="mono">{agentResult.trace_id}</p></div><span className={`badge ${agentResult.experiment.state === "CANDIDATE" ? "ok" : "err"}`}>{agentResult.experiment.state}</span></div>
        <div className="agent-expression mono">{agentResult.candidate.expression}</div>
        <p>{agentResult.candidate.hypothesis}</p>
        <div className="agent-metrics">
          <div><span>Rank IC</span><strong>{fmt.num(agentResult.factor_run.metrics.rank_ic, 4)}</strong></div>
          <div><span>ICIR</span><strong>{fmt.num(agentResult.factor_run.metrics.icir, 3)}</strong></div>
          <div><span>覆盖率</span><strong>{fmt.pct(agentResult.factor_run.metrics.coverage)}</strong></div>
          <div><span>换手率</span><strong>{fmt.pct(agentResult.factor_run.metrics.turnover)}</strong></div>
        </div>
        <div className="agent-verdicts">
          <div><strong>Critic</strong><span>{agentResult.critic.rationale}</span></div>
          <div><strong>Statistician</strong><span>{agentResult.statistician.rationale}</span></div>
          <div><strong>Orchestrator</strong><span>{agentResult.decision.rationale}</span></div>
          <div><strong>Local gates</strong><span>{Object.entries(agentResult.local_gates).map(([key, passed]) => `${key}: ${passed ? "PASS" : "FAIL"}`).join(" · ")}</span></div>
        </div>
      </section>}

      <h2>实验（生命周期状态机）</h2>
      <div className="panel">
        <DataTable
          columns={expCols}
          rows={experiments ?? []}
          rowKey={(r) => r.experiment_id}
          empty="暂无实验。可通过 API POST /api/experiments 创建。"
        />
      </div>

      <h2>Artifacts</h2>
      <div className="toolbar">
        <select value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)}>
          {ARTIFACT_TYPES.map((t) => (
            <option key={t} value={t}>{t || "全部类型"}</option>
          ))}
        </select>
        <span className="muted">点击 Artifact ID 查看血缘</span>
      </div>
      <div className="panel">
        <DataTable columns={artCols} rows={artifacts ?? []} rowKey={(r) => r.artifact_id} />
      </div>

      {lineageId && (
        <>
          <h2>血缘：<span className="mono">{lineageId}</span></h2>
          <div className="grid cols-2">
            <div className="panel">
              <h2 style={{ marginTop: 0 }}>上游（ancestors）</h2>
              {(lineage?.ancestors ?? []).map((a) => (
                <div key={a.artifact_id} className="mono" style={{ padding: "4px 0" }}>
                  {a.artifact_type} · {a.artifact_id}
                </div>
              ))}
              {lineage && lineage.ancestors.length === 0 && <span className="muted">无</span>}
            </div>
            <div className="panel">
              <h2 style={{ marginTop: 0 }}>下游（descendants）</h2>
              {(lineage?.descendants ?? []).map((a) => (
                <div key={a.artifact_id} className="mono" style={{ padding: "4px 0" }}>
                  {a.artifact_type} · {a.artifact_id}
                </div>
              ))}
              {lineage && lineage.descendants.length === 0 && <span className="muted">无</span>}
            </div>
          </div>
        </>
      )}
    </div>
  );
}

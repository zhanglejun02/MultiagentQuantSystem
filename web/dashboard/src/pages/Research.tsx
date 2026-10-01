import { FormEvent, useState } from "react";
import { Bot, CheckCircle2, CircleAlert, Play, ShieldCheck } from "lucide-react";
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
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState("");
  const [typeFilter, setTypeFilter] = useState("");
  const { data: artifacts } = useAsync(() => api.artifacts(typeFilter || undefined), [typeFilter]);
  const [lineageId, setLineageId] = useState<string | null>(null);
  const { data: lineage } = useAsync(
    () => (lineageId ? api.lineage(lineageId) : Promise.resolve(null)),
    [lineageId]
  );

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

import { useState } from "react";
import { Artifact, Experiment, api } from "../api";
import DataTable, { Column } from "../components/DataTable";
import { useAsync } from "../hooks";

const ARTIFACT_TYPES = [
  "", "DATA_SNAPSHOT", "UNIVERSE", "FEATURE_SET", "MODEL", "SIGNAL",
  "PORTFOLIO_TARGET", "BACKTEST_SPEC", "BACKTEST_RESULT", "VALIDATION_REPORT",
  "STRATEGY_PACKAGE", "DEPLOYMENT", "RESEARCH_REPORT",
];

export default function Research() {
  const { data: experiments, error: expError } = useAsync(() => api.experiments(), []);
  const [typeFilter, setTypeFilter] = useState("");
  const { data: artifacts } = useAsync(() => api.artifacts(typeFilter || undefined), [typeFilter]);
  const [lineageId, setLineageId] = useState<string | null>(null);
  const { data: lineage } = useAsync(
    () => (lineageId ? api.lineage(lineageId) : Promise.resolve(null)),
    [lineageId]
  );

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
    <div>
      <h1>Research</h1>
      {expError && <div className="error-box">{expError}</div>}

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

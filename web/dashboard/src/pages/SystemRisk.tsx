import { useState } from "react";
import { Power, RefreshCw, ShieldOff } from "lucide-react";
import { api } from "../api";
import StatCard from "../components/StatCard";
import { useAsync } from "../hooks";

export default function SystemRisk() {
  const [refresh, setRefresh] = useState(0);
  const { data: status, error } = useAsync(() => api.systemStatus(), [refresh]);
  const { data: health } = useAsync(() => api.health(), [refresh]);
  const [busy, setBusy] = useState(false);

  async function toggleKillSwitch() {
    if (!status) return;
    const engage = !status.kill_switch;
    if (engage && !confirm("确认触发 KillSwitch？将拒绝一切新订单。")) return;
    if (!engage && !confirm("确认解除 KillSwitch？")) return;
    setBusy(true);
    try {
      await api.killSwitch(engage, engage ? "manual engage from dashboard" : "manual disengage");
      setRefresh((n) => n + 1);
    } finally {
      setBusy(false);
    }
  }

  if (error) return <div className="error-box">加载失败：{error}</div>;

  return (
    <div>
      <h1>System / Risk</h1>
      <div className="toolbar">
        <button onClick={() => setRefresh((n) => n + 1)}>
          <RefreshCw size={14} /> 刷新
        </button>
        <button className="danger" disabled={busy || !status} onClick={toggleKillSwitch}>
          {status?.kill_switch ? <Power size={14} /> : <ShieldOff size={14} />}
          {status?.kill_switch ? "解除 KillSwitch" : "触发 KillSwitch"}
        </button>
      </div>

      <div className="grid cols-4">
        <StatCard
          label="KillSwitch"
          value={status ? (status.kill_switch ? "ENGAGED" : "OFF") : "…"}
          tone={status?.kill_switch ? "warn" : "ok"}
        />
        <StatCard
          label="Trading Runtime"
          value={status?.runtime_state ?? "…"}
          tone={status?.runtime_state === "SAFE_MODE" ? "warn" : "ok"}
        />
        <StatCard label="OMS" value={status?.oms ?? "…"} />
        <StatCard label="PaperBroker" value={status?.paper_broker ?? "…"} />
        <StatCard label="API 健康" value={health?.status ?? "…"} tone={health?.status === "ok" ? "ok" : "warn"} />
        <StatCard label="数据库" value={health?.db ?? "…"} tone={health?.db === "ok" ? "ok" : "warn"} />
        <StatCard label="数据快照数" value={String(status?.n_data_snapshots ?? "-")} />
        <StatCard label="回测结果数" value={String(status?.n_backtest_results ?? "-")} />
        <StatCard label="期货仿真数" value={String(status?.n_futures_simulations ?? "-")} />
        <StatCard label="期货回测数" value={String(status?.n_futures_backtests ?? "-")} />
        <StatCard label="期货 Gateway" value={status?.futures_gateway ?? "…"} />
      </div>

      <h2>状态详情</h2>
      <div className="panel mono" style={{ fontSize: 12, lineHeight: 1.8 }}>
        <div>heartbeat（最近心跳）: {status?.heartbeat ?? "-"}</div>
        <div>kill_switch_reason: {status?.kill_switch_reason ?? "-"}</div>
        <div>safe_mode_reason: {status?.safe_mode_reason ?? "-"}</div>
        <div>server time: {health?.time ?? "-"}</div>
        <div className="muted" style={{ marginTop: 8 }}>
          注：当前为 paper/backtest 环境。行情断连、券商断连、对账失败等运行时熔断在 RuntimeRiskMonitor
          中实现，接入实时数据源后在此页展示最新行情事件。
        </div>
      </div>
    </div>
  );
}

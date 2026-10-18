import { useEffect, useRef, useState } from "react";
import { ArrowRight, Check, ChevronDown, Circle, FileText, Info, LoaderCircle, RefreshCw,
  ShieldCheck, SlidersHorizontal, Sparkles, TriangleAlert } from "lucide-react";
import { api } from "../api";
import type { GoldDashboard, GoldEvidencePackage, GoldResearchResult, GoldResearchRun, GoldResearchState, GoldResearchStatus } from "../api";
import GoldInvestmentAdvice from "./GoldInvestmentAdvice";
import { activeGoldResearchRunId, display, GOLD_INSTRUMENTS, GOLD_RESEARCH_STATUSES, goldResearchHistory,
  goldResearchPriceFormat, goldResearchReport, goldResearchRequest, goldResearchRunLabel, goldResearchStageLabel, goldResearchStepState,
  goldEvidenceItem, goldEvidenceValue, goldLargestDisagreement, goldResearchSteps, goldTimestamp, isGoldDeliberationResult,
  isGoldDeliberationRun, isGoldResearchActive, pollGoldResearchRun } from "./goldModel";

const ROLE_NAMES = { researcher: "研究员", critic: "批判者", statistician: "统计员", orchestrator: "统筹者", final_auditor: "终审员" };
const ANALYST_NAMES = { technical_analyst: "技术分析师", macro_analyst: "宏观分析师", quant_analyst: "量化分析师", risk_analyst: "风险分析师" };
const DELIBERATION_ROLE_NAMES = { ...ANALYST_NAMES, orchestrator: "统筹者", final_auditor: "终审员" };
const STATE_VIEWS = { BULLISH: "偏多", NEUTRAL: "中性", BEARISH: "偏空", INSUFFICIENT_EVIDENCE: "证据不足 / 弃权" };
const REVIEW_STATUSES = { NOT_NEEDED: "无需修订", REVISED: "已完成修订", UNRESOLVED: "分歧未解决", BUDGET_SKIPPED: "预算不足 / 跳过修订" };
const REVISION_ACTIONS = { MAINTAIN: "维持观点", LOWER_CONFIDENCE: "降低置信度", RETRACT: "撤回观点" };
const DISAGREEMENT_KINDS = { DIRECTION: "方向分歧", EVIDENCE_GAP: "证据缺口", CONFIDENCE: "置信度分歧" };
const EVIDENCE_CATEGORIES = { price: "价格", diagnostic: "本地诊断", macro: "宏观", constraint: "研究约束" };
const SIGNALS = { ENTER: "规则满足（候选）", EXIT_OR_WAIT: "规则未满足（观察）", INSUFFICIENT_DATA: "数据不足" };
const DIAGNOSTIC_METRICS = [
  { label: "策略收益 · 总资产口径", key: "total_return", percent: true },
  { label: "买入持有 · 同初始仓位 / 未扣费", key: "buy_hold_same_cap_return", percent: true },
  { label: "策略最大回撤", key: "max_drawdown", percent: true },
  { label: "诊断交易次数", key: "trade_count", percent: false },
  { label: "成本 / 初始资金", key: "cost_fraction_of_initial", percent: true },
] as const;
const errorText = (error: unknown) => error instanceof Error ? error.message : String(error);

interface Props {
  dashboard: GoldDashboard | null;
  dirty: boolean;
  allocationBusy: boolean;
  onRefresh: () => void;
  onConfigure: () => void;
}

export function GoldResearchRoutes({ service }: { service: GoldResearchStatus }) {
  const roles = service.protocol === "gold-deliberation-v4" || Object.keys(ANALYST_NAMES).some((role) => role in service.roles)
    ? DELIBERATION_ROLE_NAMES : ROLE_NAMES;
  return <details className="gold-model-routes"><summary>配置路由<ChevronDown size={12} /></summary>
    <dl>{Object.entries(roles).map(([role, label]) => <div key={role}><dt>{label}</dt>
      <dd>{service.roles[role] ? `${service.roles[role].provider} / ${service.roles[role].model}` : "未配置"}</dd></div>)}</dl>
  </details>;
}

export function GoldResearchBudget({ service }: { service: GoldResearchStatus | null }) {
  const deliberation = service?.protocol === "gold-deliberation-v4" || Object.keys(service?.roles ?? {}).some((role) => role in ANALYST_NAMES);
  return <div className="gold-budget"><span>{service ? `本轮上限 ${service.budget.max_calls} 次调用 / ${service.budget.max_tokens.toLocaleString("zh-CN")} tokens` : "调用预算未返回"}</span>
    <button className="icon-button" type="button" aria-label="模型 API 费用说明" title={deliberation
      ? "通常 5 次有效调用；含分析师与建议修订最多 9 次。协议上限 12 次 / 64,000 tokens，包含格式修复；全局更低预算优先，以本轮返回上限为准。服务商可能收费，不是费用报价。"
      : "调用配置中的模型 API，服务商可能收费。此处为后端返回的调用与 token 上限，不是费用报价。"}><Info size={14} /></button></div>;
}

export function GoldResearchProgress({ run, pollError = false, protocol }: { run?: GoldResearchRun; pollError?: boolean; protocol?: string }) {
  const deliberation = isGoldDeliberationRun(run, protocol);
  function stateLabel(state: ReturnType<typeof goldResearchStepState>, stage: string) {
    if (state === "complete") return "已完成";
    if (state === "current") return pollError ? "上次状态" : "进行中";
    if (state === "skipped") {
      if (stage === "analyst_revision" && run?.result?.deliberation?.review_status === "BUDGET_SKIPPED") return "预算跳过";
      return deliberation || run?.result?.advice_audit ? "已跳过" : "历史未终审";
    }
    if (state === "unconfirmed") return stage !== "analyst_revision" && run?.result?.advice_audit?.status === "UNAVAILABLE" ? "不可用 / 未确认" : "未确认";
    return run?.status === "FAILED" ? "未确认" : "待处理";
  }
  return <><ol>{goldResearchSteps(run, protocol).map((step) => {
    const state = goldResearchStepState(run, step.stage, protocol);
    return <li key={step.stage} className={state} aria-current={state === "current" ? "step" : undefined}>
      {state === "complete" ? <Check size={13} /> : state === "current" && !pollError ? <LoaderCircle size={13} className="gold-spin" /> : <Circle size={13} />}
      <span>{step.label}</span><small>{stateLabel(state, step.stage)}</small>
    </li>;
  })}</ol>
    {!deliberation && <dl className="gold-audit-progress" aria-label="终审修订进度">{(["advice_revision", "final_reaudit"] as const).map((stage) => {
      const state = goldResearchStepState(run, stage);
      return <div key={stage} className={state} aria-current={state === "current" ? "step" : undefined}>
        <dt>{goldResearchStageLabel(stage)}{stage === "advice_revision" ? "（限一次）" : ""}</dt><dd>{stateLabel(state, stage)}</dd>
      </div>;
    })}</dl>}
  </>;
}

export function GoldResearchCalls({ result }: { result: GoldResearchResult }) {
  const deliberation = isGoldDeliberationResult(result) ? result.deliberation : null;
  const roles = deliberation ? DELIBERATION_ROLE_NAMES : ROLE_NAMES;
  return <details className="gold-audit"><summary><ShieldCheck size={16} />角色调用与本地门禁<ChevronDown size={15} /></summary>
    <div className="gold-audit-roles">{Object.entries(roles).filter(([role]) => deliberation || role !== "final_auditor"
      || result.advice_audit || result.calls.some((call) => call.role === role)).map(([role, label]) => {
      const calls = result.calls.filter((call) => call.role === role);
      const state = deliberation?.states.find((item) => item.analyst === role);
      const skipped = deliberation?.skipped_analysts[role];
      const judgment = deliberation && role in ANALYST_NAMES ? skipped ? "已跳过" : state
        ? `${STATE_VIEWS[state.view]} · 置信度 ${display(state.confidence, true, 0)}` : "观点未返回"
        : role === "researcher" ? "研究假设已返回"
        : role === "critic" ? `模型判断 approved=${result.critic.approved}`
          : role === "statistician" ? `模型判断 robust=${result.statistician.robust}`
            : role === "orchestrator" ? deliberation ? "证据汇总" : `模型判断 recommend_candidate=${result.decision.recommend_candidate}`
              : `独立角色 · ${result.advice_audit?.status ?? "审查结果未返回"}`;
      const rationale = deliberation && role in ANALYST_NAMES ? skipped ?? state?.summary ?? "未返回该角色的独立观点"
        : role === "researcher" ? result.proposal.hypothesis : role === "critic" ? result.critic.rationale
        : role === "statistician" ? result.statistician.rationale : role === "orchestrator" ? result.decision.rationale
          : result.advice_audit?.reason ?? "未返回独立终审结论";
      const findings = deliberation ? state?.risks ?? [] : role === "critic" ? result.critic.issues : role === "statistician" ? result.statistician.findings : [];
      return <article key={role}><div className="gold-heading"><h4>{label}</h4><span>{judgment} · {calls.length} 次调用</span></div>
        {calls.length ? calls.map((call, index) => <div className="gold-audit-route" key={index}><strong>{call.provider} / {call.model}</strong>
          <small>输入 {call.input_tokens ?? "未返回"} / 输出 {call.output_tokens ?? "未返回"} tokens</small>
          <small>请求标识 {call.raw_request_id ?? "未返回"}</small></div>) : <p className="gold-muted">{skipped ? "该角色已跳过，无模型调用收据" : "本报告未返回该角色的调用路由"}</p>}
        <p>{rationale}</p>{!!findings.length && <ul>{findings.map((item, index) => <li key={index}>{item}</li>)}</ul>}
      </article>;
    })}</div>
    <h4>本地门禁</h4><dl className="gold-local-gates">{Object.entries(result.local_gates).map(([gate, passed]) =>
      <div key={gate}><dt>{gate}</dt><dd className={passed ? "gold-gate-pass" : "gold-gate-fail"}>{passed ? <Check size={13} /> : <TriangleAlert size={13} />}{passed ? "通过" : "未通过"}</dd></div>)}</dl>
    {!Object.keys(result.local_gates).length && <p className="gold-muted">未返回门禁判定</p>}
  </details>;
}

export function GoldEvidenceReferences({ ids, evidence, label = "引用证据" }: {
  ids: string[]; evidence?: GoldEvidencePackage | null; label?: string;
}) {
  if (!ids.length) return <p className="gold-muted">{label}：未返回证据 ID</p>;
  return <div className="gold-evidence-refs" aria-label={label}><span>{label}</span>{[...new Set(ids)].map((id) => {
    const item = goldEvidenceItem(evidence, id);
    const value = goldEvidenceValue(item?.value);
    const explanation = item ? `${item.definition}；本地值：${value}；来源：${item.source}` : "证据未提供，无法核验引用";
    return <details className="gold-evidence-reference" key={id} data-missing={!item || value === "未提供" ? "true" : undefined}>
      <summary title={explanation}><code>{id}</code><Info size={11} /></summary>
      {item ? <dl><dt>定义</dt><dd>{item.definition || "未提供"}</dd><dt>本地值</dt><dd>{value}</dd>
        <dt>分类</dt><dd>{EVIDENCE_CATEGORIES[item.category]}</dd><dt>来源</dt><dd>{item.source || "未提供"}</dd></dl>
        : <p className="gold-muted">证据未提供，无法核验引用</p>}
    </details>;
  })}</div>;
}

function stateText(state?: GoldResearchState): string {
  return state ? `${STATE_VIEWS[state.view]} · ${display(state.confidence, true, 0)}` : "未返回";
}

export function GoldResearchDeliberation({ result }: { result: GoldResearchResult }) {
  if (!isGoldDeliberationResult(result)) return null;
  const review = result.deliberation;
  const evidence = result.evidence_package;
  const largest = goldLargestDisagreement(review);
  const perspectives = Object.entries(ANALYST_NAMES).map(([role, label]) => ({ role, label,
    initial: review.initial_states.find((state) => state.analyst === role),
    final: review.states.find((state) => state.analyst === role),
    revisions: review.revisions.filter((revision) => revision.state.analyst === role),
  }));
  const currentStates = perspectives.flatMap(({ final, initial }) => final ?? initial ?? []);
  const missing = [...new Set([...currentStates.flatMap((state) => state.missing_evidence),
    ...review.revisions.flatMap((revision) => revision.evidence_requests)])];
  const referenced = [...new Set([...currentStates.flatMap((state) => state.claims.flatMap((claim) =>
    [...claim.evidence_ids, ...claim.counterevidence_ids ?? []])), ...review.disagreements.flatMap((item) => item.evidence_ids)])];
  const missingIds = referenced.filter((id) => goldEvidenceValue(goldEvidenceItem(evidence, id)?.value) === "未提供");

  return <section className="gold-deliberation" aria-labelledby="gold-perspectives-title">
    <div className="gold-heading"><h3 id="gold-perspectives-title">四方独立观点</h3>
      <span className="gold-review-status" data-status={review.review_status}>{REVIEW_STATUSES[review.review_status]} · {
        review.conclusion === "EVIDENCE_BOUNDED" ? "结论受证据约束" : "证据不足"}</span></div>
    {!evidence && <p className="gold-warning">未返回结构化证据包，不能用兼容摘要代替引用证据。</p>}
    <div className="gold-perspectives">{perspectives.map(({ role, label, initial, final, revisions }) => {
      const state = final ?? initial;
      const skipped = review.skipped_analysts[role];
      const changed = revisions.length > 0 || initial && final && (initial.view !== final.view || initial.confidence !== final.confidence);
      return <article className="gold-perspective" data-analyst={role} key={role}>
        <h4>{label}</h4>
        <div className="gold-perspective-stance"><strong data-view={state?.view}>{state ? STATE_VIEWS[state.view] : "观点未返回"}</strong>
          <span>置信度 {state ? display(state.confidence, true, 0) : "未提供"}</span></div>
        {!final && initial && <p className="gold-meta">初始观点 · 最终观点未返回</p>}
        {changed && <div className="gold-state-change"><span>初始 {stateText(initial)}</span><ArrowRight size={12} aria-label="变更为" />
          <span>复核后 {final ? stateText(final) : "未返回"}</span></div>}
        <p>{state?.summary ?? "未返回该角色的独立观点"}</p>
        {skipped && <p className="gold-perspective-skipped">已跳过 · 无模型调用<br />{skipped}</p>}
        {state && <details className="gold-perspective-detail"><summary>论据与复核 · {state.claims.length} 项论据<ChevronDown size={12} /></summary>
          {initial && (revisions.length > 0 || initial.summary !== state.summary) && <p><strong>初始摘要：</strong>{initial.summary}</p>}
          {state.claims.length ? <ul className="gold-perspective-claims">{state.claims.map((claim, index) => <li key={index}>
            <p>{claim.claim} <small>置信度 {display(claim.confidence, true, 0)}</small></p>
            <GoldEvidenceReferences ids={claim.evidence_ids} evidence={evidence} />
            {!!claim.counterevidence_ids?.length && <GoldEvidenceReferences ids={claim.counterevidence_ids} evidence={evidence} label="反向证据" />}
          </li>)}</ul> : <p className="gold-muted">未返回可核验论据</p>}
          <dl className="gold-perspective-context">{([
            ["风险", state.risks], ["待解问题", state.open_questions], ["失效条件", state.invalidating_conditions], ["缺失证据", state.missing_evidence],
          ] as const).map(([heading, items]) => <div key={heading}><dt>{heading}</dt><dd>{items.length
            ? <ul>{items.map((item, index) => <li key={index}>{item}</li>)}</ul> : "未返回"}</dd></div>)}</dl>
          {!!revisions.length && <ol className="gold-revision-list" aria-label={`${label}修订记录`}>{revisions.map((revision, index) => <li key={index}>
            <strong>{REVISION_ACTIONS[revision.action]}</strong><p>{revision.rationale}</p>
            <p className="gold-meta">修订状态 {stateText(revision.state)}</p>
            <p className="gold-meta">回应分歧 {revision.addressed_disagreement_ids.join("、") || "未返回"}</p>
            {!!revision.evidence_requests.length && <p>待补证据：{revision.evidence_requests.join("；")}</p>}
          </li>)}</ol>}
        </details>}
      </article>;
    })}</div>
    <section className="gold-disagreements" aria-label="交叉复核分歧">
      <div className="gold-heading"><h4 title="按初始方向跨度、分歧类型及置信度差排序">最大分歧</h4>
        {largest && <small>{DISAGREEMENT_KINDS[largest.kind]} · {largest.disagreement_id}</small>}</div>
      {largest ? <><p>{largest.question}</p><p className="gold-meta">{largest.analysts.map((role) => ANALYST_NAMES[role]).join(" / ")}</p>
        <GoldEvidenceReferences ids={largest.evidence_ids} evidence={evidence} /></> : <p className="gold-muted">未记录交叉分歧，不代表已获得完整证据。</p>}
      {review.disagreements.length > 1 && <details className="gold-perspective-detail"><summary>其余分歧 · {review.disagreements.length - 1} 项<ChevronDown size={12} /></summary>
        <ul>{review.disagreements.filter((item) => item !== largest).map((item) => <li key={item.disagreement_id}>
          <p>{DISAGREEMENT_KINDS[item.kind]} · {item.question}</p>
          <p className="gold-meta">{item.disagreement_id} · {item.analysts.map((role) => ANALYST_NAMES[role]).join(" / ")}</p>
          <GoldEvidenceReferences ids={item.evidence_ids} evidence={evidence} />
        </li>)}</ul></details>}
    </section>
    <section className="gold-deliberation-gaps" aria-label="缺失证据"><h4>缺失证据</h4>
      {!!missing.length && <ul>{missing.map((item, index) => <li key={index}>{item}</li>)}</ul>}
      {!!missingIds.length && <GoldEvidenceReferences ids={missingIds} evidence={evidence} label="缺失或空值引用" />}
      {!missing.length && !missingIds.length && <p className="gold-muted">未列出额外缺失项，不代表证据完整。</p>}
    </section>
  </section>;
}

export function GoldResearchMarketView({ result }: { result: GoldResearchResult }) {
  if (isGoldDeliberationResult(result)) return <GoldResearchDeliberation result={result} />;
  return <section className="gold-report-part"><h3>市场判断</h3><p>{result.proposal.market_view}</p>
    <h4>可检验假设</h4><p>{result.proposal.hypothesis}</p>
    <h4>研究风险</h4>{result.proposal.risks.length ? <ul>{result.proposal.risks.map((risk, index) => <li key={index}>{risk}</li>)}</ul>
      : <p className="gold-muted">未返回风险条目</p>}</section>;
}

export function GoldResearchEvidence({ result, run, currentSnapshot }: {
  result: GoldResearchResult; run: GoldResearchRun; currentSnapshot?: string;
}) {
  const deliberation = isGoldDeliberationResult(result) ? result.deliberation : null;
  const evidence = deliberation ? result.evidence_package : null;
  return <details className="gold-audit gold-evidence"><summary><FileText size={16} />数据证据与溯源<ChevronDown size={15} /></summary>
    <dl className="gold-provenance"><dt>报告快照</dt><dd>{evidence?.snapshot_id ?? result.analysis_context.snapshot_id}</dd>
      <dt>当前行情快照</dt><dd>{currentSnapshot ?? "未发布"}</dd>
      <dt>研究任务</dt><dd>{run.run_id}</dd><dt>报告产物</dt><dd>{run.report_artifact_id ?? "未返回"}</dd>
      <dt>本轮研究约束</dt><dd>{run.request.symbol} · 上限 {display(run.request.max_weight, true)} · {run.request.focus}</dd>
      {deliberation && <><dt>审议协议</dt><dd>{deliberation.protocol}</dd><dt>证据哈希</dt><dd><code>{deliberation.evidence_hash}</code></dd>
        <dt>证据日期</dt><dd>{evidence?.as_of ?? "未返回"}</dd></>}
    </dl>
    <p className="gold-muted">证据限于本轮接口事实与快照，官方宏观链接不代表已采集宏观序列。</p>
    {deliberation ? evidence ? <>
      <dl className="gold-evidence-catalog">{Object.entries(evidence.items).map(([id, item]) => <div key={id}>
        <dt><code>{id}</code><small>{EVIDENCE_CATEGORIES[item.category]}</small></dt>
        <dd><strong>本地值 {goldEvidenceValue(item.value)}</strong><span>{item.definition || "定义未提供"}</span>
          <small>来源 {item.source || "未提供"}</small></dd>
      </div>)}</dl>
      {!Object.keys(evidence.items).length && <p className="gold-muted">证据包未提供条目</p>}
      {!!evidence.limitations.length && <ul className="gold-limitations">{evidence.limitations.map((item, index) => <li key={index}>{item}</li>)}</ul>}
    </> : <p className="gold-warning">未返回结构化证据包，无法核验引用。</p>
      : <pre>{JSON.stringify(result.analysis_context.facts, null, 2)}</pre>}
    {!!result.analysis_context.warnings.length && <ul className="gold-limitations">{result.analysis_context.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>}
  </details>;
}

export default function GoldResearch({ dashboard, dirty, allocationBusy, onRefresh, onConfigure }: Props) {
  const [service, setService] = useState<GoldResearchStatus | null>(null);
  const [runs, setRuns] = useState<GoldResearchRun[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [trackedId, setTrackedId] = useState("");
  const [pollId, setPollId] = useState<string | null>(null);
  const [initializing, setInitializing] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [startError, setStartError] = useState("");
  const [pollError, setPollError] = useState("");
  const [reload, setReload] = useState(0);
  const [retry, setRetry] = useState(0);
  const [symbol, setSymbol] = useState(GOLD_INSTRUMENTS[0].symbol);
  const [cap, setCap] = useState("10");
  const [focus, setFocus] = useState("分析人民币黄金趋势与风险，提出可检验策略及3至12个月配置建议");
  const submitController = useRef<AbortController | null>(null);
  const submitBusy = useRef(false);
  const restoredRunId = useRef("");
  const refresh = useRef(onRefresh);
  const refreshed = useRef(new Set<string>());
  refresh.current = onRefresh;

  function restoreRequest(run: GoldResearchRun) {
    if (restoredRunId.current === run.run_id) return;
    restoredRunId.current = run.run_id;
    setSymbol(run.request.symbol);
    setCap(String(run.request.max_weight * 100));
    setFocus(run.request.focus);
  }

  function refreshFor(run: GoldResearchRun) {
    const keys = [run.stage === "fetch" ? `${run.run_id}:fetch` : "",
      run.snapshot_id ? `${run.run_id}:${run.snapshot_id}` : "",
      run.status === "COMPLETED" ? `${run.run_id}:complete` : ""].filter(Boolean);
    if (keys.some((key) => !refreshed.current.has(key))) {
      keys.forEach((key) => refreshed.current.add(key));
      refresh.current();
    }
  }

  useEffect(() => {
    const controller = new AbortController();
    setInitializing(true);
    setLoadError("");
    void Promise.allSettled([api.goldResearchStatus(controller.signal), api.goldResearchRuns(controller.signal)])
      .then(([statusResponse, historyResponse]) => {
        if (controller.signal.aborted) return;
        const status = statusResponse.status === "fulfilled" ? statusResponse.value : null;
        const history = historyResponse.status === "fulfilled" ? goldResearchHistory(historyResponse.value) : [];
        if (status) setService(status);
        if (historyResponse.status === "fulfilled") setRuns((current) => goldResearchHistory([...history, ...current]));
        const activeId = activeGoldResearchRunId(status, history);
        const activeRun = history.find((run) => run.run_id === activeId);
        if (activeRun) restoreRequest(activeRun);
        setTrackedId(activeId ?? history[0]?.run_id ?? "");
        setSelectedId((current) => current || activeId || history[0]?.run_id || "");
        setPollId(activeId);
        if (status && historyResponse.status === "fulfilled") { setUncertain(false); setStartError(""); }
        const failures = [statusResponse, historyResponse].flatMap((response) =>
          response.status === "rejected" ? [errorText(response.reason)] : []);
        setLoadError(failures.length ? `研究状态读取失败：${failures.join("；")}` : "");
        setInitializing(false);
      });
    return () => controller.abort();
  }, [reload]);

  useEffect(() => {
    if (!pollId) return;
    return pollGoldResearchRun(pollId, api.goldResearchRun, (run) => {
      setRuns((current) => goldResearchHistory([run, ...current]));
      setTrackedId(run.run_id);
      restoreRequest(run);
      refreshFor(run);
      if (!isGoldResearchActive(run)) {
        setPollId(null);
        setService((current) => current ? { ...current, active_run_id: null, active_gold_run_id: null, busy: false } : current);
      }
    }, (error) => setPollError(`任务查询已停止：${errorText(error)}`));
  }, [pollId, retry]);

  useEffect(() => () => { submitController.current?.abort(); }, []);

  const tracked = runs.find((run) => run.run_id === trackedId);
  const selected = runs.find((run) => run.run_id === selectedId);
  const report = goldResearchReport(runs, selectedId);
  const result = report?.result;
  const deliberation = !!result && isGoldDeliberationResult(result);
  const evidence = deliberation ? result?.evidence_package : null;
  const priceFormat = result ? goldResearchPriceFormat(result.evaluation) : null;
  const active = Boolean(pollId || activeGoldResearchRunId(service, runs));
  const externalBusy = Boolean(service?.busy && !active);
  const blocked = initializing || submitting || active || externalBusy || !service?.ready || !!loadError || uncertain
    || dirty || allocationBusy || !dashboard;
  const fallback = !!report && selectedId !== report.run_id;
  const snapshotMismatch = !!result && (evidence?.snapshot_id ?? result.analysis_context.snapshot_id) !== dashboard?.snapshot?.artifact_id;
  const stageLabel = goldResearchStageLabel(tracked?.stage) ?? (active ? "正在恢复任务状态" : "尚未启动");

  async function start() {
    if (blocked || submitBusy.current) return;
    let request;
    try { request = goldResearchRequest(symbol, cap, focus); }
    catch (error) { setStartError(errorText(error)); return; }
    const controller = new AbortController();
    submitController.current = controller;
    submitBusy.current = true;
    setSubmitting(true);
    setStartError("");
    setPollError("");
    try {
      const run = await api.startGoldResearch(request, controller.signal);
      if (controller.signal.aborted) return;
      restoredRunId.current = run.run_id;
      setRuns((current) => goldResearchHistory([run, ...current]));
      setSelectedId(run.run_id);
      setTrackedId(run.run_id);
      const nextActive = isGoldResearchActive(run) ? run.run_id : null;
      setService((current) => current ? { ...current, active_run_id: nextActive, active_gold_run_id: nextActive, busy: !!nextActive } : current);
      setPollId(nextActive);
      refreshFor(run);
    } catch (error) {
      if (!controller.signal.aborted) {
        setStartError(`分析请求未确认：${errorText(error)}。未自动重发分析，请重新查询研究状态。`);
        setUncertain(true);
      }
    } finally {
      submitBusy.current = false;
      if (!controller.signal.aborted) setSubmitting(false);
    }
  }

  return <>
    <aside className="gold-research-workspace" id="gold-research" aria-labelledby="gold-research-title">
      <div className="gold-heading"><h2 id="gold-research-title"><Sparkles size={17} />多智能体研究 · 独立终审</h2>
        <span className={`gold-service-state ${service?.ready ? "ready" : ""}`}>
          {initializing ? "连接中" : service?.ready ? "已就绪" : "未就绪"}</span></div>
      {loadError && <div className="gold-error" role="alert"><TriangleAlert size={15} /><span>{loadError}</span></div>}
      {!initializing && !service?.ready && !loadError && <p className="gold-muted gold-research-notice">研究/独立终审模型路由或凭据未就绪，研究暂不可用。行情与手工配置不受影响。</p>}
      {externalBusy && <p className="gold-warning">其他研究任务占用中。<a href="/research">查看研究任务</a>
        <button type="button" className="gold-text-button" onClick={() => setReload((value) => value + 1)}><RefreshCw size={13} />查询状态</button></p>}
      {(loadError || uncertain) && <button type="button" onClick={() => { setReload((value) => value + 1); setPollError(""); setRetry((value) => value + 1); }}
        disabled={initializing} title="仅重新查询状态，不会发起分析"><RefreshCw size={14} />重新查询研究状态</button>}
      <form className="gold-research-form" onSubmit={(event) => { event.preventDefault(); void start(); }}>
        <div className="gold-research-inputs"><label htmlFor="gold-research-symbol">研究对象<select id="gold-research-symbol" value={symbol}
          disabled={submitting || active} onChange={(event) => setSymbol(event.target.value)}>
          {GOLD_INSTRUMENTS.map((item) => <option key={item.symbol} value={item.symbol}>{item.name} · {item.symbol}</option>)}
        </select></label>
          <label htmlFor="gold-research-cap">黄金研究仓位上限（%）<input id="gold-research-cap" type="number" min="0.01" max="30" step="any"
            value={cap} disabled={submitting || active} onChange={(event) => setCap(event.target.value)} /></label></div>
        <span className="gold-field-note">用户设定的研究约束，非配置建议</span>
        <label htmlFor="gold-research-focus">研究重点<textarea id="gold-research-focus" rows={3} maxLength={1000} value={focus}
          disabled={submitting || active} onChange={(event) => setFocus(event.target.value)} /></label>
        <button className="gold-research-primary" type="submit" disabled={blocked}
          title={dirty ? "配置未保存，请在我的配置中显式保存" : "每次重新采集黄金日线数据后分析；模型 API 可能产生费用"}>
          {submitting || active ? <LoaderCircle size={16} className={pollError ? "" : "gold-spin"} /> : <RefreshCw size={16} />}
          {submitting ? "正在提交" : active ? "研究进行中" : "刷新数据并分析"}<Sparkles size={15} /></button>
      </form>
      {dirty && <div className="gold-warning"><TriangleAlert size={15} /><span>配置未保存，不能发起新分析。
        <button className="gold-text-button" onClick={onConfigure}><SlidersHorizontal size={12} />我的配置</button></span></div>}
      {startError && <div className="gold-error" role="alert"><TriangleAlert size={15} /><span>{startError}</span></div>}
      <GoldResearchBudget service={service} />
      {service && <GoldResearchRoutes service={service} />}
      <div className="gold-pipeline" aria-label="研究任务进度">
        <div className="gold-pipeline-heading"><strong>当前任务</strong><span role="status" aria-live="polite">{stageLabel}{pollError ? " · 查询暂停" : ""}</span></div>
        <GoldResearchProgress run={tracked} pollError={!!pollError} protocol={service?.protocol} />
        {tracked && <p className="gold-meta">{tracked.request.symbol} · 上限 {display(tracked.request.max_weight, true)} · {GOLD_RESEARCH_STATUSES[tracked.status]}<br />状态更新 {goldTimestamp(tracked.updated_at)}</p>}
        {tracked?.status === "FAILED" && <div className="gold-error" role="alert"><TriangleAlert size={15} /><span>本次任务失败：{tracked.error ?? "后端未返回失败详情"}。历史报告不是本次结果。</span></div>}
        {pollError && <div className="gold-error" role="alert"><TriangleAlert size={15} /><div>{pollError}
          <button className="gold-text-button" onClick={() => { setPollError(""); setRetry((value) => value + 1); }}><RefreshCw size={13} />重试任务查询</button></div></div>}
      </div>
    </aside>

    <section className="gold-research-report gold-section" aria-labelledby="gold-report-title">
      <div className="gold-heading"><h2 id="gold-report-title"><FileText size={17} />策略研究报告</h2>
        <div className="gold-history-control"><label htmlFor="gold-research-history">研究记录</label>
          <select id="gold-research-history" value={selectedId} disabled={!runs.length} onChange={(event) => setSelectedId(event.target.value)}>
            {!runs.length && <option value="">暂无记录</option>}
            {selectedId && !runs.some((run) => run.run_id === selectedId) && <option value={selectedId}>正在读取任务</option>}
            {runs.map((run) => <option key={run.run_id} value={run.run_id}>{goldResearchRunLabel(run)}</option>)}
          </select>
          <button className="icon-button" disabled={initializing || submitting} aria-label="刷新研究记录" title="刷新研究状态与历史，不会调用模型"
            onClick={() => { setReload((value) => value + 1); setPollError(""); setRetry((value) => value + 1); }}><RefreshCw size={14} /></button>
        </div></div>
      {fallback && <div className="gold-warning"><Info size={15} /><span>
        {selected?.status === "FAILED" ? "所选任务失败，以下保留上次完成报告，不是本次分析结果。"
          : selected?.status === "COMPLETED" ? "所选任务未返回完整报告，以下为较早报告。"
            : selected && isGoldResearchActive(selected) || active ? "当前任务尚未完成，以下保留上次完成报告，不是新结果。"
              : "所选记录不在最近 20 条完整报告内，以下为最近完成报告。"}
        {` 报告更新：${goldTimestamp(report.updated_at)} · ${report.request.symbol}`}</span></div>}
      {selected?.status === "FAILED" && selected.run_id !== trackedId && <div className="gold-error" role="alert"><TriangleAlert size={15} /><span>所选任务失败：{selected.error ?? "后端未返回失败详情"}</span></div>}
      {!result || !report ? <div className="gold-report-empty"><FileText size={22} /><span>{selected?.status === "COMPLETED" ? "所选任务未返回完整报告" : active ? "分析尚未完成，暂无完整报告" : "暂无已完成的研究报告"}</span></div> : <>
        <div className="gold-report-heading"><div><span className="gold-report-date">{report.run_id !== runs[0]?.run_id ? "历史报告" : "报告"} · 更新 {goldTimestamp(report.updated_at)} · {report.request.symbol}</span>
          <h3>{deliberation ? "黄金证据审议" : result.proposal.name}</h3></div><div className="gold-report-flags"><span className="gold-badge">{result.outcome === "OBSERVE" ? "继续观察 · OBSERVE" : "模拟候选 · PAPER_CANDIDATE"}</span>
            <span className="gold-muted">仅研究 · 不具备部署资格</span></div></div>
        <div className="gold-evidence-meta"><span>分析证据日期 <strong>{evidence?.as_of || result.analysis_context.as_of || "未返回"}</strong></span>
          <span>规则日期 <strong>{result.evaluation.as_of ?? "未返回"}</strong></span>
          <span>本轮采集 {goldTimestamp(result.analysis_context.latest_fetch)}</span></div>
        <p className="gold-meta">分析快照 {evidence?.snapshot_id ?? result.analysis_context.snapshot_id}</p>
        {snapshotMismatch && <div className="gold-warning"><Info size={15} /><span>报告快照与当前行情快照不同，以下结论基于报告自己的证据，不代表当前图表快照。</span></div>}
        {deliberation && <GoldResearchMarketView result={result} />}
        <div className={deliberation ? "gold-local-plan" : "gold-report-columns"}>
          {!deliberation && <GoldResearchMarketView result={result} />}
          <section className="gold-report-part"><div className="gold-heading"><h3>本地规则计划</h3><span className="gold-signal">{SIGNALS[result.evaluation.signal]}</span></div>
            {deliberation && <p className="gold-meta">本地固定默认参数 · 非模型提案</p>}
            <dl className="gold-rule-plan">{([
              ["进入条件", result.evaluation.rules.entry], ["退出条件", result.evaluation.rules.exit],
              ["仓位规则", result.evaluation.rules.sizing], ["执行假设", result.evaluation.rules.execution],
              ["失效条件", result.evaluation.rules.invalidation],
            ] as const).map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value || "未返回"}</dd></div>)}</dl>
          </section>
        </div>
        <dl className="gold-indicators">{[
          [`收盘价（${priceFormat?.unit}）`, display(result.evaluation.close, false, priceFormat?.digits)],
          [`快均线 MA${result.evaluation.parameters.fast_window}（${priceFormat?.unit}）`, display(result.evaluation.fast_average, false, priceFormat?.digits)],
          [`慢均线 MA${result.evaluation.parameters.slow_window}（${priceFormat?.unit}）`, display(result.evaluation.slow_average, false, priceFormat?.digits)],
          ["年化波动率", display(result.evaluation.annualized_volatility, true)],
          [`跟踪参考（${priceFormat?.unit}）`, display(result.evaluation.trailing_reference, false, priceFormat?.digits)],
          ["规则候选权重", display(result.evaluation.target_weight, true)],
          ["研究仓位上限", display(result.evaluation.max_weight, true)],
          ["波动率目标 / 跟踪止损", `${display(result.evaluation.parameters.volatility_target, true)} / ${display(result.evaluation.parameters.trailing_stop, true)}`],
        ].map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>

        <section className="gold-diagnostic"><div className="gold-heading"><h3>{priceFormat?.spot ? "现货金价参考组合" : "研究诊断"} <small>非权威回测 / 无独立样本外</small></h3>
          <span className="gold-muted">{result.evaluation.diagnostic.start_date ?? "未返回"} 至 {result.evaluation.diagnostic.end_date ?? "未返回"}</span></div>
          {priceFormat?.spot && <p className="gold-meta">Au99.99现货参考，不是银行积存金或金条成交价</p>}
          <div className="gold-table-wrap"><table className="gold-table gold-diagnostic-table"><thead><tr><th>指标</th><th>基准成本</th><th>双倍成本</th></tr></thead>
            <tbody><tr><td>诊断状态</td><td>{result.evaluation.diagnostic.status}</td><td>{result.evaluation.double_cost.status}</td></tr>
              {DIAGNOSTIC_METRICS.map(({ label, key, percent }) => <tr key={key}><td>{label}</td>
                <td>{display(result.evaluation.diagnostic[key], percent, percent ? 2 : 0)}</td>
                <td>{display(result.evaluation.double_cost[key], percent, percent ? 2 : 0)}</td></tr>)}
            </tbody></table></div>
          <p className="gold-meta">收益以初始总资产为分母，非黄金满仓收益；买入持有按相同初始仓位持有，未扣交易费用。</p>
          <p className="gold-meta">单边成本假设 10bp / 20bp · 双倍成本区间 {result.evaluation.double_cost.start_date ?? "未返回"} 至 {result.evaluation.double_cost.end_date ?? "未返回"} · 缺失指标不按零处理</p>
          {!!result.evaluation.limitations.length && <ul className="gold-limitations">{result.evaluation.limitations.map((item, index) => <li key={index}>{item}</li>)}</ul>}
        </section>

        <section className="gold-decision"><h3>统筹结论</h3><p>{result.decision.rationale}</p>
          {!!result.decision.next_steps.length && <ul>{result.decision.next_steps.map((item, index) => <li key={index}>{item}</li>)}</ul>}</section>
        <GoldResearchCalls result={result} />
        <GoldResearchEvidence result={result} run={report} currentSnapshot={dashboard?.snapshot?.artifact_id} />
        <GoldInvestmentAdvice advice={result.investment_advice} audit={result.advice_audit}
          historical={fallback || report.run_id !== runs[0]?.run_id} asOf={evidence?.as_of ?? result.analysis_context.as_of} symbol={result.evaluation.symbol} />
      </>}
    </section>
  </>;
}
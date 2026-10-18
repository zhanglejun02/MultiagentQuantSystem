import type { ResearchModules, ResearchPlan, ResearchPlanRequest, ResearchRun, ResearchRunStatus } from "../api";

export type ResearchMode = "gold" | "macro" | "factor" | "combined";

export interface ResearchDraft {
  mode: ResearchMode;
  useModel: boolean;
  question: string;
  symbol: string;
  maxWeight: string;
  focus: string;
  startDate: string;
  endDate: string;
  seriesIds: string;
  topic: string;
  target: string;
  dataMode: "fixture" | "frozen_snapshot";
  snapshotId: string;
}

export const RESEARCH_QUESTIONS: Record<ResearchMode, string> = {
  gold: "\u9ec4\u91d1\u7814\u7a76",
  macro: "\u5b8f\u89c2\u7814\u7a76",
  factor: "\u56e0\u5b50\u7814\u7a76",
  combined: "\u9ec4\u91d1\u5b8f\u89c2\u8054\u5408\u7814\u7a76",
};

export const RESEARCH_MACRO_SERIES = [
  { id: "DGS10", label: "\u7f8e\u56fd10\u5e74\u540d\u4e49\u56fd\u503a\u6536\u76ca\u7387" },
  { id: "DFII10", label: "\u7f8e\u56fd10\u5e74\u5b9e\u9645\u56fd\u503a\u6536\u76ca\u7387" },
  { id: "DTWEXBGS", label: "\u5e7f\u4e49\u8d38\u6613\u52a0\u6743\u7f8e\u5143\u6307\u6570" },
  { id: "DEXCHUS", label: "\u7f8e\u5143\u5151\u4eba\u6c11\u5e01\uff08\u7ebd\u7ea6\u5348\u95f4\uff09" },
  { id: "CN_GOLD_RESERVES", label: "\u4e2d\u56fd\u9ec4\u91d1\u50a8\u5907\u6570\u91cf" },
  { id: "CN_GOLD_RESERVES_USD", label: "\u4e2d\u56fd\u9ec4\u91d1\u50a8\u5907\u7f8e\u5143\u4f30\u503c" },
];

export function researchDraft(mode: ResearchMode = "gold"): ResearchDraft {
  return {
    mode, useModel: false, question: RESEARCH_QUESTIONS[mode], symbol: "Au99.99", maxWeight: "10",
    focus: "\u5206\u6790\u4eba\u6c11\u5e01\u9ec4\u91d1\u8d8b\u52bf\u4e0e\u98ce\u9669", startDate: "", endDate: "",
    seriesIds: "DGS10 DFII10 DTWEXBGS DEXCHUS CN_GOLD_RESERVES", topic: "",
    target: "NEXT_OPEN_TO_CLOSE", dataMode: "fixture", snapshotId: "",
  };
}

export function researchPlanRequest(draft: ResearchDraft): ResearchPlanRequest {
  if (!draft.question.trim()) throw new Error("\u8bf7\u586b\u5199\u7814\u7a76\u95ee\u9898");
  const gold = { symbol: draft.symbol, max_weight: Number(draft.maxWeight) / 100, focus: draft.focus.trim() };
  if ((draft.mode === "gold" || draft.mode === "combined") &&
      (!draft.maxWeight.trim() || !Number.isFinite(gold.max_weight) || gold.max_weight < 0 || gold.max_weight > 0.3)) {
    throw new Error("\u4ed3\u4f4d\u4e0a\u9650\u5fc5\u987b\u5728 0 \u5230 30% \u4e4b\u95f4");
  }
  if ((draft.mode === "gold" || draft.mode === "combined") && !gold.focus) {
    throw new Error("\u8bf7\u586b\u5199\u7814\u7a76\u91cd\u70b9");
  }
  if (draft.mode !== "gold" && (!draft.startDate || !draft.endDate || draft.startDate > draft.endDate)) {
    throw new Error("\u8bf7\u586b\u5199\u6709\u6548\u7684\u8d77\u6b62\u65e5\u671f");
  }
  if (draft.mode === "factor" && draft.startDate === draft.endDate) {
    throw new Error("\u56e0\u5b50\u7814\u7a76\u7684\u7ed3\u675f\u65e5\u671f\u5fc5\u987b\u665a\u4e8e\u5f00\u59cb\u65e5\u671f");
  }
  const macro: Record<string, unknown> = { start_date: draft.startDate, end_date: draft.endDate };
  const series = [...new Set(draft.seriesIds.split(/[\s,]+/).filter(Boolean))];
  if ((draft.mode === "macro" || draft.mode === "combined") &&
      (!series.length || series.some((id) => !RESEARCH_MACRO_SERIES.some((entry) => entry.id === id)))) {
    throw new Error("\u8bf7\u9009\u62e9\u81f3\u5c11\u4e00\u9879\u652f\u6301\u7684\u5b8f\u89c2\u5e8f\u5217");
  }
  if (series.length) macro.series_ids = series;
  let payload: Record<string, unknown> = gold;
  if (draft.mode === "macro") payload = macro;
  if (draft.mode === "combined") payload = {
    "gold.collect": { ...gold }, "gold.research": { ...gold }, "macro.collect": macro,
  };
  if (draft.mode === "factor") {
    if (!draft.topic.trim()) throw new Error("\u8bf7\u586b\u5199\u56e0\u5b50\u8bfe\u9898");
    if (draft.dataMode === "frozen_snapshot" && !draft.snapshotId.trim()) {
      throw new Error("\u8bf7\u586b\u5199\u51bb\u7ed3\u5feb\u7167 ID");
    }
    payload = {
      topic: draft.topic.trim(), start_date: draft.startDate, end_date: draft.endDate,
      target: draft.target, data_mode: draft.dataMode,
      ...(draft.dataMode === "frozen_snapshot" ? { data_snapshot_id: draft.snapshotId.trim() } : {}),
    };
  }
  return { question: draft.question.trim(), payload, use_model: draft.useModel };
}

export const RESEARCH_POLL_INTERVAL = 2000;

export const RESEARCH_STATUS: Record<ResearchRunStatus, string> = {
  QUEUED: "\u6392\u961f\u4e2d", RUNNING: "\u8fd0\u884c\u4e2d", COMPLETED: "\u5df2\u5b8c\u6210",
  FAILED: "\u5931\u8d25", INTERRUPTED: "\u5df2\u4e2d\u65ad", CANCELLED: "\u5df2\u53d6\u6d88",
};

export function isResearchActive(run: Pick<ResearchRun, "status">): boolean {
  return run.status === "QUEUED" || run.status === "RUNNING";
}

export function researchHistory(runs: ResearchRun[]): ResearchRun[] {
  return [...new Map(runs.filter((run) => !run.parent_run_id).map((run) => [run.run_id, run])).values()]
    .sort((left, right) => right.created_at.localeCompare(left.created_at));
}

export function researchPlanIssues(plan: ResearchPlan, modules: ResearchModules): string[] {
  if (plan.schema_version !== "research-plan-v1") return ["\u8ba1\u5212\u7248\u672c\u4e0d\u53d7\u652f\u6301"];
  if (!plan.tasks.length) return ["\u8ba1\u5212\u6ca1\u6709\u4efb\u52a1"];
  return plan.tasks.flatMap((task) => {
    const descriptor = modules.modules.find((entry) => entry.task_type === task.task_type);
    if (!descriptor) return [`${task.task_type}: \u4efb\u52a1\u672a\u6ce8\u518c`];
    return descriptor.ready ? [] : [`${task.task_type}: ${descriptor.unavailable_reason || descriptor.missing_roles.join(", ") || "\u672a\u5c31\u7eea"}`];
  });
}

export function researchResultKind(run: ResearchRun): "root" | "gold" | "factor" | "macro" | "macro_research" | "collection" | null {
  switch (run.task_type) {
    case "research.plan":
      return run.result_schema_version === "research-plan-result-v1" && !run.parent_run_id && Array.isArray(run.result?.payload.summaries) ? "root" : null;
    case "gold.research":
      return run.result_schema_version === "gold-deliberation-v4" ? "gold" : null;
    case "factor.research":
      return run.result_schema_version === "1.0" ? "factor" : null;
    case "gold.collect":
      return run.result_schema_version === "1.0" ? "collection" : null;
    case "macro.collect":
      return run.result_schema_version === "macro-collection-v1" ? "collection" : null;
    case "macro.analyze":
      return run.result_schema_version === "macro-analysis-v1" ? "macro" : null;
    case "macro.research":
      return run.result_schema_version === "macro-research-v1" ? "macro_research" : null;
    default:
      return null;
  }
}

export function researchValue(value: unknown): string {
  if (value === null || value === undefined || value === "" || (typeof value === "number" && !Number.isFinite(value))) return "\u672a\u63d0\u4f9b";
  return typeof value === "object" ? JSON.stringify(value, null, 2) : String(value);
}

export function pollResearchRun<Value>(options: {
  read: (signal: AbortSignal) => Promise<Value>;
  active: (value: Value) => boolean;
  onValue: (value: Value) => void;
  onError: (error: unknown) => void;
  signal?: AbortSignal;
  schedule?: typeof setTimeout;
  unschedule?: typeof clearTimeout;
}): () => void {
  const controller = new AbortController();
  const schedule = options.schedule ?? setTimeout;
  const unschedule = options.unschedule ?? clearTimeout;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let stopped = false;
  function stop() {
    stopped = true;
    if (timer !== undefined) unschedule(timer);
    controller.abort();
    options.signal?.removeEventListener("abort", stop);
  }
  async function tick() {
    try {
      const value = await options.read(controller.signal);
      if (stopped) return;
      options.onValue(value);
      if (options.active(value)) timer = schedule(() => { void tick(); }, RESEARCH_POLL_INTERVAL);
      else stop();
    } catch (error) {
      if (stopped) return;
      stop();
      options.onError(error);
    }
  }
  if (options.signal?.aborted) stop();
  else {
    options.signal?.addEventListener("abort", stop, { once: true });
    void tick();
  }
  return stop;
}
import { useEffect, useRef, useState } from "react";
import { api, type ResearchCallReceipt, type ResearchEvent, type ResearchModules, type ResearchPlanRequest,
  type ResearchPlanResponse, type ResearchRun } from "../api";
import { isResearchActive, pollResearchRun, researchHistory, researchPlanIssues } from "../pages/researchModel";

export interface ResearchAudit {
  run_id: string;
  events: ResearchEvent[];
  calls: ResearchCallReceipt[];
}

export interface ResearchSnapshot {
  run: ResearchRun;
  children: ResearchRun[];
  audit: ResearchAudit[];
}

type ResearchReader = Pick<typeof api, "researchRun" | "researchEvents" | "researchCalls">;

async function allPages<Value>(read: (offset: number) => Promise<Value[]>, signal: AbortSignal): Promise<Value[]> {
  const values: Value[] = [];
  while (!signal.aborted) {
    const page = await read(values.length);
    values.push(...page);
    if (page.length < 100) return values;
  }
  throw new DOMException("Aborted", "AbortError");
}

export async function readResearchSnapshot(id: string, signal: AbortSignal, client: ResearchReader = api): Promise<ResearchSnapshot> {
  const run = await client.researchRun(id, signal);
  const children = await Promise.all([...new Set(run.children)].map((child) => client.researchRun(child, signal)));
  const audit = await Promise.all([run, ...children].map(async (entry) => {
    const [events, calls] = await Promise.all([
      allPages((offset) => client.researchEvents(entry.run_id, offset, 100, signal), signal),
      allPages((offset) => client.researchCalls(entry.run_id, offset, 100, signal), signal),
    ]);
    return { run_id: entry.run_id, events, calls };
  }));
  return { run, children, audit };
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function useResearchRun() {
  const [modules, setModules] = useState<ResearchModules | null>(null);
  const [history, setHistory] = useState<ResearchRun[]>([]);
  const [snapshot, setSnapshot] = useState<ResearchSnapshot | null>(null);
  const [plan, setPlan] = useState<ResearchPlanResponse | null>(null);
  const [error, setError] = useState("");
  const [actionError, setActionError] = useState("");
  const [loading, setLoading] = useState(true);
  const [polling, setPolling] = useState(false);
  const [pending, setPending] = useState<"preview" | "submit" | "cancel" | null>(null);
  const [refresh, setRefresh] = useState(0);
  const selected = useRef<string | null>(null);
  const stopPolling = useRef<(() => void) | null>(null);
  const mutation = useRef<AbortController | null>(null);
  const attemptedPlans = useRef(new Set<string>());

  useEffect(() => () => { mutation.current?.abort(); }, []);

  useEffect(() => {
    setLoading(true);
    setError("");
    setPolling(true);
    const stop = pollResearchRun({
      read: async (signal) => {
        const [catalog, runs] = await Promise.all([api.researchModules(signal), api.researchRuns(0, 50, signal)]);
        const roots = researchHistory(runs);
        const id = selected.current ?? catalog.active_run_id ?? roots[0]?.run_id;
        const detail = id ? await readResearchSnapshot(id, signal) : null;
        return { catalog, roots, detail };
      },
      active: ({ catalog, roots, detail }) => Boolean(catalog.busy || catalog.active_run_id ||
        roots.some(isResearchActive) || (detail && [detail.run, ...detail.children].some(isResearchActive))),
      onValue: ({ catalog, roots, detail }) => {
        setModules(catalog);
        setHistory(roots);
        setSnapshot(detail);
        if (detail) selected.current = detail.run.run_id;
        setLoading(false);
        setPolling(Boolean(catalog.busy || catalog.active_run_id || roots.some(isResearchActive) ||
          (detail && [detail.run, ...detail.children].some(isResearchActive))));
      },
      onError: (caught) => { setError(message(caught)); setLoading(false); setPolling(false); },
    });
    stopPolling.current = stop;
    return stop;
  }, [refresh]);

  function reload() {
    stopPolling.current?.();
    setRefresh((value) => value + 1);
  }

  function selectRun(id: string) {
    stopPolling.current?.();
    selected.current = id;
    setSnapshot(null);
    setRefresh((value) => value + 1);
  }

  async function action<Value>(kind: "preview" | "submit" | "cancel", execute: (signal: AbortSignal) => Promise<Value>, accept: (value: Value) => void) {
    if (mutation.current) return;
    const controller = new AbortController();
    mutation.current = controller;
    setPending(kind);
    setActionError("");
    try {
      const value = await execute(controller.signal);
      if (!controller.signal.aborted) accept(value);
    } catch (caught) {
      if (!controller.signal.aborted) setActionError(kind === "submit"
        ? `\u63d0\u4ea4\u7ed3\u679c\u672a\u786e\u8ba4\uff0c\u8bf7\u67e5\u8be2\u8fd0\u884c\u5386\u53f2\uff1b\u672c\u8ba1\u5212\u4e0d\u4f1a\u81ea\u52a8\u91cd\u53d1\u3002${message(caught)}`
        : message(caught));
    } finally {
      if (mutation.current === controller) mutation.current = null;
      if (!controller.signal.aborted) setPending(null);
    }
  }

  function preview(request: ResearchPlanRequest) {
    if (mutation.current) return Promise.resolve();
    setPlan(null);
    return action("preview", (signal) => api.researchPlan(request, signal), setPlan);
  }

  function discardPlan() {
    setPlan(null);
    setActionError("");
  }

  function start() {
    if (!plan || !modules || mutation.current || error || modules.busy || modules.active_run_id ||
        researchPlanIssues(plan.plan, modules).length || attemptedPlans.current.has(plan.plan.plan_id)) return Promise.resolve();
    attemptedPlans.current.add(plan.plan.plan_id);
    const idempotencyKey = crypto.randomUUID();
    return action("submit", (signal) => api.startResearchRun({
      plan: plan.plan, budget: plan.plan.budget, idempotency_key: idempotencyKey,
    }, signal), (run) => {
      selected.current = run.run_id;
      setSnapshot({ run, children: [], audit: [] });
      setHistory((previous) => researchHistory([run, ...previous]));
      reload();
    });
  }

  function cancel() {
    const run = snapshot?.run;
    if (!run || !isResearchActive(run) || run.cancel_requested || mutation.current) return Promise.resolve();
    return action("cancel", (signal) => api.cancelResearchRun(run.run_id, signal), (updated) => {
      setSnapshot((previous) => previous && previous.run.run_id === updated.run_id ? { ...previous, run: updated } : previous);
      reload();
    });
  }

  return {
    modules, history, snapshot, plan, error, actionError, loading, polling, pending,
    submitted: Boolean(plan && attemptedPlans.current.has(plan.plan.plan_id)),
    reload, selectRun, preview, discardPlan, start, cancel,
  };
}
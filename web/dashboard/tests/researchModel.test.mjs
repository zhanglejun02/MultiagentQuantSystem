import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { setImmediate } from "node:timers/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../src/pages/researchModel.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, {
  fileName: "researchModel.ts",
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
  reportDiagnostics: true,
});
assert.deepEqual(compiled.diagnostics, []);
const { researchDraft, researchPlanRequest, pollResearchRun, researchHistory, researchResultKind, researchValue } = await import(
  `data:text/javascript;base64,${Buffer.from(compiled.outputText).toString("base64")}`
);

test("gold preview uses deterministic planning and does not submit a run", (context) => {
  context.mock.method(globalThis, "fetch", () => assert.fail("request assembly is offline"));
  const request = researchPlanRequest(researchDraft());
  assert.equal(request.use_model, false);
  assert.deepEqual(request.payload, { symbol: "Au99.99", max_weight: 0.1, focus: "分析人民币黄金趋势与风险" });
  assert.equal(request.plan, undefined);
});

test("model planning is opt-in and stays outside task payload schemas for every mode", () => {
  for (const mode of ["gold", "macro", "factor", "combined"]) {
    const draft = { ...researchDraft(mode), startDate: "2026-01-01", endDate: "2026-10-16", topic: "factor topic" };
    assert.equal(draft.useModel, false);
    const deterministic = researchPlanRequest(draft);
    const modeled = researchPlanRequest({ ...draft, useModel: true });
    assert.equal(deterministic.use_model, false);
    assert.equal(modeled.use_model, true);
    assert.deepEqual(modeled.payload, deterministic.payload);
    assert.deepEqual(Object.keys(modeled), ["question", "payload", "use_model"]);
    assert.ok(!JSON.stringify(modeled.payload).includes("use_model"));
    assert.ok(!JSON.stringify(modeled.payload).includes("useModel"));
  }
});

test("macro series default to registered facts and empty or unknown selections are rejected locally", () => {
  const draft = { ...researchDraft("macro"), startDate: "2026-01-01", endDate: "2026-10-16" };
  assert.deepEqual(researchPlanRequest(draft).payload.series_ids, ["DGS10", "DFII10", "DTWEXBGS", "DEXCHUS", "CN_GOLD_RESERVES"]);
  for (const seriesIds of ["", " ", "DXY", "DGS10, unknown"]) assert.throws(() => researchPlanRequest({ ...draft, seriesIds }));
  assert.deepEqual(researchPlanRequest({ ...draft, seriesIds: "CN_GOLD_RESERVES_USD" }).payload.series_ids, ["CN_GOLD_RESERVES_USD"]);
});

test("combined preview keeps task-namespaced payloads and independent collection inputs", () => {
  const draft = { ...researchDraft("combined"), startDate: "2026-01-01", endDate: "2026-10-16", seriesIds: "DGS10, DFII10 DGS10" };
  const before = structuredClone(draft);
  const request = researchPlanRequest(draft);
  assert.deepEqual(Object.keys(request.payload), ["gold.collect", "gold.research", "macro.collect"]);
  assert.deepEqual(request.payload["macro.collect"], {
    start_date: draft.startDate, end_date: draft.endDate, series_ids: ["DGS10", "DFII10"],
  });
  assert.equal(request.payload["gold.research"].max_weight, 0.1);
  assert.deepEqual(draft, before);
});

test("factor frozen snapshots are explicit and fixture requests omit stale snapshot IDs", () => {
  const draft = { ...researchDraft("factor"), topic: "factor", startDate: "2024-02-01", endDate: "2024-05-31", dataMode: "frozen_snapshot" };
  assert.throws(() => researchPlanRequest(draft));
  assert.equal(researchPlanRequest({ ...draft, snapshotId: "snapshot-1" }).payload.data_snapshot_id, "snapshot-1");
  assert.equal(researchPlanRequest({ ...draft, dataMode: "fixture", snapshotId: "stale" }).payload.data_snapshot_id, undefined);
});

test("invalid dates, questions and weight limits fail before planning", () => {
  for (const maxWeight of ["", "30.01", "101", "NaN", "Infinity", "-1"]) {
    assert.throws(() => researchPlanRequest({ ...researchDraft(), maxWeight }));
  }
  assert.equal(researchPlanRequest({ ...researchDraft(), maxWeight: "0" }).payload.max_weight, 0);
  assert.throws(() => researchPlanRequest({ ...researchDraft(), focus: " " }));
  assert.throws(() => researchPlanRequest({ ...researchDraft(), question: " " }));
  assert.throws(() => researchPlanRequest(researchDraft("macro")));
  assert.throws(() => researchPlanRequest({ ...researchDraft("macro"), startDate: "2026-02-01", endDate: "2026-01-01" }));
  assert.throws(() => researchPlanRequest({ ...researchDraft("factor"), topic: "factor", startDate: "2026-02-01", endDate: "2026-02-01" }));
});

test("polling waits 2 seconds after a completed read and stops on terminal state", async () => {
  const queued = [];
  const values = [];
  let count = 0;
  pollResearchRun({
    read: async () => ++count,
    active: (value) => value < 2,
    onValue: (value) => values.push(value),
    onError: assert.fail,
    schedule: (callback, delay) => { assert.equal(delay, 2000); queued.push(callback); return 1; },
    unschedule: () => {},
  });
  await setImmediate();
  assert.deepEqual(values, [1]);
  queued.shift()();
  await setImmediate();
  assert.deepEqual(values, [1, 2]);
  assert.equal(queued.length, 0);
});

test("a read error stops polling without retrying or manufacturing progress", async () => {
  const errors = [];
  let reads = 0;
  let signal;
  pollResearchRun({
    read: async (received) => { signal = received; reads++; throw new Error("fixture offline"); },
    active: () => true,
    onValue: assert.fail,
    onError: (error) => errors.push(error.message),
    schedule: () => assert.fail("no retry after failure"),
  });
  await setImmediate();
  assert.deepEqual(errors, ["fixture offline"]);
  assert.equal(reads, 1);
  assert.equal(signal.aborted, true);
});

test("unmount cleanup aborts in-flight reads and ignores late responses", async () => {
  let finish;
  let signal;
  const stop = pollResearchRun({
    read: (received) => { signal = received; return new Promise((resolve) => { finish = resolve; }); },
    active: () => true, onValue: assert.fail, onError: assert.fail,
    schedule: () => assert.fail("no timer after cleanup"),
  });
  stop();
  assert.equal(signal.aborted, true);
  finish("late");
  await setImmediate();
});

test("external abort cancels scheduled polls and pre-aborted requests never start", async () => {
  const controller = new AbortController();
  let cleared = false;
  pollResearchRun({
    read: async () => 1, active: () => true, onValue: () => {}, onError: assert.fail,
    signal: controller.signal, schedule: () => 7, unschedule: (timer) => { assert.equal(timer, 7); cleared = true; },
  });
  await setImmediate();
  controller.abort();
  assert.equal(cleared, true);
  pollResearchRun({ read: assert.fail, active: assert.fail, onValue: assert.fail, onError: assert.fail, signal: controller.signal });
});

test("root history is deduplicated without changing child or source data", () => {
  const runs = [
    { run_id: "child", parent_run_id: "root", created_at: "2026-10-17" },
    { run_id: "old", parent_run_id: null, created_at: "2026-10-15" },
    { run_id: "root", parent_run_id: null, created_at: "2026-10-16" },
  ];
  const before = structuredClone(runs);
  assert.deepEqual(researchHistory([...runs, runs[2]]).map((run) => run.run_id), ["root", "old"]);
  assert.deepEqual(runs, before);
});

test("each task recognizes only its actual supported result version", () => {
  const supported = [
    ["research.plan", "research-plan-result-v1", "root"],
    ["gold.research", "gold-deliberation-v4", "gold"],
    ["gold.collect", "1.0", "collection"],
    ["factor.research", "1.0", "factor"],
    ["macro.collect", "macro-collection-v1", "collection"],
    ["macro.analyze", "macro-analysis-v1", "macro"],
    ["macro.research", "macro-research-v1", "macro_research"],
  ];
  for (const [task_type, result_schema_version, kind] of supported) {
    const run = { task_type, result_schema_version, parent_run_id: null, result: { payload: { summaries: [] } } };
    assert.equal(researchResultKind(run), kind, task_type);
    assert.equal(researchResultKind({ ...run, result_schema_version: "future-v99" }), null, task_type);
    if (result_schema_version !== "1.0") assert.equal(researchResultKind({ ...run, result_schema_version: "1.0" }), null, task_type);
  }
  assert.equal(researchResultKind({ task_type: "unknown.task", result_schema_version: "research-plan-result-v1", result: { payload: { summaries: [] } } }), null);
  assert.equal(researchResultKind({ task_type: "research.plan", parent_run_id: "root", result_schema_version: "research-plan-result-v1", result: { payload: { summaries: [] } } }), null);
});

test("missing evidence never becomes zero", () => {
  assert.equal(researchValue(null), "未提供");
  assert.equal(researchValue(false), "false");
  assert.equal(researchValue(0), "0");
  assert.equal(researchValue("0.10000000000000001"), "0.10000000000000001");
});
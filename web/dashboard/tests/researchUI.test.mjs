import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

async function moduleUrl(path, imports = {}) {
  const source = await readFile(new URL(path, import.meta.url), "utf8");
  const compiled = ts.transpileModule(source, {
    fileName: path,
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.ReactJSX },
    reportDiagnostics: true,
    transformers: { after: [(context) => (file) => ts.visitEachChild(file, (node) => {
      if (!ts.isImportDeclaration(node) || !ts.isStringLiteral(node.moduleSpecifier)) return node;
      return ts.factory.updateImportDeclaration(node, node.modifiers, node.importClause,
        ts.factory.createStringLiteral(imports[node.moduleSpecifier.text] ?? import.meta.resolve(node.moduleSpecifier.text)), node.attributes);
    }, context)] },
  });
  assert.deepEqual(compiled.diagnostics, []);
  return `data:text/javascript;base64,${Buffer.from(compiled.outputText).toString("base64")}`;
}

const apiUrl = await moduleUrl("../src/api.ts");
const modelUrl = await moduleUrl("../src/pages/researchModel.ts");
const hookUrl = await moduleUrl("../src/hooks/useResearchRun.ts", { "../api": apiUrl, "../pages/researchModel": modelUrl });
const cssUrl = "data:text/javascript,export default {}";
const { default: Research, ResearchBudget, ResearchCalls, ResearchEvents, ResearchPlanPreview, ResearchResult, ResearchRunView } =
  await import(await moduleUrl("../src/pages/Research.tsx", {
    "../api": apiUrl, "../hooks/useResearchRun": hookUrl, "./researchModel": modelUrl, "./research.css": cssUrl,
  }));

const budget = { max_calls: 12, max_tokens: 64000, timeout_seconds: 300, max_parallel_calls: 3 };
function makeRun(overrides = {}) {
  return {
    run_id: "root-1", root_run_id: "root-1", parent_run_id: null, task_id: null, task_type: "research.plan",
    module_id: "research", task_version: "1.0", result_schema_version: "research-plan-result-v1", status: "RUNNING", stage: "collecting",
    created_at: "2026-10-17T01:00:00Z", updated_at: "2026-10-17T01:01:00Z", request: {}, knowledge_cutoff: null,
    result: null, error: null, children: [], cancel_requested: false, revision: 3,
    usage: { calls_used: 2, tokens_used: 1200, tokens_reserved: 900, max_calls: 7, max_tokens: 12000 }, ...overrides,
  };
}
function makeResult(overrides = {}) {
  return { outcome: "OBSERVE", summary: "server summary", payload: {}, evidence: [],
    publication: { decision: "NOT_APPLICABLE", content_hash: null, reason: null }, limitations: [], report_artifact_id: "report-1", ...overrides };
}
function render(Component, props) { return renderToStaticMarkup(createElement(Component, props)); }

test("research first render is readonly, uses four modes and never invents elapsed events", (context) => {
  context.mock.method(globalThis, "fetch", () => assert.fail("SSR is readonly"));
  const markup = render(Research);
  for (const text of ["黄金", "宏观", "因子", "联合", "预览计划", "研究问题", "仓位上限", "运行历史"]) assert.ok(markup.includes(text), text);
  assert.ok(!markup.includes("确认运行"));
  assert.ok(!markup.includes("00:00:01"));
  assert.ok(!markup.includes("workflow"));
  assert.ok(markup.includes("模型规划"));
  assert.ok(markup.includes('title="模型规划会调用模型并产生费用；关闭时使用规则规划"'));
  assert.match(markup, /<input(?=[^>]*type="checkbox")(?![^>]*checked)[^>]*>/);
});

test("plan preview shows dependencies, input refs and root budget before explicit run", () => {
  const plan = { schema_version: "research-plan-v1", plan_id: "plan-1", question: "黄金宏观联合研究", mode: "LATEST", knowledge_cutoff: null,
    source: "RULE", budget, tasks: [
      { task_id: "collect", task_type: "gold.collect", payload: { symbol: "Au99.99" }, inputs: {}, depends_on: [], required: true, budget },
      { task_id: "research", task_type: "gold.research", payload: {}, inputs: { snapshot_id: { task_id: "collect", output_key: "snapshot_id" } }, depends_on: ["collect"], required: true, budget },
    ] };
  const props = { response: { plan, artifact_id: "plan-artifact", receipts: [] }, onRun: assert.fail };
  const markup = render(ResearchPlanPreview, props);
  for (const text of ["计划预览", "确认运行", "gold.collect", "gold.research", "snapshot_id = collect.snapshot_id", "64000", "plan-artifact"]) assert.ok(markup.includes(text), text);
  assert.ok(!markup.includes('disabled=""'));
  assert.ok(render(ResearchPlanPreview, { ...props, issues: ["gold.research: 缺少凭据"] }).includes('disabled=""'));
  assert.ok(render(ResearchPlanPreview, { ...props, submitted: true }).includes("已提交本计划"));
});

test("budget uses the selected server ledger, including reservation and zero, without child summation", () => {
  const markup = render(ResearchBudget, { run: makeRun({ usage: { calls_used: 0, tokens_used: 0, tokens_reserved: 900, max_calls: 7, max_tokens: 12000 } }) });
  for (const text of ["0 / 7", "0 / 12000", "900"]) assert.ok(markup.includes(text), text);
  assert.ok(!markup.includes("64000"));
});

test("root rendering uses fetched child publication permission, never withheld summary or advice", () => {
  const secret = "WITHHELD_SECRET_ADVICE";
  const child = makeRun({ run_id: "gold-child", parent_run_id: "root-1", task_id: "gold", task_type: "gold.research", result_schema_version: "gold-deliberation-v4",
    status: "COMPLETED", result: makeResult({ summary: secret, publication: { decision: "WITHHELD", content_hash: null, reason: "审核拒绝发布" },
      payload: { investment_advice: { summary: secret }, proposal: { hypothesis: secret }, evaluation: { symbol: "Au99.99", as_of: "2026-10-16" } } }) });
  const childMarkup = render(ResearchResult, { run: child });
  assert.ok(!childMarkup.includes(secret));
  for (const text of ["WITHHELD", "审核拒绝发布", 'href="/gold"', "Au99.99"]) assert.ok(childMarkup.includes(text), text);
  const root = makeRun({ status: "COMPLETED", children: [child.run_id], result: makeResult({ payload: {
    summaries: [{ task_id: "gold", task_type: "gold.research", run_id: child.run_id, status: "COMPLETED", outcome: "OBSERVE", summary: secret, approved_publication_hash: null }],
    approved_publication_hashes: [], approved_publications: [{ summary: secret }], unpublished_hash: { advice: secret },
  } }) });
  for (const children of [[], [child]]) assert.ok(!render(ResearchResult, { run: root, children }).includes(secret));
  const publicChild = { ...child, result: makeResult({ summary: "approved server summary", publication: { decision: "PASS", content_hash: "approved-hash", reason: null } }) };
  assert.ok(!render(ResearchResult, { run: root, children: [publicChild] }).includes("approved server summary"));
  root.result.payload.summaries[0].approved_publication_hash = "approved-hash";
  root.result.payload.approved_publication_hashes = ["approved-hash"];
  const approved = render(ResearchResult, { run: root, children: [publicChild] });
  assert.ok(approved.includes("approved server summary"));
  assert.ok(approved.includes("approved_publication_hashes"));
  assert.ok(!approved.includes(secret));
  assert.ok(!render(ResearchResult, { run: root, children: [{ ...publicChild, result_schema_version: "future-v99" }] }).includes("approved server summary"));
  assert.ok(!render(ResearchResult, { run: { ...root, cancel_requested: true }, children: [publicChild] }).includes("approved server summary"));
});

test("unknown result version is unavailable and does not render unrecognized payload", () => {
  const markup = render(ResearchResult, { run: makeRun({ task_type: "macro.analyze", result_schema_version: "future-v99",
    result: makeResult({ summary: "unknown secret", payload: { summary: "unknown secret" } }) }) });
  assert.ok(markup.includes("结果不可用"));
  assert.ok(markup.includes("future-v99"));
  assert.ok(!markup.includes("unknown secret"));
});

test("macro indicators retain exact values, individual observation and availability times, missing series and safe sources", () => {
  const run = makeRun({ task_type: "macro.analyze", result_schema_version: "macro-analysis-v1", result: makeResult({ payload: { snapshot_id: "macro-snapshot", indicators: {
    DGS10: { title: "美国10年国债收益率", latest_value: "4.2500000001", unit: "%", observation_date: "2026-10-15", available_at: "2026-10-16T18:00:00Z", source_url: "https://fred.stlouisfed.org/series/DGS10", change_bp: -3 },
    DFII10: { title: "实际利率", latest_value: null, unit: "%", observation_date: null, available_at: null, source_url: "javascript:alert(1)" },
  }, coverage: { present: 1, total: 2 }, missing: ["DFII10"], limitations: ["fixture limitation"], observations_metadata: { vintage: "2026-10-16" } } }) });
  const markup = render(ResearchResult, { run });
  for (const text of ["macro-snapshot", "4.2500000001", "2026-10-15", "2026-10-16T18:00:00Z", "change_bp: -3", "DFII10", "fixture limitation", "观测元数据", "未提供"]) assert.ok(markup.includes(text), text);
  assert.ok(markup.includes('href="https://fred.stlouisfed.org/series/DGS10"'));
  assert.ok(!markup.includes("javascript:"));
  assert.ok(!markup.includes("DXY"));
  assert.ok(!markup.includes("独立观点"));
});

test("macro research separates nested facts from independent cited perspectives and withholds unpublished opinions", () => {
  const run = makeRun({ task_type: "macro.research", result_schema_version: "macro-research-v1", result: makeResult({ payload: {
    facts: { snapshot_id: "macro-facts", indicators: { DFII10: { latest_value: -0.25, unit: "percent", observation_date: "2026-10-15", available_at: "2026-10-16T18:00:00Z" } },
      missing: { DGS10: "not available" }, coverage: { DFII10: { status: "AVAILABLE" } }, limitations: ["frozen facts only"] },
    perspectives: {
      "macro.rates": { state: "EVIDENCE_BOUNDED", summary: "independent rates view", claims: [{ claim: "negative observed real yield", evidence_ids: ["fact-DFII10"] }], missing: [], risks: ["no causal inference"] },
      "macro.risks": { state: "INSUFFICIENT_EVIDENCE", summary: "independent evidence risk view", claims: [], missing: ["historical vintage"], risks: ["revision risk"] },
    }, interpretation_status: "MODEL_INTERPRETATION", synthesis: { state: "INSUFFICIENT_EVIDENCE" },
  } }) });
  const markup = render(ResearchResult, { run });
  for (const text of ["macro-facts", "-0.25", "percent", "frozen facts only", "独立观点", "macro.rates", "macro.risks", "independent rates view", "independent evidence risk view", "fact-DFII10", "historical vintage", "revision risk", "MODEL_INTERPRETATION"]) assert.ok(markup.includes(text), text);
  assert.ok(!markup.includes("暂无指标"));
  const withheld = render(ResearchResult, { run: { ...run, result: { ...run.result, publication: { decision: "WITHHELD", content_hash: null, reason: "not published" } } } });
  assert.ok(withheld.includes("-0.25"));
  for (const text of ["独立观点", "independent rates view", "independent evidence risk view", "negative observed real yield"]) assert.ok(!withheld.includes(text), text);
  const future = render(ResearchResult, { run: { ...run, result_schema_version: "macro-research-v99" } });
  assert.ok(future.includes("结果不可用"));
  assert.ok(!future.includes("independent rates view"));
});

test("collection results accept actual gold and macro versions without claiming model interpretation", () => {
  for (const [task_type, result_schema_version] of [["gold.collect", "1.0"], ["macro.collect", "macro-collection-v1"]]) {
    const markup = render(ResearchResult, { run: makeRun({ task_type, result_schema_version, result: makeResult({ payload: { snapshot_id: "collected-snapshot" } }) }) });
    assert.ok(markup.includes("采集记录"), task_type);
    assert.ok(markup.includes("collected-snapshot"), task_type);
    assert.ok(!markup.includes("独立观点"));
  }
});

test("factor renderer retains old domain payload and local gate failures", () => {
  const run = makeRun({ task_type: "factor.research", result_schema_version: "1.0", result: makeResult({ payload: {
    candidate: { name: "factor candidate", expression: "rank(close)", hypothesis: "test hypothesis" }, experiment: { state: "SCREENED" },
    factor_run: { metrics: { rank_ic: -0.01, icir: 0, coverage: 1, turnover: 0.1 } }, local_gates: { independent_oos: false },
    critic: { rationale: "critic rationale" }, statistician: { rationale: "statistician rationale" }, decision: { rationale: "decision rationale" },
  } }) });
  const markup = render(ResearchResult, { run });
  for (const text of ["rank(close)", "factor candidate", "SCREENED", "-0.0100", "100.00%", "independent_oos", "FAIL", "critic rationale"]) assert.ok(markup.includes(text), text);
});

test("events use only backend timestamps, sequences and escaped data", () => {
  assert.ok(render(ResearchEvents, { events: [] }).includes("暂无服务器事件"));
  const markup = render(ResearchEvents, { events: [{ run_id: "root", sequence: 9, event_type: "budget_reserved", stage: "analysis", at: "2026-10-17T02:03:04Z", data: { message: "<script>test</script>", tokens: 0 } }] });
  for (const text of ["budget_reserved", "analysis", "2026-10-17T02:03:04Z", "&lt;script&gt;", "tokens"]) assert.ok(markup.includes(text), text);
  assert.ok(!markup.includes("00:00:01"));
  assert.ok(!markup.includes("<script>"));
});

test("aggregated events sort by absolute time then run ID then per-run sequence, retaining duplicate ones", () => {
  const events = [
    { run_id: "child-b", sequence: 1, event_type: "b-first", stage: "run", at: "2026-10-17T02:00:00Z", data: {} },
    { run_id: "child-a", sequence: 2, event_type: "a-second", stage: "run", at: "2026-10-17T02:00:00Z", data: {} },
    { run_id: "child-a", sequence: 1, event_type: "a-first", stage: "run", at: "2026-10-17T02:00:00Z", data: {} },
    { run_id: "root", sequence: 2, event_type: "root-second", stage: "run", at: "2026-10-17T09:30:00+08:00", data: {} },
    { run_id: "root", sequence: 1, event_type: "root-first", stage: "run", at: "2026-10-17T01:00:00Z", data: {} },
  ];
  const before = structuredClone(events);
  const markup = render(ResearchEvents, { events });
  assert.deepEqual([...markup.matchAll(/<strong>([^<]+)<\/strong>/g)].map((match) => match[1]), ["root-first", "root-second", "a-first", "a-second", "b-first"]);
  assert.equal((markup.match(/class="research-event-sequence">1</g) ?? []).length, 3);
  assert.deepEqual(events, before);
});

test("receipts preserve errors, parse failures, request IDs and raw messages without inventing usage", () => {
  const markup = render(ResearchCalls, { calls: [{ call_id: "call-1", sequence: 4, role: "risk_analyst", provider: "fixture", model: "mock",
    purpose: "independent review", status: "FAILED", messages: [{ role: "user", content: "<external>" }],
    response: { content: "malformed response", input_tokens: null, output_tokens: 0, raw_request_id: "request-42" },
    error: "provider error", parse_error: "schema mismatch", reservation_tokens: 1000, used_tokens: 1000 }] });
  for (const text of ["call-1", "risk_analyst", "provider error", "schema mismatch", "request-42", "&lt;external&gt;", "malformed response", "未提供"]) assert.ok(markup.includes(text), text);
  assert.ok(!markup.includes("<details open"));
});

test("run view shows real root and child states, keeps cooperative cancel pending and never infers completion", () => {
  const child = makeRun({ run_id: "child", parent_run_id: "root-1", task_id: "macro", task_type: "macro.collect", status: "QUEUED", stage: "awaiting_dependencies" });
  const root = makeRun({ children: [child.run_id], cancel_requested: true });
  const snapshot = { run: root, children: [child], audit: [] };
  const markup = render(ResearchRunView, { snapshot, pending: false, onCancel: assert.fail });
  for (const text of ["根任务", "macro.collect", "排队中", "awaiting_dependencies", "已请求取消，等待服务器确认", "结果尚未返回", 'role="tablist"']) assert.ok(markup.includes(text), text);
  assert.match(markup, /<button(?=[^>]*aria-label="已请求取消")(?=[^>]*disabled="")[^>]*>/);
  assert.ok(!markup.includes("已完成"));
  const terminal = render(ResearchRunView, { snapshot: { ...snapshot, run: { ...root, status: "CANCELLED", stage: "cancelled" } }, pending: false, onCancel: assert.fail });
  assert.ok(terminal.includes("已取消"));
  assert.ok(!terminal.includes("等待服务器确认"));
});
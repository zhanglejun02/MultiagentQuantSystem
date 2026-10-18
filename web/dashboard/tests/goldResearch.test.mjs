import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { setImmediate } from "node:timers/promises";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const source = await readFile(new URL("../src/pages/goldModel.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, {
  fileName: "goldModel.ts",
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
  reportDiagnostics: true,
});
assert.deepEqual(compiled.diagnostics, []);
const modelModuleUrl = `data:text/javascript;base64,${Buffer.from(compiled.outputText).toString("base64")}`;
const {
  activeGoldResearchRunId, display, goldEvidenceItem, goldEvidenceValue, goldInvestmentEvidence, goldLargestDisagreement, GOLD_INSTRUMENTS, goldResearchHistory,
  goldResearchPriceFormat, goldResearchReport, goldResearchRequest, goldResearchRunLabel,
  goldResearchStageLabel, goldResearchSteps, goldResearchStepState, isGoldDeliberationRun, isGoldResearchActive,
  pollGoldResearchRun, shouldHydrateGoldDraft,
} = await import(modelModuleUrl);

const apiSource = await readFile(new URL("../src/api.ts", import.meta.url), "utf8");
const apiCompiled = ts.transpileModule(apiSource, {
  fileName: "api.ts",
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
  reportDiagnostics: true,
});
assert.deepEqual(apiCompiled.diagnostics, []);
const apiModuleUrl = `data:text/javascript;base64,${Buffer.from(apiCompiled.outputText).toString("base64")}`;
const { api } = await import(apiModuleUrl);

async function componentModule(path, imports) {
  const componentSource = await readFile(new URL(path, import.meta.url), "utf8");
  const component = ts.transpileModule(componentSource, {
    fileName: path,
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.ReactJSX },
    reportDiagnostics: true,
    transformers: { after: [(context) => (file) => ts.visitEachChild(file, (node) => {
      if (!ts.isImportDeclaration(node) || !ts.isStringLiteral(node.moduleSpecifier)) return node;
      const specifier = node.moduleSpecifier.text;
      return ts.factory.updateImportDeclaration(node, node.modifiers, node.importClause,
        ts.factory.createStringLiteral(imports[specifier] ?? import.meta.resolve(specifier)), node.attributes);
    }, context)] },
  });
  assert.deepEqual(component.diagnostics, []);
  return `data:text/javascript;base64,${Buffer.from(component.outputText).toString("base64")}`;
}

const adviceModuleUrl = await componentModule("../src/pages/GoldInvestmentAdvice.tsx", { "./goldModel": modelModuleUrl });
const { default: GoldInvestmentAdvice } = await import(adviceModuleUrl);
const { default: GoldResearch, GoldEvidenceReferences, GoldResearchBudget, GoldResearchCalls, GoldResearchDeliberation,
  GoldResearchEvidence, GoldResearchMarketView, GoldResearchProgress, GoldResearchRoutes } = await import(await componentModule("../src/pages/GoldResearch.tsx", {
  "../api": apiModuleUrl, "./goldModel": modelModuleUrl, "./GoldInvestmentAdvice": adviceModuleUrl,
}));

function makeAdvice(overrides = {}) {
  return {
    horizon: "3至12个月", stance: "WAIT", summary: "等待趋势与成本证据改善，暂不增加配置。",
    reasons: [
      { evidence_key: "gold_return_126", value: 0.125, implication: "中期涨幅不代表未来收益。" },
      { evidence_key: "independent_oos", value: false, implication: "尚不能认定策略稳健。" },
    ],
    reassess_when: "出现新的独立验证证据后重新评估。", confidence: "LOW",
    limitations: "全样本诊断，没有独立样本外证据。", ...overrides,
  };
}

function makeEvidence(overrides = {}) {
  return { snapshot_id: "snapshot-v4", as_of: "2026-10-15", items: {
    price_trend: { category: "price", value: -0.125, definition: "126 个观测的收盘价收益，不代表预测收益。", source: "本地黄金快照" },
    independent_oos: { category: "diagnostic", value: false, definition: "是否经过独立样本外验证。", source: "本地研究约束" },
    macro_rate: { category: "macro", value: null, definition: "宏观利率序列，尚未采集。", source: "未接入" },
    trade_count: { category: "diagnostic", value: 0, definition: "诊断交易次数。", source: "本地固定规则诊断" },
    position_cap: { category: "constraint", value: "0.10", definition: "用户研究仓位上限。", source: "本轮请求" },
  }, limitations: ["无宏观序列与独立样本外证据。"], ...overrides };
}

test("deliberation perspectives keep initial and revised views, explicit macro abstention and all cited evidence", (context) => {
  context.mock.method(globalThis, "fetch", () => assert.fail("perspectives must not fetch or call models"));
  const initial = makeState("technical_analyst", { view: "BULLISH", confidence: 0.8, summary: "初始技术判断偏多。" });
  const revised = makeState("technical_analyst", { confidence: 0.4, summary: "证据约束后降低置信度。", risks: ["趋势反复风险"],
    open_questions: ["趋势能否持续？"], invalidating_conditions: ["价格重新跌破慢均线"], missing_evidence: ["更长样本验证"],
    claims: [{ claim: "价格证据仅支持条件判断。", confidence: 0.4, evidence_ids: ["price_trend", "position_cap"],
      counterevidence_ids: ["independent_oos"] }] });
  const review = makeDeliberation();
  review.initial_states[0] = initial;
  review.states[0] = revised;
  review.review_status = "REVISED";
  review.revisions = [{ action: "LOWER_CONFIDENCE", state: revised, addressed_disagreement_ids: ["direction-1"],
    evidence_requests: ["独立留出样本"], rationale: "缺少独立验证，不将价格趋势外推。" }];
  review.disagreements = [{ disagreement_id: "direction-1", kind: "DIRECTION", analysts: ["technical_analyst", "risk_analyst"],
    question: "趋势支持是否足以覆盖下行风险？", evidence_ids: ["price_trend", "independent_oos"] }];
  const result = { deliberation: review, evidence_package: makeEvidence() };
  const original = structuredClone(result);
  const markup = renderToStaticMarkup(createElement(GoldResearchDeliberation, { result }));
  assert.equal((markup.match(/class="gold-perspective"/g) ?? []).length, 4);
  for (const text of ["四方独立观点", "技术分析师", "宏观分析师", "量化分析师", "风险分析师", "置信度 0%", "证据不足 / 弃权",
    "初始 偏多 · 80%", "复核后 中性 · 40%", initial.summary, revised.summary, "降低置信度", review.revisions[0].rationale,
    "已完成修订", "结论受证据约束", "最大分歧", review.disagreements[0].question, "回应分歧 direction-1", "更长样本验证",
    "独立留出样本", "利率、汇率与储备时间序列", "已跳过 · 无模型调用", "反向证据", "price_trend", "position_cap", "independent_oos",
    "-0.125", "false", "本地黄金快照", result.evidence_package.items.price_trend.definition, "趋势反复风险", "价格重新跌破慢均线"]) {
    assert.ok(markup.includes(text), `Missing ${text}`);
  }
  assert.ok(!markup.includes("研究员"));
  assert.ok(!markup.includes("批判者"));
  assert.ok(!markup.includes("统计员"));
  assert.ok(!markup.includes("<details open"));
  assert.deepEqual(result, original);
});

test("missing states, macro values and unresolved evidence IDs never turn into fabricated neutral or zero evidence", () => {
  const review = makeDeliberation({ review_status: "BUDGET_SKIPPED", conclusion: "INSUFFICIENT_EVIDENCE" });
  review.states[0].claims = [{ claim: "缺失引用不能支持判断。", confidence: 0.2, evidence_ids: ["missing_series", "macro_rate"], counterevidence_ids: ["independent_oos"] }];
  review.states = review.states.filter((state) => state.analyst !== "quant_analyst");
  review.initial_states = review.initial_states.filter((state) => state.analyst !== "quant_analyst");
  const markup = renderToStaticMarkup(createElement(GoldResearchDeliberation, { result: { deliberation: review, evidence_package: makeEvidence() } }));
  for (const text of ["预算不足 / 跳过修订", "证据不足", "缺失或空值引用", "missing_series", "macro_rate", "证据未提供，无法核验引用", "本地值</dt><dd>未提供"]) {
    assert.ok(markup.includes(text), `Missing ${text}`);
  }
  const quant = markup.split('data-analyst="quant_analyst"')[1].split("</article>")[0];
  assert.ok(quant.includes("观点未返回"));
  assert.ok(quant.includes("置信度 未提供"));
  assert.ok(!quant.includes("中性"));
  assert.ok(!quant.includes("0%"));
  const noPackage = renderToStaticMarkup(createElement(GoldResearchDeliberation, { result: { deliberation: review } }));
  assert.ok(noPackage.includes("未返回结构化证据包"));
  assert.ok(!noPackage.includes("宏观分析师预测"));
});

test("all revision actions and review outcomes preserve backend wording and never imply resolved consensus", () => {
  for (const [action, label] of [["MAINTAIN", "维持观点"], ["LOWER_CONFIDENCE", "降低置信度"], ["RETRACT", "撤回观点"]]) {
    const state = makeState("risk_analyst", { view: "INSUFFICIENT_EVIDENCE", confidence: 0 });
    const review = makeDeliberation({ review_status: "UNRESOLVED", conclusion: "INSUFFICIENT_EVIDENCE",
      revisions: [{ action, state, addressed_disagreement_ids: [], evidence_requests: [], rationale: "不能消除证据缺口。" }] });
    review.states[3] = state;
    const markup = renderToStaticMarkup(createElement(GoldResearchDeliberation, { result: { deliberation: review, evidence_package: makeEvidence() } }));
    for (const text of [label, "分歧未解决", "不能消除证据缺口。", "复核后 证据不足 / 弃权 · 0%"]) assert.ok(markup.includes(text));
    assert.ok(!markup.includes("达成共识"));
  }
  const initialOnly = makeDeliberation({ states: [] });
  const markup = renderToStaticMarkup(createElement(GoldResearchDeliberation, { result: { deliberation: initialOnly } }));
  assert.ok(markup.includes("初始观点 · 最终观点未返回"));
});

test("evidence values retain false, exact strings and real zero while null and nonfinite values stay missing", () => {
  for (const [value, expected] of [[false, "false"], [true, "true"], [0, "0"], [-0.125, "-0.125"], ["0.10", "0.10"],
    [null, "未提供"], [undefined, "未提供"], [NaN, "未提供"], [Infinity, "未提供"], ["", "未提供"]]) {
    assert.equal(goldEvidenceValue(value), expected);
  }
  const markup = renderToStaticMarkup(createElement(GoldEvidenceReferences, {
    ids: ["trade_count", "independent_oos", "macro_rate", "position_cap", "trade_count"], evidence: makeEvidence(),
  }));
  assert.equal((markup.match(/class="gold-evidence-reference"/g) ?? []).length, 4);
  assert.ok(markup.includes("本地值</dt><dd>0</dd>"));
  assert.ok(markup.includes("本地值</dt><dd>false</dd>"));
  assert.ok(markup.includes("本地值</dt><dd>0.10</dd>"));
  assert.ok(markup.includes("本地值</dt><dd>未提供</dd>"));
  assert.ok(markup.includes('title="诊断交易次数。；本地值：0；来源：本地固定规则诊断"'));
});

test("evidence references escape external text and do not resolve inherited object keys as evidence", () => {
  for (const id of ["toString", "constructor", "__proto__"]) assert.equal(goldEvidenceItem(makeEvidence(), id), undefined);
  assert.equal(goldEvidenceItem(undefined, "price_trend"), undefined);
  const markup = renderToStaticMarkup(createElement(GoldEvidenceReferences, { ids: ["script", "not-provided", "constructor", "toString"],
    evidence: makeEvidence({ items: { script: { category: "price", value: '<script>alert("fixture")</script>',
      definition: '<img src="x" onerror="alert(1)">', source: "<external>" } } }),
  }));
  assert.ok(!markup.includes("<script>"));
  assert.ok(!markup.includes('<img src="x"'));
  assert.ok(markup.includes("&lt;script&gt;"));
  assert.ok(markup.includes("证据未提供，无法核验引用"));
  assert.equal((markup.match(/data-missing="true"/g) ?? []).length, 3);
});

test("largest disagreement uses initial direction span before kind and confidence gap without mutating history", () => {
  const review = makeDeliberation();
  review.initial_states[0] = makeState("technical_analyst", { view: "BULLISH", confidence: 0.8 });
  review.initial_states[2] = makeState("quant_analyst", { view: "BULLISH", confidence: 0.1 });
  review.initial_states[3] = makeState("risk_analyst", { view: "BEARISH", confidence: 0.7 });
  review.disagreements = [
    { disagreement_id: "confidence", kind: "CONFIDENCE", analysts: ["technical_analyst", "quant_analyst"], question: "置信度不同。", evidence_ids: [] },
    { disagreement_id: "direction", kind: "DIRECTION", analysts: ["technical_analyst", "risk_analyst"], question: "多空方向相反。", evidence_ids: ["price_trend"] },
    { disagreement_id: "macro-gap", kind: "EVIDENCE_GAP", analysts: ["macro_analyst", "quant_analyst"], question: "宏观证据缺失。", evidence_ids: ["macro_rate"] },
  ];
  const original = structuredClone(review);
  assert.equal(goldLargestDisagreement(review).disagreement_id, "direction");
  const markup = renderToStaticMarkup(createElement(GoldResearchDeliberation, { result: { deliberation: review, evidence_package: makeEvidence() } }));
  assert.ok(markup.indexOf("多空方向相反。") < markup.indexOf("其余分歧 · 2 项"));
  for (const disagreement of review.disagreements) assert.ok(markup.includes(disagreement.question));
  assert.deepEqual(review, original);
  assert.equal(goldLargestDisagreement(makeDeliberation()), undefined);
});

test("legacy report market view is unchanged and never gains new perspectives from compatibility summaries", (context) => {
  context.mock.method(globalThis, "fetch", () => assert.fail("legacy reports must stay immutable"));
  const result = { proposal: { market_view: "旧版市场判断原文。", hypothesis: "旧版模型假设。", risks: ["旧版风险原文。"] } };
  const original = structuredClone(result);
  const markup = renderToStaticMarkup(createElement(GoldResearchMarketView, { result }));
  for (const text of ["市场判断", "可检验假设", "研究风险", ...Object.values(result.proposal).flat()]) assert.ok(markup.includes(text));
  for (const text of ["四方独立观点", "宏观分析师", "最大分歧", "修订"]) assert.ok(!markup.includes(text));
  assert.equal(renderToStaticMarkup(createElement(GoldResearchDeliberation, { result })), "");
  assert.deepEqual(result, original);
  const current = renderToStaticMarkup(createElement(GoldResearchMarketView, { result: { ...result,
    deliberation: makeDeliberation(), evidence_package: makeEvidence() } }));
  assert.ok(current.includes("四方独立观点"));
  assert.ok(!current.includes("旧版模型假设。"));
});

function makeAuditRound(decision = "PASS", overrides = {}) {
  return { round: 1, draft: makeAdvice(), draft_hash: "draft-v1",
    review: { decision, unsupported_claims: [], missing_risks: [], confidence_adjustment: 0, rationale: "审查理由。" },
    ...overrides };
}

function makeAudit(overrides = {}) {
  return { status: "PASS", reason: "独立审查完成。", published_draft_hash: "draft-v1", rounds: [makeAuditRound()], ...overrides };
}

function renderAdvice(advice, audit, historical = false) {
  return renderToStaticMarkup(createElement(GoldInvestmentAdvice, { advice, audit, historical, asOf: "2025-06-30", symbol: "Au99.99" }));
}

function makeRun(overrides = {}) {
  return {
    run_id: "run-1", status: "RUNNING", stage: "researcher",
    created_at: "2026-10-16T01:00:00Z", updated_at: "2026-10-16T01:01:00Z",
    request: { symbol: "518880", max_weight: 0.1, focus: "gold risk" },
    snapshot_id: "snapshot-1", error: null, result: null, ...overrides,
  };
}

function makeState(analyst = "technical_analyst", overrides = {}) {
  return { analyst, view: "NEUTRAL", confidence: 0.5, summary: `${analyst} 独立判断。`, claims: [], risks: [],
    open_questions: [], invalidating_conditions: [], missing_evidence: [], ...overrides };
}

function makeDeliberation(overrides = {}) {
  const states = [makeState(), makeState("macro_analyst", { view: "INSUFFICIENT_EVIDENCE", confidence: 0,
    summary: "未接入宏观数据，明确弃权。", missing_evidence: ["利率、汇率与储备时间序列"] }),
  makeState("quant_analyst"), makeState("risk_analyst")];
  return { protocol: "evidence-deliberation-v1", evidence_hash: "evidence-hash-1", initial_states: structuredClone(states),
    states, disagreements: [], revisions: [], skipped_analysts: { macro_analyst: "缺少宏观序列，不进行模型调用。" },
    review_status: "NOT_NEEDED", conclusion: "EVIDENCE_BOUNDED", ...overrides };
}

test("V4 evidence provenance keeps its own snapshot, hash and limitations without treating legacy facts as structured evidence", (context) => {
  context.mock.method(globalThis, "fetch", () => assert.fail("provenance is readonly"));
  const result = { deliberation: makeDeliberation(), evidence_package: makeEvidence(), analysis_context: {
    snapshot_id: "legacy-context-snapshot", as_of: "2025-01-01", facts: { legacy_only_fact: "must not replace evidence package" }, warnings: ["源站数据未认证 PIT。"],
  } };
  const run = makeRun({ report_artifact_id: "report-v4" });
  const original = structuredClone(result);
  const markup = renderToStaticMarkup(createElement(GoldResearchEvidence, { result, run, currentSnapshot: "current-chart-snapshot" }));
  for (const text of ["snapshot-v4", "current-chart-snapshot", "report-v4", "evidence-deliberation-v1", "evidence-hash-1", "2026-10-15",
    "无宏观序列与独立样本外证据。", "源站数据未认证 PIT。", "本地值 false", "本地值 0", "本地值 未提供", "宏观利率序列，尚未采集。", "未接入"]) {
    assert.ok(markup.includes(text), `Missing ${text}`);
  }
  assert.ok(!markup.includes("legacy-context-snapshot"));
  assert.ok(!markup.includes("legacy_only_fact"));
  assert.ok(!markup.includes("2025-01-01"));
  const missing = renderToStaticMarkup(createElement(GoldResearchEvidence, { result: { ...result, evidence_package: undefined }, run }));
  assert.ok(missing.includes("未返回结构化证据包，无法核验引用。"));
  assert.ok(!missing.includes("legacy_only_fact"));
  const legacy = renderToStaticMarkup(createElement(GoldResearchEvidence, { result: { ...result, deliberation: undefined }, run }));
  assert.ok(legacy.includes("legacy-context-snapshot"));
  assert.ok(legacy.includes("legacy_only_fact"));
  assert.ok(!legacy.includes("evidence-hash-1"));
  assert.deepEqual(result, original);
});

test("research requests use a separate user-set percent cap, never a quote or an order", () => {
  assert.deepEqual(goldResearchRequest("518880", "10", "  focus  "), {
    symbol: "518880", max_weight: 0.1, focus: "focus",
  });
  assert.equal(goldResearchRequest("159937", "12.5", "risk").max_weight, 0.125);
  assert.equal(goldResearchRequest("518880", "30", "risk").max_weight, 0.3);
  for (const cap of ["0", "-1", "30.01", "100", "100.01", "", "NaN", "Infinity"]) {
    assert.throws(() => goldResearchRequest("518880", cap, "risk"));
  }
  assert.deepEqual(goldResearchRequest("Au99.99", "10", "  spot risk  "), {
    symbol: "Au99.99", max_weight: 0.1, focus: "spot risk",
  });
  assert.equal(goldResearchRequest("Au99.99", "30", "a".repeat(1000)).focus.length, 1000);
  assert.throws(() => goldResearchRequest("Au99.99", "30.01", "risk"));
  assert.throws(() => goldResearchRequest("Au99.99", "10", "a".repeat(1001)));
  assert.throws(() => goldResearchRequest("AU2612", "10", "risk"));
  assert.throws(() => goldResearchRequest("518880", "10", "  "));
  assert.throws(() => goldResearchRequest("518880", "10", "a".repeat(1001)));
});

test("research instruments put Au99.99 first and accept every supported research object", () => {
  assert.equal(GOLD_INSTRUMENTS[0].symbol, "Au99.99");
  for (const { symbol } of GOLD_INSTRUMENTS) assert.equal(goldResearchRequest(symbol, "10", "risk").symbol, symbol);
});

test("report prices use explicit units and fall back to legacy report symbols, not the current selection", () => {
  for (const evaluation of [{ symbol: "Au99.99", unit: "CNY/g" }, { symbol: "Au99.99" },
    { symbol: "legacy-spot", unit: "CNY/g" }]) {
    const format = goldResearchPriceFormat(evaluation);
    assert.deepEqual(format, { spot: true, unit: "元/克", digits: 2 });
    assert.equal(display(823.456, false, format.digits), "823.46");
  }
  for (const evaluation of [{ symbol: "518880", unit: "CNY/share" }, { symbol: "159937" },
    { symbol: "Au99.99", unit: "CNY/share" }]) {
    const format = goldResearchPriceFormat(evaluation);
    assert.deepEqual(format, { spot: false, unit: "元/份", digits: 3 });
    assert.equal(display(8.1234, false, format.digits), "8.123");
  }
});

test("investment evidence labels observation windows, relative gaps and diagnostic returns as percentages", () => {
  for (const [evidence_key, label] of [
    ["gold_return_126", "近126观测收益（约6个月）"],
    ["gold_return_252", "近252观测收益（约12个月）"],
    ["gold_ma120_gap", "收盘相对 MA120 偏离"],
    ["gold_ma250_gap", "收盘相对 MA250 偏离"],
    ["gold_drawdown_252", "距近252观测高点回撤（约12个月）"],
    ["diagnostic_excess_return", "诊断收益差（策略减同仓位持有）"],
    ["double_cost_return", "双倍成本诊断收益"],
  ]) {
    assert.equal(goldInvestmentEvidence({ evidence_key, value: 0.125 }), `${label}：12.50%`);
    assert.equal(goldInvestmentEvidence({ evidence_key, value: -0.02 }), `${label}：-2.00%`);
    assert.equal(goldInvestmentEvidence({ evidence_key, value: 0 }), `${label}：0.00%`);
    for (const value of [false, null, undefined, NaN, Infinity]) {
      assert.equal(goldInvestmentEvidence({ evidence_key, value }), `${label}：—`);
    }
  }
});

test("independent OOS is a validation result, never a numeric zero or percentage", () => {
  assert.equal(goldInvestmentEvidence({ evidence_key: "independent_oos", value: false }), "独立样本外验证：未通过");
  assert.equal(goldInvestmentEvidence({ evidence_key: "independent_oos", value: true }), "独立样本外验证：通过");
  assert.equal(goldInvestmentEvidence({ evidence_key: "independent_oos", value: 0 }), "独立样本外验证：未返回");
});

test("research form defaults to spot with the requested focus and performs no request during rendering", (context) => {
  context.mock.method(globalThis, "fetch", () => assert.fail("rendering must not call an API"));
  const markup = renderToStaticMarkup(createElement(GoldResearch, {
    dashboard: null, dirty: false, allocationBusy: false, onRefresh() {}, onConfigure() {},
  }));
  assert.match(markup, /研究对象<select/);
  assert.match(markup, /<option value="Au99\.99" selected="">/);
  assert.ok(markup.includes("多智能体研究 · 独立终审"));
  assert.ok(markup.includes("分析人民币黄金趋势与风险，提出可检验策略及3至12个月配置建议"));
  assert.ok(!markup.includes("研究 ETF"));
});

test("investment advice renders the backend summary first with concise evidence and its own historical date", (context) => {
  context.mock.method(globalThis, "fetch", () => assert.fail("advice must not call an API"));
  const advice = makeAdvice();
  const markup = renderAdvice(advice);
  for (const text of ["中长期投资建议", advice.horizon, "等待", advice.summary,
    "近126观测收益（约6个月）：12.50%", "独立样本外验证：未通过", ...advice.reasons.map(({ implication }) => implication),
    "重新评估条件：", advice.reassess_when, "证据有限", advice.limitations, "非个性化投资建议", "报告证据截至", "Au99.99"]) {
    assert.ok(markup.includes(text), `Missing ${text}`);
  }
  assert.ok(markup.indexOf(advice.summary) < markup.indexOf("gold-advice-reasons"));
  assert.match(markup, /<time dateTime="2025-06-30">2025-06-30<\/time>/);
  assert.equal((markup.match(/<li>/g) ?? []).length, 2);
  assert.ok(!markup.includes("<button"));
});

test("investment advice uses Chinese stances and shows at most three backend reasons without inventing any", () => {
  for (const [stance, label] of [["WAIT", "等待"], ["GRADUAL_ALLOCATION", "条件式分批配置"], ["DEFENSIVE", "防守观察"]]) {
    assert.ok(renderAdvice(makeAdvice({ stance })).includes(`<span class="gold-badge">${label}</span>`));
  }
  const advice = makeAdvice({ reasons: [
    ...makeAdvice().reasons,
    { evidence_key: "double_cost_return", value: -0.02, implication: "成本压力仍在。" },
    { evidence_key: "gold_ma250_gap", value: 0.03, implication: "不应扩展成长篇报告。" },
  ] });
  const original = structuredClone(advice);
  const markup = renderAdvice(advice);
  assert.equal((markup.match(/<li>/g) ?? []).length, 3);
  assert.ok(markup.includes("双倍成本诊断收益：-2.00%"));
  assert.ok(!markup.includes(advice.reasons[3].implication));
  assert.deepEqual(advice, original);
});

test("immutable legacy reports show a dated absence notice, not a newly generated recommendation", (context) => {
  context.mock.method(globalThis, "fetch", () => assert.fail("legacy advice must not be regenerated"));
  const markup = renderAdvice(undefined);
  assert.ok(markup.includes("该历史报告未生成中长期建议"));
  assert.ok(markup.includes("2025-06-30"));
  for (const text of ["gold-advice-summary", "gold-advice-reasons", "3至12个月", "重新评估条件", "<button"]) {
    assert.ok(!markup.includes(text), `Legacy report must not invent ${text}`);
  }
});

test("final audit PASS publishes only the final advice, never a round draft", (context) => {
  context.mock.method(globalThis, "fetch", () => assert.fail("final audit rendering must not call an API"));
  const advice = makeAdvice({ summary: "终审后允许发布的建议。" });
  const markup = renderAdvice(advice, {
    status: "PASS", reason: "建议已有可追溯证据。", published_draft_hash: "approved-hash",
    rounds: [{ round: 0, draft: makeAdvice({ summary: "未发布的送审草案。" }), draft_hash: "approved-hash",
      review: { decision: "PASS", unsupported_claims: [], missing_risks: [], confidence_adjustment: 0, rationale: "证据一致。" } }],
  });
  assert.ok(markup.includes("独立终审通过 · PASS"));
  assert.ok(markup.includes(advice.summary));
  assert.ok(markup.includes(advice.horizon));
  assert.ok(!markup.includes("未发布的送审草案。"));
  assert.ok(!markup.includes("未经独立终审"));
});

test("final audit REJECT and UNAVAILABLE withhold nullable or stale advice without inventing WAIT", (context) => {
  context.mock.method(globalThis, "fetch", () => assert.fail("unpublished advice must not call an API"));
  for (const status of ["REJECT", "UNAVAILABLE"]) {
    for (const advice of [null, undefined, makeAdvice({ summary: "不可作为最终建议的旧内容。" })]) {
      const markup = renderAdvice(advice, { status, reason: "终审证据不足。", published_draft_hash: null, rounds: [] });
      for (const text of [status, "证据不足 / 暂不发布投资建议", "终审证据不足。", "2025-06-30"]) {
        assert.ok(markup.includes(text), `Missing ${text}`);
      }
      for (const text of ["不可作为最终建议的旧内容。", "该历史报告未生成", "gold-advice-reasons", "3至12个月", "等待", "重新评估条件"]) {
        assert.ok(!markup.includes(text), `Unpublished advice must not show ${text}`);
      }
    }
  }
});

test("final audit PASS without final advice does not promote a round draft", () => {
  const markup = renderAdvice(null, { status: "PASS", reason: "发布内容缺失。", published_draft_hash: "draft-hash",
    rounds: [{ round: 0, draft: makeAdvice(), draft_hash: "draft-hash",
      review: { decision: "PASS", unsupported_claims: [], missing_risks: [], confidence_adjustment: 0, rationale: "通过。" } }],
  });
  assert.ok(markup.includes("发布记录不完整 · 未发布"));
  assert.ok(markup.includes("证据不足 / 暂不发布投资建议"));
  assert.ok(!markup.includes(makeAdvice().summary));
  assert.ok(!markup.includes("独立终审通过 · PASS"));
});

test("legacy advice stays labeled unaudited and fallback advice stays historical", () => {
  const advice = makeAdvice();
  const markup = renderAdvice(advice, undefined, true);
  assert.ok(markup.includes("历史建议 · 未经独立终审"));
  assert.ok(markup.includes("历史中长期投资建议"));
  assert.ok(markup.includes("历史报告 · 非本次分析结果"));
  assert.ok(markup.includes(advice.summary));
  assert.ok(!markup.includes("独立终审通过"));
  assert.ok(!markup.includes("PASS"));
  assert.ok(!renderAdvice(null).includes("历史建议 · 未经独立终审"));
});

test("audit history is collapsed and readonly, with distinct hashes and one revision before publication", (context) => {
  context.mock.method(globalThis, "fetch", () => assert.fail("audit history must be readonly"));
  const first = makeAuditRound("REVISE", { draft: makeAdvice({ summary: "未经批准的第一版正文。" }),
    review: { decision: "REVISE", unsupported_claims: ["价格预测缺少证据。"], missing_risks: ["渠道价差未披露。"],
      confidence_adjustment: -0.25, rationale: "先收窄论断，再补齐成本风险。" } });
  const revised = makeAuditRound("PASS", { round: 2, draft_hash: "draft-v2",
    draft: makeAdvice({ summary: "不应替代最终字段的草案正文。" }) });
  const audit = makeAudit({ rounds: [first, revised], published_draft_hash: "draft-v2" });
  const original = structuredClone(audit);
  const advice = makeAdvice({ summary: "唯一可发布的最终建议。" });
  const markup = renderAdvice(advice, audit);
  for (const text of ["终审记录 · 2 轮 / 2 版", "第 1 轮 · 版本 1", "第 2 轮 · 版本 2", "要求修订 · REVISE", "通过 · PASS",
    "初次终审", "修订后复审", "待审草案 / 未发布", "已发布版本", "发布哈希", "draft-v1", "draft-v2",
    first.review.rationale, "未支撑论断：价格预测缺少证据。", "风险缺漏：渠道价差未披露。", "置信度调整 -0.25", advice.summary]) {
    assert.ok(markup.includes(text), `Missing ${text}`);
  }
  assert.match(markup, /<details class="gold-audit gold-advice-audit">/);
  assert.ok(!markup.includes("<details open"));
  assert.ok(!markup.includes(" open="));
  assert.ok(!markup.includes(first.draft.summary));
  assert.ok(!markup.includes(revised.draft.summary));
  assert.ok(!markup.includes("<button"));
  assert.equal((markup.match(/class="gold-advice-summary"/g) ?? []).length, 1);
  assert.deepEqual(audit, original);
});

test("rejected drafts keep their issues and hashes without ever becoming final advice", () => {
  const round = makeAuditRound("REJECT", { draft: makeAdvice({ summary: "被拒草案不是等待建议。" }),
    review: { decision: "REJECT", unsupported_claims: ["过度推断趋势。"], missing_risks: ["缺少回撤风险。"],
      confidence_adjustment: -1, rationale: "证据不能支持该论断。" } });
  const markup = renderAdvice(null, makeAudit({ status: "REJECT", published_draft_hash: null, rounds: [round] }));
  for (const text of ["证据不足 / 暂不发布投资建议", "终审记录 · 1 轮 / 1 版", "待审草案 / 未发布", "拒绝 · REJECT",
    round.review.rationale, "过度推断趋势。", "缺少回撤风险。", round.draft_hash]) assert.ok(markup.includes(text), `Missing ${text}`);
  assert.ok(markup.indexOf("过度推断趋势。") < markup.indexOf("<details"));
  assert.ok(!markup.includes(round.draft.summary));
  assert.ok(!markup.includes("已发布版本"));
  assert.ok(!markup.includes("发布哈希"));
  assert.ok(!markup.includes("gold-advice-reasons"));
});

test("audit history counts identical draft hashes as one version and shows unavailable empty history", () => {
  const markup = renderAdvice(makeAdvice(), makeAudit({ rounds: [makeAuditRound("REVISE"), makeAuditRound("PASS", { round: 2 })] }));
  assert.ok(markup.includes("终审记录 · 2 轮 / 1 版"));
  assert.ok(markup.includes("第 2 轮 · 版本 1"));
  const unavailable = renderAdvice(null, makeAudit({ status: "UNAVAILABLE", published_draft_hash: null, rounds: [] }));
  assert.ok(unavailable.includes("终审记录 · 0 轮 / 0 版"));
  assert.ok(unavailable.includes("未返回有效审查轮次"));
  assert.ok(!unavailable.includes("已发布版本"));
  assert.ok(!unavailable.includes("gold-advice-reasons"));
});

test("history retains the latest version per run, orders by time and limits to twenty", () => {
  const first = makeRun();
  const complete = makeRun({ status: "COMPLETED", stage: "complete", updated_at: "2026-10-16T01:02:00Z" });
  const next = makeRun({ run_id: "run-2", created_at: "2026-10-16T02:00:00Z" });
  const input = [complete, first, next];
  assert.deepEqual(goldResearchHistory(input), [next, complete]);
  assert.deepEqual(input, [complete, first, next]);
  assert.equal(goldResearchHistory(Array.from({ length: 22 }, (_, index) => makeRun({
    run_id: `run-${index}`, created_at: new Date(Date.UTC(2026, 9, 16, index)).toISOString(),
  }))).length, 20);
  const label = goldResearchRunLabel(complete);
  assert.match(label, /2026/);
  assert.match(label, /518880/);
  assert.ok(!label.includes(complete.run_id));
  const sameTimestamp = makeRun({ status: "COMPLETED", stage: "complete" });
  assert.deepEqual(goldResearchHistory([sameTimestamp, first]), [sameTimestamp]);
});

test("mount recovery prefers status.active_run_id, falling back to the latest active history job", () => {
  const older = makeRun();
  const newer = makeRun({ run_id: "run-2", created_at: "2026-10-16T02:00:00Z" });
  const finished = makeRun({ run_id: "run-3", status: "COMPLETED", created_at: "2026-10-16T03:00:00Z" });
  assert.equal(activeGoldResearchRunId({ active_run_id: "not-in-history" }, [newer]), "not-in-history");
  assert.equal(activeGoldResearchRunId({ active_run_id: null }, [older, newer, finished]), "run-2");
  assert.equal(activeGoldResearchRunId(null, [finished, older]), "run-1");
  assert.equal(activeGoldResearchRunId(null, [finished]), null);
});

test("shared research slots never route non-gold tasks into gold polling", () => {
  const history = [makeRun({ run_id: "old-gold", status: "RUNNING" })];
  assert.equal(activeGoldResearchRunId({ active_run_id: "factor-run", active_gold_run_id: null, busy: true }, history), null);
  assert.equal(activeGoldResearchRunId({ active_run_id: "combined-run", active_gold_run_id: "combined-run", busy: true }, []), "combined-run");
  assert.equal(activeGoldResearchRunId({ active_run_id: null, active_gold_run_id: null, busy: false }, history), null);
  assert.equal(activeGoldResearchRunId({ active_run_id: "legacy-gold" }, []), "legacy-gold");
});

test("dashboard refresh cannot hydrate an unsaved draft or a draft edited during a request", () => {
  assert.equal(shouldHydrateGoldDraft(3, 3, false, false), true);
  assert.equal(shouldHydrateGoldDraft(3, 3, true, false), false);
  assert.equal(shouldHydrateGoldDraft(3, 4, false, false), false);
  assert.equal(shouldHydrateGoldDraft(3, 4, true, false), false);
  assert.equal(shouldHydrateGoldDraft(3, 3, true, true), true);
  assert.equal(shouldHydrateGoldDraft(3, 4, true, true), false);
});

test("a queued, failed or incomplete job preserves the previous completed report", () => {
  const previous = makeRun({ run_id: "old", status: "COMPLETED", stage: "complete", result: { outcome: "OBSERVE" } });
  const older = makeRun({ run_id: "older", status: "COMPLETED", stage: "complete",
    created_at: "2026-10-15T01:00:00Z", result: { outcome: "PAPER_CANDIDATE" } });
  for (const status of ["QUEUED", "RUNNING", "FAILED", "COMPLETED"]) {
    const current = makeRun({ run_id: "new", status, created_at: "2026-10-16T02:00:00Z" });
    assert.equal(goldResearchReport([older, previous, current], "new"), previous);
    assert.equal(goldResearchReport([older, previous, current], "older"), older);
  }
  assert.equal(goldResearchReport([], "unknown"), undefined);
});

test("pipeline state derives only from reported stages, with no invented progress after failure", () => {
  const run = makeRun({ stage: "critic" });
  assert.equal(goldResearchStepState(run, "researcher"), "complete");
  assert.equal(goldResearchStepState(run, "critic"), "current");
  assert.equal(goldResearchStepState(run, "evaluation"), "pending");
  assert.equal(goldResearchStepState(makeRun({ status: "QUEUED", stage: "queued" }), "fetch"), "pending");
  assert.equal(goldResearchStepState(makeRun({ status: "FAILED", stage: "failed" }), "researcher"), "pending");
  assert.equal(isGoldResearchActive(makeRun()), true);
  assert.equal(isGoldResearchActive(makeRun({ status: "COMPLETED" })), false);
});

test("V4 stages use the report protocol first and only use current status hints for unfinished runs", () => {
  const stages = (run, protocol) => goldResearchSteps(run, protocol).map(({ stage }) => stage);
  const expected = ["fetch", "evaluation", "independent_analysis", "cross_review", "analyst_revision", "orchestrator",
    "final_audit", "advice_revision", "final_reaudit", "complete"];
  for (const stage of ["independent_analysis", "cross_review", "analyst_revision"]) {
    assert.deepEqual(stages(makeRun({ stage })), expected);
    assert.equal(isGoldDeliberationRun(makeRun({ stage })), true);
  }
  for (const stage of ["queued", "fetch", "evaluation", "orchestrator", "final_audit", "advice_revision", "final_reaudit"]) {
    assert.deepEqual(stages(makeRun({ stage, protocol: "gold-deliberation-v4" })), expected);
    assert.deepEqual(stages(makeRun({ stage }), "gold-deliberation-v4"), expected);
  }
  for (const stage of ["researcher", "critic", "statistician"]) {
    assert.ok(stages(makeRun({ stage }), "gold-deliberation-v4").includes("researcher"));
  }
  const legacy = makeRun({ status: "COMPLETED", stage: "complete", result: { advice_audit: makeAudit() } });
  assert.ok(stages(legacy, "gold-deliberation-v4").includes("researcher"));
  for (const stage of ["independent_analysis", "cross_review", "analyst_revision"]) {
    assert.equal(goldResearchStepState(legacy, stage, "gold-deliberation-v4"), "skipped");
  }
  assert.ok(stages(makeRun({ stage: "fetch", protocol: "gold-trend-advice-audited-v3" }), "gold-deliberation-v4").includes("researcher"));
  assert.deepEqual(stages(undefined, "gold-deliberation-v4"), expected);
  assert.equal(goldResearchStageLabel("independent_analysis"), "独立分析");
  assert.equal(goldResearchStageLabel("cross_review"), "交叉复核");
  assert.equal(goldResearchStageLabel("analyst_revision"), "分析师修订");
});

test("V4 optional analyst revision requires recorded revisions, not just a completed report", () => {
  const completed = (review_status, revisions = []) => makeRun({ status: "COMPLETED", stage: "complete", result: {
    deliberation: { protocol: "evidence-deliberation-v1", review_status, revisions }, advice_audit: makeAudit(),
  } });
  for (const status of ["NOT_NEEDED", "BUDGET_SKIPPED"]) assert.equal(goldResearchStepState(completed(status), "analyst_revision"), "skipped");
  for (const status of ["REVISED", "UNRESOLVED"]) {
    assert.equal(goldResearchStepState(completed(status), "analyst_revision"), "unconfirmed");
    assert.equal(goldResearchStepState(completed(status, [{ action: "MAINTAIN" }]), "analyst_revision"), "complete");
  }
  const active = (stage) => makeRun({ stage, protocol: "gold-deliberation-v4" });
  assert.equal(goldResearchStepState(active("evaluation"), "independent_analysis"), "pending");
  assert.equal(goldResearchStepState(active("independent_analysis"), "evaluation"), "complete");
  assert.equal(goldResearchStepState(active("cross_review"), "cross_review"), "current");
  assert.equal(goldResearchStepState(active("cross_review"), "analyst_revision"), "pending");
  assert.equal(goldResearchStepState(active("analyst_revision"), "analyst_revision"), "current");
  assert.equal(goldResearchStepState(active("orchestrator"), "analyst_revision"), "unconfirmed");
  assert.equal(goldResearchStepState(active("advice_revision"), "final_audit"), "complete");
  assert.equal(goldResearchStepState(completed("NOT_NEEDED"), "advice_revision"), "skipped");
  const unavailable = completed("NOT_NEEDED");
  delete unavailable.result.advice_audit;
  assert.equal(goldResearchStepState(unavailable, "final_audit"), "unconfirmed");
  for (const status of ["FAILED", "QUEUED"]) {
    for (const stage of ["independent_analysis", "cross_review", "analyst_revision"]) {
      assert.equal(goldResearchStepState(makeRun({ status, stage: "failed", protocol: "gold-deliberation-v4" }), stage), "pending");
    }
  }
});

test("first-pass final audit skips author revision and reaudit rather than completing them", () => {
  const run = makeRun({ status: "COMPLETED", stage: "complete",
    result: { investment_advice: makeAdvice(), advice_audit: makeAudit() } });
  assert.equal(goldResearchStepState(run, "final_audit"), "complete");
  assert.equal(goldResearchStepState(run, "complete"), "complete");
  assert.equal(goldResearchStepState(run, "advice_revision"), "skipped");
  assert.equal(goldResearchStepState(run, "final_reaudit"), "skipped");
});

test("REVISE progress follows one actual revision and reaudit, keeping both under the audit step", () => {
  for (const stage of ["final_audit", "advice_revision", "final_reaudit"]) {
    const run = makeRun({ stage });
    assert.equal(goldResearchStepState(run, "orchestrator"), "complete");
    assert.equal(goldResearchStepState(run, "final_audit"), "current");
    assert.equal(goldResearchStepState(run, "complete"), "pending");
    assert.equal(goldResearchStepState(run, "advice_revision"), stage === "advice_revision" ? "current"
      : stage === "final_reaudit" ? "complete" : "pending");
    assert.equal(goldResearchStepState(run, "final_reaudit"), stage === "final_reaudit" ? "current" : "pending");
  }
  const run = makeRun({ status: "COMPLETED", stage: "complete", result: { investment_advice: makeAdvice(),
    advice_audit: makeAudit({ published_draft_hash: "draft-v2", rounds: [makeAuditRound("REVISE"),
      makeAuditRound("PASS", { round: 2, draft_hash: "draft-v2" })] }) } });
  assert.equal(goldResearchStepState(run, "final_audit"), "complete");
  assert.equal(goldResearchStepState(run, "advice_revision"), "complete");
  assert.equal(goldResearchStepState(run, "final_reaudit"), "complete");
  assert.equal(goldResearchStageLabel("final_audit"), "独立终审");
  assert.equal(goldResearchStageLabel("advice_revision"), "作者修订");
  assert.equal(goldResearchStageLabel("final_reaudit"), "独立复审");
});

test("first-round REJECT archives the report without inventing a revision or losing the rejection result", () => {
  const rejected = makeRun({ run_id: "rejected", status: "COMPLETED", stage: "complete",
    result: { investment_advice: null, advice_audit: makeAudit({ status: "REJECT", published_draft_hash: null,
      rounds: [makeAuditRound("REJECT")] }) } });
  const previous = makeRun({ run_id: "previous", status: "COMPLETED", stage: "complete",
    created_at: "2026-10-15T01:00:00Z", result: { investment_advice: makeAdvice() } });
  assert.equal(goldResearchStepState(rejected, "final_audit"), "complete");
  assert.equal(goldResearchStepState(rejected, "advice_revision"), "skipped");
  assert.equal(goldResearchStepState(rejected, "final_reaudit"), "skipped");
  assert.equal(goldResearchReport([previous, rejected], "rejected"), rejected);
  assert.equal(goldResearchReport([previous, rejected], "rejected").result.investment_advice, null);
});

test("unavailable final audit never appears complete merely because research was archived", () => {
  const run = makeRun({ status: "COMPLETED", stage: "complete", result: { investment_advice: null,
    advice_audit: makeAudit({ status: "UNAVAILABLE", published_draft_hash: null, rounds: [] }) } });
  assert.equal(goldResearchStepState(run, "final_audit"), "unconfirmed");
  assert.equal(goldResearchStepState(run, "complete"), "complete");
  assert.equal(goldResearchStepState(run, "advice_revision"), "skipped");
  assert.equal(goldResearchStepState(run, "final_reaudit"), "skipped");
  run.result.advice_audit.rounds.push(makeAuditRound("REVISE"));
  assert.equal(goldResearchStepState(run, "advice_revision"), "unconfirmed");
  assert.equal(goldResearchStepState(run, "final_reaudit"), "unconfirmed");
});

test("legacy archives skip the new audit step while failed audits never acquire invented progress", () => {
  const legacy = makeRun({ status: "COMPLETED", stage: "complete", result: { investment_advice: makeAdvice() } });
  assert.equal(goldResearchStepState(legacy, "orchestrator"), "complete");
  assert.equal(goldResearchStepState(legacy, "final_audit"), "skipped");
  assert.equal(goldResearchStepState(legacy, "advice_revision"), "skipped");
  assert.equal(goldResearchStepState(legacy, "final_reaudit"), "skipped");
  for (const status of ["FAILED", "QUEUED"]) {
    for (const stage of ["final_audit", "advice_revision", "final_reaudit"]) {
      assert.equal(goldResearchStepState(makeRun({ status, stage: "failed" }), stage), "pending");
    }
  }
});

test("progress markup distinguishes skipped revisions, actual reaudit, unavailable audit and legacy archives", () => {
  const completed = (audit) => makeRun({ status: "COMPLETED", stage: "complete", result: { advice_audit: audit } });
  const markup = renderToStaticMarkup(createElement(GoldResearchProgress, { run: completed(makeAudit()) }));
  assert.match(markup, /class="skipped"><dt>作者修订（限一次）<\/dt><dd>已跳过<\/dd>/);
  assert.match(markup, /class="skipped"><dt>独立复审<\/dt><dd>已跳过<\/dd>/);
  const revising = renderToStaticMarkup(createElement(GoldResearchProgress, { run: makeRun({ stage: "advice_revision" }) }));
  assert.match(revising, /class="current" aria-current="step"><dt>作者修订（限一次）<\/dt><dd>进行中<\/dd>/);
  assert.match(revising, /class="pending"><dt>独立复审<\/dt><dd>待处理<\/dd>/);
  const reauditing = renderToStaticMarkup(createElement(GoldResearchProgress, { run: makeRun({ stage: "final_reaudit" }) }));
  assert.match(reauditing, /class="complete"><dt>作者修订（限一次）<\/dt><dd>已完成<\/dd>/);
  assert.match(reauditing, /class="current" aria-current="step"><dt>独立复审<\/dt><dd>进行中<\/dd>/);
  const unavailable = renderToStaticMarkup(createElement(GoldResearchProgress, { run: completed(makeAudit({
    status: "UNAVAILABLE", published_draft_hash: null, rounds: [],
  })) }));
  assert.match(unavailable, /class="unconfirmed"[^>]*>.*?<span>独立终审<\/span><small>不可用 \/ 未确认<\/small>/);
  const legacy = renderToStaticMarkup(createElement(GoldResearchProgress, { run: completed(undefined) }));
  assert.match(legacy, /class="skipped"[^>]*>.*?<span>独立终审<\/span><small>历史未终审<\/small>/);
  const paused = renderToStaticMarkup(createElement(GoldResearchProgress, { run: makeRun({ stage: "final_reaudit" }), pollError: true }));
  assert.ok(paused.includes("上次状态"));
  assert.ok(!paused.includes("gold-spin"));
});

test("V4 progress renders actual chronological steps and skips unexecuted optional stages", () => {
  const run = makeRun({ status: "COMPLETED", stage: "complete", result: { deliberation: makeDeliberation(), advice_audit: makeAudit() } });
  const markup = renderToStaticMarkup(createElement(GoldResearchProgress, { run }));
  const labels = ["数据采集", "本地诊断", "独立分析", "交叉复核", "分析师修订", "统筹者", "独立终审", "作者修订", "独立复审", "报告归档"];
  labels.slice(1).forEach((label, index) => assert.ok(markup.indexOf(label) > markup.indexOf(labels[index])));
  for (const label of ["分析师修订", "作者修订", "独立复审"]) {
    assert.match(markup, new RegExp(`class="skipped"[^>]*>.*?<span>${label}</span><small>已跳过</small>`));
  }
  for (const label of ["研究员", "批判者", "统计员", "gold-audit-progress"]) assert.ok(!markup.includes(label));
  run.result.deliberation.review_status = "BUDGET_SKIPPED";
  assert.ok(renderToStaticMarkup(createElement(GoldResearchProgress, { run })).includes("预算跳过"));
  run.result.deliberation.review_status = "UNRESOLVED";
  run.result.advice_audit = makeAudit({ status: "UNAVAILABLE", published_draft_hash: null, rounds: [] });
  const unconfirmed = renderToStaticMarkup(createElement(GoldResearchProgress, { run }));
  assert.match(unconfirmed, /class="unconfirmed"[^>]*>.*?<span>分析师修订<\/span><small>未确认<\/small>/);
  const loading = renderToStaticMarkup(createElement(GoldResearchProgress, {
    run: makeRun({ stage: "evaluation" }), protocol: "gold-deliberation-v4",
  }));
  assert.ok(loading.includes("独立分析"));
  assert.match(loading, /class="current" aria-current="step"[^>]*>.*?<span>本地诊断<\/span>/);
  const legacy = renderToStaticMarkup(createElement(GoldResearchProgress, {
    run: makeRun({ status: "COMPLETED", stage: "complete", result: { advice_audit: makeAudit() } }), protocol: "gold-deliberation-v4",
  }));
  assert.ok(legacy.includes("研究员"));
  assert.ok(!legacy.includes("独立分析"));
});

test("V4 call receipts use six gold roles, keep every retry receipt and never call derived legacy summaries", (context) => {
  context.mock.method(globalThis, "fetch", () => assert.fail("receipts must not initiate model calls"));
  const roles = ["technical_analyst", "quant_analyst", "risk_analyst", "orchestrator", "final_auditor",
    "technical_analyst", "risk_analyst", "orchestrator", "final_auditor"];
  const result = { deliberation: makeDeliberation(), decision: { recommend_candidate: false, rationale: "基于证据的统筹结论。" },
    advice_audit: makeAudit(), local_gates: { 独立样本外: false },
    calls: roles.map((role, index) => ({ role, provider: "test-provider", model: `model-${role}`, input_tokens: index === 0 ? 0 : 100 + index,
      output_tokens: index === 8 ? null : 200 + index, raw_request_id: index === 8 ? null : `v4-receipt-${index}` })),
  };
  for (const field of ["proposal", "critic", "statistician"]) {
    Object.defineProperty(result, field, { get() { assert.fail(`V4 must not read derived ${field} as a role call`); } });
  }
  const markup = renderToStaticMarkup(createElement(GoldResearchCalls, { result }));
  assert.equal((markup.match(/<article>/g) ?? []).length, 6);
  assert.equal((markup.match(/class="gold-audit-route"/g) ?? []).length, roles.length);
  for (let index = 0; index < 8; index += 1) assert.ok(markup.includes(`v4-receipt-${index}`));
  for (const text of ["输入 0 / 输出 200 tokens", "输入 108 / 输出 未返回 tokens", "请求标识 未返回", "独立角色 · PASS · 2 次调用"]) {
    assert.ok(markup.includes(text), `Missing ${text}`);
  }
  const macro = markup.split("<article>").find((article) => article.includes("<h4>宏观分析师</h4>"));
  for (const text of ["已跳过 · 0 次调用", "无模型调用收据", result.deliberation.skipped_analysts.macro_analyst]) assert.ok(macro.includes(text));
  for (const text of ["研究员", "批判者", "统计员", "approved=", "robust=", "recommend_candidate="]) assert.ok(!markup.includes(text));
});

test("V4 budget displays exact backend limits even below the protocol maximum", () => {
  const service = { protocol: "gold-deliberation-v4", roles: {}, budget: { max_calls: 12, max_tokens: 64000 } };
  for (const [max_calls, max_tokens] of [[12, 64000], [6, 16000], [3, 8000]]) {
    const markup = renderToStaticMarkup(createElement(GoldResearchBudget, { service: { ...service, budget: { max_calls, max_tokens } } }));
    assert.ok(markup.includes(`<span>本轮上限 ${max_calls} 次调用 / ${max_tokens.toLocaleString("zh-CN")} tokens</span>`));
    assert.ok(markup.includes("通常 5 次有效调用"));
    assert.ok(markup.includes("最多 9 次"));
    assert.ok(markup.includes("全局更低预算优先"));
  }
  const routes = renderToStaticMarkup(createElement(GoldResearchRoutes, { service }));
  assert.equal((routes.match(/<dt>/g) ?? []).length, 6);
  assert.equal((routes.match(/未配置/g) ?? []).length, 6);
});

test("model routes list five logical roles including the independent final auditor and do not invent missing routes", () => {
  const service = { ready: true, roles: {
    researcher: { provider: "Kimi", model: "kimi-k3" }, critic: { provider: "DeepSeek", model: "deepseek-v4-pro" },
    statistician: { provider: "DeepSeek", model: "deepseek-v4-pro" }, orchestrator: { provider: "Kimi", model: "kimi-k3" },
    final_auditor: { provider: "DeepSeek", model: "deepseek-v4-pro" },
  }, budget: { max_calls: 8, max_tokens: 32000 }, active_run_id: null };
  const markup = renderToStaticMarkup(createElement(GoldResearchRoutes, { service }));
  assert.equal((markup.match(/<dt>/g) ?? []).length, 5);
  assert.ok(markup.includes("<dt>终审员</dt><dd>DeepSeek / deepseek-v4-pro</dd>"));
  const missing = renderToStaticMarkup(createElement(GoldResearchRoutes, { service: { ...service, ready: false, roles: {} } }));
  assert.ok(missing.includes("<dt>终审员</dt><dd>未配置</dd>"));
  assert.ok(!missing.includes("deepseek-v4-pro"));
});

test("gold deliberation status lists six routes without importing globally configured legacy roles", (context) => {
  context.mock.method(globalThis, "fetch", () => assert.fail("routes must not call an API"));
  const roles = ["technical_analyst", "macro_analyst", "quant_analyst", "risk_analyst", "orchestrator", "final_auditor"];
  const service = { ready: true, roles: Object.fromEntries(roles.map((role) => [role, { provider: "test", model: role }])),
    budget: { max_calls: 12, max_tokens: 64000 }, active_run_id: null };
  const markup = renderToStaticMarkup(createElement(GoldResearchRoutes, { service }));
  assert.equal((markup.match(/<dt>/g) ?? []).length, 6);
  for (const role of roles) assert.ok(markup.includes(`test / ${role}`), `Missing route ${role}`);
  for (const label of ["技术分析师", "宏观分析师", "量化分析师", "风险分析师", "统筹者", "终审员"]) {
    assert.ok(markup.includes(`<dt>${label}</dt>`));
  }
  for (const label of ["研究员", "批判者", "统计员"]) assert.ok(!markup.includes(`<dt>${label}</dt>`));
  delete service.roles.macro_analyst;
  assert.ok(renderToStaticMarkup(createElement(GoldResearchRoutes, { service })).includes("<dt>宏观分析师</dt><dd>未配置</dd>"));
});

test("role calls display independent auditor routes and every token receipt without relying on a task field", () => {
  const roles = ["researcher", "critic", "statistician", "orchestrator", "final_auditor", "orchestrator", "final_auditor"];
  const result = {
    proposal: { hypothesis: "研究假设。" }, critic: { approved: false, rationale: "批判理由。", issues: [] },
    statistician: { robust: false, rationale: "统计理由。", findings: [] },
    decision: { recommend_candidate: false, rationale: "统筹理由。" },
    advice_audit: makeAudit(), local_gates: { 最终建议独立终审通过: true },
    calls: roles.map((role, index) => ({ role, provider: role === "orchestrator" || role === "researcher" ? "Kimi" : "DeepSeek",
      model: role === "orchestrator" || role === "researcher" ? "kimi-k3" : "deepseek-v4-pro",
      input_tokens: 100 + index, output_tokens: index === 6 ? null : 200 + index, raw_request_id: `receipt-${index}` })),
  };
  const markup = renderToStaticMarkup(createElement(GoldResearchCalls, { result }));
  assert.ok(markup.includes("角色调用与本地门禁"));
  assert.ok(!markup.includes("四角色审计"));
  assert.equal((markup.match(/<article>/g) ?? []).length, 5);
  const auditor = markup.split("<article>").find((article) => article.includes("<h4>终审员</h4>"));
  for (const text of ["独立角色 · PASS · 2 次调用", "DeepSeek / deepseek-v4-pro", "输入 104 / 输出 204 tokens",
    "输入 106 / 输出 未返回 tokens", "receipt-4", "receipt-6", result.advice_audit.reason]) assert.ok(auditor.includes(text), `Missing ${text}`);
  assert.ok(!auditor.includes("recommend_candidate"));
  assert.ok(markup.includes("最终建议独立终审通过"));
  assert.equal((markup.match(/class="gold-audit-route"/g) ?? []).length, 7);
  const firstPass = renderToStaticMarkup(createElement(GoldResearchCalls, { result: { ...result, calls: result.calls.slice(0, 5) } }));
  assert.equal((firstPass.match(/class="gold-audit-route"/g) ?? []).length, 5);
  assert.ok(firstPass.includes("独立角色 · PASS · 1 次调用"));
  const legacy = renderToStaticMarkup(createElement(GoldResearchCalls, { result: { ...result, advice_audit: undefined,
    calls: result.calls.slice(0, 4), local_gates: {} } }));
  assert.equal((legacy.match(/<article>/g) ?? []).length, 4);
  assert.ok(!legacy.includes("<h4>终审员</h4>"));
  const unavailable = renderToStaticMarkup(createElement(GoldResearchCalls, { result: { ...result, calls: [],
    advice_audit: makeAudit({ status: "UNAVAILABLE", published_draft_hash: null, rounds: [] }),
    local_gates: { 最终建议独立终审通过: false } } }));
  assert.ok(unavailable.includes("独立角色 · UNAVAILABLE · 0 次调用"));
  assert.ok(unavailable.includes("未通过"));
});

test("polling waits two seconds between requests and stops at completion", async (context) => {
  const scheduled = [];
  context.mock.method(globalThis, "setTimeout", (callback, delay) => { scheduled.push({ callback, delay }); return 1; });
  context.mock.method(globalThis, "clearTimeout", () => {});
  const updates = [];
  let requests = 0;
  const stop = pollGoldResearchRun("run-1", async (id, signal) => {
    assert.equal(id, "run-1");
    assert.equal(signal.aborted, false);
    requests += 1;
    return requests === 1 ? makeRun() : makeRun({ status: "COMPLETED", stage: "complete" });
  }, (run) => updates.push(run), assert.fail);
  await setImmediate();
  assert.equal(requests, 1);
  assert.equal(scheduled.length, 1);
  assert.equal(scheduled[0].delay, 2000);
  scheduled.shift().callback();
  await setImmediate();
  assert.equal(requests, 2);
  assert.equal(updates.at(-1).status, "COMPLETED");
  assert.equal(scheduled.length, 0);
  stop();
});

test("poll failures stop without an automatic retry or a synthetic completed result", async (context) => {
  const schedule = context.mock.method(globalThis, "setTimeout", () => assert.fail("must not retry"));
  const errors = [];
  const stop = pollGoldResearchRun("run-1", async () => { throw new Error("offline"); },
    () => assert.fail("must not publish a run"), (error) => errors.push(error.message));
  await setImmediate();
  assert.deepEqual(errors, ["offline"]);
  assert.equal(schedule.mock.callCount(), 0);
  stop();
});

test("unmount aborts an in-flight request and ignores its late result", async (context) => {
  context.mock.method(globalThis, "setTimeout", () => assert.fail("must not schedule after unmount"));
  let resolve;
  let pendingSignal;
  const stop = pollGoldResearchRun("run-1", (_id, signal) => {
    pendingSignal = signal;
    return new Promise((finish) => { resolve = finish; });
  }, () => assert.fail("must not update after unmount"), assert.fail);
  stop();
  assert.equal(pendingSignal.aborted, true);
  resolve(makeRun());
  await setImmediate();
});

test("unmount clears an already scheduled poll", async (context) => {
  context.mock.method(globalThis, "setTimeout", () => 42);
  const clear = context.mock.method(globalThis, "clearTimeout", () => {});
  const stop = pollGoldResearchRun("run-1", async () => makeRun(), () => {}, assert.fail);
  await setImmediate();
  stop();
  assert.equal(clear.mock.callCount(), 1);
  assert.deepEqual(clear.mock.calls[0].arguments, [42]);
});

test("gold API matches the supplied contract, propagates abort signals and accepts 202 without real requests", async (context) => {
  const requests = [];
  const controller = new AbortController();
  const status = { ready: false, roles: { final_auditor: { provider: "DeepSeek", model: "deepseek-v4-pro" } },
    budget: { max_calls: 8, max_tokens: 32000 }, active_run_id: null };
  const requestBody = { symbol: "Au99.99", max_weight: 0.1, focus: "gold risk" };
  const queued = makeRun({ status: "QUEUED", stage: "queued", request: requestBody });
  context.mock.method(globalThis, "fetch", async (path, options) => {
    requests.push({ path, options });
    assert.equal(options.signal, controller.signal);
    if (path === "/api/gold/research/status") return new Response(JSON.stringify(status));
    if (path === "/api/gold/research/runs" && options.method === "POST") {
      assert.deepEqual(JSON.parse(options.body), requestBody);
      return new Response(JSON.stringify(queued), { status: 202 });
    }
    if (path === "/api/gold/research/runs") return new Response(JSON.stringify([queued]));
    if (path === "/api/gold/research/runs/run%2F%3F%23") return new Response(JSON.stringify(queued));
    assert.fail(`Unexpected request: ${path}`);
  });
  assert.deepEqual(await api.goldResearchStatus(controller.signal), status);
  assert.deepEqual(await api.goldResearchRuns(controller.signal), [queued]);
  assert.deepEqual(await api.startGoldResearch(requestBody, controller.signal), queued);
  assert.deepEqual(await api.goldResearchRun("run/?#", controller.signal), queued);
  assert.equal(requests.length, 4);
  assert.equal(requests.filter(({ options }) => options.method === "POST").length, 1);
});

test("API failure remains an explicit error instead of an invented report or readiness flag", async (context) => {
  context.mock.method(globalThis, "fetch", async () => new Response("research unavailable", { status: 503 }));
  await assert.rejects(api.goldResearchStatus(), /503 research unavailable/);
  await assert.rejects(api.goldResearchRun("run-1"), /503 research unavailable/);
});
import assert from "node:assert/strict";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";

const parameters = { fast_window: 20, slow_window: 60, volatility_target: 0.1, trailing_stop: 0.08 };
const analystRoles = ["technical_analyst", "macro_analyst", "quant_analyst", "risk_analyst"];
const missingId = "missing_macro_time_series_with_a_long_identifier_for_mobile_wrapping";

function state(analyst, overrides = {}) {
  return { analyst, view: "NEUTRAL", confidence: 0.4, summary: "现有证据不足以确认趋势反转。", claims: [], risks: [],
    open_questions: [], invalidating_conditions: [], missing_evidence: [], ...overrides };
}

function fixtures() {
  const technical = state("technical_analyst", { view: "BULLISH", confidence: 0.7, summary: "价格回升，但需要独立验证。" });
  const revised = { ...technical, view: "NEUTRAL", confidence: 0.4, summary: "降低置信度，等待更完整的验证证据。",
    claims: [{ claim: "短期价格回升不等于中期趋势反转。", confidence: 0.4,
      evidence_ids: ["gold_return_126", "trade_count", missingId], counterevidence_ids: ["independent_oos"] }],
    missing_evidence: ["独立留出样本"] };
  const macro = state("macro_analyst", { view: "INSUFFICIENT_EVIDENCE", confidence: 0,
    summary: "宏观时间序列未接入，明确弃权。", missing_evidence: ["利率、汇率与储备时间序列"] });
  const quant = state("quant_analyst", { summary: "固定规则全样本诊断不能代替样本外验证。" });
  const risk = state("risk_analyst", { view: "BEARISH", confidence: 0.6, summary: "保留对下行及渠道成本的担忧。" });
  const advice = { horizon: "3至12个月", stance: "WAIT", summary: "暂缓增加配置，等待趋势和独立验证改善。",
    reasons: [{ evidence_key: "gold_return_126", value: -0.125, implication: "不据此外推未来收益。" },
      { evidence_key: "independent_oos", value: false, implication: "尚未获得独立验证。" }],
    reassess_when: "出现新的趋势与独立验证证据后复核。", confidence: "LOW", limitations: "宏观时间序列未接入。" };
  const diagnostic = { status: "OK", total_return: 0.03, max_drawdown: -0.02, trade_count: 0,
    cost_fraction_of_initial: 0, buy_hold_same_cap_return: 0.04, start_date: "2025-01-08", end_date: "2026-10-15", trades: [] };
  const callRoles = ["technical_analyst", "quant_analyst", "risk_analyst", "technical_analyst", "orchestrator", "final_auditor"];
  const result = {
    proposal: { name: "旧版研究原文", hypothesis: "旧版可检验假设原文", market_view: "旧版市场判断原文", risks: ["旧版风险原文"], parameters },
    evaluation: { symbol: "Au99.99", unit: "CNY/g", instrument_kind: "spot", signal: "EXIT_OR_WAIT", target_weight: 0,
      as_of: "2026-10-15", close: 892.96, fast_average: 900, slow_average: 920, annualized_volatility: 0.15,
      trailing_reference: 940, max_weight: 0.1, parameters, rules: { entry: "快均线高于慢均线且价格确认。",
        exit: "跌破慢均线或触发止损。", sizing: "受用户仓位上限约束。", execution: "下一观测开盘参考执行。", invalidation: "窗口不足不生成候选。" },
      diagnostic, double_cost: { ...diagnostic, total_return: 0.028 }, limitations: ["不是权威回测，无独立样本外。"],
      deployment_eligible: false, research_only: true },
    critic: { approved: false, issues: [], rationale: "旧版批判理由" }, statistician: { robust: false, findings: [], rationale: "旧版统计理由" },
    decision: { recommend_candidate: false, rationale: "保持观察，不将分歧视为已达成共识。", next_steps: ["补齐独立验证。"] },
    investment_advice: advice, advice_audit: { status: "PASS", reason: "只发布有证据支持的条件式建议。", published_draft_hash: "fixture-draft-1",
      rounds: [{ round: 1, draft: advice, draft_hash: "fixture-draft-1", review: { decision: "PASS", unsupported_claims: [],
        missing_risks: [], confidence_adjustment: 0, rationale: "没有超出证据边界。" } }] },
    local_gates: { 独立样本外: false, 证据引用核验: true }, outcome: "OBSERVE", deployment_eligible: false,
    analysis_context: { as_of: "2026-10-15", latest_fetch: "2026-10-16T00:30:00Z", snapshot_id: "fixture-v4-snapshot", facts: {}, warnings: [] },
    calls: callRoles.map((role, index) => ({ role, provider: "fixture", model: role, input_tokens: 100 + index,
      output_tokens: index === 5 ? null : 200 + index, raw_request_id: `fixture-receipt-${index}` })),
    evidence_package: { snapshot_id: "fixture-v4-snapshot", as_of: "2026-10-15", items: {
      gold_return_126: { category: "price", value: -0.125, definition: "126 个观测的收盘价收益，不是价格预测。", source: "冻结黄金快照" },
      trade_count: { category: "diagnostic", value: 0, definition: "本地固定规则诊断交易次数。", source: "本地诊断" },
      independent_oos: { category: "constraint", value: false, definition: "是否有独立样本外验证。", source: "本地门禁" },
      macro_rate: { category: "macro", value: null, definition: "宏观利率序列未接入。", source: "未采集" },
    }, limitations: ["无宏观序列，不推断宏观驱动因素。"] },
    deliberation: { protocol: "evidence-deliberation-v1", evidence_hash: "fixture-evidence-hash",
      initial_states: [technical, macro, quant, risk], states: [revised, macro, quant, risk],
      disagreements: [{ disagreement_id: "direction-1", kind: "DIRECTION", analysts: ["technical_analyst", "risk_analyst"],
        question: "短期回升是否足以抵消中期下行风险？", evidence_ids: ["gold_return_126", "independent_oos"] }],
      revisions: [{ action: "LOWER_CONFIDENCE", state: revised, addressed_disagreement_ids: ["direction-1"],
        evidence_requests: ["独立留出样本"], rationale: "目前证据不足以确认趋势反转。" }],
      skipped_analysts: { macro_analyst: "缺少宏观数据，不进行模型调用。" }, review_status: "UNRESOLVED", conclusion: "EVIDENCE_BOUNDED" },
  };
  const run = { run_id: "fixture-v4-run", status: "COMPLETED", stage: "complete", created_at: "2026-10-16T00:00:00Z",
    updated_at: "2026-10-16T00:31:00Z", request: { symbol: "Au99.99", max_weight: 0.1, focus: "黄金趋势与风险" },
    snapshot_id: "fixture-v4-snapshot", report_artifact_id: "fixture-report", error: null, result };
  const legacy = { ...run, run_id: "fixture-legacy-run", created_at: "2026-10-15T00:00:00Z", result: { ...result,
    evidence_package: undefined, deliberation: undefined, advice_audit: undefined,
    analysis_context: { ...result.analysis_context, as_of: "2026-10-14", snapshot_id: "fixture-legacy-snapshot" },
    calls: ["researcher", "critic", "statistician", "orchestrator"].map((role, index) => ({ ...result.calls[index], role })),
  } };
  const quote = { symbol: "Au99.99", name: "黄金现货 Au99.99", kind: "spot", unit: "CNY/g", source: "浏览器本地夹具",
    source_url: "https://example.com/", fetched_at: "2026-10-16T00:30:00Z", stale: false,
    points: Array.from({ length: 40 }, (_, index) => ({ date: new Date(Date.UTC(2026, 8, index + 6)).toISOString().slice(0, 10),
      close: String(880 + index / 3), open: null, high: null, low: null, volume: null, amount: null })) };
  const dashboard = { snapshot: { artifact_id: "fixture-v4-snapshot", fetched_at: quote.fetched_at, quotes: [quote], warnings: [] },
    allocation: { artifact_id: null, updated_at: null, record: { other_assets: "0", target_weight: "0.1", holdings: [], watchlist: [], note: "" } },
    summary: { gold_value: null, total_assets: null, weight: null, target_weight: "0.1", deviation: null,
      unrealized_pnl: null, unpriced_symbols: [], has_holdings: false }, sources: [], futures: { trade_date: null, contracts: [] } };
  return { run, legacy, dashboard };
}

test("gold V4 browser: readonly fixtures, evidence, receipts, legacy reports and responsive layout", {
  skip: !process.env.GOLD_PLAYWRIGHT_MODULE, timeout: 120000,
}, async () => {
  const { chromium } = await import(pathToFileURL(process.env.GOLD_PLAYWRIGHT_MODULE).href);
  const browser = await chromium.launch({ channel: "chrome", headless: true });
  const output = await mkdtemp(join(tmpdir(), "gold-v4-browser-"));
  const { run, legacy, dashboard } = fixtures();
  let history = [run, legacy];
  let activeRunId = null;
  let protocol = "gold-deliberation-v4";
  const requests = [];
  const errors = [];
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, reducedMotion: "reduce" });
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/api/**", async (route) => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      requests.push({ method: request.method(), path });
      if (request.method() !== "GET") return route.abort("blockedbyclient");
      const roles = Object.fromEntries([...analystRoles, "orchestrator", "final_auditor"].map((role) => [role, { provider: "fixture", model: role }]));
      const body = path === "/api/gold/dashboard" ? dashboard
        : path === "/api/gold/research/status" ? { ready: true, protocol, roles, budget: { max_calls: 8, max_tokens: 24000 }, active_run_id: activeRunId }
          : path === "/api/gold/research/runs" ? history : history.find((item) => path === `/api/gold/research/runs/${item.run_id}`);
      await route.fulfill({ status: body ? 200 : 404, contentType: "application/json", body: JSON.stringify(body ?? { error: "Unexpected fixture request" }) });
    });
    const url = new URL("/gold", process.env.GOLD_DASHBOARD_URL ?? "http://127.0.0.1:5173").href;
    await page.goto(url);
    await page.getByRole("heading", { name: "四方独立观点" }).waitFor();
    assert.equal(await page.locator(".gold-perspective").count(), 4);
    assert.match(await page.locator('[data-analyst="macro_analyst"]').textContent(), /置信度 0%/);
    assert.match(await page.locator(".gold-state-change").textContent(), /初始 偏多 · 70%.*复核后 中性 · 40%/);
    assert.match(await page.locator(".gold-budget").textContent(), /8 次调用 \/ 24,000 tokens/);
    assert.equal(await page.getByText("本地固定默认参数 · 非模型提案", { exact: true }).count(), 1);
    assert.equal(await page.getByText("旧版可检验假设原文", { exact: true }).count(), 0);
    await page.locator(".gold-model-routes > summary").click();
    assert.equal(await page.locator(".gold-model-routes dt").count(), 6);
    const technical = page.locator('[data-analyst="technical_analyst"]');
    await technical.locator(".gold-perspective-detail > summary").click();
    await technical.locator(".gold-evidence-reference > summary").filter({ hasText: "gold_return_126" }).click();
    assert.equal(await technical.getByText("126 个观测的收盘价收益，不是价格预测。", { exact: true }).isVisible(), true);
    assert.equal(await technical.getByText("-0.125", { exact: true }).isVisible(), true);
    await page.locator(".gold-audit > summary").filter({ hasText: "角色调用与本地门禁" }).click();
    assert.equal(await page.locator(".gold-audit-roles article").count(), 6);
    assert.equal(await page.locator(".gold-audit-route").count(), run.result.calls.length);
    const macroReceipt = page.locator(".gold-audit-roles article").filter({ has: page.getByRole("heading", { name: "宏观分析师" }) });
    assert.match(await macroReceipt.textContent(), /已跳过 · 0 次调用/);
    await page.locator(".gold-advice-audit > summary").click();
    assert.equal(await page.getByRole("heading", { name: "第 1 轮 · 版本 1" }).isVisible(), true);
    await technical.locator(".gold-perspective-detail > summary").click();
    await page.locator(".gold-deliberation").screenshot({ path: join(output, "desktop-perspectives.png") });
    for (const width of [1440, 390, 320]) {
      await page.setViewportSize({ width, height: width === 1440 ? 1000 : 844 });
      const bounds = await page.evaluate(() => ({ width: innerWidth, scroll: document.documentElement.scrollWidth,
        perspectives: [...document.querySelectorAll(".gold-perspective")].map((element) => {
          const rect = element.getBoundingClientRect();
          return { left: rect.left, right: rect.right, width: rect.width, scroll: element.scrollWidth };
        }) }));
      assert.ok(bounds.scroll <= bounds.width, `Page overflow at ${width}: ${JSON.stringify(bounds)}`);
      assert.ok(bounds.perspectives.every((rect) => rect.left >= 0 && rect.right <= width + 1 && rect.scroll <= rect.width + 1));
      if (width === 390) await page.locator(".gold-deliberation").screenshot({ path: join(output, "mobile-perspectives.png") });
    }
    await technical.locator(".gold-perspective-detail > summary").click();
    const expandedBounds = await page.evaluate(() => ({ width: innerWidth, scroll: document.documentElement.scrollWidth }));
    assert.ok(expandedBounds.scroll <= expandedBounds.width);
    await page.getByLabel("研究记录", { exact: true }).selectOption(legacy.run_id);
    await page.getByRole("heading", { name: "旧版研究原文" }).waitFor();
    assert.equal(await page.locator(".gold-perspective").count(), 0);
    assert.equal(await page.locator(".gold-report-part").getByText("旧版可检验假设原文", { exact: true }).isVisible(), true);
    assert.equal(await page.getByText("历史建议 · 未经独立终审", { exact: true }).isVisible(), true);
    assert.equal(await page.locator(".gold-investment-advice time").textContent(), "2026-10-14");
    assert.equal(await page.locator(".gold-audit-roles article").count(), 4);
    const active = { ...run, run_id: "fixture-active-run", result: null, status: "RUNNING", stage: "independent_analysis" };
    history = [active, legacy];
    activeRunId = active.run_id;
    protocol = undefined;
    await page.reload();
    await page.locator('.gold-pipeline li[aria-current="step"]').filter({ hasText: "独立分析" }).waitFor();
    assert.equal(await page.locator(".gold-pipeline li").count(), 10);
    assert.equal(await page.locator(".gold-report-part").getByText("旧版可检验假设原文", { exact: true }).isVisible(), true);
    assert.match(await page.locator(".gold-warning").allTextContents().then((items) => items.join(" ")), /不是新结果/);
    history = [{ ...active, stage: "evaluation" }, legacy];
    protocol = "gold-deliberation-v4";
    await page.reload();
    await page.locator('.gold-pipeline li[aria-current="step"]').filter({ hasText: "本地诊断" }).waitFor();
    assert.equal(await page.locator(".gold-pipeline li").count(), 10);
    assert.ok(requests.length > 0);
    assert.ok(requests.every((request) => request.method === "GET"));
    assert.deepEqual(errors, []);
    console.log(`Gold browser checks passed; ${requests.length} intercepted GETs, zero writes. Screenshots: ${output}`);
  } finally {
    await browser.close();
  }
});
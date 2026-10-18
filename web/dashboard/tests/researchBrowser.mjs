import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import test from "node:test";

const playwrightModule = process.env.RESEARCH_PLAYWRIGHT_MODULE ?? process.env.RESEARCH_PLAYWRIGHT_PATH;
const enabled = Boolean(process.env.RESEARCH_BROWSER_URL && playwrightModule);

test("research browser fixtures: explicit planning, child results, cancellation, responsive layout and stopped polling", {
  skip: !enabled, timeout: 90000,
}, async () => {
  const { chromium, expect } = await import(playwrightModule.startsWith("/") ? pathToFileURL(playwrightModule).href : playwrightModule);
  const browser = await chromium.launch({ channel: "chrome", headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: "reduce", serviceWorkers: "block" });
  const page = await context.newPage();
  const errors = [];
  const requests = [];
  const state = { active: false, cancelRequested: false, cancelled: false, created: false, failReads: false, factorReady: true, futureMacro: false };
  const budget = { max_calls: 12, max_tokens: 64000, timeout_seconds: 300, max_parallel_calls: 3 };
  const versions = {
    "research.plan": "research-plan-result-v1", "gold.collect": "1.0", "gold.research": "gold-deliberation-v4",
    "factor.research": "1.0", "macro.collect": "macro-collection-v1", "macro.analyze": "macro-analysis-v1", "macro.research": "macro-research-v1",
  };
  const childTasks = { "gold-collect-fixture": "gold.collect", "macro-collect-fixture": "macro.collect",
    "macro-fixture": "macro.analyze", "macro-research-fixture": "macro.research", "gold-fixture": "gold.research" };
  const eventFixtures = [
    { run_id: "root-fixture", sequence: 1, event_type: "root_started", stage: "collecting", at: "2026-10-17T01:00:00Z", data: {} },
    { run_id: "root-fixture", sequence: 2, event_type: "root_frozen", stage: "frozen", at: "2026-10-17T01:00:20Z", data: {} },
    { run_id: "macro-fixture", sequence: 1, event_type: "server_fixture_event", stage: "analysis", at: "2026-10-17T01:00:30Z", data: {} },
    { run_id: "macro-fixture", sequence: 2, event_type: "server_fixture_second", stage: "complete", at: "2026-10-17T01:00:30Z", data: {} },
    { run_id: "macro-research-fixture", sequence: 1, event_type: "research_view_started", stage: "independent_analysis", at: "2026-10-17T01:00:30Z", data: {} },
    { run_id: "gold-fixture", sequence: 1, event_type: "gold_review_started", stage: "final_audit", at: "2026-10-17T01:00:30Z", data: {} },
  ];
  const base = new URL(process.env.RESEARCH_BROWSER_URL);
  page.on("pageerror", (error) => errors.push(error.message));
  await context.route("**/*", (route) => {
    if (new URL(route.request().url()).origin !== base.origin) return route.abort("blockedbyclient");
    assert.ok(!new URL(route.request().url()).pathname.startsWith("/api/"), "all API traffic must be mocked");
    assert.equal(route.request().method(), "GET", "no unmocked writes");
    return route.continue();
  });
  function descriptors() {
    return ["gold.collect", "gold.research", "macro.collect", "macro.analyze", "macro.research", "factor.research"].map((task_type) => ({
      task_type, module_id: task_type.split(".")[0], title: task_type, version: "1.0",
      result_schema_version: versions[task_type],
      roles: task_type === "factor.research" ? ["researcher"] : [], capabilities: [], default_budget: budget,
      acquisition: task_type.endsWith("collect"), ready: task_type !== "factor.research" || state.factorReady,
      unavailable_reason: task_type === "factor.research" && !state.factorReady ? "fixture: missing factor role" : null,
      missing_roles: task_type === "factor.research" && !state.factorReady ? ["researcher"] : [], request_schema: {},
    }));
  }
  function result(payload, summary = "server fixture summary") {
    return { outcome: "OBSERVE", summary, payload, evidence: [{ artifact_id: "snapshot-fixture", content_hash: "a".repeat(64), namespace: "macro", schema_version: "macro-snapshot-v1" }],
      publication: { decision: "NOT_APPLICABLE", content_hash: null, reason: null }, limitations: ["fixture only"], report_artifact_id: "report-fixture" };
  }
  function run(id, task_type, parent = null) {
    return { run_id: id, root_run_id: "root-fixture", parent_run_id: parent, task_id: parent ? task_type : null,
      task_type, module_id: task_type.split(".")[0], task_version: "1.0", result_schema_version: versions[task_type],
      status: parent ? "COMPLETED" : state.cancelled ? "CANCELLED" : state.active ? "RUNNING" : "COMPLETED",
      stage: parent ? "complete" : state.cancelled ? "cancelled" : state.active ? "collecting" : "complete",
      created_at: "2026-10-17T01:00:00Z", updated_at: "2026-10-17T01:01:00Z", request: { question: "黄金宏观联合研究" },
      knowledge_cutoff: "2026-10-17T01:00:20Z", result: null, error: null, children: parent ? [] : Object.keys(childTasks),
      usage: { calls_used: 2, tokens_used: 1200, tokens_reserved: 300, max_calls: 12, max_tokens: 64000 },
      cancel_requested: !parent && state.cancelRequested, revision: 3 };
  }
  function facts() {
    return { schema_version: "macro-analysis-v1", snapshot_id: "macro-snapshot-fixture", indicators: {
      DGS10: { title: "美国10年国债收益率", latest_value: "4.2500000001", unit: "percent", observation_date: "2026-10-15", available_at: "2026-10-16T18:00:00Z", source_url: "https://fred.stlouisfed.org/series/DGS10", change_from_previous_observation: { value: -3, unit: "basis_points", evidence_ids: ["fact-DGS10"] } },
      DFII10: { title: "实际利率", latest_value: null, unit: "percent", observation_date: null, available_at: null },
    }, coverage: { DGS10: { status: "AVAILABLE" }, DFII10: { status: "MISSING" } }, missing: { DFII10: "fixture unavailable" },
    limitations: ["fixture limitation"], observations_metadata: { vintage: "2026-10-16" } };
  }
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const body = request.postDataJSON();
    requests.push({ path, method: request.method(), body });
    if (state.failReads && request.method() === "GET" && path.startsWith("/api/research/")) {
      return route.fulfill({ status: 503, contentType: "text/plain", body: "fixture offline" });
    }
    let output = [];
    let status = 200;
    if (path === "/api/research/modules") {
      output = { ready: true, active_run_id: state.active ? "root-fixture" : null, busy: state.active, max_parallel_tasks: 2, max_parallel_calls: 3, modules: descriptors() };
    } else if (path === "/api/research/plans") {
      assert.equal(typeof body.use_model, "boolean");
      assert.ok(!JSON.stringify(body.payload).includes("use_model"));
      assert.ok(!JSON.stringify(body.payload).includes("useModel"));
      const mode = body.question.includes("联合") ? "combined" : body.question.includes("因子") ? "factor" : body.question.includes("宏观") ? "macro" : "gold";
      const tasks = descriptors().filter((entry) => (mode === "combined" ? entry.module_id !== "factor" : entry.module_id === mode)
        && (body.use_model || entry.task_type !== "macro.research")).map((entry) => {
        const dependency = entry.acquisition || entry.module_id === "factor" ? null : `${entry.module_id}.collect`;
        return { task_id: entry.task_type, task_type: entry.task_type,
          payload: body.payload[entry.task_type] ?? (dependency ? {} : body.payload),
          inputs: dependency ? { snapshot_id: { task_id: dependency, output_key: "snapshot_id" } } : {},
          depends_on: dependency ? [dependency] : [], required: true, budget };
      });
      output = { artifact_id: "plan-artifact-fixture", receipts: [], plan: { schema_version: "research-plan-v1", plan_id: `plan-fixture-${requests.length}`,
        question: body.question, tasks, mode: "LATEST", knowledge_cutoff: null, budget, source: body.use_model ? "MODEL" : "RULE" } };
    } else if (path === "/api/research/runs" && request.method() === "POST") {
      assert.deepEqual(body.budget, body.plan.budget);
      assert.match(body.idempotency_key, /^[0-9a-f-]{36}$/);
      state.active = true;
      state.created = true;
      output = run("root-fixture", "research.plan");
      status = 202;
    } else if (path === "/api/research/runs") {
      output = state.created ? [run("root-fixture", "research.plan")] : [];
    } else if (path === "/api/research/runs/root-fixture/cancel") {
      state.cancelRequested = true;
      output = run("root-fixture", "research.plan");
    } else if (path.endsWith("/events")) {
      const runId = path.split("/")[4];
      output = eventFixtures.filter((event) => runId === "root-fixture" || event.run_id === runId).toReversed();
    } else if (path.endsWith("/calls")) {
      output = ["root-fixture", "macro-research-fixture"].includes(path.split("/")[4]) ? [{ call_id: "call-fixture", sequence: 1, role: "macro.rates", provider: "fixture", model: "fixture-only", purpose: "fixture review",
        status: "COMPLETED", messages: [{ role: "user", content: "fixture input" }], response: { content: "fixture response", input_tokens: 12, output_tokens: 10, raw_request_id: "request-fixture" },
        error: null, parse_error: null, reservation_tokens: 300, used_tokens: 22 }] : [];
    } else if (path.endsWith("/macro-collect-fixture") || path.endsWith("/gold-collect-fixture")) {
      const id = path.split("/")[4];
      output = run(id, childTasks[id], "root-fixture");
      output.result = result({ schema_version: versions[childTasks[id]], snapshot_id: `${id}-snapshot`, missing: {} });
    } else if (path.endsWith("/macro-fixture")) {
      output = run("macro-fixture", "macro.analyze", "root-fixture");
      output.result = result(facts(), "macro deterministic fixture summary");
    } else if (path.endsWith("/macro-research-fixture")) {
      output = run("macro-research-fixture", "macro.research", "root-fixture");
      if (state.futureMacro) output.result_schema_version = "macro-research-v99";
      output.result = result({ schema_version: "macro-research-v1", facts: facts(), perspectives: {
        "macro.rates": { state: "EVIDENCE_BOUNDED", summary: "independent rates fixture", claims: [{ claim: "observed yield fixture", evidence_ids: ["fact-DGS10"] }], missing: [], risks: ["no causal inference"] },
        "macro.risks": { state: "INSUFFICIENT_EVIDENCE", summary: "independent risk fixture", claims: [], missing: ["DFII10"], risks: ["vintage uncertainty fixture"] },
      }, interpretation_status: "MODEL_INTERPRETATION", synthesis: { state: "INSUFFICIENT_EVIDENCE" }, no_investment_advice: true });
    } else if (path.endsWith("/gold-fixture")) {
      output = run("gold-fixture", "gold.research", "root-fixture");
      output.result = result({ proposal: { hypothesis: "WITHHELD_SECRET" }, evaluation: { symbol: "Au99.99", as_of: "2026-10-16" }, investment_advice: { summary: "WITHHELD_SECRET" } }, "WITHHELD_SECRET");
      output.result.publication = { decision: "WITHHELD", content_hash: null, reason: "审核拒绝发布" };
    } else if (path.endsWith("/root-fixture")) {
      output = run("root-fixture", "research.plan");
      if (!state.active) output.result = result({ summaries: [
        { task_type: "macro.analyze", run_id: "macro-fixture", status: "COMPLETED", outcome: "OBSERVE", summary: "macro fixture summary" },
        { task_type: "gold.research", run_id: "gold-fixture", status: "COMPLETED", outcome: "OBSERVE", summary: "WITHHELD_SECRET" },
      ], approved_publication_hashes: [], approved_publications: [{ advice: "WITHHELD_SECRET" }], unpublished_hash: { advice: "WITHHELD_SECRET" } });
    } else {
      assert.equal(request.method(), "GET", `Unexpected fixture write: ${path}`);
    }
    return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(output) });
  });
  try {
    await page.clock.install();
    await page.goto(new URL("/research", base).href);
    await expect(page.getByRole("button", { name: "预览计划" })).toBeEnabled();
    const modelPlanning = page.getByRole("checkbox", { name: "模型规划", exact: true });
    await expect(modelPlanning).not.toBeChecked();
    await expect(modelPlanning.locator("..")).toHaveAttribute("title", /产生费用/);
    assert.equal(requests.filter((request) => request.method === "POST").length, 0);
    await page.getByRole("button", { name: "预览计划" }).click();
    await expect(page.getByRole("button", { name: "确认运行" })).toBeEnabled();
    assert.equal(requests.filter((request) => request.path === "/api/research/plans").at(-1).body.use_model, false);
    assert.equal(requests.filter((request) => request.path === "/api/research/runs" && request.method === "POST").length, 0);
    await page.getByRole("textbox", { name: "研究重点" }).fill("edited focus");
    await expect(page.getByRole("button", { name: "确认运行" })).toHaveCount(0);
    await page.getByLabel("联合", { exact: true }).check();
    await page.getByLabel("开始日期", { exact: true }).fill("2026-01-01");
    await page.getByLabel("结束日期", { exact: true }).fill("2026-10-16");
    for (const id of ["DTWEXBGS", "DEXCHUS", "CN_GOLD_RESERVES"]) await page.getByRole("checkbox", { name: new RegExp(`${id}$`) }).uncheck();
    await modelPlanning.check();
    await page.getByRole("button", { name: "预览计划" }).click();
    await expect(page.getByRole("button", { name: "确认运行" })).toBeEnabled();
    const combined = requests.filter((request) => request.path === "/api/research/plans").at(-1).body;
    assert.equal(combined.use_model, true);
    assert.deepEqual(Object.keys(combined.payload), ["gold.collect", "gold.research", "macro.collect"]);
    assert.deepEqual(combined.payload["macro.collect"].series_ids, ["DGS10", "DFII10"]);
    await expect(page.getByText("MODEL / LATEST", { exact: true })).toBeVisible();
    await modelPlanning.uncheck();
    await expect(page.getByRole("button", { name: "确认运行" })).toHaveCount(0);
    await modelPlanning.check();
    await page.getByRole("button", { name: "预览计划" }).click();
    await page.getByRole("button", { name: "确认运行" }).click();
    await expect(page.getByRole("button", { name: /macro\.analyze/ })).toBeVisible();
    await expect(page.getByRole("button", { name: "已提交本计划" })).toBeDisabled();
    assert.equal(requests.filter((request) => request.path === "/api/research/runs" && request.method === "POST").length, 1);
    const beforePoll = requests.length;
    await page.clock.runFor(2100);
    await expect.poll(() => requests.length).toBeGreaterThan(beforePoll);
    await page.getByRole("button", { name: /macro\.analyze/ }).click();
    await expect(page.getByRole("cell", { name: "4.2500000001 percent", exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "独立观点", exact: true })).toHaveCount(0);
    await page.getByRole("tab", { name: /^事件/ }).click();
    await expect(page.getByText("server_fixture_event", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: /根任务/ }).click();
    await expect(page.locator(".research-events > li")).toHaveCount(6);
    assert.deepEqual(await page.locator(".research-events > li strong").allTextContents(), ["root_started", "root_frozen", "gold_review_started", "server_fixture_event", "server_fixture_second", "research_view_started"]);
    assert.equal((await page.locator(".research-event-sequence").allTextContents()).filter((value) => value === "1").length, 4);
    await page.getByRole("button", { name: /macro\.collect/ }).click();
    await page.getByRole("tab", { name: "结果", exact: true }).click();
    await expect(page.getByText("采集记录", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: /macro\.research/ }).click();
    await expect(page.getByRole("heading", { name: "独立观点", exact: true })).toBeVisible();
    await expect(page.getByText("independent rates fixture", { exact: true })).toBeVisible();
    await expect(page.getByText("independent risk fixture", { exact: true })).toBeVisible();
    await expect(page.getByRole("cell", { name: "4.2500000001 percent", exact: true })).toBeVisible();
    await page.getByRole("tab", { name: /^收据/ }).click();
    await page.locator(".research-receipt > summary").click();
    await expect(page.getByText("request-fixture", { exact: true })).toBeVisible();
    await page.getByRole("tab", { name: "结果", exact: true }).click();
    for (const width of [1440, 390, 320]) {
      await page.setViewportSize({ width, height: width === 1440 ? 1000 : 844 });
      await page.getByRole("heading", { name: "研究", exact: true }).scrollIntoViewIfNeeded();
      const layout = await page.evaluate(() => ({ viewport: innerWidth, width: document.documentElement.scrollWidth,
        overflowingButtons: [...document.querySelectorAll(".research-page button")].filter((button) => button.scrollWidth > button.clientWidth + 1).map((button) => button.textContent),
        overflowingLabels: [...document.querySelectorAll(".research-check")].filter((label) => label.scrollWidth > label.clientWidth + 1).map((label) => label.textContent) }));
      assert.ok(layout.width <= layout.viewport, JSON.stringify(layout));
      assert.deepEqual(layout.overflowingButtons, []);
      assert.deepEqual(layout.overflowingLabels, []);
      await page.screenshot({ path: `/tmp/quant-research-${width}.png`, fullPage: true, animations: "disabled" });
    }
    state.futureMacro = true;
    await page.getByRole("button", { name: "重新查询", exact: true }).click();
    await expect(page.getByText(/结果不可用：不支持的结果版本/)).toBeVisible();
    await expect(page.getByText("independent rates fixture", { exact: true })).toHaveCount(0);
    state.futureMacro = false;
    await page.getByRole("button", { name: "重新查询", exact: true }).click();
    await expect(page.getByText("independent rates fixture", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: /gold\.research/ }).click();
    await expect(page.getByText("审核拒绝发布", { exact: true })).toBeVisible();
    await expect(page.getByText("WITHHELD_SECRET", { exact: true })).toHaveCount(0);
    await page.getByRole("button", { name: "取消运行", exact: true }).click();
    await expect(page.getByText("已请求取消，等待服务器确认", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "已请求取消", exact: true })).toBeDisabled();
    assert.equal(state.active, true);
    state.active = false;
    state.cancelled = true;
    await page.clock.runFor(2100);
    await expect(page.locator(".research-run-tree").getByText("已取消", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: /根任务/ }).click();
    await expect(page.getByText("WITHHELD_SECRET", { exact: true })).toHaveCount(0);
    await expect(page.getByText("approved_publication_hashes", { exact: true })).toBeVisible();
    await expect(page.getByText(/结果不可用：不支持的结果版本/)).toHaveCount(0);
    const terminalReads = requests.length;
    await page.clock.runFor(6100);
    assert.equal(requests.length, terminalReads);
    state.failReads = true;
    await page.getByRole("button", { name: "重新查询", exact: true }).click();
    await expect(page.getByRole("alert")).toContainText("fixture offline");
    const failedReads = requests.length;
    await page.clock.runFor(6100);
    assert.equal(requests.length, failedReads);
    state.failReads = false;
    state.factorReady = false;
    await page.getByRole("button", { name: "重新查询", exact: true }).click();
    await expect(page.getByRole("status")).toHaveText("已同步");
    await page.getByLabel("因子", { exact: true }).check();
    await page.getByLabel("因子课题", { exact: true }).fill("factor fixture topic");
    await page.getByRole("combobox", { name: "数据模式", exact: true }).selectOption("frozen_snapshot");
    await page.getByLabel("冻结快照 ID", { exact: true }).fill("snapshot-factor-fixture");
    await page.getByRole("button", { name: "预览计划" }).click();
    await expect(page.getByRole("button", { name: "确认运行" })).toBeDisabled();
    await expect(page.getByText("factor.research: fixture: missing factor role", { exact: true })).toBeVisible();
    await modelPlanning.uncheck();
    await page.getByLabel("宏观", { exact: true }).check();
    await page.getByRole("button", { name: "预览计划" }).click();
    await expect(page.getByRole("button", { name: "确认运行" })).toBeEnabled();
    assert.equal(requests.filter((request) => request.path === "/api/research/plans").at(-1).body.use_model, false);
    await expect(page.locator(".research-plan-tasks").getByText("macro.research", { exact: true })).toHaveCount(0);
    await page.getByRole("link", { name: "回测", exact: true }).click();
    await expect(page.locator(".research-page")).toHaveCount(0);
    const unmountedReads = requests.filter((request) => request.path.startsWith("/api/research/")).length;
    await page.clock.runFor(6100);
    assert.equal(requests.filter((request) => request.path.startsWith("/api/research/")).length, unmountedReads);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ screenshots: [1440, 390, 320].map((width) => `/tmp/quant-research-${width}.png`),
      fixtureWrites: requests.filter((request) => request.method === "POST").map((request) => request.path),
      planningModes: requests.filter((request) => request.path === "/api/research/plans").map((request) => request.body.use_model),
      resultVersions: versions, rootEvents: eventFixtures.length, realApiWrites: 0, pageErrors: errors }, null, 2));
  } finally {
    await browser.close();
  }
});
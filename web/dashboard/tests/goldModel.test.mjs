import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../src/pages/goldModel.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, {
  fileName: "goldModel.ts",
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
  reportDiagnostics: true,
});
assert.deepEqual(compiled.diagnostics, []);
const {
  allocationFromDraft, closePoints, commonSeries, createDraft, display, drawdowns,
  GOLD_ETFS, inPeriod, number, riskMetrics,
} = await import(`data:text/javascript;base64,${Buffer.from(compiled.outputText).toString("base64")}`);

const ALL_PERIOD = "\u5168\u90e8";
const UNKNOWN_DISPLAY = "\u2014";

function makeQuote(observations, symbol = "Au99.99") {
  return {
    symbol, name: symbol, kind: symbol === "Au99.99" ? "spot" : "etf",
    unit: symbol === "Au99.99" ? "CNY/g" : "CNY/share",
    source: "test", source_url: "https://example.com/", fetched_at: "2026-10-16T00:00:00Z",
    stale: false,
    points: observations.map(([date, close]) => ({
      date, close, open: null, high: null, low: null, volume: null, amount: null,
    })),
  };
}

function makePoints(observations) {
  return closePoints(makeQuote(observations));
}

function makeAllocation(overrides = {}) {
  return {
    other_assets: "9007199254740993.01",
    target_weight: "0.123456789123456789",
    holdings: [{ symbol: "518880", quantity: "100.0000000000000001", average_cost: "8.100000000000000001" }],
    watchlist: ["518880", "159934"], note: "manual allocation", ...overrides,
  };
}

function assertNear(actual, expected) {
  assert.ok(Number.isFinite(actual), `Expected a finite value, received ${actual}`);
  assert.ok(Math.abs(actual - expected) <= 1e-12 * Math.max(1, Math.abs(expected)),
    `Expected ${actual} to be close to ${expected}`);
}

test("number accepts finite numbers and numeric strings, including zero", () => {
  for (const [input, expected] of [[0, 0], [-12.5, -12.5], ["0", 0], [" 12.5 ", 12.5], ["1e2", 100]]) {
    assert.equal(number(input), expected);
  }
});

test("number keeps absent, nonfinite and unsupported values unknown", () => {
  const invalid = [null, undefined, "", " \t\n ", NaN, Infinity, -Infinity,
    "NaN", "Infinity", "-Infinity", "1e309", "unknown", true, false, [], [1], {},
    Object.create(null), Symbol("price")];
  for (const input of invalid) assert.equal(number(input), null);
});

test("display distinguishes unknown values from valid zero and formats percentages", () => {
  for (const input of [null, undefined, "", "unknown", NaN, Infinity, true, {}]) {
    assert.equal(display(input), UNKNOWN_DISPLAY);
    assert.equal(display(input, true), UNKNOWN_DISPLAY);
  }
  assert.equal(display(0), "0.00");
  assert.equal(display("1234.5"), "1,234.50");
  assert.equal(display("0.125", true), "12.50%");
  assert.equal(display("9.1234", false, 3), "9.123");
});

test("closePoints sorts, deduplicates and excludes invalid or nonpositive closes without mutation", () => {
  const quote = makeQuote([
    ["2026-10-03", "120"], ["2026-10-01", "100"],
    ["2026-10-02", "105"], ["2026-10-02", "110"], ["2026-10-02", "unknown"],
    ...[null, undefined, "", "NaN", "Infinity", Infinity, "unknown", "0", "-1", true, [1]]
      .map((close, index) => [`2026-10-${String(index + 4).padStart(2, "0")}`, close]),
  ]);
  const original = structuredClone(quote);
  const points = closePoints(quote);
  assert.deepEqual(points.map(({ date, value }) => [date, value]), [
    ["2026-10-01", 100], ["2026-10-02", 110], ["2026-10-03", 120],
  ]);
  assert.equal(points[1].close, "110");
  assert.equal(points[1].amount, null);
  assert.deepEqual(quote, original);
});

test("closePoints has no fallback prices when a quote is missing or empty", () => {
  assert.deepEqual(closePoints(), []);
  assert.deepEqual(closePoints(makeQuote([])), []);
});

test("inPeriod clamps one-month cutoffs at calendar month ends, inclusively", () => {
  for (const [end, cutoff, previous] of [
    ["2025-03-31", "2025-02-28", "2025-02-27"],
    ["2024-03-31", "2024-02-29", "2024-02-28"],
    ["2026-01-31", "2025-12-31", "2025-12-30"],
  ]) {
    const points = makePoints([[previous, "90"], [cutoff, "100"], [end, "120"]]);
    const original = structuredClone(points);
    assert.deepEqual(inPeriod(points, "1M").map(({ date }) => date), [cutoff, end]);
    assert.deepEqual(points, original);
  }
});

test("inPeriod handles quarter-end and leap-year annual boundaries", () => {
  for (const [period, end, cutoff, previous] of [
    ["3M", "2024-05-31", "2024-02-29", "2024-02-28"],
    ["3M", "2025-05-31", "2025-02-28", "2025-02-27"],
    ["1Y", "2024-02-29", "2023-02-28", "2023-02-27"],
    ["1Y", "2025-02-28", "2024-02-28", "2024-02-27"],
  ]) {
    const points = makePoints([[previous, "90"], [cutoff, "100"], [end, "120"]]);
    assert.deepEqual(inPeriod(points, period).map(({ date }) => date), [cutoff, end]);
  }
});

test("inPeriod preserves all history and handles empty or invalid end dates", () => {
  const points = makePoints([["2020-01-01", "100"], ["2026-10-16", "120"]]);
  assert.deepEqual(inPeriod(points, ALL_PERIOD), points);
  for (const period of ["1M", "3M", "1Y", ALL_PERIOD]) assert.deepEqual(inPeriod([], period), []);
  assert.deepEqual(inPeriod(makePoints([["invalid-date", "100"]]), "1M"), []);
});

test("commonSeries uses the exact intersection and anchors every instrument at its first common close", () => {
  const quotes = [
    makeQuote([["2026-10-06", "250"], ["2026-10-05", "150"], ["2026-10-01", "50"],
      ["2026-10-03", "100"], ["2026-10-04", "125"]]),
    makeQuote([["2026-10-02", "5"], ["2026-10-03", "10"], ["2026-10-04", "15"],
      ["2026-10-05", "20"], ["2026-10-07", "30"]], "518880"),
    makeQuote([["2026-09-30", "80"], ["2026-10-03", "40"], ["2026-10-04", "30"],
      ["2026-10-05", "50"], ["2026-10-06", "60"]], "159934"),
  ];
  assert.deepEqual(commonSeries(quotes, ALL_PERIOD), {
    dates: ["2026-10-03", "2026-10-04", "2026-10-05"],
    values: [[0, 25, 50], [0, 50, 100], [0, -25, 25]],
  });
});

test("commonSeries anchors its period at the last common date, then rebases inside that period", () => {
  const first = makeQuote([["2024-02-27", "50"], ["2024-02-28", "60"], ["2024-02-29", "80"],
    ["2024-03-15", "100"], ["2024-03-31", "120"], ["2024-04-30", "300"]]);
  const second = makeQuote([["2024-02-27", "5"], ["2024-02-28", "6"], ["2024-02-29", "8"],
    ["2024-03-15", "12"], ["2024-03-31", "16"]], "518880");
  assert.deepEqual(commonSeries([first, second], "1M"), {
    dates: ["2024-02-29", "2024-03-15", "2024-03-31"],
    values: [[0, 25, 50], [0, 50, 100]],
  });
});

test("commonSeries returns empty arrays if any requested comparison is absent or unpriced", () => {
  const quote = makeQuote([["2026-10-01", "100"], ["2026-10-02", "120"]]);
  for (const quotes of [[], [undefined], [quote, undefined], [undefined, quote],
    [quote, makeQuote([])], [quote, makeQuote([["2026-10-01", "NaN"]])]]) {
    assert.deepEqual(commonSeries(quotes, ALL_PERIOD), { dates: [], values: [] });
  }
});

test("commonSeries never forward-fills gaps or invalid closes", () => {
  const first = makeQuote([["2026-10-01", "100"], ["2026-10-02", "125"],
    ["2026-10-03", null], ["2026-10-04", "200"]]);
  const second = makeQuote([["2026-10-01", "10"], ["2026-10-03", "15"], ["2026-10-04", "20"]]);
  assert.deepEqual(commonSeries([first, second], ALL_PERIOD), {
    dates: ["2026-10-01", "2026-10-04"], values: [[0, 100], [0, 100]],
  });
});

test("nonoverlapping quotes have no common observations; a single shared date is zero-based", () => {
  const first = makeQuote([["2026-10-01", "100"]]);
  const disjoint = commonSeries([first, makeQuote([["2026-10-02", "10"]])], ALL_PERIOD);
  assert.deepEqual(disjoint.dates, []);
  assert.deepEqual(disjoint.values.flat(), []);
  assert.deepEqual(commonSeries([first, makeQuote([["2026-10-01", "10"]])], ALL_PERIOD), {
    dates: ["2026-10-01"], values: [[0], [0]],
  });
});

test("drawdowns follow the running peak and reset at new highs", () => {
  const points = makePoints([["2026-10-01", "100"], ["2026-10-02", "80"], ["2026-10-03", "120"],
    ["2026-10-04", "60"], ["2026-10-05", "150"], ["2026-10-06", "135"]]);
  const original = structuredClone(points);
  const expected = [0, -0.2, 0, -0.5, 0, -0.1];
  const actual = drawdowns(points);
  assert.equal(actual.length, expected.length);
  actual.forEach((value, index) => assertNear(value, expected[index]));
  assertNear(riskMetrics(points).range, 0.35);
  assertNear(riskMetrics(points).drawdown, -0.5);
  assert.deepEqual(points, original);
  assert.deepEqual(drawdowns([]), []);
});

test("riskMetrics annualizes sample variance of log returns with 252 sessions", () => {
  const metrics = riskMetrics(makePoints([["2026-10-01", "100"], ["2026-10-02", "200"], ["2026-10-03", "100"]]));
  assertNear(metrics.volatility, Math.LN2 * Math.sqrt(504));
  assert.equal(metrics.range, 0);
  assert.equal(metrics.drawdown, -0.5);
});

test("riskMetrics keeps insufficient observations unknown rather than zero", () => {
  for (const observations of [[], [["2026-10-01", "100"]]]) {
    assert.deepEqual(riskMetrics(makePoints(observations)), { range: null, volatility: null, drawdown: null });
  }
  const two = riskMetrics(makePoints([["2026-10-01", "100"], ["2026-10-02", "80"]]));
  assertNear(two.range, -0.2);
  assertNear(two.drawdown, -0.2);
  assert.equal(two.volatility, null);
  assert.deepEqual(riskMetrics(makePoints([["2026-10-01", "100"], ["2026-10-02", "100"], ["2026-10-03", "100"]])),
    { range: 0, volatility: 0, drawdown: 0 });
});

test("createDraft defaults to four zero holdings and a 10 percent target", () => {
  assert.deepEqual(createDraft(), {
    other_assets: "0", target_percent: "10",
    holdings: GOLD_ETFS.map(({ symbol }) => ({ symbol, quantity: "0", average_cost: "0" })),
    watchlist: GOLD_ETFS.map(({ symbol }) => symbol), note: "",
  });
});

test("createDraft and allocationFromDraft preserve exact decimal strings and persisted settings", () => {
  const record = makeAllocation();
  const original = structuredClone(record);
  const draft = createDraft(record);
  assert.equal(draft.target_percent, "12.3456789123456789");
  assert.equal(draft.holdings.length, 4);
  assert.deepEqual(draft.holdings[0], record.holdings[0]);
  assert.deepEqual(allocationFromDraft(draft), record);
  assert.deepEqual(record, original);
  draft.watchlist.push("518800");
  assert.deepEqual(record.watchlist, original.watchlist);
});

test("target percentages round-trip without binary floating-point rounding", () => {
  for (const [weight, percent, normalized] of [
    ["0", "0", "0"], ["1", "100", "1"], ["0.10", "10", "0.1"], ["0.29", "29", "0.29"],
    ["0.000000000000000001", "0.0000000000000001", "0.000000000000000001"],
    ["0.010000000000000001", "1.0000000000000001", "0.010000000000000001"],
    ["0.999999999999999999", "99.9999999999999999", "0.999999999999999999"],
  ]) {
    const draft = createDraft(makeAllocation({ target_weight: weight }));
    assert.equal(draft.target_percent, percent);
    assert.equal(allocationFromDraft(draft).target_weight, normalized);
  }
});

test("allocationFromDraft omits zero positions and detaches output without changing the draft", () => {
  const draft = createDraft(makeAllocation());
  draft.holdings[1].quantity = "0.000";
  const original = structuredClone(draft);
  const allocation = allocationFromDraft(draft);
  assert.equal(allocation.holdings.length, 1);
  assert.deepEqual(draft, original);
  allocation.holdings[0].quantity = "2";
  allocation.watchlist.push("518800");
  assert.deepEqual(draft, original);
});

test("allocationFromDraft rejects negative, nonfinite and malformed decimal fields", () => {
  for (const invalid of ["-1", "-0", "NaN", "Infinity", "1e309", "1e3", "", " ", ".5", "1.", "+1", "1,000"]) {
    for (const field of ["other_assets", "target_percent", "quantity", "average_cost"]) {
      const draft = createDraft();
      if (field === "quantity" || field === "average_cost") draft.holdings[0][field] = invalid;
      else draft[field] = invalid;
      assert.throws(() => allocationFromDraft(draft), /\u975e\u8d1f\u6709\u9650\u5341\u8fdb\u5236\u6570/,
        `${field} must reject ${JSON.stringify(invalid)}`);
    }
  }
});

test("allocationFromDraft enforces the exact 100 percent boundary", () => {
  for (const target_percent of ["100", "100.000", "00100.000000000"]) {
    assert.equal(allocationFromDraft({ ...createDraft(), target_percent }).target_weight, "1");
  }
  for (const target_percent of ["101", "100.01", "100.000000000000000001"]) {
    assert.throws(() => allocationFromDraft({ ...createDraft(), target_percent }), /100%/);
  }
});

test("allocationFromDraft rejects duplicate and unsupported holdings or watchlist symbols", () => {
  const duplicateHoldings = createDraft();
  duplicateHoldings.holdings.push({ ...duplicateHoldings.holdings[0] });
  const unsupportedHolding = createDraft();
  unsupportedHolding.holdings[0].symbol = "Au99.99";
  const duplicateWatchlist = { ...createDraft(), watchlist: ["518880", "518880"] };
  const unsupportedWatchlist = { ...createDraft(), watchlist: ["AU2612"] };
  const spotWatchlist = { ...createDraft(), watchlist: ["Au99.99"] };
  for (const draft of [duplicateHoldings, unsupportedHolding, duplicateWatchlist, unsupportedWatchlist, spotWatchlist]) {
    assert.throws(() => allocationFromDraft(draft), /\u91cd\u590d\u6216\u4e0d\u53d7\u652f\u6301/);
  }
});
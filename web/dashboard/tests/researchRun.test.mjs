import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
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
const { readResearchSnapshot } = await import(hookUrl);
const { api } = await import(apiUrl);

test("root child IDs drive result and audit fetching, with one shared AbortSignal", async () => {
  const controller = new AbortController();
  const reads = [];
  const client = {
    researchRun: async (id, signal) => {
      assert.equal(signal, controller.signal);
      reads.push(id);
      return { run_id: id, children: id === "root" ? ["child-a", "child-b", "child-a"] : [] };
    },
    researchEvents: async (id, offset, limit, signal) => {
      assert.equal(signal, controller.signal);
      assert.equal(limit, 100);
      return [{ run_id: id, sequence: offset, event_type: "server-event" }];
    },
    researchCalls: async (id, offset, limit, signal) => {
      assert.equal(signal, controller.signal);
      return [{ call_id: id, sequence: offset, role: "server-role" }];
    },
  };
  const snapshot = await readResearchSnapshot("root", controller.signal, client);
  assert.deepEqual(reads, ["root", "child-a", "child-b"]);
  assert.deepEqual(snapshot.children.map((run) => run.run_id), ["child-a", "child-b"]);
  assert.equal(snapshot.audit.length, 3);
  assert.equal(snapshot.audit[2].events[0].event_type, "server-event");
});

test("event and receipt pagination preserves complete server history", async () => {
  const events = Array.from({ length: 101 }, (_, sequence) => ({ sequence }));
  const calls = Array.from({ length: 201 }, (_, sequence) => ({ sequence }));
  const snapshot = await readResearchSnapshot("root", new AbortController().signal, {
    researchRun: async () => ({ run_id: "root", children: [] }),
    researchEvents: async (_id, offset, limit) => events.slice(offset, offset + limit),
    researchCalls: async (_id, offset, limit) => calls.slice(offset, offset + limit),
  });
  assert.deepEqual(snapshot.audit[0].events, events);
  assert.deepEqual(snapshot.audit[0].calls, calls);
});

test("generic API helpers encode IDs and forward signals without touching legacy gold methods", async (context) => {
  const controller = new AbortController();
  const requests = [];
  context.mock.method(globalThis, "fetch", async (path, options) => {
    requests.push({ path, options });
    return { ok: true, json: async () => [] };
  });
  await api.researchRun("run/id ?", controller.signal);
  await api.researchEvents("run/id ?", 100, 100, controller.signal);
  await api.researchCalls("run/id ?", 0, 100, controller.signal);
  assert.equal(requests[0].path, "/api/research/runs/run%2Fid%20%3F");
  assert.ok(requests[1].path.endsWith("/events?offset=100&limit=100"));
  assert.ok(requests.every((request) => request.options.signal === controller.signal));
  assert.equal(typeof api.goldResearchRun, "function");
});

test("preview, run and cancel are separate mocked requests with the exact run budget", async (context) => {
  const requests = [];
  const signal = new AbortController().signal;
  context.mock.method(globalThis, "fetch", async (path, options) => {
    requests.push({ path, options });
    return { ok: true, json: async () => ({}) };
  });
  const plan = { plan_id: "plan-1", budget: { max_calls: 12, max_tokens: 64000 } };
  await api.researchPlan({ question: "gold", use_model: false }, signal);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].path, "/api/research/plans");
  await api.startResearchRun({ plan, budget: plan.budget, idempotency_key: "explicit-key" }, signal);
  await api.cancelResearchRun("root", signal);
  assert.deepEqual(JSON.parse(requests[1].options.body), { plan, budget: plan.budget, idempotency_key: "explicit-key" });
  assert.equal(requests[2].path, "/api/research/runs/root/cancel");
  assert.ok(requests.every((request) => request.options.signal === signal));
});
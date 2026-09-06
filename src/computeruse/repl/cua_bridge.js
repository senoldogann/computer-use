/**
 * Trusted transport. Model code and CUA objects exist only in QuickJS/WASM.
 * No Node functions, promises or object references cross the string-only ABI.
 * Node vm contexts are deliberately not used as a security boundary.
 */
const readline = require("node:readline");
const fs = require("node:fs");
const path = require("node:path");
const { getQuickJS } = require("quickjs-emscripten");
const apiSource = fs.readFileSync(path.join(__dirname, "cua_api.js"), "utf8");
let nextId = 1;
let running = false;
const pending = new Map();
const write = (message) => process.stdout.write(JSON.stringify(message) + "\n");

async function runEval(QuickJS, callId, code) {
  if (running) {
    write({ id: callId, error: { message: "Concurrent evaluation refused" } });
    return;
  }
  running = true;
  const vm = QuickJS.newContext();
  vm.runtime.setMemoryLimit(64 * 1024 * 1024);
  vm.runtime.setMaxStackSize(512 * 1024);
  const deadline = Date.now() + 60000;
  vm.runtime.setInterruptHandler(() => Date.now() >= deadline);
  let active = true;
  const requests = new Set();
  const timers = new Set();
  const deferreds = new Set();
  const pump = () => {
    if (!active) return;
    const jobs = vm.runtime.executePendingJobs();
    if (jobs.error) {
      const message = vm.dump(jobs.error);
      jobs.error.dispose();
      throw new Error(JSON.stringify(message));
    }
  };
  const rpc = vm.newFunction("__hostRpc", (methodHandle, paramsHandle) => {
    const method = vm.getString(methodHandle);
    const params = JSON.parse(vm.getString(paramsHandle));
    const deferred = vm.newPromise();
    deferreds.add(deferred);
    const settle = (response) => {
      if (!active) return;
      const value = vm.newString(JSON.stringify(response));
      deferred.resolve(value);
      value.dispose();
      // Run only after QuickJS has returned from this native callback.
      queueMicrotask(pump);
    };
    if (method === "sleep") {
      const ms = params.ms;
      if (!Number.isFinite(ms) || ms < 0 || ms > 60000) {
        settle({ error: "sleep requires milliseconds between 0 and 60000" });
      } else {
        const timer = setTimeout(() => {
          timers.delete(timer);
          settle({ result: null });
        }, ms);
        timers.add(timer);
      }
    } else {
      const id = nextId++;
      requests.add(id);
      pending.set(id, settle);
      write({ jsonrpc: "2.0", id, method, params });
    }
    return deferred.handle;
  });
  vm.setProp(vm.global, "__hostRpc", rpc);
  rpc.dispose();

  let finalContent = null;
  let evalError = null;

  try {
    vm.unwrapResult(vm.evalCode(apiSource)).dispose();
    const evaluated = vm.unwrapResult(vm.evalCode(
      "evaluateCode(" + JSON.stringify(code) + ")"
    ));
    const finished = vm.resolvePromise(evaluated);
    pump();
    const result = await finished;
    evaluated.dispose();
    if (result.error) {
      const error = vm.dump(result.error);
      result.error.dispose();
      throw new Error([error.name, error.message, error.stack].filter(Boolean).join("\n") || JSON.stringify(error));
    }
    finalContent = vm.getString(result.value);
    result.value.dispose();
  } catch (error) {
    evalError = error;
  } finally {
    active = false;
    for (const id of requests) pending.delete(id);
    for (const timer of timers) clearTimeout(timer);
    for (const deferred of deferreds) {
      if (deferred.alive) deferred.dispose();
    }
    try {
      vm.runtime.executePendingJobs();
    } catch {}
    try {
      vm.dispose();
    } catch (disposeError) {
      if (!evalError) evalError = disposeError;
    }
    running = false;
  }

  if (evalError) {
    write({
      jsonrpc: "2.0",
      id: callId,
      error: {
        code: -32603,
        message: evalError.stack || evalError.message || String(evalError),
      },
    });
  } else {
    write({ jsonrpc: "2.0", id: callId, result: { content: finalContent } });
  }
}

getQuickJS().then((QuickJS) => {
  if (typeof QuickJS.module?._malloc === "function" && typeof QuickJS.module?._free === "function") {
    // Warm up linear WebAssembly memory to 64MB so mid-evaluation growth never corrupts QuickJS GC
    const ptr = QuickJS.module._malloc(64 * 1024 * 1024);
    QuickJS.module._free(ptr);
  }
  const rl = readline.createInterface({ input: process.stdin, terminal: false });
  rl.on("line", (line) => {
    try {
      const msg = JSON.parse(line);
      if (msg.method === "eval") {
        void runEval(QuickJS, msg.id, msg.params.code);
      } else if (pending.has(msg.id)) {
        const settle = pending.get(msg.id);
        pending.delete(msg.id);
        settle(msg.error ? { error: msg.error.message } : { result: msg.result });
      } else {
        throw new Error("Unrecognized bridge response id");
      }
    } catch (error) {
      process.stderr.write(error.stack + "\n");
      process.exitCode = 1;
      rl.close();
    }
  });
  write({ jsonrpc: "2.0", method: "ready" });
}).catch((error) => {
  process.stderr.write(error.stack + "\n");
  process.exitCode = 1;
});

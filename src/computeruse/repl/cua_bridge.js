/**
 * CUA REPL Bridge (Node.js runtime worker)
 *
 * Exposes `globalThis.cua` matching OpenAI's Computer Use Agent API surface.
 * Communicates with the Python orchestrator over stdin/stdout line-delimited JSON-RPC.
 */

const readline = require("readline");

const rl = readline.createInterface({
  input: process.stdin,
  output: process.stdout,
  terminal: false,
});

let nextId = 1;
const pendingRequests = new Map();

function sendRpc(method, params) {
  return new Promise((resolve, reject) => {
    const id = nextId++;
    pendingRequests.set(id, { resolve, reject });
    const payload = JSON.stringify({ jsonrpc: "2.0", id, method, params });
    process.stdout.write(payload + "\n");
  });
}

function handleIncomingLine(line) {
  if (!line.trim()) return;
  try {
    const msg = JSON.parse(line);
    // Response to a pending RPC sent by JS to Python
    if (msg.id && pendingRequests.has(msg.id)) {
      const { resolve, reject } = pendingRequests.get(msg.id);
      pendingRequests.delete(msg.id);
      if (msg.error) {
        reject(new Error(msg.error.message || JSON.stringify(msg.error)));
      } else {
        resolve(msg.result);
      }
      return;
    }

    // Top-level command from Python to evaluate code
    if (msg.method === "eval") {
      runEval(msg.id, msg.params.code);
    }
  } catch (err) {
    process.stderr.write(`Bridge parse error: ${err.message}\n`);
  }
}

rl.on("line", handleIncomingLine);

function parseTargetCoord(target) {
  let elementIndex = null;
  let x = null;
  let y = null;
  let query = null;
  let role = null;
  let title = null;

  if (typeof target === "number") {
    elementIndex = target;
  } else if (typeof target === "string") {
    query = target;
    title = target;
  } else if (Array.isArray(target) && target.length === 2) {
    [x, y] = target;
  } else if (target && typeof target === "object") {
    elementIndex = target.elementIndex ?? target.index ?? null;
    x = target.x ?? null;
    y = target.y ?? null;
    query = target.query ?? null;
    role = target.role ?? null;
    title = target.title ?? target.text ?? target.label ?? null;
  }
  return { elementIndex, x, y, query, role, title };
}

class AppTarget {
  constructor(appId, appName, initialAXState) {
    this.id = appId;
    this.name = appName;
    this._lastState = initialAXState || "";
  }

  async getAXState(options = {}) {
    const res = await sendRpc("getAXState", {
      app: this.name,
      disableDiffing: !!options.disableDiffing,
    });
    this._lastState = res;
    return res;
  }

  async find(target) {
    const parsed = parseTargetCoord(target);
    return await sendRpc("findElement", {
      app: this.name,
      ...parsed,
    });
  }

  async findAll(target) {
    const parsed = parseTargetCoord(target);
    return await sendRpc("findAllElements", {
      app: this.name,
      ...parsed,
    });
  }

  async hasElement(target) {
    try {
      const el = await this.find(target);
      return !!el;
    } catch {
      return false;
    }
  }

  async waitForElement(target, options = {}) {
    const timeoutMs = options.timeoutMs ?? 5000;
    const intervalMs = options.intervalMs ?? 150;
    const startTime = Date.now();

    while (Date.now() - startTime < timeoutMs) {
      try {
        const el = await this.find(target);
        if (el) return el;
      } catch {}
      await cua.sleep(intervalMs);
    }
    throw new Error(
      `Timed out waiting for element matching ${JSON.stringify(target)} in undefined after ${timeoutMs}ms`
    );
  }

  async click(target, options = {}) {
    const parsed = parseTargetCoord(target);

    return await sendRpc("click", {
      app: this.name,
      ...parsed,
      mouseButton: options.mouseButton || "left",
      clickCount: options.clickCount || 1,
    });
  }

  async doubleClick(target, options = {}) {
    return await this.click(target, { ...options, clickCount: 2 });
  }

  async rightClick(target, options = {}) {
    return await this.click(target, { ...options, mouseButton: "right" });
  }

  async drag(startTarget, endTarget, options = {}) {
    const start = parseTargetCoord(startTarget);
    const end = parseTargetCoord(endTarget);
    return await sendRpc("drag", {
      app: this.name,
      startElementIndex: start.elementIndex,
      startX: start.x,
      startY: start.y,
      endElementIndex: end.elementIndex,
      endX: end.x,
      endY: end.y,
      durationMs: options.durationMs || 250,
    });
  }

  async pressKey(key, modifiers = []) {
    return await sendRpc("pressKey", {
      app: this.name,
      key,
      modifiers,
    });
  }

  async pressHotkey(modifiers, key) {
    return await this.pressKey(key, modifiers);
  }

  async typeText(text) {
    return await sendRpc("typeText", {
      app: this.name,
      text,
    });
  }

  async paste(text, options = {}) {
    return await sendRpc("paste", {
      app: this.name,
      text,
      format: options.format || "text",
    });
  }

  async setValue(elementIndex, value) {
    return await sendRpc("setValue", {
      app: this.name,
      elementIndex,
      value: String(value),
    });
  }

  async scroll(target, direction = "down", pages = 1) {
    const { elementIndex, x, y } = parseTargetCoord(target);
    return await sendRpc("scroll", {
      app: this.name,
      elementIndex,
      x,
      y,
      direction,
      pages,
    });
  }

  async getScreenshot() {
    return await sendRpc("getScreenshot", {
      app: this.name,
    });
  }
}

globalThis.cua = {
  async getApp(appName) {
    const res = await sendRpc("getApp", { app: appName });
    return new AppTarget(res.id, res.name, res.initialAXState);
  },

  async listApps() {
    return await sendRpc("listApps", {});
  },

  async getState() {
    return await sendRpc("getState", {});
  },

  sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  },

  wait(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  },
};

// Global shorthand aliases matching prompt examples
globalThis.getApp = globalThis.cua.getApp;
globalThis.sleep = globalThis.cua.sleep;
globalThis.wait = globalThis.cua.wait;

function prepareCode(code) {
  const trimmed = code.trim();
  if (!trimmed) return trimmed;

  // If code already contains an explicit return, leave it untouched
  if (/\breturn\b/.test(trimmed)) {
    return trimmed;
  }

  // Strip trailing semicolons
  let clean = trimmed.replace(/;+\s*$/, "");

  // Find last semicolon or line break
  const lastSemi = clean.lastIndexOf(";");
  const lastNewline = clean.lastIndexOf("\n");
  const cutIdx = Math.max(lastSemi, lastNewline);

  const declRegex = /^(const|let|var|if|for|while|try|catch|throw|switch|class|function)\b/;

  if (cutIdx === -1) {
    if (!declRegex.test(clean)) {
      return `return (${clean});`;
    }
    return clean;
  }

  const head = clean.slice(0, cutIdx + 1);
  const tail = clean.slice(cutIdx + 1).trim();

  if (tail && !declRegex.test(tail)) {
    return `${head}\nreturn (${tail});`;
  }

  return clean;
}

async function runEval(callId, code) {
  try {
    const preparedCode = prepareCode(code);
    const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
    let fn;

    try {
      fn = new AsyncFunction("cua", preparedCode);
    } catch {
      fn = new AsyncFunction("cua", code);
    }

    const evalResult = await fn(globalThis.cua);

    let content = "";
    if (typeof evalResult === "string") {
      content = evalResult;
    } else if (evalResult !== undefined && evalResult !== null) {
      content = typeof evalResult === "object" ? JSON.stringify(evalResult) : String(evalResult);
    }

    const payload = JSON.stringify({
      jsonrpc: "2.0",
      id: callId,
      result: { content },
    });
    process.stdout.write(payload + "\n");
  } catch (err) {
    const payload = JSON.stringify({
      jsonrpc: "2.0",
      id: callId,
      error: {
        code: -32603,
        message: err.stack || err.message,
      },
    });
    process.stdout.write(payload + "\n");
  }
}

// Signal readiness
process.stdout.write(JSON.stringify({ jsonrpc: "2.0", method: "ready" }) + "\n");

// All objects in this file live inside QuickJS/WASM, never in Node's realm.
async function sendRpc(method, params) {
  const response = JSON.parse(await __hostRpc(method, JSON.stringify(params)));
  if (response.error) throw new Error(response.error);
  return response.result;
}

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

/**
 * One found element, plus the ability to photograph itself.
 *
 * `find()` used to answer with a bare record, so a model that wanted to *look*
 * at what it had found had to read the bounds back out, do rectangle
 * arithmetic in JavaScript and call `cropScreenshot` — or, far more often,
 * call `getScreenshot()` and pay for the whole display. `crop()` closes that
 * gap: the element already knows where it is.
 */
class ElementHandle {
  constructor(appName, record) {
    Object.assign(this, record);
    Object.defineProperty(this, "_appName", { value: appName, enumerable: false });
  }

  /**
   * A base64 PNG data URI of just this element.
   *
   * `padding` (logical points, default 8) widens the crop so the element is
   * shown with enough of its surroundings to be recognisable — a checkbox
   * cropped to its own bounds is a square with no label.
   */
  async crop(options = {}) {
    return await sendRpc("cropScreenshot", {
      app: this._appName,
      bounds: { x: this.x, y: this.y, width: this.width, height: this.height },
      padding: options.padding ?? 8,
    });
  }

  toJSON() {
    const { x, y, width, height, role, title, value, index, focused } = this;
    return { x, y, width, height, role, title, value, index, focused };
  }
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
    const record = await sendRpc("findElement", {
      app: this.name,
      ...parsed,
    });
    return record ? new ElementHandle(this.name, record) : null;
  }

  async findAll(target) {
    const parsed = parseTargetCoord(target);
    const records = await sendRpc("findAllElements", {
      app: this.name,
      ...parsed,
    });
    return (records || []).map((record) => new ElementHandle(this.name, record));
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

  async selectMenuItem(menuPath) {
    const path = Array.isArray(menuPath)
      ? menuPath
      : String(menuPath).split(">").map((s) => s.trim()).filter(Boolean);

    return await sendRpc("selectMenuItem", {
      app: this.name,
      path,
    });
  }

  async findVisual(query) {
    return await sendRpc("findVisualElement", {
      app: this.name,
      query: typeof query === "string" ? query : (query.query || query.text || ""),
    });
  }

  async getWindowBounds() {
    return await sendRpc("getWindowBounds", {
      app: this.name,
    });
  }

  async cropScreenshot(bounds, options = {}) {
    return await sendRpc("cropScreenshot", {
      app: this.name,
      bounds,
      padding: options.padding ?? 0,
    });
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
    return sendRpc("sleep", { ms });
  },

  wait(ms) {
    return sendRpc("sleep", { ms });
  },

  async transaction(actionFn, options = {}) {
    const maxRetries = options.retries ?? 2;
    const backoffMs = options.backoffMs ?? 150;
    let attempt = 0;

    while (attempt <= maxRetries) {
      try {
        return await actionFn();
      } catch (err) {
        attempt++;
        if (attempt > maxRetries) {
          if (options.rollbackAction === "escape") {
            try { await sendRpc("pressKey", { key: "Escape" }); } catch {}
          }
          throw err;
        }
        await cua.sleep(backoffMs * attempt);
      }
    }
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

async function evaluateCode(code) {
  const prepared = prepareCode(code);
  const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
  // Dynamic compilation is confined to QuickJS: constructors have no Node realm.
  const fn = new AsyncFunction("cua", prepared);
  const value = await fn(cua);
  return typeof value === "string" ? value :
    value === undefined || value === null ? "" : JSON.stringify(value);
}

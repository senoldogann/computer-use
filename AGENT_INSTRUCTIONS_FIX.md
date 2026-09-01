# 🛠️ Freebuff Framework — Bug Fixes, Security & UI Stability Instructions

Please implement the following 6 fixes and test suites in the codebase. Ensure all changes adhere strictly to the project constitution (AGENTS.md) and pass all linters and tests with 0 errors and 0 warnings.

---

## 1. Fix `ClipboardPaste` Security Bypass in Autonomy Guard
* **File:** `src/computeruse/security/autonomy.py` (around line 215)
* **Problem:** `classify_risk` only checks `press_hotkey` and `type_text` payloads when detecting destructive shell commands (`_TYPED_COMMANDS`). Destructive commands pasted via `clipboard_paste` bypass the guard at Autonomy Level 2.
* **Fix:** Include `ClipboardPaste` in the risk classification type check:
  ```python
  if turn.action.type in {"press_hotkey", "type_text", "clipboard_paste"} and isinstance(
      turn.action, (PressHotkey, TypeText, ClipboardPaste)
  ):
      payload = getattr(turn.action, "key", "") or getattr(turn.action, "text", "")
      subject = f"{subject} {payload}".lower()
  ```

---

## 2. Make `parse_decision` Resilient to Preceding Explanatory Braces
* **File:** `src/computeruse/orchestrator/prompts.py` (around line 260)
* **Problem:** `parse_decision` slices between the first `{` and last `}` using `find("{")`. If the model outputs text with braces before the JSON block (e.g. `Plan {step 1}: \n```json\n{"thought": ...}\n``` `), `json.loads` fails.
* **Fix:** First attempt extraction using regex matching for Markdown code blocks (`r"```(?:json)?\s*(\{.*?\})\s*```"`, `re.DOTALL`). If not matched, fallback to finding the outer JSON object boundaries.

---

## 3. Preserve `screenshot_b64` in `WorkingState` on Exception Recovery
* **File:** `src/computeruse/orchestrator/loop.py` (around line 498)
* **Problem:** When an exception occurs during actuation or orient (`except Exception as exc:`), the newly instantiated `WorkingState` omits `screenshot_b64=state.screenshot_b64`, clearing visual perception on error recovery turns.
* **Fix:** Pass `screenshot_b64=state.screenshot_b64` into the `WorkingState(...)` constructor in the exception handler.

---

## 4. Normalize Composite Hotkeys (e.g., `"cmd+c"`, `"ctrl+shift+p"`)
* **File:** `src/computeruse/orchestrator/prompts.py` (around line 224)
* **Problem:** If a model returns a composite shortcut as `key="Cmd+Shift+P"` with empty `modifiers`, the Rust driver cannot map `"Cmd+Shift+P"` to a single virtual keycode.
* **Fix:** In `_normalize_action_payload`, if `action.get("key")` contains `+`, split by `+`, map modifier tokens (`cmd`, `command`, `shift`, `alt`, `opt`, `ctrl`, `control`) into `action["modifiers"]`, and set `action["key"]` to the trailing primary key token.

---

## 5. Fix History "Clear All" Button in `menu.html` (`WKWebView window.confirm` Limitation)
* **File:** `driver/assets/menu.html` (around line 1729)
* **Problem:** `window.clearAllHistory` relies on `if(confirm(...))`. In macOS embedded `WKWebView`, `window.confirm()` returns `false` by default without a custom delegate, preventing history from ever clearing.
* **Fix:** Remove the blocking `window.confirm` call and clear history directly, triggering view re-render:
  ```javascript
  window.clearAllHistory = function(){
    saveHistory([]);
    renderHistoryView();
    if(window.renderAnalyticsView) {
      window.renderAnalyticsView();
    }
  };
  ```

---

## 6. Comprehensive Verification Suite
After applying all changes, verify that the entire test and lint suite passes cleanly:

```bash
uv run pytest -q && uv run pyright && uv run ruff check . && cargo test --manifest-path driver/Cargo.toml && cargo clippy --manifest-path driver/Cargo.toml --all-targets -- -D warnings
```

Then rebuild and package the application:
```bash
bash scripts/package_app.sh
```

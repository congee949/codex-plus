// Claude-style prompt suggestions: after a turn finishes, a model from the configured provider
// predicts the user's next message. It shows as the composer placeholder and Tab accepts it.
// When to stay silent, the output filter and the prompt follow Claude Code 2.1.283
// (`enr`, `q` and the SUGGESTION MODE prompt in its bundle).
// The renderer CSP only allows OpenAI hosts, so requests go through whoever resolves the
// provider (`suggestion_provider` in codex_tw.py): the launcher (`codex_launch.py`) through
// the CDP binding `__lpsSend`, or the main process of a re-signed build
// (`window.__localPromptSuggestion`).
export const ENABLED_KEY = "codex.local.prompt-suggestion.enabled.v1";
export const MODEL_KEY = "codex.local.prompt-suggestion.model.v1";
export const UNUSED_KEY = "codex.local.prompt-suggestion.unused.v1";

const PREFERRED_MODELS = [/sonnet/i, /flash|fast/i, /mini/i];
const EFFORT_ORDER = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"];
const SETTLE_MS = 1500;
const MIN_ASSISTANT_MESSAGES = 2;
// After this many shown-but-unused suggestions in a row, only every UNUSED_EVERY-th
// eligible turn gets one, until one is accepted.
const UNUSED_LIMIT = 20;
const UNUSED_EVERY = 10;
const MAX_TURNS = 6;
const INPUT_BUDGET = 8000;
const ORIGINAL_CHARS = 1500;
const USER_CHARS = 800;
// Options and questions sit at the end of a reply, so assistant text keeps its tail.
const ASSISTANT_TAIL_CHARS = 1500;
const STATS_LIMIT = 50;
// The app's own test for a pending approval card (`hasActiveApprovalSurface`).
const APPROVAL_SURFACE =
  "[data-codex-approval-surface]:not([data-app-shell-active-page='false'] *)";
// The app keeps the threads you left mounted as inactive pages; everything read from the
// page must come from the active one, or another thread's suggestion would show here.
const INACTIVE = "[data-app-shell-active-page='false'], [aria-hidden='true'], [inert]";
const INSTRUCTIONS = `[SUGGESTION MODE: Suggest what the user might naturally type next into Codex.]
FIRST: Look at the user's recent messages and original request.
Your job is to predict what THEY would type - not what you think they should do.
THE TEST: Would they think "I was just about to type that"?
EXAMPLES:
User asked "fix the bug and run tests", bug is fixed → "run the tests"
After code written → "try it out"
The assistant offers options → suggest the one the user would likely pick, based on conversation
The assistant asks to continue → "yes" or "go ahead"
Task complete, obvious follow-up → "commit this" or "push it"
After error or misunderstanding → silence (let them assess/correct)
Be specific: "run the tests" beats "continue".
NEVER SUGGEST:
- Evaluative ("looks good", "thanks")
- Questions ("what about...?")
- Assistant-voice ("Let me...", "I'll...", "Here's...")
- New ideas they didn't ask about
- Multiple sentences
Stay silent if the next step isn't obvious from what the user said.
Stay silent if a suggestion could be unsafe or inappropriate — including any sensitive topic (security incidents, credentials, harm, private data). Even when the user is doing legitimate security or cybersecurity work, do not predict potentially unsafe actions.
Format: 2-12 words (a Chinese character counts as half a word), in the user's language and style. Or nothing.
Reply with ONLY the suggestion, no quotes or explanation.`;

const listeners = new Set();
const state = {
  enabled: true,
  model: "",
  models: null,
  modelsError: null,
  lastError: null,
  persistenceError: false,
};
let language = "en";
let initialized = false;

// Per conversation: the turn signature seen on arrival, the last signature evaluated,
// and the latest result.
const baselines = new Map();
const opened = new Set();
const decided = new Map();
const results = new Map();
let pending = null;
let settleTimer = 0;
let ghostEditor = null;
// The suggestion on screen that has been neither accepted nor dismissed yet.
let outstanding = null;
let eligibleTurns = 0;
const stats = [];
globalThis.__lpsStats = stats;

const BRIDGE_TIMEOUT_MS = 70000;
const calls = new Map();
let nextCall = 0;
let launcherBridge = null;

// The launcher answers a `__lpsSend` call by evaluating `__lpsResolve(id, ok, value)` here.
globalThis.__lpsResolve = (id, ok, value) => {
  const call = calls.get(id);
  if (!call) return;
  calls.delete(id);
  clearTimeout(call.timer);
  if (ok) call.resolve(value);
  else call.reject(new Error(String(value)));
};

function bridge() {
  if (globalThis.__localPromptSuggestion) return globalThis.__localPromptSuggestion;
  if (typeof globalThis.__lpsSend !== "function") return null;
  launcherBridge ??= {
    request: (request) => new Promise((resolve, reject) => {
      const id = ++nextCall;
      const timer = setTimeout(() => {
        calls.delete(id);
        reject(new Error("launcher timeout"));
      }, BRIDGE_TIMEOUT_MS);
      calls.set(id, { resolve, reject, timer });
      globalThis.__lpsSend(JSON.stringify({ ...request, id }));
    }),
  };
  return launcherBridge;
}

function notify() {
  for (const listener of listeners) listener();
}

export function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function getState() {
  return state;
}

let snapshot = null;
let snapshotKey = "";
function getSnapshot() {
  const key = JSON.stringify([
    state.enabled, state.model, state.models?.length ?? -1, state.modelsError,
    state.lastError, state.persistenceError,
  ]);
  if (key !== snapshotKey) {
    snapshotKey = key;
    snapshot = { ...state };
  }
  return snapshot;
}

export function copyFor(locale = language) {
  return String(locale).toLowerCase().startsWith("zh")
    ? {
        label: "下一句建议",
        description: "每轮回复结束后，用所选模型预测你可能发送的下一句，以灰字显示在输入框，按 Tab 填入。",
        modelLabel: "建议模型",
        modelDescription: "来自 codex-plus 设置的模型提供商。",
        loading: "正在读取模型…",
        unavailable: "无法读取模型列表",
        failed: "上次生成失败",
        saveError: "设置未能保存，重新打开后可能恢复默认。",
        retry: "重试",
      }
    : {
        label: "Prompt suggestions",
        description: "After each reply, predict your likely next message with the selected model. It appears in the composer; press Tab to accept.",
        modelLabel: "Suggestion model",
        modelDescription: "From the model provider set for codex-plus.",
        loading: "Loading models…",
        unavailable: "Couldn't load models",
        failed: "Last suggestion failed",
        saveError: "Unable to save this setting. It may reset when reopened.",
        retry: "Retry",
      };
}

function persist(key, value) {
  try {
    localStorage.setItem(key, value);
    state.persistenceError = false;
  } catch {
    state.persistenceError = true;
  }
}

export function setEnabled(value) {
  state.enabled = Boolean(value);
  persist(ENABLED_KEY, state.enabled ? "1" : "0");
  if (!state.enabled) clearGhost();
  else scheduleCheck();
  notify();
}

export function setModel(value) {
  state.model = String(value);
  persist(MODEL_KEY, state.model);
  results.clear();
  decided.clear();
  clearGhost();
  notify();
}

export async function loadModels() {
  const api = bridge();
  if (!api) {
    state.modelsError = "bridge unavailable";
    notify();
    return;
  }
  state.modelsError = null;
  notify();
  try {
    const response = await api.request({ op: "models" });
    const list = Array.isArray(response?.data) ? response.data : [];
    state.models = list
      .filter((model) => typeof model?.id === "string")
      .map((model) => ({
        id: model.id,
        name: model.display_name || model.magpie_label || model.id,
        efforts: (model.supported_reasoning_levels ?? [])
          .map((level) => level?.effort)
          .filter(Boolean),
      }));
    if (!state.models.some((model) => model.id === state.model)) {
      const fallback = PREFERRED_MODELS
        .map((pattern) => state.models.find((model) => pattern.test(model.id)))
        .find(Boolean) ?? state.models[0];
      if (fallback) {
        state.model = fallback.id;
        persist(MODEL_KEY, state.model);
      }
    }
  } catch (error) {
    state.modelsError = errorText(error);
  }
  notify();
}

function errorText(error) {
  return String(error?.message ?? error)
    .replace(/^Error invoking remote method '[^']+': (Error: )?/, "")
    .slice(0, 300);
}

// ---------------------------------------------------------------- transcript

function active(selector) {
  return [...document.querySelectorAll(selector)].filter((element) => !element.closest(INACTIVE));
}

// The router is in-memory, so the URL is the same for every thread.
function conversationKey() {
  return active("[data-above-composer-conversation-id]")[0]
    ?.getAttribute("data-above-composer-conversation-id") ?? `${location.pathname}${location.hash}`;
}

// Each assistant message starts with a screen-reader heading ("ChatGPT said:") carrying
// data-conversation-role; its parent holds the message.
function lastAssistant(turn) {
  const headings = turn.querySelectorAll("[data-conversation-role='assistant']");
  return headings[headings.length - 1]?.parentElement ?? null;
}

// Besides the reply, the container holds the heading, a live region that briefly announces
// "Response complete: …", and the timestamp row; none of them is part of the reply.
const NOT_REPLY = "[data-conversation-role], [aria-live], .turn-action-controls";

function assistantText(container, rendered) {
  if (!container) return "";
  const parts = [];
  for (const child of container.children) {
    if (!child.matches(NOT_REPLY)) parts.push(rendered ? child.innerText : child.textContent);
  }
  return parts.join("\n").trim();
}

function clean(text) {
  return text.replace(/\n{3,}/g, "\n\n").trim();
}

function turnText(element) {
  return clean(element?.innerText ?? "");
}

function head(text, limit) {
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

function tail(text, limit) {
  return text.length > limit ? `…${text.slice(-limit)}` : text;
}

function readTurns() {
  const turns = [];
  for (const turn of active("[data-turn-key]")) {
    const user = turnText(turn.querySelector("[data-user-message-bubble]"));
    const assistant = clean(assistantText(lastAssistant(turn), true));
    if (user || assistant) {
      turns.push({ key: turn.getAttribute("data-turn-key"), user, assistant, element: turn });
    }
  }
  return turns;
}

function isStreaming(element) {
  return Boolean(element.querySelector(
    "[data-w-streaming='true'], [data-content-streaming='true'], [data-w-streaming-cursor]",
  ));
}

// Cheap enough to run on every mutation: no layout, and the list is virtualized.
function lastTurn() {
  const turns = active("[data-turn-key]");
  return turns[turns.length - 1] ?? null;
}

function signature(element) {
  const text = assistantText(lastAssistant(element), false);
  return `${element.getAttribute("data-turn-key")}:${text.length}:${text.slice(-80)}`;
}

function scrollParent(element) {
  for (let parent = element.parentElement; parent; parent = parent.parentElement) {
    if (/auto|scroll/.test(getComputedStyle(parent).overflowY) &&
        parent.scrollHeight > parent.clientHeight + 1) {
      return parent;
    }
  }
  return document.scrollingElement;
}

// The turn list is virtualized; the last rendered turn is the newest only while it is on screen.
function onScreen(element) {
  const scroller = scrollParent(element);
  const bounds = scroller === document.scrollingElement
    ? { top: 0, bottom: innerHeight }
    : scroller.getBoundingClientRect();
  const rect = element.getBoundingClientRect();
  return rect.top < bounds.bottom && rect.bottom > bounds.top;
}

// The virtualized list positions each turn slot absolutely; the first turn sits at the top.
function isThreadStart(element) {
  const slot = element.parentElement;
  if (slot?.style.top) return parseFloat(slot.style.top) === 0;
  return element === active("[data-turn-key]")[0] &&
    !slot?.previousElementSibling;
}

function turnBlock({ user, assistant }) {
  return [
    user ? `User: ${head(user, USER_CHARS)}` : null,
    assistant ? `Assistant: ${tail(assistant, ASSISTANT_TAIL_CHARS)}` : null,
  ].filter(Boolean).join("\n\n");
}

// Newest turns first until the budget runs out; the last turn is always kept. The thread's
// first request goes in front when it is rendered and not already among those turns.
export function buildContext(turns, threadStart) {
  const recent = turns.slice(-MAX_TURNS);
  const first = threadStart && turns[0]?.user && !recent.includes(turns[0])
    ? `Original request: ${head(turns[0].user, ORIGINAL_CHARS)}`
    : "";
  const blocks = [];
  let used = first.length;
  for (let index = recent.length - 1; index >= 0; index--) {
    const block = turnBlock(recent[index]);
    if (blocks.length && used + block.length > INPUT_BUDGET) break;
    blocks.unshift(block);
    used += block.length;
  }
  if (first && used <= INPUT_BUDGET) blocks.unshift(first);
  return blocks.join("\n\n---\n\n");
}

function buildInput(context) {
  return [{
    role: "user",
    content: [{
      type: "input_text",
      text: `${context}\n\nSuggest the user's next message.`,
    }],
  }];
}

function lowestEffort(modelId) {
  const efforts = state.models?.find((model) => model.id === modelId)?.efforts ?? [];
  return [...efforts].sort((a, b) => EFFORT_ORDER.indexOf(a) - EFFORT_ORDER.indexOf(b))[0];
}

function outputText(response) {
  if (typeof response?.output_text === "string") return response.output_text;
  return (response?.output ?? [])
    .filter((item) => item?.type === "message")
    .flatMap((item) => item.content ?? [])
    .filter((part) => part?.type === "output_text" && typeof part.text === "string")
    .map((part) => part.text)
    .join("");
}

function cleanSuggestion(text) {
  let line = String(text ?? "").trim()
    .replace(/^<(suggestion|response|output|answer|result)>([\s\S]*)<\/\1>$/i, "$2")
    .trim().split(/\n/)[0].trim();
  return line.replace(/^(user|用户)\s*[:：]\s*/i, "").replace(/^["“'「](.*)["”'」]$/, "$1").trim();
}

// ---------------------------------------------------------------- filter (Claude Code `q`)

const HAN = /\p{Script=Han}/u;
const PHONETIC =
  /[\p{Script=Hiragana}\p{Script=Katakana}ーｰ\p{Script=Thai}\p{Script=Lao}\p{Script=Khmer}\p{Script=Myanmar}]/u;
const HANGUL = /\p{Script=Hangul}/u;
const LETTER = /[\p{L}\p{N}]/u;
const SHORT_REPLIES = new Set([
  "yes", "yeah", "yep", "yea", "yup", "sure", "ok", "okay", "push", "commit", "deploy",
  "stop", "continue", "check", "exit", "quit", "no",
]);

function scripts(text) {
  const counts = { han: 0, phonetic: 0, hangul: 0, other: 0 };
  for (const char of text) {
    if (HAN.test(char)) counts.han++;
    else if (PHONETIC.test(char)) counts.phonetic++;
    else if (HANGUL.test(char)) counts.hangul++;
    else if (LETTER.test(char)) counts.other++;
  }
  return counts;
}

function cjkChars(text) {
  const { han, phonetic, hangul } = scripts(text);
  return han + phonetic + hangul;
}

// A space-separated word counts 1; a Han character counts 1/2, kana and similar 1/4.
export function wordCount(text) {
  const trimmed = text.trim();
  if (!trimmed) return 0;
  let total = 0;
  for (const word of trimmed.split(/\s+/)) {
    const { han, phonetic, hangul, other } = scripts(word);
    total += han === 0 && phonetic === 0
      ? 1
      : (other + hangul > 0 ? 1 : 0) + han / 2 + phonetic / 4;
  }
  return Math.ceil(total);
}

const PAIRS = [["“", "”"], ["「", "」"], ["『", "』"], ["（", "）"], ["(", ")"], ["《", "》"], ["【", "】"], ["[", "]"]];

// One statement followed by one closing question, such as "好的。要苹果清单吗？",
// with every quote and bracket closed. The word limit applies before this.
function shortQuestion(text) {
  if (!/[？?]$/.test(text)) return false;
  const sentences = text.split(/(?<=[。！？]|[.!?](?=\s))\s*/u).filter(Boolean);
  if (sentences.length !== 2 || /[？?]$/.test(sentences[0])) return false;
  if ((text.match(/"/g) ?? []).length % 2) return false;
  return PAIRS.every(([open, close]) => text.split(open).length === text.split(close).length);
}

const REJECTIONS = [
  ["done", (text, lower) => lower === "done" ||
    /^\P{L}*(完了(しました)?|完成了?|완료됨?)\P{L}*$/u.test(text)],
  ["meta_text", (text, lower) => lower === "nothing found" || lower === "nothing found." ||
    lower.startsWith("nothing to suggest") || lower.startsWith("no suggestion") ||
    /\bsilence is\b|\bstay(s|ing)? silent\b/.test(lower) || /^\W*silence\W*$/.test(lower) ||
    /^\P{L}*(沈黙|沉默|静默|침묵|提案(なし|はありません)|特に(なし|ありません)|[无没沒]有?建[议議]|(제안|해당)\s*없음)\P{L}*$/u.test(text)],
  ["meta_wrapped", (text) => /^(\(.*\)|\[.*\]|（.*）|［.*］|【.*】|〔.*〕)$/.test(text)],
  ["error_message", (text, lower) => lower.startsWith("api error:") ||
    lower.startsWith("prompt is too long") || lower.startsWith("request timed out") ||
    lower.startsWith("invalid api key") || lower.startsWith("image was too large")],
  ["prefixed_label", (text) => /^\w+:\s/.test(text)],
  ["too_few_words", (text, lower, words) => {
    if (words >= 2 || text.startsWith("/")) return false;
    const cjk = cjkChars(text);
    if (cjk > 0) return cjk < 2;
    return !SHORT_REPLIES.has(lower);
  }],
  ["too_many_words", (text, lower, words) => words > 12],
  ["too_long", (text) => text.length >= 100],
  ["multiple_sentences", (text) =>
    /[.!?]\s+[A-Z]|[。！？]\s*[\p{L}\p{N}]/u.test(text) && !shortQuestion(text)],
  ["has_formatting", (text) => /[\n*]|\*\*/.test(text)],
  ["evaluative", (text, lower) =>
    /thanks|thank you|looks good|sounds good|that works|that worked|that's all|nice|great|perfect|makes sense|awesome|excellent/.test(lower) ||
    /^\P{L}*(ありがとう(ございます|ございました)?|助かりました|[谢謝][谢謝][你您]?|感谢[你您]?|感謝します|감사합니다|고마워요?)(\P{L}|的|$)/u.test(text) ||
    /(良さそう|よさそう|いいですね|看起来不错|太好了|完璧|完美|좋네요)(です|ですね|だね)?\P{L}*$/u.test(text)],
  ["assistant_voice", (text) =>
    /^(let me|i'll|i've|i'm|i can|i would|i think|i notice|here's|here is|here are|that's|this is|this will|you can|you should|you could|sure,|of course|certainly|让我(?!们)|我来|我会|我将)/i.test(text)],
];

// The reason a candidate is not shown, or null when it passes.
export function rejectReason(text) {
  if (!text) return "empty";
  const lower = text.toLowerCase();
  const words = wordCount(text);
  for (const [reason, test] of REJECTIONS) {
    if (test(text, lower, words)) return reason;
  }
  return null;
}

// ---------------------------------------------------------------- gating

function record(key, outcome, extra) {
  const entry = { at: new Date().toISOString(), thread: key, outcome, ...extra };
  stats.push(entry);
  if (stats.length > STATS_LIMIT) stats.splice(0, stats.length - STATS_LIMIT);
  // The launcher writes each outcome to launcher.log; there is no reply.
  if (typeof globalThis.__lpsSend === "function") {
    globalThis.__lpsSend(JSON.stringify({ op: "log", entry }));
  }
}

function readUnused() {
  try {
    return Number(localStorage.getItem(UNUSED_KEY)) || 0;
  } catch {
    return 0;
  }
}

// Shown and not accepted: count toward the streak, which stays at the limit once reached.
function resolveOutstanding(outcome) {
  if (!outstanding) return;
  const { key, sig } = outstanding;
  outstanding = null;
  record(key, outcome, { sig });
  if (outcome === "accepted") {
    persist(UNUSED_KEY, "0");
    eligibleTurns = 0;
  } else {
    persist(UNUSED_KEY, String(Math.min(readUnused() + 1, UNUSED_LIMIT)));
  }
}

function assistantMessages() {
  return active("[data-turn-key] [data-conversation-role='assistant']").length;
}

// The same order as Claude Code: focus, conversation age, blocking UI, then the unused streak.
function suppressReason(key) {
  if (!document.hasFocus() || document.visibilityState !== "visible") return "unfocused";
  // A thread opened with history is past its start even if older turns are not rendered.
  if (!opened.has(key) && assistantMessages() < MIN_ASSISTANT_MESSAGES) {
    return "early_conversation";
  }
  if (document.querySelector(APPROVAL_SURFACE)) return "pending_approval";
  if (readUnused() >= UNUSED_LIMIT) {
    const allowed = eligibleTurns % UNUSED_EVERY === 0;
    eligibleTurns++;
    if (!allowed) return "unused_streak";
  }
  return null;
}

// ---------------------------------------------------------------- generation

async function generate(key, sig, turns, threadStart) {
  const api = bridge();
  if (!api || !state.model) return;
  if (state.models === null) await loadModels();
  const controller = { cancelled: false };
  pending?.cancel?.();
  pending = { key, sig, cancel: () => { controller.cancelled = true; } };
  const effort = lowestEffort(state.model);
  const context = buildContext(turns, threadStart);
  try {
    const response = await api.request({
      op: "suggest",
      body: {
        model: state.model,
        instructions: INSTRUCTIONS,
        input: buildInput(context),
        reasoning: effort ? { effort } : undefined,
        max_output_tokens: 2048,
      },
    });
    if (controller.cancelled) return;
    const text = cleanSuggestion(outputText(response));
    const reason = rejectReason(text);
    const editor = composerEditor();
    // The user moved on while the model was working.
    const stale = conversationKey() !== key || (editor && !editorIsEmpty(editor));
    results.set(key, { sig, text: reason || stale ? "" : text });
    record(key, reason ?? (stale ? "discarded" : "ready"), { chars: context.length, text });
    if (state.lastError) {
      state.lastError = null;
      notify();
    }
  } catch (error) {
    if (controller.cancelled) return;
    results.set(key, { sig, text: "" });
    state.lastError = errorText(error);
    record(key, "error", { chars: context.length, error: state.lastError });
    notify();
  } finally {
    if (pending?.sig === sig) pending = null;
  }
  render();
}

function check() {
  settleTimer = 0;
  if (!state.enabled) return;
  const key = conversationKey();
  const element = lastTurn();
  if (!element || !onScreen(element)) {
    render();
    return;
  }
  const turns = readTurns();
  const last = turns[turns.length - 1];
  if (!last?.assistant || last.element !== element || isStreaming(element)) {
    if (!baselines.has(key)) baselines.set(key, null);
    render();
    return;
  }
  const sig = signature(element);
  // Opening a thread shows its history; only turns that finish while it is open count.
  if (!baselines.has(key)) {
    baselines.set(key, sig);
    opened.add(key);
  }
  if (baselines.get(key) === sig) {
    render();
    return;
  }
  // Each finished turn is evaluated once, so refocusing later does not bring a suggestion.
  if (decided.get(key) !== sig) {
    decided.set(key, sig);
    results.delete(key);
    resolveOutstanding("ignored");
    const reason = suppressReason(key);
    if (reason) record(key, reason, { sig });
    else generate(key, sig, turns, isThreadStart(turns[0].element));
  }
  render();
}

function scheduleCheck() {
  clearTimeout(settleTimer);
  settleTimer = setTimeout(check, SETTLE_MS);
}

// ---------------------------------------------------------------- composer ghost

function composerEditor() {
  const editors = active(".ProseMirror[contenteditable='true']")
    .filter((editor) => editor.getClientRects().length > 0);
  return editors.find((editor) => editor.closest("[data-codex-composer-root]")) ??
    editors.find((editor) => editor.contains(document.activeElement)) ??
    editors[editors.length - 1] ?? null;
}

function editorIsEmpty(editor) {
  return editor.querySelector(".placeholder") !== null ||
    editor.textContent.trim() === "";
}

function cssString(text) {
  return `"${text.replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/[\n\r]+/g, " ")}"`;
}

function clearGhost() {
  if (!ghostEditor) return;
  delete ghostEditor.dataset.localPromptSuggestion;
  ghostEditor.style.removeProperty("--local-prompt-suggestion");
  ghostEditor = null;
}

function currentSuggestion() {
  if (!state.enabled) return "";
  const result = results.get(conversationKey());
  if (!result?.text) return "";
  const element = lastTurn();
  return element && result.sig === signature(element) ? result.text : "";
}

function render() {
  const text = currentSuggestion();
  const editor = text ? composerEditor() : null;
  if (!editor || !editorIsEmpty(editor)) {
    clearGhost();
    return;
  }
  if (ghostEditor !== editor) clearGhost();
  ghostEditor = editor;
  editor.dataset.localPromptSuggestion = "";
  editor.style.setProperty("--local-prompt-suggestion", cssString(`${text}  ⇥`));
  const key = conversationKey();
  const { sig } = results.get(key);
  if (outstanding?.key !== key || outstanding.sig !== sig) {
    resolveOutstanding("ignored");
    outstanding = { key, sig };
    record(key, "shown", { sig });
  }
}

function accept(event) {
  if (event.key !== "Tab" || event.shiftKey || event.altKey || event.ctrlKey ||
      event.metaKey || event.isComposing) return;
  const editor = ghostEditor;
  const text = currentSuggestion();
  if (!editor || !text || !editor.contains(event.target) || !editorIsEmpty(editor)) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  results.delete(conversationKey());
  resolveOutstanding("accepted");
  clearGhost();
  editor.focus();
  // ProseMirror ignores execCommand here; a synthetic paste goes through its input pipeline.
  const data = new DataTransfer();
  data.setData("text/plain", text);
  editor.dispatchEvent(new ClipboardEvent("paste", {
    clipboardData: data, bubbles: true, cancelable: true,
  }));
}

function dismiss(event) {
  if (event.key !== "Escape" || !ghostEditor?.contains(event.target)) return;
  results.delete(conversationKey());
  resolveOutstanding("dismissed");
  clearGhost();
}

// Treat the current thread's last turn as new, so a suggestion appears without a fresh turn.
export function forgetBaseline() {
  const key = conversationKey();
  baselines.set(key, null);
  decided.delete(key);
  results.delete(key);
  scheduleCheck();
}

export function initializePromptSuggestion() {
  if (initialized || typeof document === "undefined") return;
  initialized = true;
  language = document.documentElement.lang || navigator.language || "en";
  try {
    state.enabled = localStorage.getItem(ENABLED_KEY) !== "0";
    state.model = localStorage.getItem(MODEL_KEY) ?? "";
  } catch {
    state.persistenceError = true;
  }
  window.addEventListener("keydown", accept, true);
  window.addEventListener("keydown", dismiss, true);
  window.addEventListener("storage", (event) => {
    if (event.key === ENABLED_KEY) state.enabled = event.newValue !== "0";
    else if (event.key === MODEL_KEY) state.model = event.newValue ?? "";
    else return;
    notify();
    render();
  });
  const start = () => {
    new MutationObserver(() => {
      render();
      scheduleCheck();
    }).observe(document.body, { childList: true, subtree: true, characterData: true });
    if (bridge()) loadModels();
    scheduleCheck();
  };
  if (document.body) start();
  else document.addEventListener("DOMContentLoaded", start, { once: true });
}

// ---------------------------------------------------------------- settings

// Several routes share a display name (e.g. "Opus 5.5"); the route prefix tells them apart.
function optionLabel(model, models) {
  const route = model.id.includes("/") ? model.id.split("/")[0] : "";
  const shared = models.filter((other) => other.name === model.name).length > 1;
  return shared && route ? `${model.name} (${route})` : model.name;
}

// The app supplies its own React runtime and controls (SettingsRow, Switch, the dropdown
// used by other General rows, and its secondary button), avoiding a second React and
// keeping the rows visually identical to the native ones.
export function createPromptSuggestionSetting(React, jsx, ui) {
  const { SettingsRow, Switch, Dropdown, DropdownTrigger, Menu, Button } = ui;
  return function PromptSuggestionSetting({ locale = language } = {}) {
    const current = React.useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
    const text = copyFor(locale);
    React.useEffect(() => {
      if (current.models === null && !current.modelsError) loadModels();
    }, []);

    const alert = (message) => jsx.jsx("p", {
      role: "alert",
      className: "local-prompt-suggestion-error",
      children: message,
    }, message);
    const toggleErrors = [
      current.persistenceError ? text.saveError : null,
      current.enabled && current.lastError ? `${text.failed}: ${current.lastError}` : null,
    ].filter(Boolean);
    const toggle = jsx.jsx(SettingsRow, {
      label: text.label,
      description: jsx.jsxs("div", { children: [text.description, ...toggleErrors.map(alert)] }),
      control: jsx.jsx(Switch, {
        checked: current.enabled,
        onChange: setEnabled,
        ariaLabel: text.label,
      }),
    }, "toggle");

    const models = current.models ?? [];
    const selected = models.find((model) => model.id === current.model);
    let control;
    if (current.modelsError) {
      control = jsx.jsx(Button, {
        color: "secondary",
        size: "toolbar",
        onClick: () => loadModels(),
        children: text.retry,
      });
    } else {
      control = (props) => jsx.jsx(Dropdown, {
        align: "end",
        triggerButton: jsx.jsx(DropdownTrigger, {
          ...props,
          disabled: !current.enabled || current.models === null,
          children: current.models === null
            ? text.loading
            : selected ? optionLabel(selected, models) : current.model,
        }),
        children: jsx.jsx(Menu.RadioGroup, {
          value: current.model,
          children: models.map((model) => jsx.jsx(Menu.RadioItem, {
            value: model.id,
            onSelect: () => setModel(model.id),
            children: optionLabel(model, models),
          }, model.id)),
        }),
      });
    }
    const model = jsx.jsx(SettingsRow, {
      label: text.modelLabel,
      description: current.modelsError
        ? jsx.jsxs("div", {
            children: [text.modelDescription, alert(`${text.unavailable}: ${current.modelsError}`)],
          })
        : text.modelDescription,
      control,
    }, "model");
    return jsx.jsxs(jsx.Fragment, { children: [toggle, model] });
  };
}

initializePromptSuggestion();

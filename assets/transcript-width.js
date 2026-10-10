// Mirrors Claude's Transcript width setting: Narrow / Medium / Wide set the
// content measure (768 / 960 / 1280px) of the transcript and composer columns.
export const STORAGE_KEY = "codex.local.appearance.transcript-width.v1";
export const WIDTHS = Object.freeze({ narrow: 768, medium: 960, wide: 1280 });
export const DEFAULT_WIDTH = "narrow";

const ORDER = Object.keys(WIDTHS);
const listeners = new Set();
let selectedWidth = DEFAULT_WIDTH;
let persistenceError = false;
let initialized = false;
let layoutRevision = 0;
let language = "en";

export function normalizeWidth(value) {
  return Object.hasOwn(WIDTHS, value) ? value : DEFAULT_WIDTH;
}

export function copyFor(locale = language) {
  return String(locale).toLowerCase().startsWith("zh")
    ? {
        label: "对话宽度",
        description: "对话记录与输入框列的最大宽度。",
        options: { narrow: "窄", medium: "中", wide: "宽" },
        error: "设置未能保存，重新打开后可能恢复默认。",
      }
    : {
        label: "Transcript width",
        description: "Maximum width of the transcript and composer columns.",
        options: { narrow: "Narrow", medium: "Medium", wide: "Wide" },
        error: "Unable to save this setting. It may reset when reopened.",
      };
}

function notify() {
  for (const listener of listeners) listener();
}

export function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function getWidth() {
  return selectedWidth;
}

export function getPersistenceError() {
  return persistenceError;
}

function applyLayout() {
  if (!document.body) return;
  document.body.dataset.localTranscriptWidth = selectedWidth;
  document.body.style.setProperty(
    "--local-transcript-max-width",
    `${WIDTHS[selectedWidth]}px`,
  );
}

let paddingProbe = null;

// Outer width of the transcript column (content + both inline paddings). The patched
// thread-summary-panel layout reads this instead of its built-in 736px, so the pinned
// summary decides gutter / shift / overlay against the column that is actually drawn.
export function columnWidth() {
  if (!document.body) return 736;
  if (!paddingProbe?.isConnected) {
    paddingProbe = document.createElement("div");
    paddingProbe.setAttribute("aria-hidden", "true");
    paddingProbe.style.cssText =
      "position:absolute;visibility:hidden;pointer-events:none;height:0;" +
      "width:var(--thread-body-inline-padding,16px)";
    document.body.append(paddingProbe);
  }
  return WIDTHS[selectedWidth] + 2 * paddingProbe.getBoundingClientRect().width;
}

globalThis.__ltwColumnWidth = columnWidth;

function scrollParent(element) {
  for (let parent = element.parentElement; parent; parent = parent.parentElement) {
    if (
      /auto|scroll/.test(getComputedStyle(parent).overflowY) &&
      parent.scrollHeight > parent.clientHeight + 1
    ) {
      return parent;
    }
  }
  return document.scrollingElement;
}

function captureAnchors() {
  const snapshots = new Map();
  const selector = "[data-turn-key], [data-content-search-turn-key]";
  for (const turn of document.querySelectorAll(selector)) {
    const scroller = scrollParent(turn);
    if (!scroller || snapshots.has(scroller)) continue;
    const bounds = scroller === document.scrollingElement
      ? { top: 0, bottom: innerHeight }
      : scroller.getBoundingClientRect();
    const rect = turn.getBoundingClientRect();
    if (rect.bottom <= bounds.top || rect.top >= bounds.bottom) continue;
    const atBottom = scroller.scrollHeight - scroller.scrollTop -
      scroller.clientHeight <= 8;
    snapshots.set(scroller, {
      scroller,
      turn,
      top: rect.top - bounds.top,
      atBottom,
    });
  }
  return [...snapshots.values()];
}

function restoreAnchors(snapshots, revision) {
  if (revision !== layoutRevision) return;
  for (const { scroller, turn, top, atBottom } of snapshots) {
    if (!scroller.isConnected) continue;
    if (atBottom) {
      scroller.scrollTop = scroller.scrollHeight;
    } else if (turn.isConnected) {
      const origin = scroller === document.scrollingElement
        ? 0
        : scroller.getBoundingClientRect().top;
      scroller.scrollTop += turn.getBoundingClientRect().top - origin - top;
    }
  }
}

function updateWidth(value) {
  const next = normalizeWidth(value);
  const snapshots = next === selectedWidth ? [] : captureAnchors();
  const revision = ++layoutRevision;
  selectedWidth = next;
  applyLayout();
  // Force layout before restoring the visible turn, then settle after paint.
  restoreAnchors(snapshots, revision);
  requestAnimationFrame(() => restoreAnchors(snapshots, revision));
  // Panels size themselves from the main content width; let them re-measure.
  window.dispatchEvent(new Event("resize"));
}

export function setWidth(value) {
  if (!Object.hasOwn(WIDTHS, value)) {
    throw new RangeError(`Unknown transcript width: ${value}`);
  }
  initializeTranscriptWidth();
  updateWidth(value);
  try {
    localStorage.setItem(STORAGE_KEY, value);
    persistenceError = false;
  } catch {
    persistenceError = true;
  }
  notify();
}

export function initializeTranscriptWidth() {
  if (initialized || typeof document === "undefined") return;
  initialized = true;
  language = document.documentElement.lang || navigator.language || "en";
  try {
    selectedWidth = normalizeWidth(localStorage.getItem(STORAGE_KEY));
  } catch {
    persistenceError = true;
  }
  applyLayout();
  if (!document.body) {
    document.addEventListener("DOMContentLoaded", applyLayout, { once: true });
  }
  window.addEventListener("storage", (event) => {
    if (event.storageArea !== localStorage) return;
    if (event.key !== STORAGE_KEY && event.key !== null) return;
    updateWidth(event.key === null ? DEFAULT_WIDTH : event.newValue);
    persistenceError = false;
    notify();
  });
}

// The app supplies its own React runtime and SettingsRow, avoiding a second React.
export function createTranscriptWidthSetting(React, jsx, SettingsRow) {
  return function TranscriptWidthSetting({ locale = language } = {}) {
    const value = React.useSyncExternalStore(subscribe, getWidth, () => DEFAULT_WIDTH);
    const failed = React.useSyncExternalStore(
      subscribe,
      getPersistenceError,
      () => false,
    );
    const text = copyFor(locale);
    const buttons = React.useRef({});

    // Radio-group keyboard model: arrows move and select, Home/End jump.
    const onKeyDown = (event) => {
      const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
      let index = ORDER.indexOf(value);
      if (step) index = (index + step + ORDER.length) % ORDER.length;
      else if (event.key === "Home") index = 0;
      else if (event.key === "End") index = ORDER.length - 1;
      else return;
      event.preventDefault();
      setWidth(ORDER[index]);
      buttons.current[ORDER[index]]?.focus();
    };

    const control = jsx.jsx("div", {
      role: "radiogroup",
      "aria-label": text.label,
      className: "local-transcript-width-control",
      onKeyDown,
      children: ORDER.map((width) => jsx.jsx("button", {
        type: "button",
        role: "radio",
        "aria-checked": value === width,
        tabIndex: value === width ? 0 : -1,
        "data-state": value === width ? "on" : "off",
        ref: (node) => { buttons.current[width] = node; },
        onClick: () => setWidth(width),
        children: text.options[width],
      }, width)),
    });
    return jsx.jsx(SettingsRow, {
      label: text.label,
      description: jsx.jsxs("div", {
        children: [
          text.description,
          failed ? jsx.jsx("p", {
            role: "alert",
            className: "local-transcript-width-error",
            children: text.error,
          }) : null,
        ],
      }),
      control,
    });
  };
}

initializeTranscriptWidth();

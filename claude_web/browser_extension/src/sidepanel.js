import { renderMarkdown } from "./markdown.js";
import { actionTitle, assertLocalServiceUrl, loadSettings } from "./templates.js";

const $ = (id) => document.getElementById(id);

let currentAsk = null;
let currentController = null;
let currentSessionId = "";
let currentOpenUrl = "";
let answerText = "";
let lastStreamedMessageId = "";
let streamedMessageIds = new Set();
let contextExpanded = false;
let activeTabContext = null;
let requestVersion = 0;
let saveTimer = 0;

const TAB_STATES_KEY = "tabStates";
const MAX_TAB_STATES = 20;
const MAX_PAGE_CONTEXT_CHARS = 40000;

function show(el, visible) {
  el.classList.toggle("hidden", !visible);
}

function resizeQuestionBox() {
  const el = $("customQuestion");
  el.style.height = "auto";
  const maxHeight = 132;
  el.style.height = `${Math.min(el.scrollHeight, maxHeight)}px`;
}

function setStatus(text) {
  $("statusLine").textContent = text;
  const dot = $("statusDot");
  if (!dot) return;
  dot.classList.toggle("active", /连接|正在|准备/.test(text));
  dot.classList.toggle("done", /完成|已复制/.test(text));
}

function setLastError(error) {
  const el = $("lastError");
  const message = error?.message || "";
  el.textContent = message ? `最近一次错误：${message}` : "";
  show(el, Boolean(message));
}

function askSourceLabel(ask) {
  return ask?.sourceType === "page" || ask?.action === "page" ? "当前页面" : "当前选中";
}

function renderAnswer() {
  $("answer").innerHTML = renderMarkdown(answerText);
}

function normalizeStateUrl(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  try {
    const url = new URL(raw);
    url.hash = "";
    return url.href;
  } catch {
    return raw.split("#")[0];
  }
}

function tabStateKey(context = activeTabContext) {
  const tabId = context?.tabId;
  if (tabId == null) return "";
  const url = normalizeStateUrl(context.pageUrl || context.url || "");
  return `${tabId}:${url}`;
}

function createStateSnapshot() {
  if (!currentAsk) return null;
  return {
    ask: currentAsk,
    sessionId: currentSessionId,
    openUrl: currentOpenUrl,
    answerText,
    contextExpanded,
    status: $("statusLine").textContent || "",
    running: Boolean(currentController),
    updatedAt: Date.now(),
  };
}

async function saveCurrentTabState() {
  if (saveTimer) {
    clearTimeout(saveTimer);
    saveTimer = 0;
  }
  const key = tabStateKey(currentAsk || activeTabContext);
  const snapshot = createStateSnapshot();
  if (!key || !snapshot) return;
  const stored = await chrome.storage.local.get(TAB_STATES_KEY);
  const tabStates = stored[TAB_STATES_KEY] || {};
  tabStates[key] = snapshot;
  const entries = Object.entries(tabStates).sort((a, b) => (b[1].updatedAt || 0) - (a[1].updatedAt || 0));
  await chrome.storage.local.set({
    [TAB_STATES_KEY]: Object.fromEntries(entries.slice(0, MAX_TAB_STATES)),
  });
}

function scheduleSaveCurrentTabState() {
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    saveTimer = 0;
    saveCurrentTabState().catch(() => {});
  }, 350);
}

function restoreTabState(snapshot) {
  if (!snapshot?.ask) return false;
  if (saveTimer) {
    clearTimeout(saveTimer);
    saveTimer = 0;
  }
  if (currentController) {
    currentController.abort();
    currentController = null;
  }
  requestVersion += 1;
  currentAsk = snapshot.ask;
  currentSessionId = snapshot.sessionId || "";
  currentOpenUrl = snapshot.openUrl || "";
  answerText = snapshot.answerText || "";
  lastStreamedMessageId = "";
  streamedMessageIds = new Set();
  contextExpanded = Boolean(snapshot.contextExpanded);
  $("subtitle").textContent = actionTitle(currentAsk.action);
  $("sourceKicker").textContent = askSourceLabel(currentAsk);
  $("askTitle").textContent = currentAsk.pageTitle || "当前网页";
  $("askUrl").textContent = currentAsk.pageUrl || "";
  $("askUrl").href = currentAsk.pageUrl || "#";
  $("selectionPreview").textContent = selectedPreview(currentAsk.selectedText);
  updateContextToggle();
  $("customQuestion").value = "";
  resizeQuestionBox();
  renderAnswer();
  $("copyBtn").disabled = !answerText.trim();
  $("continueBtn").disabled = !currentOpenUrl;
  $("stopBtn").disabled = true;
  $("askBtn").disabled = false;
  show($("emptyState"), false);
  show($("workState"), true);
  show($("answerState"), true);
  setStatus(snapshot.running ? "回答已在其他标签页中断，可继续追问" : (snapshot.status || "完成"));
  return true;
}

async function restoreStateForActiveTab() {
  const key = tabStateKey();
  if (!key) {
    resetPanel("选中网页内容后右键提问");
    return false;
  }
  const stored = await chrome.storage.local.get(TAB_STATES_KEY);
  const snapshot = stored[TAB_STATES_KEY]?.[key];
  if (snapshot && restoreTabState(snapshot)) return true;
  resetPanel("当前标签页还没有提问");
  return false;
}

function askMatchesActiveTab(ask) {
  if (!ask?.selectedText) return false;
  if (ask.tabId == null || !activeTabContext?.tabId) return true;
  if (ask.tabId !== activeTabContext.tabId) return false;
  const askUrl = normalizeStateUrl(ask.pageUrl || "");
  const activeUrl = normalizeStateUrl(activeTabContext.url || "");
  return !askUrl || !activeUrl || askUrl === activeUrl;
}

function activeTabLabel() {
  return activeTabContext?.title || activeTabContext?.url || "当前标签页";
}

function resetPanel(message = "选中网页内容后右键提问") {
  requestVersion += 1;
  if (saveTimer) {
    clearTimeout(saveTimer);
    saveTimer = 0;
  }
  if (currentController) {
    currentController.abort();
    currentController = null;
  }
  currentAsk = null;
  currentSessionId = "";
  currentOpenUrl = "";
  answerText = "";
  lastStreamedMessageId = "";
  streamedMessageIds = new Set();
  $("subtitle").textContent = message;
  $("customQuestion").value = "";
  resizeQuestionBox();
  $("answer").innerHTML = "";
  $("copyBtn").disabled = true;
  $("continueBtn").disabled = true;
  $("stopBtn").disabled = true;
  $("askBtn").disabled = false;
  show($("workState"), false);
  show($("answerState"), false);
  show($("emptyState"), true);
}

function resetForDifferentTab() {
  saveCurrentTabState().then(() => restoreStateForActiveTab()).catch(() => {
    resetPanel("当前标签页还没有提问");
  });
  chrome.storage.local.remove("pendingAsk").catch(() => {});
}

function selectedPreview(text) {
  const value = String(text || "").trim();
  return value.length > 3500 ? value.slice(0, 3500) + "\n\n...已截断预览" : value;
}

async function getActiveTab() {
  if (activeTabContext?.tabId != null) {
    try {
      return await chrome.tabs.get(activeTabContext.tabId);
    } catch {}
  }
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  return tabs?.[0] || null;
}

function extractReadablePageText() {
  const blockedTags = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "SVG", "CANVAS"]);
  const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      const text = node.nodeValue || "";
      if (!text.trim()) return NodeFilter.FILTER_REJECT;
      const parent = node.parentElement;
      if (!parent || blockedTags.has(parent.tagName)) return NodeFilter.FILTER_REJECT;
      const style = window.getComputedStyle(parent);
      if (style && (style.display === "none" || style.visibility === "hidden")) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const parts = [];
  let total = 0;
  while (walker.nextNode()) {
    const text = (walker.currentNode.nodeValue || "").replace(/\s+/g, " ").trim();
    if (!text) continue;
    parts.push(text);
    total += text.length + 1;
    if (total > 60000) break;
  }
  const headings = Array.from(document.querySelectorAll("h1,h2,h3"))
    .map((el) => (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim())
    .filter(Boolean)
    .slice(0, 24);
  const metaDescription = document.querySelector('meta[name="description"]')?.content || "";
  return {
    title: document.title || "",
    url: location.href,
    description: metaDescription.trim(),
    headings,
    text: parts.join("\n"),
  };
}

function pageContextText(result) {
  const headings = Array.isArray(result?.headings) && result.headings.length
    ? `页面标题层级：\n${result.headings.map((h) => `- ${h}`).join("\n")}\n\n`
    : "";
  const description = result?.description ? `页面描述：${result.description}\n\n` : "";
  const body = String(result?.text || "").trim();
  const combined = `${description}${headings}页面正文：\n${body}`.trim();
  return combined.length > MAX_PAGE_CONTEXT_CHARS
    ? combined.slice(0, MAX_PAGE_CONTEXT_CHARS) + "\n\n...已截断当前页面正文"
    : combined;
}

async function captureCurrentPage() {
  if (currentController) return false;
  setStatus("正在读取当前页面...");
  try {
    const tab = await getActiveTab();
    if (!tab?.id) throw new Error("找不到当前标签页");
    const url = tab.url || "";
    if (!/^https?:|^file:/.test(url)) {
      throw new Error("当前页面类型不支持读取");
    }
    if (!chrome.scripting?.executeScript) {
      throw new Error("当前扩展未启用页面读取权限，请在 chrome://extensions 刷新 Claude Code Web 扩展后重试");
    }
    const [injection] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: extractReadablePageText,
    });
    const result = injection?.result || {};
    const text = pageContextText(result);
    if (!text.trim()) throw new Error("没有读取到可见正文");
    renderAsk({
      id: crypto.randomUUID(),
      action: "page",
      sourceType: "page",
      selectedText: text,
      pageUrl: result.url || tab.url || "",
      pageTitle: result.title || tab.title || "当前页面",
      tabId: tab.id,
      windowId: tab.windowId ?? null,
      createdAt: Date.now(),
    });
    return true;
  } catch (error) {
    setStatus(`读取失败：${error.message || error}`);
    setLastError(error);
    return false;
  }
}

async function startQuickPageAsk(question) {
  const ready = await captureCurrentPage();
  if (!ready || !currentAsk) return;
  $("customQuestion").value = question;
  resizeQuestionBox();
  await sendAsk();
}

async function startNewAsk() {
  const key = tabStateKey(currentAsk || activeTabContext);
  if (key) {
    const stored = await chrome.storage.local.get(TAB_STATES_KEY);
    const states = stored[TAB_STATES_KEY] || {};
    delete states[key];
    await chrome.storage.local.set({ [TAB_STATES_KEY]: states });
  }
  resetPanel("选中网页内容后右键提问，或读取当前页");
}

function renderAsk(ask) {
  if (!askMatchesActiveTab(ask)) {
    resetForDifferentTab();
    return;
  }
  requestVersion += 1;
  currentAsk = ask;
  currentSessionId = "";
  currentOpenUrl = "";
  answerText = "";
  lastStreamedMessageId = "";
  streamedMessageIds = new Set();
  $("subtitle").textContent = actionTitle(ask.action);
  $("sourceKicker").textContent = askSourceLabel(ask);
  $("askTitle").textContent = ask.pageTitle || "当前网页";
  $("askUrl").textContent = ask.pageUrl || "";
  $("askUrl").href = ask.pageUrl || "#";
  $("selectionPreview").textContent = selectedPreview(ask.selectedText);
  contextExpanded = false;
  updateContextToggle();
  $("customQuestion").value = "";
  resizeQuestionBox();
  $("answer").innerHTML = "";
  $("copyBtn").disabled = true;
  $("continueBtn").disabled = true;
  show($("emptyState"), false);
  setLastError(null);
  show($("workState"), true);
  show($("answerState"), false);
  chrome.storage.local.remove("pendingAsk").catch(() => {});
  saveCurrentTabState().catch(() => {});
  if (ask.action !== "custom" && ask.action !== "page") sendAsk();
}

function getEventText(obj) {
  if (obj.type === "stream_event") {
    if (obj.event?.type === "message_start") {
      lastStreamedMessageId = obj.event.message?.id || "";
      if (lastStreamedMessageId) streamedMessageIds.add(lastStreamedMessageId);
      return "";
    }
    if (obj.event?.type === "content_block_delta") {
      const delta = obj.event.delta || {};
      return delta.type === "text_delta" ? (delta.text || "") : "";
    }
    return "";
  }
  if (obj.type === "assistant" && Array.isArray(obj.message?.content)) {
    if (obj.message.id && streamedMessageIds.has(obj.message.id)) return "";
    return obj.message.content
      .filter((block) => block && block.type === "text")
      .map((block) => block.text || "")
      .join("\n");
  }
  if (obj.type === "error") {
    throw new Error(obj.message || "Claude returned an error");
  }
  return "";
}

async function sendAsk() {
  if (!currentAsk || currentController) return;
  if (!askMatchesActiveTab(currentAsk)) {
    resetForDifferentTab();
    return;
  }
  const ask = currentAsk;
  const thisRequestVersion = requestVersion;
  const settings = await loadSettings();
  if (thisRequestVersion !== requestVersion) return;
  if (!settings.token) {
    setStatus("请先在设置页填写 Token");
    chrome.runtime.openOptionsPage();
    return;
  }
  let serviceUrl = "";
  try {
    serviceUrl = assertLocalServiceUrl(settings.serviceUrl);
  } catch (error) {
    if (thisRequestVersion !== requestVersion) return;
    setStatus(error.message || String(error));
    chrome.runtime.openOptionsPage();
    return;
  }
  const question = $("customQuestion").value.trim();
  if (ask.action === "custom" && !question) {
    setStatus("请输入自定义问题");
    return;
  }
  if (ask.action === "page" && !question) {
    setStatus("请输入要问当前页面的问题");
    return;
  }
  if (question) {
    $("customQuestion").value = "";
    resizeQuestionBox();
  }
  saveCurrentTabState().catch(() => {});

  currentController = new AbortController();
  answerText = "";
  $("answer").innerHTML = "";
  $("stopBtn").disabled = false;
  $("askBtn").disabled = true;
  $("copyBtn").disabled = true;
  $("continueBtn").disabled = true;
  show($("answerState"), true);
  setStatus("连接 Claude Code Web...");
  saveCurrentTabState().catch(() => {});

  try {
    const resp = await fetch(`${serviceUrl}/api/extension/ask`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Claude-Web-Extension-Token": settings.token,
      },
      body: JSON.stringify({
        action: ask.action,
        selected_text: ask.selectedText,
        context_type: ask.sourceType || (ask.action === "page" ? "page" : "selection"),
        question,
        page_url: ask.pageUrl,
        page_title: ask.pageTitle,
        cwd: settings.cwd || null,
        model: $("modelSelect").value || settings.model || null,
        permission_mode: settings.permissionMode || "default",
        session_id: currentSessionId || null,
      }),
      signal: currentController.signal,
    });
    if (!resp.ok || !resp.body) {
      let detail = `HTTP ${resp.status}`;
      try {
        const data = await resp.json();
        detail = data.detail || detail;
      } catch {}
      throw new Error(detail);
    }

    setStatus("Claude 正在回答...");
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const chunks = buffer.split("\n\n");
      buffer = chunks.pop() || "";
      for (const chunk of chunks) {
        const line = chunk.split("\n").find((item) => item.startsWith("data: "));
        if (!line) continue;
        const obj = JSON.parse(line.slice(6));
        if (obj.type === "extension_meta") {
          if (thisRequestVersion !== requestVersion) return;
          currentSessionId = obj.session_id || "";
          currentOpenUrl = obj.open_url || "";
          $("continueBtn").disabled = !currentOpenUrl;
          scheduleSaveCurrentTabState();
          continue;
        }
        const text = getEventText(obj);
        if (text) {
          if (thisRequestVersion !== requestVersion) return;
          answerText += text;
          renderAnswer();
          scheduleSaveCurrentTabState();
        }
        if (obj.type === "done") {
          setStatus("完成");
          saveCurrentTabState().catch(() => {});
        }
      }
    }
    if (!answerText.trim()) setStatus("完成，但没有文本输出");
    else setStatus("完成");
    saveCurrentTabState().catch(() => {});
  } catch (error) {
    if (thisRequestVersion !== requestVersion) return;
    if (error.name === "AbortError") setStatus("已停止");
    else setStatus(`出错：${error.message || error}`);
    saveCurrentTabState().catch(() => {});
  } finally {
    if (thisRequestVersion !== requestVersion) return;
    currentController = null;
    $("stopBtn").disabled = true;
    $("askBtn").disabled = false;
    $("copyBtn").disabled = !answerText.trim();
    $("continueBtn").disabled = !currentOpenUrl;
    saveCurrentTabState().catch(() => {});
  }
}

async function stopAsk() {
  if (currentController) currentController.abort();
  const settings = await loadSettings();
  if (currentSessionId && settings.token) {
    let serviceUrl = "";
    try { serviceUrl = assertLocalServiceUrl(settings.serviceUrl); }
    catch { return; }
    fetch(`${serviceUrl}/api/extension/stop/${encodeURIComponent(currentSessionId)}`, {
      method: "POST",
      headers: { "X-Claude-Web-Extension-Token": settings.token },
    }).catch(() => {});
  }
}

async function copyAnswer() {
  if (!answerText.trim()) return;
  await navigator.clipboard.writeText(answerText);
  setStatus("已复制");
  saveCurrentTabState().catch(() => {});
}

async function openContinue() {
  if (currentOpenUrl) {
    await chrome.tabs.create({ url: currentOpenUrl });
  }
}

function updateContextToggle() {
  const preview = $("selectionPreview");
  preview.classList.toggle("expanded", contextExpanded);
  preview.classList.toggle("collapsed", !contextExpanded);
  const label = contextExpanded ? "收起上下文" : "展开上下文";
  $("toggleContextBtn").title = label;
  $("toggleContextBtn").setAttribute("aria-label", label);
  $("toggleContextTextBtn").textContent = contextExpanded ? "收起" : "展开";
}

function toggleContext() {
  contextExpanded = !contextExpanded;
  updateContextToggle();
  saveCurrentTabState().catch(() => {});
}

function renderModelSelect(settings) {
  const modelSelect = $("modelSelect");
  const value = (settings.model || "").trim();
  if (value && Array.from(modelSelect.options).some((option) => option.value === value)) {
    modelSelect.value = value;
  } else {
    modelSelect.value = "";
  }
}

async function loadPendingAsk() {
  const { pendingAsk, lastExtensionError, activeTabContext: storedActiveTabContext } = await chrome.storage.local.get([
    "pendingAsk",
    "lastExtensionError",
    "activeTabContext",
  ]);
  activeTabContext = storedActiveTabContext || activeTabContext;
  if (pendingAsk?.selectedText) {
    renderAsk(pendingAsk);
    return;
  }
  if (lastExtensionError?.message && Date.now() - lastExtensionError.createdAt < 60000) {
    setLastError(lastExtensionError);
  }
  await restoreStateForActiveTab();
}

$("askBtn").addEventListener("click", sendAsk);
$("stopBtn").addEventListener("click", stopAsk);
$("copyBtn").addEventListener("click", copyAnswer);
$("continueBtn").addEventListener("click", openContinue);
$("capturePageBtn").addEventListener("click", captureCurrentPage);
$("openOptionsBtn").addEventListener("click", () => {
  saveCurrentTabState().catch(() => {});
  chrome.runtime.openOptionsPage();
});
$("emptyOptionsBtn").addEventListener("click", () => {
  saveCurrentTabState().catch(() => {});
  chrome.runtime.openOptionsPage();
});
$("emptyCapturePageBtn").addEventListener("click", captureCurrentPage);
$("newAskBtn").addEventListener("click", startNewAsk);
document.querySelectorAll("[data-quick-question]").forEach((button) => {
  button.addEventListener("click", () => startQuickPageAsk(button.dataset.quickQuestion || ""));
});
$("toggleContextBtn").addEventListener("click", toggleContext);
$("toggleContextTextBtn").addEventListener("click", toggleContext);
$("customQuestion").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    sendAsk();
  }
});
$("customQuestion").addEventListener("input", resizeQuestionBox);
$("modelSelect").addEventListener("change", () => {
  chrome.storage.sync.set({ model: $("modelSelect").value }).catch(() => {});
});
renderModelSelect(await loadSettings());
resizeQuestionBox();

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.activeTabContext?.newValue) {
    const nextContext = changes.activeTabContext.newValue;
    const previousContext = activeTabContext;
    const switchedTab = !previousContext || previousContext.tabId !== nextContext.tabId;
    const switchedUrl = !previousContext || normalizeStateUrl(previousContext.url) !== normalizeStateUrl(nextContext.url);
    saveCurrentTabState().catch(() => {});
    activeTabContext = nextContext;
    if (currentAsk && (switchedTab || switchedUrl) && !askMatchesActiveTab(currentAsk)) {
      resetForDifferentTab();
    } else if (switchedTab || switchedUrl) {
      restoreStateForActiveTab().catch(() => {});
    }
  }
  if (area === "local" && changes.pendingAsk?.newValue) {
    renderAsk(changes.pendingAsk.newValue);
  }
  if (area === "local" && changes.lastExtensionError?.newValue) {
    setLastError(changes.lastExtensionError.newValue);
  }
});

loadPendingAsk();

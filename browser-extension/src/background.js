import { ACTIONS, loadSettings } from "./templates.js";

const MENU_ROOT = "claude-web-root";
const MENU_PREFIX = "claude-web-action-";
const ACTIVE_TAB_CONTEXT_KEY = "activeTabContext";

function isExtensionPage(url) {
  return Boolean(url && url.startsWith(chrome.runtime.getURL("")));
}

function tabContext(tab) {
  if (!tab || tab.id == null) return null;
  if (isExtensionPage(tab.url)) return null;
  return {
    tabId: tab.id,
    windowId: tab.windowId ?? null,
    url: tab.url || "",
    title: tab.title || "",
    updatedAt: Date.now(),
  };
}

function rememberError(error) {
  const message = error?.message || String(error || "Unknown extension error");
  chrome.storage.local.set({
    lastExtensionError: {
      message,
      createdAt: Date.now(),
    },
  }).catch(() => {});
}

async function createMenus() {
  try {
    const settings = await loadSettings();
    const mode = settings.assistantMode === "code" ? "code" : "chat";
    const visibleActions = mode === "code"
      ? ["review", "test", "explain", "custom"]
      : ["explain", "rewrite", "custom"];
    await chrome.contextMenus.removeAll();
    chrome.contextMenus.create({
      id: MENU_ROOT,
      title: mode === "code" ? "Claude Code" : "Claude 网页问答",
      contexts: ["selection"],
    });
    for (const action of visibleActions) {
      const item = ACTIONS[action];
      chrome.contextMenus.create({
        id: MENU_PREFIX + action,
        parentId: MENU_ROOT,
        title: item.menuTitle,
        contexts: ["selection"],
      });
    }
  } catch (error) {
    rememberError(error);
  }
}

async function configureSidePanel() {
  if (!chrome.sidePanel?.setPanelBehavior) return;
  try {
    await chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
  } catch (error) {
    rememberError(error);
  }
}

function publishActiveTab(tab) {
  const context = tabContext(tab);
  if (!context) return;
  chrome.storage.local.set({ [ACTIVE_TAB_CONTEXT_KEY]: context }).catch(rememberError);
}

chrome.runtime.onInstalled.addListener(() => {
  createMenus();
  configureSidePanel();
});

chrome.runtime.onStartup.addListener(() => {
  createMenus();
  configureSidePanel();
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "sync" && changes.assistantMode) createMenus();
});

chrome.tabs.onActivated.addListener(({ tabId }) => {
  chrome.tabs.get(tabId).then(publishActiveTab).catch(rememberError);
});

chrome.tabs.onUpdated.addListener((_tabId, changeInfo, tab) => {
  if (!tab?.active) return;
  if (!changeInfo.url && !changeInfo.title && changeInfo.status !== "complete") return;
  publishActiveTab(tab);
});

chrome.windows.onFocusChanged.addListener((windowId) => {
  if (windowId === chrome.windows.WINDOW_ID_NONE) return;
  chrome.tabs.query({ active: true, windowId }).then((tabs) => {
    publishActiveTab(tabs?.[0]);
  }).catch(rememberError);
});

async function openAssistant(tab) {
  if (tab?.id && chrome.sidePanel?.open) {
    try {
      await chrome.sidePanel.open({ tabId: tab.id });
      return;
    } catch (error) {
      rememberError(error);
    }
  }

  try {
    await chrome.tabs.create({ url: chrome.runtime.getURL("src/sidepanel.html") });
  } catch (error) {
    rememberError(error);
  }
}

chrome.action.onClicked.addListener((tab) => {
  const context = tabContext(tab);
  const payload = context
    ? { [ACTIVE_TAB_CONTEXT_KEY]: context, lastExtensionError: null }
    : { lastExtensionError: null };
  chrome.storage.local.set(payload).catch(rememberError);
  openAssistant(tab);
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (!info.menuItemId || !String(info.menuItemId).startsWith(MENU_PREFIX)) return;
  const action = String(info.menuItemId).slice(MENU_PREFIX.length);
  const selectedText = (info.selectionText || "").trim();
  if (!selectedText) return;

  const pendingAsk = {
    id: crypto.randomUUID(),
    action,
    selectedText,
    pageUrl: info.pageUrl || tab?.url || "",
    pageTitle: tab?.title || "",
    tabId: tab?.id ?? null,
    windowId: tab?.windowId ?? null,
    createdAt: Date.now(),
  };

  // Chrome requires sidePanel.open() to be directly tied to the user's click.
  // Start opening the panel before any awaited storage work, then the panel
  // either reads the pending ask on load or receives the storage change event.
  openAssistant(tab);
  chrome.storage.local.set({
    [ACTIVE_TAB_CONTEXT_KEY]: tabContext(tab),
    pendingAsk,
    lastExtensionError: null,
  }).catch(rememberError);
});

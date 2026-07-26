export const ACTIONS = {
  explain: {
    label: "解释",
    menuTitle: "解释选中内容",
    title: "解释选中内容",
  },
  review: {
    label: "审查",
    menuTitle: "审查这段代码",
    title: "代码审查",
  },
  rewrite: {
    label: "改写",
    menuTitle: "改写选中内容",
    title: "改写选中内容",
  },
  test: {
    label: "测试",
    menuTitle: "生成测试",
    title: "生成测试",
  },
  custom: {
    label: "自定义",
    menuTitle: "自定义提问",
    title: "自定义提问",
  },
  page: {
    label: "当前页",
    menuTitle: "询问当前页面",
    title: "当前页面",
  },
};

export const DEFAULT_SETTINGS = {
  serviceUrl: "http://127.0.0.1:8765",
  token: "",
  assistantMode: "chat",
  cwd: "",
  model: "",
  permissionMode: "default",
};

export function normalizeServiceUrl(url) {
  return String(url || DEFAULT_SETTINGS.serviceUrl).trim().replace(/\/+$/, "");
}

export function assertLocalServiceUrl(url) {
  const parsed = new URL(normalizeServiceUrl(url));
  if (!["http:", "https:"].includes(parsed.protocol)) {
    throw new Error("服务地址必须是 http/https");
  }
  if (!["127.0.0.1", "localhost", "::1"].includes(parsed.hostname)) {
    throw new Error("服务地址只能指向本机 localhost/127.0.0.1");
  }
  return parsed.toString().replace(/\/+$/, "");
}

export async function loadSettings() {
  const stored = await chrome.storage.sync.get(DEFAULT_SETTINGS);
  return {
    ...DEFAULT_SETTINGS,
    ...stored,
    serviceUrl: normalizeServiceUrl(stored.serviceUrl),
  };
}

export function actionTitle(action) {
  return (ACTIONS[action] || ACTIONS.custom).title;
}

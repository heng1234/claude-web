import { DEFAULT_SETTINGS, assertLocalServiceUrl, loadSettings } from "./templates.js";

const $ = (id) => document.getElementById(id);
const fields = {
  serviceUrl: $("serviceUrl"),
  token: $("token"),
  assistantMode: $("assistantMode"),
  cwd: $("cwd"),
  model: $("model"),
  permissionMode: $("permissionMode"),
};
const status = $("status");
const connectionBadge = $("connectionBadge");
const cwdField = $("cwdField");

function setStatus(text) {
  status.textContent = text;
}

function setConnectionBadge(text, state = "") {
  connectionBadge.textContent = text;
  connectionBadge.classList.toggle("ok", state === "ok");
  connectionBadge.classList.toggle("bad", state === "bad");
}

function updateModeFields() {
  const codeMode = fields.assistantMode.value === "code";
  cwdField.classList.toggle("mode-field-hidden", !codeMode);
  fields.cwd.required = codeMode;
  fields.cwd.setAttribute("aria-required", codeMode ? "true" : "false");
}

async function load() {
  const settings = await loadSettings();
  for (const key of Object.keys(DEFAULT_SETTINGS)) {
    fields[key].value = settings[key] || "";
  }
  updateModeFields();
  setStatus("已加载");
  testConnection({ silent: true });
}

async function save() {
  const next = {};
  for (const key of Object.keys(DEFAULT_SETTINGS)) {
    next[key] = fields[key].value.trim();
  }
  try {
    next.serviceUrl = assertLocalServiceUrl(next.serviceUrl);
  } catch (error) {
    setStatus(error.message || String(error));
    return;
  }
  if (next.assistantMode === "code" && !next.cwd) {
    setStatus("Code 模式需要填写项目目录");
    fields.cwd.focus();
    return;
  }
  await chrome.storage.sync.set(next);
  setStatus("已保存");
  testConnection({ silent: true });
}

async function testConnection(options = {}) {
  let serviceUrl = "";
  try {
    serviceUrl = assertLocalServiceUrl(fields.serviceUrl.value);
  } catch (error) {
    setStatus(error.message || String(error));
    setConnectionBadge("地址错误", "bad");
    return;
  }
  if (!options.silent) setStatus("测试中...");
  setConnectionBadge("测试中");
  try {
    const resp = await fetch(`${serviceUrl}/api/extension/status`);
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
    if (data.token_configured) {
      setStatus("连接成功，Token 已启用");
      setConnectionBadge("可用", "ok");
    } else {
      setStatus("连接成功，但服务端还未生成 Token");
      setConnectionBadge("待 Token");
    }
  } catch (error) {
    setStatus(`连接失败：${error.message || error}`);
    setConnectionBadge("失败", "bad");
  }
}

$("saveBtn").addEventListener("click", save);
$("testBtn").addEventListener("click", () => testConnection());
fields.assistantMode.addEventListener("change", updateModeFields);
for (const input of Object.values(fields)) {
  input.addEventListener("input", () => {
    setStatus("有未保存修改");
    setConnectionBadge("未保存");
  });
}

load();

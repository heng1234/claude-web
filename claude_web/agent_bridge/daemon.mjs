#!/usr/bin/env node

import { createInterface } from 'node:readline';
import { randomUUID } from 'node:crypto';
import { existsSync, readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, isAbsolute, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createPreToolUseHook } from './permission-policy.mjs';

const BRIDGE_DIR = dirname(fileURLToPath(import.meta.url));
const PACKAGE_NAME = '@anthropic-ai/claude-agent-sdk';
const BRIDGE_PACKAGE = JSON.parse(readFileSync(join(BRIDGE_DIR, 'package.json'), 'utf8'));
const EXPECTED_SDK_VERSION = BRIDGE_PACKAGE.dependencies?.[PACKAGE_NAME];
const ALLOW_UNSUPPORTED_SDK = process.env.CLAUDE_WEB_ALLOW_UNSUPPORTED_SDK === '1';
const IDLE_RUNTIME_MS = 30 * 60 * 1000;
const MAX_RUNTIMES = 8;
const BRIDGE_PROTOCOL_VERSION = 2;
const DEFAULT_MAX_FRAME_SIZE = 64 * 1024 * 1024;
const configuredMaxFrameSize = Number.parseInt(process.env.CLAUDE_WEB_AGENT_BRIDGE_MAX_FRAME_SIZE || '', 10);
const MAX_FRAME_SIZE = Number.isFinite(configuredMaxFrameSize)
  ? Math.max(1024 * 1024, Math.min(configuredMaxFrameSize, 256 * 1024 * 1024))
  : DEFAULT_MAX_FRAME_SIZE;

const runtimes = new Map();
const permissionWaiters = new Map();
let sdk = null;
let sdkInfo = null;
let shuttingDown = false;
let outputTail = Promise.resolve();
let runtimeMutationTail = Promise.resolve();

async function withRuntimeMutation(action) {
  const previous = runtimeMutationTail;
  let release;
  runtimeMutationTail = new Promise((resolveRelease) => { release = resolveRelease; });
  await previous;
  try {
    return await action();
  } finally {
    release();
  }
}

function write(payload) {
  const body = Buffer.from(JSON.stringify(payload, (_key, value) =>
    typeof value === 'bigint' ? Number(value) : value), 'utf8');
  if (body.length <= 0 || body.length > MAX_FRAME_SIZE) {
    return Promise.reject(new Error(
      `Claude Agent SDK bridge frame size ${body.length} exceeds the configured limit ${MAX_FRAME_SIZE}`
    ));
  }
  const header = Buffer.allocUnsafe(4);
  header.writeUInt32BE(body.length, 0);
  const frame = Buffer.concat([header, body]);
  const pending = outputTail.then(() => new Promise((resolveWrite, rejectWrite) => {
    process.stdout.write(frame, (error) => error ? rejectWrite(error) : resolveWrite());
  }));
  outputTail = pending.catch(() => {});
  return pending;
}

function log(...parts) {
  process.stderr.write(`[claude-agent-bridge] ${parts.map(String).join(' ')}\n`);
}

function errorText(error) {
  return error instanceof Error ? (error.stack || error.message) : String(error || 'Unknown error');
}

function packageDir(root) {
  return join(root, 'node_modules', '@anthropic-ai', 'claude-agent-sdk');
}

function managedSelectedVersion(root) {
  try {
    const metadata = JSON.parse(readFileSync(join(root, '.claude-web-sdk.json'), 'utf8'));
    if (metadata?.package !== PACKAGE_NAME) return null;
    const version = String(metadata?.version || '').trim();
    return /^\d+\.\d+\.\d+$/.test(version)
      ? version
      : null;
  } catch {
    return null;
  }
}

function packageEntry(candidate) {
  let location = resolve(candidate);
  if (!existsSync(location)) return null;
  if (/\.(?:mjs|cjs|js)$/.test(location)) {
    return { entry: location, packageDir: dirname(location), version: null };
  }
  const packageJsonPath = join(location, 'package.json');
  if (!existsSync(packageJsonPath)) return null;
  try {
    const pkg = JSON.parse(readFileSync(packageJsonPath, 'utf8'));
    const rootExport = pkg.exports?.['.'] ?? pkg.exports;
    const target = typeof rootExport === 'string'
      ? rootExport
      : rootExport?.import || rootExport?.default || pkg.module || pkg.main || 'sdk.mjs';
    const entry = resolve(location, target);
    if (!existsSync(entry)) return null;
    return { entry, packageDir: location, version: pkg.version || null };
  } catch {
    const entry = join(location, 'sdk.mjs');
    return existsSync(entry) ? { entry, packageDir: location, version: null } : null;
  }
}

function sdkCandidates() {
  const configured = (process.env.CLAUDE_AGENT_SDK_PATH || '').trim();
  const managedRoot = (process.env.CLAUDE_WEB_AGENT_SDK_HOME || '').trim()
    || join(homedir(), '.claude-web', 'dependencies', 'claude-sdk');
  const candidates = [];
  if (configured) {
    candidates.push({ path: configured, source: 'environment_override', selectedVersion: null });
    candidates.push({ path: packageDir(configured), source: 'environment_override', selectedVersion: null });
  }
  candidates.push({
    path: join(managedRoot, 'node_modules', '@anthropic-ai', 'claude-agent-sdk'),
    source: 'managed',
    selectedVersion: managedSelectedVersion(managedRoot),
  });
  candidates.push({
    path: join(BRIDGE_DIR, 'node_modules', '@anthropic-ai', 'claude-agent-sdk'),
    source: 'bundled',
    selectedVersion: null,
  });
  candidates.push({
    path: join(homedir(), '.codemoss', 'dependencies', 'claude-sdk', 'node_modules', '@anthropic-ai', 'claude-agent-sdk'),
    source: 'migration',
    selectedVersion: null,
  });
  const unique = new Map();
  for (const candidate of candidates) {
    const path = isAbsolute(candidate.path) ? candidate.path : resolve(candidate.path);
    const previous = unique.get(path);
    if (!previous || candidate.selectedVersion) unique.set(path, { ...candidate, path });
  }
  return [...unique.values()];
}

async function loadSdk() {
  if (sdk) return sdk;
  const rejectedVersions = [];
  for (const candidate of sdkCandidates()) {
    const found = packageEntry(candidate.path);
    if (!found) continue;
    const recommended = found.version === EXPECTED_SDK_VERSION;
    const approvedSelection = !!candidate.selectedVersion && found.version === candidate.selectedVersion;
    if (!ALLOW_UNSUPPORTED_SDK && !recommended && !approvedSelection) {
      rejectedVersions.push(`${found.packageDir} (${found.version || 'unknown'})`);
      log(`skipping unsupported SDK ${found.version || 'unknown'} at ${found.packageDir}; expected ${EXPECTED_SDK_VERSION}`);
      continue;
    }
    try {
      const loaded = await import(pathToFileURL(found.entry).href);
      if (typeof loaded.query !== 'function') {
        throw new Error(`query export missing from ${found.entry}`);
      }
      sdk = loaded;
      sdkInfo = {
        path: found.packageDir,
        version: found.version,
        expectedVersion: EXPECTED_SDK_VERSION,
        compatible: recommended || approvedSelection,
        recommended,
        selected: approvedSelection,
        source: candidate.source,
      };
      return sdk;
    } catch (error) {
      log(`failed to load SDK from ${found.entry}:`, errorText(error));
    }
  }
  throw new Error(
    `Claude Agent SDK ${EXPECTED_SDK_VERSION} is not installed. Use claude-web Settings to install it.` +
    (rejectedVersions.length ? ` Unsupported installs: ${rejectedVersions.join(', ')}` : '')
  );
}

function createInputQueue() {
  const values = [];
  const waiters = [];
  let closed = false;

  return {
    push(value) {
      if (closed) throw new Error('runtime input is closed');
      const waiter = waiters.shift();
      if (waiter) waiter({ value, done: false });
      else values.push(value);
    },
    close() {
      if (closed) return;
      closed = true;
      while (waiters.length) waiters.shift()({ value: undefined, done: true });
    },
    async next() {
      if (values.length) return { value: values.shift(), done: false };
      if (closed) return { value: undefined, done: true };
      return new Promise((resolveNext) => waiters.push(resolveNext));
    },
    [Symbol.asyncIterator]() { return this; },
  };
}

function normalizePermissionMode(value) {
  const mode = String(value || '').trim();
  if (mode === 'auto' || mode === 'free') return 'bypassPermissions';
  if (['default', 'acceptEdits', 'bypassPermissions', 'plan', 'dontAsk', 'delegate'].includes(mode)) {
    return mode;
  }
  return 'default';
}

function stringList(value) {
  if (!Array.isArray(value)) return undefined;
  const result = [...new Set(value.map(String).map((item) => item.trim()).filter(Boolean))];
  return result.length ? result : undefined;
}

function runtimeSignature(params) {
  const permissionMode = normalizePermissionMode(params.permissionMode);
  const modelContextVariant = String(params.model || '').match(/\[[0-9.]+\s*[kKmM]\]$/)?.[0]?.toLowerCase() || '';
  return JSON.stringify({
    cwd: resolve(params.cwd || process.cwd()),
    modelContextVariant,
    effort: params.effort || '',
    bypassPermissions: permissionMode === 'bypassPermissions',
    allowedTools: stringList(params.allowedTools) || [],
    disallowedTools: stringList(params.disallowedTools) || [],
    systemPromptAppend: params.systemPromptAppend || '',
    runtimeEpoch: params.runtimeEpoch || '',
    resumeSessionAt: params.resumeSessionAt || '',
  });
}

function permissionRequest(runtime, toolName, input, options = {}) {
  if (!runtime.activeRequestId) {
    return Promise.resolve({ behavior: 'deny', message: 'No active browser turn owns this permission request' });
  }
  const approvalId = randomUUID();
  return new Promise((resolvePermission) => {
    const finish = (result) => {
      const waiter = permissionWaiters.get(approvalId);
      if (!waiter) return;
      permissionWaiters.delete(approvalId);
      runtime.pendingApprovals.delete(approvalId);
      if (waiter.timer) clearTimeout(waiter.timer);
      if (waiter.signal && waiter.abortHandler) waiter.signal.removeEventListener('abort', waiter.abortHandler);
      if (toolName === 'ExitPlanMode' && result?.behavior === 'allow') {
        runtime.permissionModeState.value = result.updatedInput?.targetMode || 'default';
      }
      resolvePermission(result);
    };
    const abortHandler = () => finish({ behavior: 'deny', message: 'Permission request was interrupted', interrupt: true });
    const timer = setTimeout(() => {
      finish({ behavior: 'deny', message: 'Permission request timed out' });
    }, 30 * 60 * 1000);
    permissionWaiters.set(approvalId, {
      approvalId,
      runtime,
      sessionKey: runtime.key,
      toolName,
      input,
      suggestions: Array.isArray(options.suggestions) ? options.suggestions : [],
      toolUseId: options.toolUseID || null,
      agentId: options.agentID || null,
      blockedPath: options.blockedPath || null,
      decisionReason: options.decisionReason || null,
      title: options.title || null,
      displayName: options.displayName || null,
      description: options.description || null,
      signal: options.signal,
      abortHandler,
      timer,
      finish,
    });
    runtime.pendingApprovals.add(approvalId);
    if (options.signal?.aborted) {
      abortHandler();
      return;
    }
    if (options.signal) options.signal.addEventListener('abort', abortHandler, { once: true });
    write({
      id: runtime.activeRequestId,
      type: 'permission_request',
      approvalId,
      sessionKey: runtime.key,
      toolName,
      input,
      suggestions: Array.isArray(options.suggestions) ? options.suggestions : [],
      toolUseId: options.toolUseID || null,
      agentId: options.agentID || null,
      blockedPath: options.blockedPath || null,
      decisionReason: options.decisionReason || null,
      title: options.title || null,
      displayName: options.displayName || null,
      description: options.description || null,
    }).catch(() => finish({ behavior: 'deny', message: 'Web approval channel closed', interrupt: true }));
  });
}

function cancelRuntimePermissions(runtime, message = 'Runtime closed') {
  for (const approvalId of [...(runtime?.pendingApprovals || [])]) {
    const waiter = permissionWaiters.get(approvalId);
    if (waiter) waiter.finish({ behavior: 'deny', message, interrupt: true });
  }
}

function buildOptions(params, abortController, runtime) {
  const permissionMode = normalizePermissionMode(params.permissionMode);
  const options = {
    cwd: resolve(params.cwd || process.cwd()),
    permissionMode,
    includePartialMessages: true,
    enableFileCheckpointing: true,
    persistSession: true,
    maxTurns: 100,
    tools: { type: 'preset', preset: 'claude_code' },
    settingSources: ['user', 'project', 'local'],
    systemPrompt: {
      type: 'preset',
      preset: 'claude_code',
      ...(params.systemPromptAppend ? { append: params.systemPromptAppend } : {}),
    },
    abortController,
    canUseTool: (toolName, input, options) => permissionRequest(runtime, toolName, input, options),
    hooks: {
      PreToolUse: [{
        hooks: [createPreToolUseHook(runtime.permissionModeState, resolve(params.cwd || process.cwd()))],
      }],
    },
    ...(permissionMode === 'bypassPermissions' ? { allowDangerouslySkipPermissions: true } : {}),
  };
  if (params.model) options.model = params.model;
  if (['low', 'medium', 'high', 'xhigh', 'max'].includes(params.effort)) options.effort = params.effort;
  const allowedTools = stringList(params.allowedTools);
  const disallowedTools = stringList(params.disallowedTools);
  if (allowedTools) options.allowedTools = allowedTools;
  if (disallowedTools) options.disallowedTools = disallowedTools;
  if (params.resumeSessionId) options.resume = params.resumeSessionId;
  else if (params.sessionId) options.sessionId = params.sessionId;
  if (params.resumeSessionAt) options.resumeSessionAt = String(params.resumeSessionAt);
  return options;
}

function userMessage(params, runtime) {
  const content = Array.isArray(params.content) && params.content.length
    ? params.content
    : [{ type: 'text', text: String(params.message || '').trim() || '[Empty message]' }];
  return {
    type: 'user',
    session_id: runtime.sessionId || params.resumeSessionId || params.sessionId || '',
    parent_tool_use_id: null,
    message: { role: 'user', content },
  };
}

function messageSessionId(message) {
  const value = message?.session_id || message?.sessionId;
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function isTurnResult(message) {
  return message?.type === 'result' && !message?.parent_tool_use_id;
}

async function readRuntime(runtime) {
  try {
    for await (const message of runtime.query) {
      runtime.lastUsed = Date.now();
      const discovered = messageSessionId(message);
      if (discovered) runtime.sessionId = discovered;
      const requestId = runtime.activeRequestId;
      if (!requestId) continue;
      await write({ id: requestId, type: 'event', event: message });
      if (isTurnResult(message)) {
        await write({ id: requestId, type: 'done', success: !message.is_error, sessionId: runtime.sessionId });
        runtime.activeRequestId = null;
      }
    }
    if (runtime.activeRequestId) {
      await write({ id: runtime.activeRequestId, type: 'error', message: 'Claude Agent SDK runtime ended unexpectedly' });
      await write({ id: runtime.activeRequestId, type: 'done', success: false, sessionId: runtime.sessionId });
      runtime.activeRequestId = null;
    }
  } catch (error) {
    const requestId = runtime.activeRequestId;
    if (requestId) {
      await write({ id: requestId, type: 'error', message: errorText(error) });
      await write({ id: requestId, type: 'done', success: false, sessionId: runtime.sessionId });
      runtime.activeRequestId = null;
    }
    log(`runtime ${runtime.key} failed:`, errorText(error));
  } finally {
    cancelRuntimePermissions(runtime, 'Claude Agent SDK runtime ended');
    if (runtimes.get(runtime.key) === runtime) runtimes.delete(runtime.key);
    runtime.input.close();
  }
}

async function disposeRuntime(runtime) {
  if (!runtime || runtime.disposed) return;
  runtime.disposed = true;
  cancelRuntimePermissions(runtime);
  runtime.input.close();
  try {
    if (typeof runtime.query?.close === 'function') runtime.query.close();
    else runtime.abortController.abort();
  } catch (error) {
    log(`runtime ${runtime.key} close failed:`, errorText(error));
  }
  if (runtimes.get(runtime.key) === runtime) runtimes.delete(runtime.key);
}

async function enforceRuntimeLimit(exceptKey) {
  const idle = [...runtimes.values()]
    .filter((runtime) => runtime.key !== exceptKey && !runtime.activeRequestId && !runtime.controlActive)
    .sort((left, right) => left.lastUsed - right.lastUsed);
  while (runtimes.size >= MAX_RUNTIMES && idle.length) {
    await disposeRuntime(idle.shift());
  }
  if (runtimes.size >= MAX_RUNTIMES && !runtimes.has(exceptKey)) {
    throw new Error(`Claude Agent SDK runtime limit reached (${MAX_RUNTIMES}); stop an active Code session and retry`);
  }
}

async function createRuntime(key, params, signature) {
  await enforceRuntimeLimit(key);
  const loaded = await loadSdk();
  const input = createInputQueue();
  const abortController = new AbortController();
  const runtime = {
    key,
    signature,
    input,
    abortController,
    sessionId: params.resumeSessionId || params.sessionId || null,
    initialSessionId: params.resumeSessionId || params.sessionId || null,
    activeRequestId: null,
    controlActive: false,
    lastUsed: Date.now(),
    disposed: false,
    runtimeEpoch: params.runtimeEpoch || null,
    permissionModeState: { value: normalizePermissionMode(params.permissionMode) },
    currentPermissionMode: normalizePermissionMode(params.permissionMode),
    currentModel: params.model || null,
    pendingApprovals: new Set(),
    query: null,
  };
  const options = buildOptions(params, abortController, runtime);
  const query = loaded.query({ prompt: input, options });
  runtime.query = query;
  runtimes.set(key, runtime);
  runtime.reader = readRuntime(runtime);
  return runtime;
}

function assertRuntimeEpoch(runtime, params) {
  const requested = String(params?.runtimeEpoch || '').trim();
  const owned = String(runtime?.runtimeEpoch || '').trim();
  if (requested && owned && requested !== owned) {
    throw new Error('Claude Agent SDK runtime epoch mismatch');
  }
}

async function applyDynamicControls(runtime, params) {
  assertRuntimeEpoch(runtime, params);
  const targetPermissionMode = normalizePermissionMode(params.permissionMode);
  if (runtime.currentPermissionMode !== targetPermissionMode) {
    const bypassChanged = (runtime.currentPermissionMode === 'bypassPermissions')
      !== (targetPermissionMode === 'bypassPermissions');
    if (bypassChanged) {
      throw new Error('Changing bypassPermissions requires an idle runtime rebuild');
    }
    if (typeof runtime.query?.setPermissionMode !== 'function') {
      throw new Error('SDK setPermissionMode is unavailable');
    }
    await runtime.query.setPermissionMode(targetPermissionMode);
    runtime.currentPermissionMode = targetPermissionMode;
    runtime.permissionModeState.value = targetPermissionMode;
  }
  const targetModel = params.model || null;
  if (runtime.currentModel !== targetModel) {
    if (typeof runtime.query?.setModel !== 'function') throw new Error('SDK setModel is unavailable');
    await runtime.query.setModel(targetModel || undefined);
    runtime.currentModel = targetModel;
  }
}

async function runtimeForSendLocked(key, params) {
  const signature = runtimeSignature(params);
  let runtime = runtimes.get(key);
  const requestedSessionId = params.resumeSessionId || params.sessionId || null;
  const sameConversation = !runtime || !requestedSessionId
    || requestedSessionId === runtime.sessionId
    || requestedSessionId === runtime.initialSessionId;
  const configChanged = !!runtime && runtime.signature !== signature;
  if (runtime && (configChanged || !sameConversation)) {
    if (runtime.activeRequestId || runtime.controlActive) {
      throw new Error('Cannot change Code runtime settings while the runtime is active');
    }
    // Settings changes should resume the same native conversation. A different
    // requested session id (force-new/clear/compact) intentionally detaches it.
    const resumeSessionId = sameConversation ? (runtime.sessionId || params.resumeSessionId) : null;
    await disposeRuntime(runtime);
    if (resumeSessionId) params = { ...params, resumeSessionId, sessionId: undefined };
    runtime = await createRuntime(key, params, runtimeSignature(params));
  }
  if (!runtime) runtime = await createRuntime(key, params, signature);
  return runtime;
}

async function runtimeForSend(key, params) {
  return withRuntimeMutation(() => runtimeForSendLocked(key, params));
}

function preflightSend(command) {
  const params = command.params || {};
  const key = String(params.sessionKey || '').trim();
  if (!key) throw new Error('sessionKey is required');
  const runtime = runtimes.get(key);
  if (runtime?.activeRequestId || runtime?.controlActive) {
    throw new Error('A Code turn is already running for this session');
  }
  if (!runtime && runtimes.size >= MAX_RUNTIMES) {
    const hasDisposableRuntime = [...runtimes.values()].some(
      (candidate) => !candidate.activeRequestId && !candidate.controlActive
    );
    if (!hasDisposableRuntime) {
      throw new Error(`Claude Agent SDK runtime limit reached (${MAX_RUNTIMES}); stop an active Code session and retry`);
    }
  }
  return { key, params };
}

async function handleSend(command) {
  const params = command.params || {};
  const key = String(params.sessionKey || '').trim();
  if (!key) throw new Error('sessionKey is required');
  await withRuntimeMutation(async () => {
    const runtime = await runtimeForSendLocked(key, params);
    if (runtime.activeRequestId || runtime.controlActive) {
      throw new Error('A Code turn is already running for this session');
    }
    await applyDynamicControls(runtime, params);
    runtime.activeRequestId = command.id;
    runtime.lastUsed = Date.now();
    try {
      runtime.input.push(userMessage(params, runtime));
    } catch (error) {
      runtime.activeRequestId = null;
      throw error;
    }
  });
}

async function handleInterrupt(command) {
  const key = String(command.params?.sessionKey || '').trim();
  const runtime = runtimes.get(key);
  if (!runtime || !runtime.activeRequestId) throw new Error('No active Claude Agent SDK turn for this session');
  cancelRuntimePermissions(runtime, 'User interrupted the turn');
  if (typeof runtime.query?.interrupt !== 'function') throw new Error('SDK interrupt is unavailable');
  await runtime.query.interrupt();
  await write({ id: command.id, type: 'response', ok: true, sessionId: runtime.sessionId });
}

async function handleContext(command) {
  const params = command.params || {};
  const key = String(params.sessionKey || '').trim();
  if (!key) throw new Error('sessionKey is required');
  // Re-run the same signature check as a turn. This matters for context-window
  // changes (for example a local 200k/1M model selection), which are frozen
  // when the Claude subprocess is created.
  const runtime = await runtimeForSend(key, params);
  await applyDynamicControls(runtime, params);
  if (typeof runtime.query?.getContextUsage !== 'function') throw new Error('SDK context usage is unavailable');
  const usage = await runtime.query.getContextUsage();
  await write({ id: command.id, type: 'response', ok: true, usage, sessionId: runtime.sessionId });
}

async function handleReconnect(command) {
  const params = command.params || {};
  const key = String(params.sessionKey || '').trim();
  if (!key) throw new Error('sessionKey is required');
  const runtime = await withRuntimeMutation(async () => {
    const current = runtimes.get(key);
    if (current?.activeRequestId || current?.controlActive) {
      throw new Error('Cannot reconnect while the Code runtime is active');
    }
    if (current) await disposeRuntime(current);
    return createRuntime(key, params, runtimeSignature(params));
  });
  await write({
    id: command.id,
    type: 'response',
    ok: true,
    reconnected: true,
    sessionId: runtime.sessionId,
  });
}

async function handleSetModel(command) {
  const params = command.params || {};
  const key = String(params.sessionKey || '').trim();
  const runtime = runtimes.get(key);
  if (!runtime) {
    await write({ id: command.id, type: 'response', ok: true, applied: false, reason: 'runtime_not_loaded' });
    return;
  }
  assertRuntimeEpoch(runtime, params);
  if (typeof runtime.query?.setModel !== 'function') throw new Error('SDK setModel is unavailable');
  await runtime.query.setModel(params.model || undefined);
  runtime.currentModel = params.model || null;
  await write({ id: command.id, type: 'response', ok: true, applied: true, sessionId: runtime.sessionId });
}

async function handleSetPermissionMode(command) {
  const params = command.params || {};
  const key = String(params.sessionKey || '').trim();
  const runtime = runtimes.get(key);
  if (!runtime) {
    await write({ id: command.id, type: 'response', ok: true, applied: false, reason: 'runtime_not_loaded' });
    return;
  }
  assertRuntimeEpoch(runtime, params);
  const target = normalizePermissionMode(params.permissionMode);
  const bypassChanged = (runtime.currentPermissionMode === 'bypassPermissions')
    !== (target === 'bypassPermissions');
  if (bypassChanged) {
    if (runtime.activeRequestId || runtime.controlActive) {
      throw new Error('Cannot change bypassPermissions while the runtime is active');
    }
    await disposeRuntime(runtime);
    await write({ id: command.id, type: 'response', ok: true, applied: false, requiresRestart: true });
    return;
  }
  if (typeof runtime.query?.setPermissionMode !== 'function') throw new Error('SDK setPermissionMode is unavailable');
  await runtime.query.setPermissionMode(target);
  runtime.currentPermissionMode = target;
  runtime.permissionModeState.value = target;
  await write({ id: command.id, type: 'response', ok: true, applied: true, sessionId: runtime.sessionId });
}

function pendingPermissionPayload(waiter) {
  return {
    approvalId: waiter.approvalId,
    sessionKey: waiter.sessionKey,
    toolName: waiter.toolName,
    input: waiter.input,
    suggestions: waiter.suggestions,
    toolUseId: waiter.toolUseId,
    agentId: waiter.agentId,
    blockedPath: waiter.blockedPath,
    decisionReason: waiter.decisionReason,
    title: waiter.title,
    displayName: waiter.displayName,
    description: waiter.description,
  };
}

async function handlePendingPermissions(command) {
  const key = String(command.params?.sessionKey || '').trim();
  const pending = [...permissionWaiters.values()]
    .filter((waiter) => !key || waiter.sessionKey === key)
    .map(pendingPermissionPayload);
  await write({ id: command.id, type: 'response', ok: true, pending });
}

async function handleForkSession(command) {
  const params = command.params || {};
  const sourceSessionId = String(params.sourceSessionId || '').trim();
  if (!sourceSessionId) throw new Error('sourceSessionId is required');
  const loaded = await loadSdk();
  if (typeof loaded.forkSession !== 'function') throw new Error('SDK forkSession is unavailable');
  const options = {};
  if (params.cwd) options.dir = resolve(params.cwd);
  if (params.upToMessageId) options.upToMessageId = String(params.upToMessageId);
  if (params.title) options.title = String(params.title);
  const result = await loaded.forkSession(sourceSessionId, options);
  await write({ id: command.id, type: 'response', ok: true, ...result });
}

async function handleSessionMessages(command) {
  const params = command.params || {};
  const sessionId = String(params.sessionId || '').trim();
  if (!sessionId) throw new Error('sessionId is required');
  const loaded = await loadSdk();
  if (typeof loaded.getSessionMessages !== 'function') throw new Error('SDK getSessionMessages is unavailable');
  const options = {};
  if (params.cwd) options.dir = resolve(params.cwd);
  if (Number.isInteger(params.limit) && params.limit > 0) options.limit = params.limit;
  const messages = await loaded.getSessionMessages(sessionId, options);
  await write({ id: command.id, type: 'response', ok: true, messages });
}

async function handleRewindFiles(command) {
  const params = command.params || {};
  const key = String(params.sessionKey || '').trim();
  const userMessageId = String(params.userMessageId || '').trim();
  if (!key || !userMessageId) throw new Error('sessionKey and userMessageId are required');
  const runtime = await withRuntimeMutation(async () => {
    const candidate = await runtimeForSendLocked(key, params);
    if (candidate.activeRequestId || candidate.controlActive) {
      throw new Error('Cannot rewind files while the runtime is active');
    }
    candidate.controlActive = true;
    return candidate;
  });
  try {
    await applyDynamicControls(runtime, params);
    if (typeof runtime.query?.rewindFiles !== 'function') throw new Error('SDK rewindFiles is unavailable');
    const result = await runtime.query.rewindFiles(userMessageId, { dryRun: params.dryRun === true });
    await write({ id: command.id, type: 'response', ok: true, result, sessionId: runtime.sessionId });
  } finally {
    runtime.controlActive = false;
    runtime.lastUsed = Date.now();
  }
}

async function handlePermissionResponse(command) {
  const params = command.params || {};
  const approvalId = String(params.approvalId || '').trim();
  const sessionKey = String(params.sessionKey || '').trim();
  const waiter = permissionWaiters.get(approvalId);
  if (!waiter) throw new Error('Permission request is no longer pending');
  if (!sessionKey || waiter.sessionKey !== sessionKey) throw new Error('Permission request ownership mismatch');
  if (params.allow === true) {
    const result = {
      behavior: 'allow',
      updatedInput: params.updatedInput && typeof params.updatedInput === 'object'
        ? params.updatedInput
        : waiter.input,
    };
    if (params.useSuggestions === true && waiter.suggestions.length) {
      result.updatedPermissions = waiter.suggestions;
    }
    waiter.finish(result);
  } else {
    waiter.finish({
      behavior: 'deny',
      message: String(params.message || `User denied permission for ${waiter.toolName}`),
      interrupt: params.interrupt === true,
    });
  }
  await write({ id: command.id, type: 'response', ok: true, approvalId });
}

async function handleClose(command) {
  const key = String(command.params?.sessionKey || '').trim();
  const runtime = runtimes.get(key);
  if (runtime) await disposeRuntime(runtime);
  await write({ id: command.id, type: 'response', ok: true });
}

async function shutdown(command) {
  shuttingDown = true;
  await Promise.allSettled([...runtimes.values()].map(disposeRuntime));
  if (command?.id) await write({ id: command.id, type: 'response', ok: true });
  process.exit(0);
}

async function handle(command) {
  if (!command || typeof command !== 'object') throw new Error('Invalid bridge command');
  switch (command.method) {
    case 'send': return handleSend(command);
    case 'interrupt': return handleInterrupt(command);
    case 'context': return handleContext(command);
    case 'reconnect_session': return handleReconnect(command);
    case 'set_model': return handleSetModel(command);
    case 'set_permission_mode': return handleSetPermissionMode(command);
    case 'pending_permissions': return handlePendingPermissions(command);
    case 'fork_session': return handleForkSession(command);
    case 'session_messages': return handleSessionMessages(command);
    case 'rewind_files': return handleRewindFiles(command);
    case 'permission_response': return handlePermissionResponse(command);
    case 'close_session': return handleClose(command);
    case 'ping':
      await write({ id: command.id, type: 'response', ok: true, sdk: sdkInfo, runtimes: runtimes.size });
      return;
    case 'shutdown': return shutdown(command);
    default: throw new Error(`Unknown method: ${command.method}`);
  }
}

async function main() {
  await loadSdk();
  await write({ type: 'ready', sdk: sdkInfo, protocol: BRIDGE_PROTOCOL_VERSION });
  const reader = createInterface({ input: process.stdin, crlfDelay: Infinity });
  reader.on('line', (line) => {
    if (!line.trim() || shuttingDown) return;
    let command;
    try {
      command = JSON.parse(line);
    } catch (error) {
      void write({ type: 'error', message: `Invalid JSON: ${errorText(error)}` });
      return;
    }
    Promise.resolve((async () => {
      if (command.method === 'send') {
        const { params } = preflightSend(command);
        // Claim SDK ownership as soon as the daemon has accepted the turn into
        // its in-process dispatch queue.
        // Runtime creation and dynamic controls may be slow, but they must not
        // make Python treat the request as unacknowledged and risk replaying it.
        await write({
          id: command.id,
          type: 'accepted',
          phase: 'queued',
          sessionId: params.resumeSessionId || params.sessionId || null,
        });
      }
      await handle(command);
    })()).catch(async (error) => {
      await write({ id: command.id, type: 'error', message: errorText(error) });
      if (command.method === 'send') await write({ id: command.id, type: 'done', success: false });
    });
  });
  reader.on('close', () => shutdown(null));
  setInterval(() => {
    const cutoff = Date.now() - IDLE_RUNTIME_MS;
    for (const runtime of runtimes.values()) {
      if (!runtime.activeRequestId && !runtime.controlActive && runtime.lastUsed < cutoff) disposeRuntime(runtime);
    }
  }, 60_000).unref();
}

process.on('SIGTERM', () => shutdown(null));
process.on('SIGINT', () => shutdown(null));
main().catch(async (error) => {
  await write({ type: 'fatal', message: errorText(error) });
  process.exit(1);
});

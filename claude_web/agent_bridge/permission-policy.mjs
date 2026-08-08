// Adapted from jetbrains-cc-gui's Claude permission-mode policy.  Keep the
// policy in the SDK process so project/local settings cannot silently bypass
// an explicit Web approval for tools with side effects.

const SAFE_ALWAYS_ALLOW_TOOLS = new Set([
  'ToolSearch',
  'Glob',
  'Grep',
  'Read',
  'NotebookRead',
  'BashOutput',
  'LSP',
  'ListMcpResourcesTool',
  'ReadMcpResourceTool',
  'TodoWrite',
  'TaskCreate',
  'TaskGet',
  'TaskUpdate',
  'TaskList',
  'TaskStop',
  'TaskOutput',
  'AskUserQuestion',
  'EnterPlanMode',
  'ExitPlanMode',
  'SendMessage',
  'Sleep',
]);

const EXECUTION_TOOLS = new Set(['Bash', 'Agent', 'Task']);
const PLAN_MODE_ALLOWED_TOOLS = new Set([
  'WebFetch',
  'WebSearch',
  'ListMcpResources',
  'ListMcpResourcesTool',
  'ReadMcpResource',
  'ReadMcpResourceTool',
  'mcp__ace-tool__search_context',
  'mcp__context7__resolve-library-id',
  'mcp__context7__query-docs',
  'mcp__conductor__GetWorkspaceDiff',
  'mcp__conductor__GetTerminalOutput',
  'mcp__conductor__AskUserQuestion',
  'mcp__conductor__DiffComment',
  'mcp__time__get_current_time',
  'mcp__time__convert_time',
]);
const EDIT_TOOLS = new Set(['Edit', 'Write', 'MultiEdit', 'NotebookEdit']);
const READ_ONLY_MCP_ACTION = /^(read|list|get|search|query|fetch|find|view|describe|show|resolve|lookup|status|info|inspect|count|exists|preview|ls|cat|head|tail)([_-]|$)/i;
const YIELD_TO_SDK = Object.freeze({ continue: true });

function isReadOnlyMcpTool(toolName) {
  if (typeof toolName !== 'string' || !toolName.startsWith('mcp__')) return false;
  const action = toolName.split('__').slice(2).join('__');
  return action.length > 0 && READ_ONLY_MCP_ACTION.test(action);
}

function planFilePaths(toolName, toolInput) {
  if (!toolInput || typeof toolInput !== 'object') return [];
  if (toolName === 'MultiEdit' && Array.isArray(toolInput.edits)) {
    return toolInput.edits.map((edit) => edit?.file_path || edit?.path).filter(Boolean);
  }
  const value = toolInput.file_path || toolInput.path || toolInput.notebook_path;
  return value ? [value] : [];
}

function isRootPlanFile(filePath, cwd) {
  if (!filePath || typeof filePath !== 'string') return false;
  const normalizedPath = filePath.replace(/\\/g, '/');
  const normalizedCwd = String(cwd || process.cwd()).replace(/\\/g, '/').replace(/\/$/, '');
  const name = normalizedPath.split('/').pop() || '';
  if (name.toLowerCase() !== 'plan.md') return false;
  if (!normalizedPath.includes('/')) return true;
  return normalizedPath === `${normalizedCwd}/${name}`;
}

function decision(permissionDecision, permissionDecisionReason, updatedInput) {
  return {
    hookSpecificOutput: {
      hookEventName: 'PreToolUse',
      permissionDecision,
      ...(permissionDecisionReason ? { permissionDecisionReason } : {}),
      ...(updatedInput ? { updatedInput } : {}),
    },
  };
}

export function createPreToolUseHook(permissionModeState, cwd) {
  return async (hookInput) => {
    const toolName = hookInput?.tool_name;
    const toolInput = hookInput?.tool_input || {};
    let mode = permissionModeState?.value || 'default';

    if (toolName === 'EnterPlanMode') {
      if (permissionModeState) permissionModeState.value = 'plan';
      return decision('allow');
    }

    // Bypass mode auto-approves everything. The SDK's allowDangerouslySkipPermissions
    // usually prevents canUseTool from firing at all, but subagent/Task paths can slip
    // through — returning allow here keeps the UI from showing a permission card.
    if (mode === 'bypassPermissions') {
      return decision('allow');
    }

    if (mode === 'plan') {
      if (toolName === 'ExitPlanMode') {
        return decision('ask', 'Plan mode: leaving the plan requires explicit Web approval.');
      }
      if (SAFE_ALWAYS_ALLOW_TOOLS.has(toolName)) return YIELD_TO_SDK;
      if (toolName === 'Agent' || toolName === 'Task') return decision('allow');
      if (EDIT_TOOLS.has(toolName)) {
        const paths = planFilePaths(toolName, toolInput);
        if (paths.length && paths.every((path) => isRootPlanFile(path, cwd))) return decision('allow');
        return decision('ask', 'Plan mode: editing files other than the workspace PLAN.md requires explicit Web approval.');
      }
      if (toolName === 'Bash') {
        return decision('ask', 'Plan mode: command execution requires explicit Web approval.');
      }
      if (PLAN_MODE_ALLOWED_TOOLS.has(toolName) || isReadOnlyMcpTool(toolName)) return YIELD_TO_SDK;
      return decision('deny', `Tool "${toolName || 'unknown'}" is not allowed in plan mode.`);
    }

    if (mode === 'default') {
      if (SAFE_ALWAYS_ALLOW_TOOLS.has(toolName) || isReadOnlyMcpTool(toolName)) return YIELD_TO_SDK;
      return decision(
        'ask',
        'Default mode: project settings cannot auto-approve tools with side effects; explicit Web confirmation is required.',
      );
    }

    if (mode === 'acceptEdits' && EXECUTION_TOOLS.has(toolName)) {
      return decision('ask', 'Accept-edits mode: command and agent execution still require explicit Web approval.');
    }

    return YIELD_TO_SDK;
  };
}

export const permissionPolicyInternals = {
  SAFE_ALWAYS_ALLOW_TOOLS,
  EXECUTION_TOOLS,
  PLAN_MODE_ALLOWED_TOOLS,
  isReadOnlyMcpTool,
  isRootPlanFile,
};

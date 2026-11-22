import json
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node.js is required for permission policy tests")
class PermissionPolicyTest(unittest.TestCase):
    def test_readonly_work_modes_deny_local_mutation_even_in_bypass_mode(self):
        module_uri = (Path(__file__).parents[1] / "claude_web" / "agent_bridge" / "permission-policy.mjs").resolve().as_uri()
        script = f"""
          import {{ createPreToolUseHook }} from {json.dumps(module_uri)};
          const readOnly = createPreToolUseHook({{value:'bypassPermissions'}}, '/workspace', true);
          const browser = createPreToolUseHook({{value:'default'}}, '/workspace', true, true);
          const result = {{}};
          for (const name of ['Bash','Write','Edit','Agent','Task','mcp__github__create_issue']) {{
            result[name] = await readOnly({{tool_name:name,tool_input:{{}}}});
          }}
          result.read = await readOnly({{tool_name:'Read',tool_input:{{}}}});
          result.browser = await browser({{tool_name:'mcp__playwright__browser_click',tool_input:{{}}}});
          process.stdout.write(JSON.stringify(result));
        """
        result = subprocess.run([shutil.which('node'),'--input-type=module','--eval',script],check=True,capture_output=True,text=True)
        payload=json.loads(result.stdout)
        for name in ['Bash','Write','Edit','Agent','Task','mcp__github__create_issue']:
            self.assertEqual('deny',payload[name]['hookSpecificOutput']['permissionDecision'])
        self.assertNotEqual('deny',payload['browser']['hookSpecificOutput']['permissionDecision'])

    def test_permission_hook_requires_explicit_side_effect_approval(self):
        module_uri = (Path(__file__).parents[1] / "claude_web" / "agent_bridge" / "permission-policy.mjs").resolve().as_uri()
        script = f"""
          import {{ createPreToolUseHook }} from {json.dumps(module_uri)};
          const defaults = {{value: 'default'}};
          const defaultHook = createPreToolUseHook(defaults, '/workspace');
          const bash = await defaultHook({{tool_name: 'Bash', tool_input: {{command: 'pwd'}}}});
          const read = await defaultHook({{tool_name: 'Read', tool_input: {{file_path: 'README.md'}}}});
          const plan = {{value: 'plan'}};
          const planHook = createPreToolUseHook(plan, '/workspace');
          const planWrite = await planHook({{tool_name: 'Write', tool_input: {{file_path: '/workspace/app.py'}}}});
          const planFile = await planHook({{tool_name: 'Write', tool_input: {{file_path: '/workspace/PLAN.md'}}}});
          process.stdout.write(JSON.stringify({{bash, read, planWrite, planFile}}));
        """
        result = subprocess.run(
            [shutil.which("node"), "--input-type=module", "--eval", script],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual("ask", payload["bash"]["hookSpecificOutput"]["permissionDecision"])
        self.assertTrue(payload["read"]["continue"])
        self.assertEqual("ask", payload["planWrite"]["hookSpecificOutput"]["permissionDecision"])
        self.assertEqual("allow", payload["planFile"]["hookSpecificOutput"]["permissionDecision"])


if __name__ == "__main__":
    unittest.main()

import asyncio
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from claude_web.workbench import Workbench, scope_key


class WorkbenchStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Workbench(self.root / 'workbench')
        self.plugin = self.root / 'demo'
        (self.plugin / 'skills/demo/scripts').mkdir(parents=True)
        (self.plugin / 'skills/demo/SKILL.md').write_text('---\nname: demo\ndescription: Test\n---\nRead scripts/run.py.')
        (self.plugin / 'skills/demo/scripts/run.py').write_text('print("hello")')
        (self.plugin / 'commands').mkdir()
        (self.plugin / 'commands/review.md').write_text('Review {{input}}')
        (self.plugin / 'hooks').mkdir()
        (self.plugin / 'hooks/hooks.json').write_text('{"hooks": {"PreToolUse": []}}')
        (self.plugin / '.mcp.json').write_text('{"mcpServers": {"demo": {"command": "echo", "args": ["test"]}}}')
        (self.plugin / 'ccw-plugin.json').write_text('{"id":"demo","name":"Demo toolkit","version":"1.0.0"}')

    def tearDown(self):
        self.temp.cleanup()

    def install(self):
        return self.store.install(self.store.preview(str(self.plugin))['preview_id'])

    def test_install_preserves_scripts_and_requires_activation(self):
        record = self.install()
        self.assertEqual([], self.store.runtime_plugins(str(self.root)))
        self.store.bind('demo', True)
        runtime = Path(self.store.runtime_plugins(str(self.root))[0]['path'])
        self.assertTrue((runtime / 'skills/demo/scripts/run.py').is_file())
        self.assertFalse((runtime / '.mcp.json').exists())
        self.assertFalse((runtime / 'hooks').exists())
        self.assertEqual('demo', json.loads((runtime / '.claude-plugin/plugin.json').read_text())['name'])
        self.assertEqual(['demo'], record['inventory']['mcp'])

    def test_native_manifest_and_component_filter(self):
        (self.plugin / 'ccw-plugin.json').unlink()
        (self.plugin / '.claude-plugin').mkdir()
        (self.plugin / '.claude-plugin/plugin.json').write_text('{"name":"demo","version":"2.0.0","commands":"./commands"}')
        self.install()
        self.store.bind('demo', True, components={'mcp': True})
        runtime = Path(self.store.runtime_plugins(str(self.root))[0]['path'])
        self.assertTrue((runtime / '.mcp.json').is_file())
        self.assertFalse((runtime / 'skills').exists())
        self.assertFalse((runtime / 'commands').exists())

    def test_scope_overrides_and_profile_filter(self):
        self.install()
        self.store.bind('demo', True)
        self.store.bind('demo', False, 'project', str(self.root))
        self.assertEqual([], self.store.runtime_plugins(str(self.root)))
        self.store.bind('demo', True, 'session', session_id='one')
        self.assertEqual(1, len(self.store.runtime_plugins(str(self.root), 'one')))
        self.assertEqual([], self.store.runtime_plugins(str(self.root), 'one', []))

    def test_update_rollback_and_uninstall_preserve_running_snapshot(self):
        one = self.install()
        self.store.bind('demo', True)
        path = Path(self.store.runtime_plugins(str(self.root))[0]['path'])
        (self.plugin / 'ccw-plugin.json').write_text('{"id":"demo","version":"2.0.0"}')
        two = self.install()
        self.assertNotEqual(one['revision'], two['revision'])
        self.assertEqual('1.0.0', self.store.rollback('demo')['version'])
        self.store.uninstall('demo')
        self.assertTrue(path.is_dir())
        self.assertEqual([], self.store.list('bindings'))
        self.assertEqual([], self.store.runtime_plugins(str(self.root)))

    def test_zip_rejects_traversal_and_links(self):
        for filename, mode in [('../escape', 0), ('/abs', 0), ('x\\escape', 0), ('link', 0o120777 << 16)]:
            with self.subTest(filename=filename):
                output = io.BytesIO()
                with zipfile.ZipFile(output, 'w') as archive:
                    info = zipfile.ZipInfo(filename)
                    info.external_attr = mode
                    archive.writestr(info, 'bad')
                with self.assertRaises(ValueError):
                    self.store.preview(source_type='zip', data=output.getvalue())
        self.assertFalse((self.root / 'escape').exists())

    def test_zip_wrapper_and_resources(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            for path in self.plugin.rglob('*'):
                if path.is_file():
                    archive.writestr('repo-main/' + str(path.relative_to(self.plugin)), path.read_bytes())
        preview = self.store.preview(source_type='zip', data=output.getvalue())
        self.assertEqual('demo', preview['id'])
        self.assertEqual(1, len(preview['inventory']['skills']))

    def test_local_symlink_rejected_and_stage_cleaned(self):
        (self.plugin / 'escape').symlink_to('/etc/hosts')
        with self.assertRaises(ValueError):
            self.store.preview(str(self.plugin))
        self.assertEqual([], list(self.store.staging.iterdir()))

    def test_team_import_validation_is_atomic(self):
        blob = {'format':'ccw-desk-workspace', 'version':1, 'profiles':[{'id':'valid','name':'Valid'}, {'id':'../bad','name':'Bad'}]}
        with self.assertRaises(ValueError):
            self.store.import_bundle(blob)
        self.assertEqual([], self.store.list('profiles'))

    def test_configuration_roundtrip_and_credentials_excluded(self):
        self.store.save_config('profiles', {'id':'review', 'name':'Review', 'mode':'review', 'model':'sonnet'})
        self.store.save_config('commands', {'id':'review-code', 'name':'Review code', 'prompt':'Review {{input}}', 'mode':'review'})
        self.store.save_config('workflows', {'id':'checks','name':'Checks','steps':[{'type':'command','command':'python3 -V'}]})
        self.store.put('providers','private',{'id':'private','api_key':'do-not-export','key_ref':'cwsecret://hidden'})
        bundle = self.store.export_bundle()
        self.assertNotIn('do-not-export', json.dumps(bundle))
        self.assertNotIn('cwsecret://', json.dumps(bundle))
        imported = Workbench(self.root / 'other')
        self.assertEqual(3, imported.import_bundle(bundle))
        self.assertEqual('Review {{input}}', imported.get('commands','review-code')['prompt'])

    def test_readonly_mode_cannot_be_overridden_by_profile_permission(self):
        self.store.save_config('profiles', {'id':'review','name':'Review','mode':'review','permission_mode':'bypassPermissions'})
        req = SimpleNamespace(profile_id='review', command_id=None, work_mode=None, workspace_mode='code', provider_id=None, model=None, effort=None, permission_mode=None, system_prompt='', message='hello', disallowed_tools=None, browser_enabled=None, extension_ids=None)
        self.store.resolve_request(req,str(self.root),'session')
        self.assertEqual('default',req.permission_mode)
        self.assertIn('Bash',req.disallowed_tools)
        self.assertIn('Agent',req.disallowed_tools)
        self.assertFalse(req.browser_enabled)

    def test_slash_command_expansion_and_extension_workflow_catalog(self):
        self.store.save_config('commands', {'id':'inspect','name':'Inspect','mode':'review','prompt':'Review {{input}}'})
        req=SimpleNamespace(profile_id=None,command_id=None,work_mode=None,workspace_mode='code',provider_id=None,model=None,effort=None,permission_mode=None,system_prompt='',message='/inspect the changes',display_message=None,disallowed_tools=None,browser_enabled=None,extension_ids=None)
        context=self.store.resolve_request(req,str(self.root),'session')
        self.assertEqual('Review the changes',req.message)
        self.assertEqual('/inspect the changes',req.display_message)
        self.assertEqual('review',context['mode'])
        self.install()
        self.store.bind('demo',True)
        self.assertEqual([],self.store.available_workflows(str(self.root)))

    def test_restart_marks_running_tasks_interrupted(self):
        self.store.put('tasks','one',{'id':'one','status':'running'})
        reopened=Workbench(self.store.root)
        reopened.recover_tasks()
        self.assertEqual('interrupted',reopened.get('tasks','one')['status'])


os.environ.setdefault('CLAUDE_WEB_DATA_DIR', tempfile.mkdtemp(prefix='ccw-workbench-api-'))
try:
    from fastapi.testclient import TestClient
    from claude_web import server
    HAS_SERVER = True
except ImportError:
    HAS_SERVER = False


@unittest.skipUnless(HAS_SERVER, 'server dependencies are required')
class WorkbenchApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # create_router creates its own runtime from _DATA_DIR; replacing the
        # global runtime's store does not isolate these endpoints.
        isolated_server = SimpleNamespace(**vars(server))
        isolated_server._DATA_DIR = Path(self.temp.name)
        from fastapi import FastAPI
        from claude_web.workbench_api import create_router
        self.app = FastAPI()
        self.router, self.runtime = create_router(isolated_server)
        self.app.include_router(self.router)
        self.client = TestClient(self.app, client=('127.0.0.1', 1234))

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def test_remote_cannot_install_or_read_configuration(self):
        remote = TestClient(self.app, client=('8.8.8.8',1234))
        self.assertEqual(403,remote.get('/api/workbench/catalog').status_code)
        self.assertEqual(403,remote.post('/api/workbench/extensions/preview',json={'source':'/tmp'}).status_code)
        remote.close()

    def test_local_browser_cross_origin_requests_are_rejected(self):
        self.assertEqual(403,self.client.post('/api/workbench/extensions/upload',files={'file':('bad.zip',b'zip')},headers={'Origin':'https://untrusted.example'}).status_code)
        self.assertEqual(403,self.client.get('/api/workbench/catalog',headers={'Sec-Fetch-Site':'cross-site'}).status_code)
        self.assertEqual(200,self.client.get('/api/workbench/catalog',headers={'Origin':'http://testserver'}).status_code)

    def test_catalog_crud_and_export(self):
        response=self.client.put('/api/workbench/configs/commands',json={'id':'fix','name':'Fix','prompt':'Fix {{input}}'})
        self.assertEqual(200,response.status_code,response.text)
        catalog=self.client.get('/api/workbench/catalog').json()
        self.assertEqual('fix',catalog['commands'][0]['id'])
        bundle=self.client.get('/api/workbench/export').json()
        self.assertEqual('ccw-desk-workspace',bundle['format'])
        self.assertEqual(200,self.client.delete('/api/workbench/configs/commands/fix').status_code)

    def test_builtin_marketplace_and_zip_team_bundle(self):
        response=self.client.get('/api/workbench/marketplace')
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual({'review-kit','research-kit'}, {p['id'] for p in response.json()['plugins']})
        self.client.put('/api/workbench/configs/profiles',json={'id':'share','name':'Shared','mode':'review'})
        exported=self.client.get('/api/workbench/export.zip')
        self.assertEqual('application/zip',exported.headers['content-type'])
        self.assertEqual(200,self.client.post('/api/workbench/import-zip',files={'file':('team.zip',exported.content,'application/zip')}).status_code)

    def test_invalid_config_and_routes_return_actionable_errors(self):
        self.assertEqual(400,self.client.put('/api/workbench/configs/workflows',json={'name':'bad','steps':[]}).status_code)
        self.assertEqual(400,self.client.put('/api/workbench/routes/unknown',json={}).status_code)
        self.assertEqual(400,self.client.put('/api/workbench/providers',json={'name':'bad','base_url':'http://example.com/v1','model':'m'}).status_code)

    def test_uploaded_zip_install_activation_upgrade_rollback_and_uninstall(self):
        def upload(version):
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w') as archive:
                archive.writestr('ccw-plugin.json', json.dumps({'id':'upload-demo','name':'Uploaded demo','version':version}))
                archive.writestr('skills/demo/SKILL.md', '---\nname: demo\ndescription: Test fixture\n---\nRead only.')
                archive.writestr('.mcp.json', '{"mcpServers":{"demo":{"command":"echo"}}}')
            response = self.client.post('/api/workbench/extensions/upload', files={'file':('demo.zip',buffer.getvalue(),'application/zip')})
            self.assertEqual(200,response.status_code,response.text)
            installed = self.client.post('/api/workbench/extensions/install', json={'preview_id':response.json()['preview_id']})
            self.assertEqual(200,installed.status_code,installed.text)
            return installed.json()
        upload('1.0.0')
        self.assertEqual([],self.client.get('/api/workbench/catalog').json()['effective'])
        bound = self.client.post('/api/workbench/extensions/upload-demo/binding',json={'enabled':True,'scope':'global','components':{'skills':True,'mcp':False}})
        self.assertEqual(200,bound.status_code,bound.text)
        effective = self.client.get('/api/workbench/catalog').json()['effective']
        self.assertEqual(['upload-demo'],[row['id'] for row in effective])
        self.assertFalse(effective[0]['binding']['components']['mcp'])
        self.assertEqual(400,self.client.post('/api/workbench/extensions/upload-demo/update').status_code)
        self.assertEqual('2.0.0',upload('2.0.0')['version'])
        rolled = self.client.post('/api/workbench/extensions/upload-demo/rollback')
        self.assertEqual(200,rolled.status_code,rolled.text)
        self.assertEqual('1.0.0',rolled.json()['version'])
        self.assertEqual(200,self.client.delete('/api/workbench/extensions/upload-demo').status_code)
        self.assertEqual([],self.client.get('/api/workbench/catalog').json()['extensions'])

    def test_local_extension_update_endpoint_previews_before_installing(self):
        plugin = Path(self.temp.name) / 'local-plugin'
        (plugin / 'skills/demo').mkdir(parents=True)
        (plugin / 'skills/demo/SKILL.md').write_text('---\nname: demo\ndescription: Test\n---\nTest.')
        manifest = plugin / 'ccw-plugin.json'
        manifest.write_text('{"id":"local-demo","name":"Local demo","version":"1.0.0"}')
        preview = self.client.post('/api/workbench/extensions/preview',json={'source_type':'local','source':str(plugin)})
        self.assertEqual(200,preview.status_code,preview.text)
        self.client.post('/api/workbench/extensions/install',json={'preview_id':preview.json()['preview_id']})
        manifest.write_text('{"id":"local-demo","name":"Local demo","version":"2.0.0"}')
        update = self.client.post('/api/workbench/extensions/local-demo/update')
        self.assertEqual(200,update.status_code,update.text)
        self.assertEqual('2.0.0',update.json()['version'])
        self.assertEqual('1.0.0',self.runtime.store.get('extensions','local-demo')['version'])
        installed = self.client.post('/api/workbench/extensions/install',json={'preview_id':update.json()['preview_id']})
        self.assertEqual('2.0.0',installed.json()['version'])

    def test_all_configuration_kinds_update_export_and_import_through_api(self):
        rows = {'profiles':{'id':'shared-profile','name':'Profile','mode':'review'},
                'commands':{'id':'shared-command','name':'Command','mode':'chat','prompt':'Review {{input}}'},
                'workflows':{'id':'shared-workflow','name':'Workflow','steps':[{'type':'report','name':'Report'}]}}
        for kind,row in rows.items():
            with self.subTest(kind=kind):
                response = self.client.put('/api/workbench/configs/'+kind,json=row)
                self.assertEqual(200,response.status_code,response.text)
                response = self.client.put('/api/workbench/configs/'+kind,json={**row,'name':row['name']+' edited'})
                self.assertEqual(row['name']+' edited',response.json()['name'])
        exported = self.client.get('/api/workbench/export')
        self.assertIn('attachment;',exported.headers['content-disposition'])
        self.assertIn('.json',exported.headers['content-disposition'])
        zipped = self.client.get('/api/workbench/export.zip')
        self.assertIn('.zip',zipped.headers['content-disposition'])
        with zipfile.ZipFile(io.BytesIO(zipped.content)) as archive:
            self.assertEqual(exported.json(),json.loads(archive.read('ccw-workspace.json')))
        for kind,row in rows.items():
            self.assertEqual(200,self.client.delete('/api/workbench/configs/'+kind+'/'+row['id']).status_code)
        imported = self.client.post('/api/workbench/import',json=exported.json())
        self.assertEqual(200,imported.status_code,imported.text)
        self.assertEqual(3,imported.json()['imported'])
        catalog = self.client.get('/api/workbench/catalog').json()
        for kind,row in rows.items():
            self.assertEqual(row['name']+' edited',catalog[kind][0]['name'])

    def test_corrupt_imports_leave_existing_configuration_unchanged(self):
        self.client.put('/api/workbench/configs/profiles',json={'id':'keep','name':'Keep me','mode':'review'})
        before = self.client.get('/api/workbench/export').json()
        invalid = {**before,'profiles':[{'id':'other','name':'Valid first'},{'id':'bad','name':''}]}
        self.assertEqual(400,self.client.post('/api/workbench/import',json=invalid).status_code)
        self.assertEqual(400,self.client.post('/api/workbench/import-zip',files={'file':('broken.zip',b'not a zip')}).status_code)
        self.assertEqual(422,self.client.post('/api/workbench/import',content=b'{broken',headers={'Content-Type':'application/json'}).status_code)
        self.assertEqual(before,self.client.get('/api/workbench/export').json())

    def test_json_and_zip_preview_classify_changes_without_writing_until_confirmed(self):
        for use_zip in (False, True):
            with self.subTest(zip=use_zip):
                before = self.client.put('/api/workbench/configs/profiles', json={'id':'existing','name':'Before'}).json()
                same = self.client.put('/api/workbench/configs/profiles', json={'id':'same','name':'Unchanged'}).json()
                bundle = {'format':'ccw-desk-workspace','version':1,'profiles':[
                    {**before,'name':'After'}, {'id':'fresh','name':'Fresh'}, same,
                ]}
                path = '/api/workbench/import'
                kwargs = {'json':bundle}
                if use_zip:
                    path += '-zip'
                    data = io.BytesIO()
                    with zipfile.ZipFile(data,'w') as archive:
                        archive.writestr('ccw-workspace.json',json.dumps(bundle))
                    kwargs = {'files':{'file':('team.zip',data.getvalue())}}
                preview = self.client.post(path+'?preview=true',**kwargs)
                self.assertEqual(200,preview.status_code,preview.text)
                self.assertEqual(['replace','new','unchanged'],[item['status'] for item in preview.json()['items']])
                self.assertEqual([before,same],self.runtime.store.list('profiles'))
                confirmed = self.client.post(path,headers={'X-Workbench-Import-Revision':preview.json()['revision']},**kwargs)
                self.assertEqual(200,confirmed.status_code,confirmed.text)
                self.assertEqual(2,confirmed.json()['imported'])
                self.assertEqual(same,self.runtime.store.get('profiles','same'))
                self.assertEqual('After',self.runtime.store.get('profiles','existing')['name'])
                self.assertEqual('Fresh',self.runtime.store.get('profiles','fresh')['name'])
                self.client.delete('/api/workbench/configs/profiles/fresh')

    def test_import_confirmation_rejects_changed_target_or_payload_atomically(self):
        path = '/api/workbench/import'
        bundle = {'format':'ccw-desk-workspace','version':1,'profiles':[{'id':'new','name':'New'},{'id':'same','name':'Replacement'}]}
        preview = self.client.post(path+'?preview=true',json=bundle)
        headers = {'X-Workbench-Import-Revision':preview.json()['revision']}
        changed = {**bundle,'profiles':[{'id':'different','name':'Different'}]}
        self.assertEqual(409,self.client.post(path,json=changed,headers=headers).status_code)
        current = self.client.put('/api/workbench/configs/profiles',json={'id':'same','name':'Concurrent change'}).json()
        self.assertEqual(409,self.client.post(path,json=bundle,headers=headers).status_code)
        self.assertEqual([current],self.runtime.store.list('profiles'))

    def test_custom_catalog_and_rating_with_mocked_https_transport(self):
        import httpx
        from unittest.mock import patch
        payload = {'name':'Fixture catalog','plugins':[{'id':'fixture','name':'Fixture','source':'https://example.com/fixture.git'}]}
        transport = httpx.MockTransport(lambda request:httpx.Response(200,json=payload))
        with patch('claude_web.workbench_api.httpx.AsyncClient',return_value=httpx.AsyncClient(transport=transport)):
            response = self.client.put('/api/workbench/marketplace/sources',json={'id':'fixture-source','url':'https://example.com/catalog.json'})
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual(200,self.client.put('/api/workbench/marketplace/ratings/fixture',json={'stars':4}).status_code)
        catalog = self.client.get('/api/workbench/marketplace').json()
        row = next(row for row in catalog['plugins'] if row['id']=='fixture')
        self.assertEqual(4,row['local_rating'])
        self.assertEqual(400,self.client.put('/api/workbench/marketplace/ratings/fixture',json={'stars':6}).status_code)
        self.assertEqual(400,self.client.put('/api/workbench/marketplace/sources',json={'url':'http://example.com/catalog.json'}).status_code)
        self.assertEqual(200,self.client.delete('/api/workbench/marketplace/sources/fixture-source').status_code)
        self.assertEqual([],self.client.get('/api/workbench/marketplace').json()['sources'])

    def test_provider_deletion_is_blocked_until_configuration_references_are_removed(self):
        row = {'id':'fixture-provider','name':'Fixture provider','protocol':'openai','base_url':'http://127.0.0.1:9999/v1','model':'fixture'}
        response = self.client.put('/api/workbench/providers',json=row)
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual(200,self.client.put('/api/workbench/routes/chat',json={'provider_id':row['id'],'model':'fixture-model'}).status_code)
        self.assertEqual(409,self.client.delete('/api/workbench/providers/'+row['id']).status_code)
        self.assertEqual(400,self.client.put('/api/workbench/routes/code',json={'provider_id':row['id']}).status_code)
        self.client.put('/api/workbench/routes/chat',json={'provider_id':''})
        self.assertEqual(200,self.client.delete('/api/workbench/providers/'+row['id']).status_code)
        self.assertEqual([],self.client.get('/api/workbench/catalog').json()['providers'])

    def wait_task(self, task_id, status):
        import time
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            response = self.client.get('/api/workbench/tasks/'+task_id)
            self.assertEqual(200,response.status_code,response.text)
            task = response.json()
            if task['status'] == status:
                return task
            time.sleep(0.01)
        self.fail(f'Expected {status}, got {task}')

    def test_task_approval_list_detail_and_report_endpoints(self):
        from unittest.mock import AsyncMock
        self.runtime.server._run_validation_command = AsyncMock(return_value={'returncode':0,'stdout':'API_COMMAND_FIXTURE_OK','stderr':''})
        self.runtime.server.stop_chat = AsyncMock(return_value={'ok':True})
        self.client.put('/api/workbench/configs/workflows',json={'id':'api-task','name':'API task','steps':[{'type':'command','value':'echo API_COMMAND_FIXTURE_OK'},{'type':'report'}]})
        with self.client:
            response = self.client.post('/api/workbench/tasks',json={'workflow_id':'api-task','cwd':self.temp.name})
            self.assertEqual(200,response.status_code,response.text)
            task_id = response.json()['id']
            self.wait_task(task_id,'waiting_approval')
            self.runtime.server._run_validation_command.assert_not_awaited()
            self.assertEqual(200,self.client.post('/api/workbench/tasks/'+task_id+'/approve').status_code)
            task = self.wait_task(task_id,'completed')
            self.assertEqual(2,task['next_step'])
            listed = self.client.get('/api/workbench/tasks')
            self.assertEqual(200,listed.status_code,listed.text)
            self.assertEqual(task_id,listed.json()['tasks'][0]['id'])
            report = self.client.get('/api/workbench/tasks/'+task_id+'/report')
            self.assertIn('API_COMMAND_FIXTURE_OK',report.text)
            self.assertIn('attachment;',report.headers['content-disposition'])
            self.assertEqual(409,self.client.post('/api/workbench/tasks/'+task_id+'/cancel').status_code)
            self.assertEqual(409,self.client.post('/api/workbench/tasks/'+task_id+'/resume').status_code)

    def test_task_cancel_and_resume_do_not_bypass_approval(self):
        from unittest.mock import AsyncMock
        self.runtime.server._run_validation_command = AsyncMock(return_value={'returncode':0,'stdout':'Resumed','stderr':''})
        self.runtime.server.stop_chat = AsyncMock(return_value={'ok':True})
        self.client.put('/api/workbench/configs/workflows',json={'id':'resume-task','name':'Resume task','steps':[{'type':'command','value':'echo Resumed'}]})
        with self.client:
            response = self.client.post('/api/workbench/tasks',json={'workflow_id':'resume-task','cwd':self.temp.name})
            self.assertEqual(200,response.status_code,response.text)
            task_id = response.json()['id']
            self.wait_task(task_id,'waiting_approval')
            self.assertEqual(200,self.client.post('/api/workbench/tasks/'+task_id+'/cancel').status_code)
            self.wait_task(task_id,'cancelled')
            self.runtime.server._run_validation_command.assert_not_awaited()
            self.assertEqual(409,self.client.post('/api/workbench/tasks/'+task_id+'/approve').status_code)
            self.assertEqual(200,self.client.post('/api/workbench/tasks/'+task_id+'/resume').status_code)
            self.wait_task(task_id,'waiting_approval')
            self.runtime.server._run_validation_command.assert_not_awaited()
            self.client.post('/api/workbench/tasks/'+task_id+'/approve')
            self.wait_task(task_id,'completed')
            self.runtime.server._run_validation_command.assert_awaited_once()

@unittest.skipUnless(HAS_SERVER, 'server dependencies are required')
class WorkbenchWorkflowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from claude_web.workbench_api import WorkbenchRuntime
        from unittest.mock import AsyncMock
        self.temp = tempfile.TemporaryDirectory()
        self.fake = SimpleNamespace(
            _DATA_DIR=Path(self.temp.name),
            _run_validation_command=AsyncMock(return_value={'returncode':0,'stdout':'ok','stderr':''}),
            stop_chat=AsyncMock(return_value={'ok':True}),
        )
        self.runtime = WorkbenchRuntime(self.fake)

    async def asyncTearDown(self):
        await self.runtime.shutdown()
        self.temp.cleanup()

    def task(self, steps):
        return {'id':'demo','name':'Test','workflow':{'steps':steps},'cwd':self.temp.name,
                'session_id':'isolated-session','next_step':0,'results':[],'status':'queued','updated_at':0}

    async def test_shell_step_requires_approval_then_completes_with_report(self):
        task=self.task([{'type':'command','value':'python3 -V','name':'Check','timeout':30}, {'type':'report','value':'','name':'Report'}])
        self.runtime.launch(task)
        for _ in range(100):
            await asyncio.sleep(0.001)
            if self.runtime.store.get('tasks','demo')['status']=='waiting_approval':
                break
        self.fake._run_validation_command.assert_not_awaited()
        self.runtime.approvals['demo'].set()
        await self.runtime.jobs['demo']
        saved=self.runtime.store.get('tasks','demo')
        self.assertEqual('completed',saved['status'])
        self.assertEqual(2,saved['next_step'])
        self.assertIn('ok',saved['report'])

    async def test_failed_step_can_resume_without_repeating_successful_steps(self):
        from unittest.mock import AsyncMock
        task=self.task([{'type':'prompt','value':'one','name':'One'}, {'type':'prompt','value':'two','name':'Two'}])
        self.runtime.collect_prompt=AsyncMock(side_effect=[{'text':'one'}, ValueError('failed')])
        await self.runtime.run(task)
        self.assertEqual('failed',task['status'])
        self.assertEqual(1,task['next_step'])
        self.runtime.collect_prompt=AsyncMock(return_value={'text':'two'})
        await self.runtime.run(task)
        self.runtime.collect_prompt.assert_awaited_once()
        self.assertEqual('completed',task['status'])
        self.assertEqual(2,len(task['results']))

    async def test_cancel_while_waiting_does_not_execute_command(self):
        task=self.task([{'type':'command','value':'echo unsafe','name':'Check','timeout':30}])
        self.runtime.launch(task)
        for _ in range(100):
            await asyncio.sleep(0.001)
            if 'demo' in self.runtime.approvals:
                break
        job=self.runtime.jobs['demo']
        job.cancel()
        await job
        self.assertEqual('cancelled',self.runtime.store.get('tasks','demo')['status'])
        self.fake._run_validation_command.assert_not_awaited()


@unittest.skipUnless(HAS_SERVER, 'server dependencies are required')
class WorkbenchProviderTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from claude_web.workbench_api import WorkbenchRuntime
        from claude_web.secret_store import SecretStore
        from unittest.mock import AsyncMock, Mock
        self.temp=tempfile.TemporaryDirectory()
        root=Path(self.temp.name)
        self.secret_store=SecretStore(db_path=root/'secrets.db',key_file=root/'test.key',use_keychain=False)
        self.fake=SimpleNamespace(_DATA_DIR=root,_get_secret_store=lambda:self.secret_store,_stopped_sessions=set(),
                                  upsert_session=Mock(),derive_title=lambda text:text[:40],async_append_event=AsyncMock(),async_record_usage=AsyncMock())
        self.runtime=WorkbenchRuntime(self.fake)

    async def asyncTearDown(self):
        await self.runtime.shutdown()
        self.temp.cleanup()

    def test_credentials_are_encrypted_and_key_rotation_preserves_record(self):
        data={'id':'example','name':'Example','base_url':'https://example.com/v1','model':'example-model','api_key':'secret-value'}
        public=self.runtime.save_provider(data)
        self.assertTrue(public['has_key'])
        self.assertNotIn('secret-value',json.dumps(public))
        private=self.runtime.store.get('providers','example')
        self.assertNotIn('secret-value',json.dumps(private))
        self.assertEqual('secret-value',self.secret_store.resolve_secret(private['key_ref']))
        public=self.runtime.save_provider({**data,'api_key':'','protocol':'anthropic'})
        self.assertEqual('anthropic',public['protocol'])
        self.assertTrue(public['has_key'])

    async def test_stream_error_is_not_reported_as_success(self):
        from unittest.mock import patch
        import httpx
        self.runtime.save_provider({'id':'errors','name':'Errors','base_url':'https://example.com/v1','model':'m'})
        content='data: '+json.dumps({'choices':[{'delta':{},'finish_reason':'stop'}]})+'\n\n'
        content+='data: '+json.dumps({'error':{'message':'provider failed'}})+'\n\ndata: [DONE]\n\n'
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,content=content)))
        req=SimpleNamespace(workspace_mode='chat',images=[],model=None,system_prompt='',message='hi',display_message=None)
        with patch('claude_web.workbench_api.httpx.AsyncClient',return_value=client):
            response=await self.runtime.provider_chat(req,'errors-session',self.temp.name,[],'errors')
            frames=[json.loads(chunk[6:]) async for chunk in response.body_iterator]
        self.assertFalse(frames[-1]['success'])
        self.assertTrue(any(frame['type']=='error' for frame in frames))
        self.assertNotIn('errors-session',self.runtime.provider_sessions)

    async def test_text_provider_keeps_profile_configuration_for_followup_turns(self):
        from unittest.mock import patch,AsyncMock
        from starlette.responses import StreamingResponse
        self.runtime.store.put('providers','text',{'id':'text','name':'Text','model':'m','base_url':'https://example.com/v1','protocol':'openai','key_ref':''})
        self.runtime.store.save_config('profiles',{'id':'text-profile','name':'Text profile','mode':'chat','provider_id':'text','system_prompt':'Keep this profile'})
        response=StreamingResponse(iter(()))
        self.runtime.provider_chat=AsyncMock(return_value=response)
        req=server.ChatRequest(message='hi',profile_id='text-profile',workspace_mode='chat')
        with patch.object(server,'_workbench_runtime',self.runtime):
            self.assertIs(response,await server._chat_response(req))
        saved=self.runtime.store.list('session_modes')
        self.assertEqual('Keep this profile',saved[0]['config']['system_prompt'])

    async def test_compatible_stream_adapts_to_existing_ui_and_persists_reply(self):
        from unittest.mock import patch
        data={'id':'example','name':'Example','base_url':'https://example.com/v1','model':'example-model','api_key':'secret-value'}
        self.runtime.save_provider(data)
        def respond(request):
            self.assertEqual('Bearer secret-value',request.headers['Authorization'])
            self.assertEqual('example-model',json.loads(request.content)['model'])
            content='data: '+json.dumps({'choices':[{'delta':{'content':'hello'},'finish_reason':None}]})+'\n\n'
            content+='data: '+json.dumps({'choices':[{'delta':{},'finish_reason':'stop'}]})+'\n\ndata: [DONE]\n\n'
            return __import__('httpx').Response(200,content=content,headers={'content-type':'text/event-stream'})
        httpx=__import__('httpx')
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        req=SimpleNamespace(workspace_mode='chat',images=[],model=None,system_prompt='',message='hi',display_message=None)
        with patch('claude_web.workbench_api.httpx.AsyncClient',return_value=client):
            response=await self.runtime.provider_chat(req,'s',self.temp.name,[],'example')
            frames=[]
            async for chunk in response.body_iterator:
                frames.append(json.loads(chunk[6:]))
        self.assertEqual('meta',frames[0]['type'])
        self.assertEqual('message_start',frames[1]['event']['type'])
        self.assertTrue(frames[-1]['success'])
        self.assertEqual('hello',next(f for f in frames if f['type']=='assistant')['message']['content'][0]['text'])
        self.assertNotIn('s',self.runtime.provider_sessions)
        self.assertEqual('example',self.runtime.store.get('session_providers','s')['provider_id'])


@unittest.skipUnless(HAS_SERVER, 'server dependencies are required')
class WorkbenchCommandExecutionTest(unittest.IsolatedAsyncioTestCase):
    async def test_approved_command_executes_and_report_contains_actual_output(self):
        from claude_web.workbench_api import WorkbenchRuntime
        from unittest.mock import AsyncMock
        with tempfile.TemporaryDirectory() as temp:
            fake=SimpleNamespace(_DATA_DIR=Path(temp),_run_validation_command=server._run_validation_command,stop_chat=AsyncMock())
            runtime=WorkbenchRuntime(fake)
            task={'id':'real-command','name':'Environment smoke','workflow':{'steps':[{'type':'command','name':'Actual command','value':'printf ccw-workflow-smoke','timeout':10}]},'cwd':temp,'session_id':'real-command-session','next_step':0,'results':[],'status':'queued','updated_at':0}
            runtime.launch(task)
            job=runtime.jobs['real-command']
            for _ in range(100):
                if 'real-command' in runtime.approvals:
                    break
                await asyncio.sleep(0.001)
            self.assertEqual('waiting_approval',runtime.store.get('tasks','real-command')['status'])
            runtime.approvals['real-command'].set()
            await asyncio.wait_for(job,timeout=10)
            saved=runtime.store.get('tasks','real-command')
            self.assertEqual('completed',saved['status'],saved)
            self.assertEqual('ccw-workflow-smoke',saved['results'][0]['result']['stdout'])
            self.assertIn('ccw-workflow-smoke',saved['report'])
            await runtime.shutdown()


class WorkbenchAuditRegressionTest(unittest.TestCase):
    setUp = WorkbenchStoreTest.setUp
    tearDown = WorkbenchStoreTest.tearDown
    def request(self, **values):
        return SimpleNamespace(profile_id=None, command_id=None, work_mode=None,
            workspace_mode='code', provider_id=None, model=None, effort=None,
            permission_mode=None, system_prompt='', message='hello', display_message=None,
            disallowed_tools=None, browser_enabled=True, extension_ids=None, **values)

    def test_code_mode_preserves_browser_toggle(self):
        req = self.request()
        req.work_mode = 'code'
        self.store.resolve_request(req, str(self.root), 'new')
        self.assertTrue(req.browser_enabled)

    def test_opening_store_does_not_interrupt_live_tasks(self):
        self.store.put('tasks', 'live', {'id':'live','status':'running'})
        reopened = Workbench(self.store.root)
        self.assertEqual('running', reopened.get('tasks','live')['status'])

    def test_explicit_provider_model_is_not_replaced_by_default(self):
        self.store.put('providers','p',{'id':'p','model':'default-model'})
        req = self.request()
        req.provider_id = 'p'
        req.model_provider_id = 'p'
        req.model = 'chosen-model'
        self.store.resolve_request(req, str(self.root), 'new')
        self.assertEqual('chosen-model', req.model)

    def test_existing_native_session_does_not_inherit_a_new_provider_route(self):
        self.store.put('providers','new-provider',{'id':'new-provider','model':'new-model'})
        self.store.put('routes','code',{'id':'code','provider_id':'new-provider','model':'new-model'})
        self.store.put('session_modes','existing',{'id':'existing','mode':'code','route_model':'native-original'})
        req = self.request()
        context = self.store.resolve_request(req,str(self.root),'existing',existing_session=True)
        self.assertFalse(context['provider_id'])
        self.assertEqual('native-original',req.model)
        context = self.store.resolve_request(self.request(),str(self.root),'new')
        self.assertEqual('new-provider',context['provider_id'])

    def test_profile_defaults_survive_followup_without_repeating_prompt(self):
        self.store.put('session_modes', 'saved', {'id':'saved','mode':'code','config':{'permission_mode':'acceptEdits','effort':'medium','system_prompt':'Profile instructions'}})
        req = self.request()
        self.store.resolve_request(req, str(self.root), 'saved')
        self.assertEqual('acceptEdits',req.permission_mode)
        self.assertEqual('medium',req.effort)
        self.assertEqual('Profile instructions',req.system_prompt)

    def test_workflow_command_cannot_override_readonly_profile(self):
        self.store.save_config('profiles', {'id':'readonly','name':'Readonly','mode':'review'})
        self.store.save_config('commands', {'id':'write-code','name':'Write','mode':'code','prompt':'Write {{input}}'})
        req = self.request()
        req.profile_id = 'readonly'
        req.message = '/write-code hello'
        req.command_id = None
        result = self.store.resolve_request(req,str(self.root),'new')
        # A profile does not reinterpret task content as a second user command.
        self.assertEqual('review',result['mode'])
        self.assertIn('Write',req.disallowed_tools)

    def test_zip_preserves_executable_resource_permissions(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output,'w') as archive:
            archive.writestr('ccw-plugin.json','{"id":"exec-test"}')
            script = zipfile.ZipInfo('scripts/check.sh')
            script.external_attr = 0o100755 << 16
            archive.writestr(script,'#!/bin/sh\nexit 0\n')
        preview=self.store.preview(source_type='zip',data=output.getvalue())
        record=self.store.install(preview['preview_id'])
        path=self.store.packages/record['id']/record['revision']/'package/scripts/check.sh'
        self.assertTrue(path.stat().st_mode & 0o111)


@unittest.skipUnless(HAS_SERVER and os.name != 'nt', 'POSIX server dependencies required')
class WorkbenchProcessCleanupTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancelling_command_stops_child_processes(self):
        import signal
        from unittest.mock import AsyncMock
        from claude_web.workbench_api import WorkbenchRuntime
        with tempfile.TemporaryDirectory() as temp:
            pidfile=Path(temp)/'child.pid'
            fake=SimpleNamespace(_DATA_DIR=Path(temp),_run_validation_command=server._run_validation_command,stop_chat=AsyncMock())
            runtime=WorkbenchRuntime(fake)
            task={'id':'cancel-child','name':'Cancel child','workflow':{'steps':[{'type':'command','name':'Child','value':f'sleep 60 & echo $! > {pidfile}; wait','timeout':30}]},'cwd':temp,'session_id':'child-session','next_step':0,'results':[],'status':'queued','updated_at':0}
            runtime.launch(task);job=runtime.jobs[task['id']]
            child=None
            try:
                for _ in range(200):
                    if task['id'] in runtime.approvals:break
                    await asyncio.sleep(.005)
                runtime.approvals[task['id']].set()
                for _ in range(200):
                    if pidfile.exists():break
                    await asyncio.sleep(.005)
                self.assertTrue(pidfile.exists())
                child=int(pidfile.read_text())
                job.cancel()
                await asyncio.wait_for(job,timeout=3)
                # A killed orphan can be a short-lived zombie; ps must not show
                # a live process capable of continuing work after cancellation.
                proc=await asyncio.create_subprocess_exec('ps','-p',str(child),'-o','stat=',stdout=asyncio.subprocess.PIPE)
                output,_=await proc.communicate()
                self.assertTrue(not output.strip() or output.strip().startswith(b'Z'),output)
            finally:
                if child:
                    try:os.kill(child,signal.SIGKILL)
                    except ProcessLookupError:pass
                await runtime.shutdown()

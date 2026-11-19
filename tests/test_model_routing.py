"""End-to-end routing checks against a local HTTP provider, without paid models."""
import json
import os
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('CLAUDE_WEB_DATA_DIR', tempfile.mkdtemp(prefix='ccw-routing-tests-'))
from fastapi.testclient import TestClient
from claude_web import server
from claude_web.secret_store import SecretStore
from claude_web.workbench_api import create_router


class ProviderHandler(BaseHTTPRequestHandler):
    requests = []
    reject_chat = False
    finish_reason = 'stop'

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'data': [{'id': 'provider-model'}, {'id': 'route-model'}]}).encode())

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
        type(self).requests.append({'path':self.path,'auth':self.headers.get('Authorization'),'body':body})
        if self.reject_chat:
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"model unavailable","code":"model_not_found"}}')
            return
        if body.get('model') not in {'provider-model','route-model','updated-model'}:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"wrong model"}}')
            return
        if body.get('stream') is False:
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'choices':[{'message':{'content':'OK'},'finish_reason':'stop'}],'usage':{'prompt_tokens':4,'completion_tokens':1}}).encode())
            return
        self.send_response(200)
        self.send_header('Content-Type','text/event-stream')
        self.end_headers()
        frames = [
            {'choices':[{'delta':{'content':'LOCAL_ROUTING_OK'},'finish_reason':None}]},
            {'choices':[{'delta':{},'finish_reason':self.finish_reason}],'usage':{'prompt_tokens':12,'completion_tokens':4}},
        ]
        for frame in frames:
            self.wfile.write(('data: '+json.dumps(frame)+'\n\n').encode())
        self.wfile.write(b'data: [DONE]\n\n')


class ModelRoutingHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd=ThreadingHTTPServer(('127.0.0.1',0),ProviderHandler)
        cls.thread=threading.Thread(target=cls.httpd.serve_forever,daemon=True)
        cls.thread.start()
        cls.url=f'http://127.0.0.1:{cls.httpd.server_port}/v1'

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown();cls.httpd.server_close();cls.thread.join()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.secrets=SecretStore(db_path=Path(self.temp.name)/'secrets.sqlite3',key_file=Path(self.temp.name)/'key',use_keychain=False)
        self.secret_patch=patch.object(server,'_get_secret_store',return_value=self.secrets)
        self.secret_patch.start()
        self.dir_patch=patch.object(server,'_DATA_DIR',Path(self.temp.name))
        self.dir_patch.start()
        self.router,self.runtime=create_router(server)
        self.runtime_patch=patch.object(server,'_workbench_runtime',self.runtime)
        self.runtime_patch.start()
        from fastapi import FastAPI
        app=FastAPI()
        app.include_router(self.router)
        app.post('/api/chat')(server.chat)
        app.post('/api/suggest-followups')(server.suggest_followups)
        app.post('/api/sessions/{session_id}/suggest-title')(server.suggest_title)
        app.post('/api/sessions/{session_id}/compact')(server.compact_session)
        self.client=TestClient(app,client=('127.0.0.1',4321))
        ProviderHandler.requests=[];ProviderHandler.reject_chat=False;ProviderHandler.finish_reason='stop'
        saved=self.client.put('/api/workbench/providers',json={'id':'local-provider','name':'Local test provider','base_url':self.url,'model':'provider-model','api_key':'local-test-key','protocol':'openai'})
        self.assertEqual(200,saved.status_code,saved.text)
        self.sessions=[]

    def tearDown(self):
        self.client.close()
        for sid in self.sessions:
            with server.db_connect() as conn:
                conn.execute('DELETE FROM sessions WHERE id=?',(sid,))
            (server.HISTORY_DIR/f'{sid}.jsonl').unlink(missing_ok=True)
        self.runtime_patch.stop();self.dir_patch.stop();self.secret_patch.stop();self.temp.cleanup()

    def route(self,model=''):
        response=self.client.put('/api/workbench/routes/chat',json={'provider_id':'local-provider','model':model})
        self.assertEqual(200,response.status_code,response.text)

    def chat(self,session_id=None,**extra):
        response=self.client.post('/api/chat',json={'message':'hello','workspace_mode':'chat','work_mode':'chat','session_id':session_id,**extra})
        self.assertEqual(200,response.status_code,response.text)
        frames=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        meta=next(frame for frame in frames if frame['type']=='meta')
        if meta['session_id'] not in self.sessions:self.sessions.append(meta['session_id'])
        return frames,meta['session_id']

    def test_saved_route_overrides_stale_claude_model_from_composer(self):
        self.route('route-model')
        frames,_=self.chat(model='sonnet')
        self.assertTrue(frames[-1]['success'],frames)
        self.assertEqual('route-model',ProviderHandler.requests[-1]['body']['model'])
        self.assertEqual('Bearer local-test-key',ProviderHandler.requests[-1]['auth'])
        self.assertEqual('/v1/chat/completions',ProviderHandler.requests[-1]['path'])

    def test_service_default_model_is_used_when_route_model_is_blank(self):
        self.route()
        frames,_=self.chat(model='claude-opus-4-6')
        self.assertTrue(frames[-1]['success'],frames)
        self.assertEqual('provider-model',ProviderHandler.requests[-1]['body']['model'])

    def test_existing_session_keeps_model_when_default_route_changes(self):
        self.route('provider-model')
        first,sid=self.chat()
        self.assertTrue(first[-1]['success'],first)
        expected={'service':'Local test provider','model':'provider-model','source':'binding'}
        self.assertEqual(expected,first[0]['session_model'])
        self.route('updated-model')
        displayed=self.client.get('/api/workbench/selection/'+sid)
        self.assertEqual(200,displayed.status_code,displayed.text)
        self.assertEqual(expected,displayed.json()['session_model'])
        second,_=self.chat(session_id=sid)
        self.assertTrue(second[-1]['success'],second)
        self.assertEqual('provider-model',ProviderHandler.requests[-1]['body']['model'])
        messages=ProviderHandler.requests[-1]['body']['messages']
        self.assertEqual(['user','assistant','user'],[m['role'] for m in messages])

    def test_connection_check_validates_inference_not_only_models_list(self):
        ProviderHandler.reject_chat=True
        response=self.client.post('/api/workbench/providers/local-provider/test')
        self.assertGreaterEqual(response.status_code,400,response.text)
        self.assertTrue(ProviderHandler.requests,'Connection check never made an inference request')

    def test_inference_check_accepts_a_working_model_and_route_override(self):
        response=self.client.post('/api/workbench/providers/local-provider/test',json={'model':'route-model'})
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual('route-model',response.json()['model'])
        self.assertEqual('OK',response.json()['reply'])
        self.assertEqual(16,ProviderHandler.requests[-1]['body']['max_tokens'])
        self.assertFalse(ProviderHandler.requests[-1]['body']['stream'])

    def test_followups_title_and_compact_use_bound_provider_without_cli_fallback(self):
        self.route('route-model')
        frames,sid=self.chat()
        self.assertTrue(frames[-1]['success'],frames)
        self.chat(session_id=sid)
        self.chat(session_id=sid)
        with patch.object(server.asyncio,'create_subprocess_exec',side_effect=AssertionError('Unexpected Claude CLI fallback')):
            self.assertEqual(200,self.client.post('/api/suggest-followups',params={'session_id':sid}).status_code)
            self.assertEqual('route-model',ProviderHandler.requests[-1]['body']['model'])
            response=self.client.post(f'/api/sessions/{sid}/suggest-title')
            self.assertEqual(200,response.status_code,response.text)
            response=self.client.post(f'/api/sessions/{sid}/compact?keep_last=1')
            self.assertEqual(200,response.status_code,response.text)
            self.assertTrue(response.json()['ok'])
        self.assertTrue(all(req['body']['model']=='route-model' for req in ProviderHandler.requests))

    def test_truncated_generation_does_not_claim_success(self):
        self.route()
        ProviderHandler.finish_reason='length'
        frames,_=self.chat()
        self.assertFalse(frames[-1]['success'],frames)

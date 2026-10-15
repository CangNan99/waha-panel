import base64
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer
from cryptography.fernet import Fernet

from panel.app import PanelHandler, PanelState, init_db


class Client:
    def get_health(self): return {'status':'ok'}
    def get_version(self): return {'version':'test'}
    def get_sessions(self): return [{'name':'default','status':'WORKING'}]
    def get_chats(self,*args,**kwargs): return [{'id':'synthetic@c.us','name':'测试客户'}]


class UIRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.database=Path(self.temp.name)/'route.sqlite3'
        init_db(self.database,seed_business=False)
        self.state=PanelState(self.database,Client(),'fixture-key',admin_username='admin',admin_password='fixture-password',data_encryption_key=Fernet.generate_key().decode())
        handler=type('FixtureHandler',(PanelHandler,),{'state':self.state})
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base=f'http://127.0.0.1:{self.server.server_port}'
        self.authorization='Basic '+base64.b64encode(b'admin:fixture-password').decode()

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2)
        self.state.stop_background_services();self.temp.cleanup()

    def request(self,path,authenticated=True,method='GET',payload=None,csrf=False):
        headers={'Authorization':self.authorization} if authenticated else {}
        if csrf:headers['X-CSRF-Token']=self.state.csrf_token
        body=None
        if payload is not None:body=json.dumps(payload).encode();headers['Content-Type']='application/json'
        try:
            response=urlopen(Request(self.base+path,headers=headers,data=body,method=method),timeout=5)
        except HTTPError as error:
            response=error
        with closing(response):
            data=response.read().decode()
            return response.status,dict(response.headers),data

    def test_all_ui_data_and_modules_require_authentication(self):
        for path in ['/overview','/logs','/api/overview','/api/logs?limit=50','/api/ui/view?view=chat','/assets/ui/shell.js']:
            with self.subTest(path=path):
                self.assertEqual(self.request(path,False)[0],401)

    def test_shell_deep_links_and_scoped_payloads_remain_private(self):
        for path in ['/','/settings?session=default','/sessions/default/chats','/overview','/logs']:
            status,headers,data=self.request(path)
            self.assertEqual(status,200)
            self.assertEqual(headers['Cache-Control'],'private, no-store')
            self.assertIn('data-shell-nav="overview"',data)
        status,headers,data=self.request('/api/ui/view?view=chat&session=default')
        payload=json.loads(data)
        self.assertEqual(status,200)
        self.assertIn('id="composerInput"',payload['html'])
        self.assertNotIn('<script>',payload['html'])
        self.assertIn(':host',payload['css'])

    def test_resources_cannot_read_paths_or_unknown_views(self):
        self.assertEqual(self.request('/assets/ui/../../.env')[0],404)
        self.assertEqual(self.request('/assets/ui/controllers/../../../app.py')[0],404)
        self.assertEqual(self.request('/api/ui/view?view=../../.env')[0],400)

    def test_overview_and_log_filters_validate_scope(self):
        status,_,body=self.request('/api/overview?scope=all')
        self.assertEqual(status,200)
        self.assertEqual(json.loads(body)['session_counts']['connected'],1)
        self.assertEqual(self.request('/api/overview?session=other')[0],403)
        self.assertEqual(self.request('/api/logs?session=other&limit=50')[0],403)
        self.assertEqual(self.request('/api/logs?limit=101')[0],400)
        self.assertEqual(self.request('/api/logs?limit=1&limit=2')[0],400)

    def test_legacy_logs_keep_twenty_records_without_new_time_window(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            for n in range(25):
                connection.execute('INSERT INTO system_logs(session_name,level,event,message,created_at) VALUES (?,?,?,?,?)',('default','INFO','test.record',f'历史 {n}',int(time.time())-40*86400))
        status,_,body=self.request('/api/logs?session=default')
        self.assertEqual(status,200)
        self.assertEqual(len(json.loads(body)['logs']),20)
        status,_,body=self.request('/api/logs?session=default&limit=50')
        self.assertEqual(status,200)
        self.assertEqual(json.loads(body)['logs'],[])

    def test_note_preview_requires_csrf_and_owned_chat_and_does_not_save(self):
        ref=self.state.chat.overview('default')['items'][0]['chat_ref']
        calls=[]
        self.state.translation.normalize_internal_note=lambda text:calls.append(text) or '客户希望获取报价'
        route='/api/chat/sessions/default/notes/normalize'
        payload={'chat_ref':ref,'text':'Please quote'}
        self.assertEqual(self.request(route,False,'POST',payload)[0],401)
        self.assertEqual(self.request(route,True,'POST',payload)[0],403)
        invalid={'chat_ref':'invalid-reference','text':'Please quote'}
        self.assertIn(self.request(route,True,'POST',invalid,True)[0],(400,403))
        self.assertEqual(calls,[])
        status,_,body=self.request(route,True,'POST',payload,True)
        self.assertEqual(status,200)
        self.assertEqual(json.loads(body),{'note_zh':'客户希望获取报价'})
        self.assertEqual(self.state.chat.note('default',ref)['note'],'')


if __name__=='__main__':unittest.main()

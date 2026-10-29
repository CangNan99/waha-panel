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
    def __init__(self):
        self.sent = []
        self.fail_send = False
        self.block_send = False
        self.send_started = threading.Event()
        self.release_send = threading.Event()
    def get_health(self): return {'status':'ok'}
    def get_version(self): return {'version':'test'}
    def get_sessions(self): return [{'name':'default','status':'WORKING'}]
    def get_chats(self,*args,**kwargs): return [{'id':'synthetic@c.us','name':'测试客户'}]
    def send_text(self, session, chat_id, text):
        if self.fail_send:
            raise TimeoutError('fixture send failed')
        if self.block_send:
            self.send_started.set()
            self.release_send.wait(4)
        self.sent.append((session, chat_id, text))
        return {'id': 'manual-' + str(len(self.sent))}


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

    def test_iptv_check_is_admin_csrf_protected_and_records_hide_credentials(self):
        self.state.save_settings({'iptv':{'api_key':'synthetic-provider-key','pack':'all','duration_confirmed':True,'enabled':True}})
        class Supplier:
            def check(self):return {'account_enabled':True,'packages':[]}
            def create_trial(self,pack,note):return {'url':'http://stream.example/get.php?username=private-user&password=private-password'}
        self.state.iptv.client_factory=lambda _key:Supplier()
        self.state.iptv.create_trial('default','synthetic@c.us','test-event')
        self.assertEqual(self.request('/api/iptv/trials',False)[0],401)
        self.assertEqual(self.request('/api/iptv/check',False,method='POST',payload={})[0],401)
        self.assertEqual(self.request('/api/iptv/check',method='POST',payload={})[0],403)
        status,_,body=self.request('/api/iptv/check',method='POST',payload={},csrf=True)
        self.assertEqual(status,200)
        self.assertTrue(json.loads(body)['account_enabled'])
        status,_,body=self.request('/api/iptv/trials')
        self.assertEqual(status,200)
        self.assertEqual(json.loads(body)['items'][0]['state'],'created')
        self.assertNotIn('private-password',body)
        self.assertNotIn('private-user',body)
        self.assertEqual(self.request('/api/iptv/trials?session=other')[0],403)
        _,_,settings=self.request('/api/settings')
        self.assertNotIn('synthetic-provider-key',settings)
        self.assertTrue(json.loads(settings)['iptv']['api_key_configured'])

    def test_manual_iptv_card_route_sends_selected_duration_and_repeats(self):
        self.state.save_settings({'iptv': {'api_key': 'synthetic-provider-key', 'pack': 'all',
                                           'duration_confirmed': True, 'enabled': True}})
        class Supplier:
            def __init__(self): self.calls = []
            def create_account(self, pack, subscription, note):
                self.calls.append((pack, str(subscription), note))
                return {'url': 'http://stream.example/get.php?username=private-user&password=private-password'}
        supplier = Supplier()
        self.state.iptv.client_factory = lambda _key: supplier
        chat_ref = self.state.chat.overview('default')['items'][0]['chat_ref']
        route = '/api/chat/sessions/default/iptv/manual-card'
        payload = {'chat_ref': chat_ref, 'subscription': '3', 'client_request_id': 'manual-request-1'}
        self.assertEqual(self.request(route, False, 'POST', payload, True)[0], 401)
        self.assertEqual(self.request(route, True, 'POST', payload)[0], 403)
        status, _, body = self.request(route, True, 'POST', payload, True)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['status'], 'sent')
        self.assertEqual(len(supplier.calls), 1)
        self.assertEqual(len(self.state.client.sent), 1)
        self.assertNotIn('private-password', body)
        second = {**payload, 'subscription': '12', 'client_request_id': 'manual-request-2'}
        status, _, body = self.request(route, True, 'POST', second, True)
        self.assertEqual(status, 200)
        self.assertEqual(len(supplier.calls), 2)
        sent_count = len(self.state.client.sent)
        status, _, body = self.request(route, True, 'POST', second, True)
        self.assertEqual(status, 200)
        self.assertEqual(len(supplier.calls), 2)
        self.assertEqual(len(self.state.client.sent), sent_count)

    def test_manual_iptv_card_auto_language_uses_recent_customer_context(self):
        self.state.save_settings({'iptv': {'api_key': 'synthetic-provider-key', 'pack': 'all',
                                           'duration_confirmed': True, 'enabled': True}})
        class Supplier:
            def create_account(self, pack, subscription, note):
                return {'url': 'http://stream.example/get.php?username=private-user&password=private-password'}
        self.state.iptv.client_factory = lambda _key: Supplier()
        self.state._archive_message('default', 'synthetic@c.us', 'inbound', 'customer-language-1',
                                     'Quero um teste IPTV', self.state.clock())
        chat_ref = self.state.chat.overview('default')['items'][0]['chat_ref']
        route = '/api/chat/sessions/default/iptv/manual-card'
        payload = {'chat_ref': chat_ref, 'subscription': '3', 'client_request_id': 'manual-language-1', 'language': 'auto'}
        status, _, body = self.request(route, True, 'POST', payload, True)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['status'], 'sent')
        self.assertIn('Sua conta IPTV de 3 meses está pronta.', self.state.client.sent[-1][2])

    def test_manual_iptv_send_failure_schedules_resend_of_existing_card(self):
        self.state.save_settings({'iptv': {'api_key': 'synthetic-provider-key', 'pack': 'all',
                                           'duration_confirmed': True, 'enabled': True}})
        class Supplier:
            def create_account(self, pack, subscription, note):
                return {'url': 'http://stream.example/get.php?username=private-user&password=private-password'}
        self.state.iptv.client_factory = lambda _key: Supplier()
        chat_ref = self.state.chat.overview('default')['items'][0]['chat_ref']
        self.state.client.fail_send = True
        route = '/api/chat/sessions/default/iptv/manual-card'
        payload = {'chat_ref': chat_ref, 'subscription': '1', 'client_request_id': 'manual-send-fail'}
        status, _, body = self.request(route, True, 'POST', payload, True)
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(body)['status'], 'send_retrying')
        self.state.client.fail_send = False
        self.assertEqual(len(self.state.iptv.retry_due_once(send_callback=self.state._send_text)), 1)
        self.assertEqual(len(self.state.client.sent), 1)

    def test_concurrent_manual_replay_sends_one_message(self):
        self.state.save_settings({'iptv': {'api_key': 'synthetic-provider-key', 'pack': 'all',
                                           'duration_confirmed': True, 'enabled': True}})
        class Supplier:
            def __init__(self): self.calls = []
            def create_account(self, pack, subscription, note):
                self.calls.append((pack, str(subscription), note))
                return {'url': 'http://stream.example/get.php?username=private-user&password=private-password'}
        supplier = Supplier()
        self.state.iptv.client_factory = lambda _key: supplier
        chat_ref = self.state.chat.overview('default')['items'][0]['chat_ref']
        route = '/api/chat/sessions/default/iptv/manual-card'
        payload = {'chat_ref': chat_ref, 'subscription': '6', 'client_request_id': 'manual-concurrent'}
        self.state.client.block_send = True
        results = []
        first = threading.Thread(target=lambda: results.append(self.request(route, True, 'POST', payload, True)))
        second = threading.Thread(target=lambda: results.append(self.request(route, True, 'POST', payload, True)))
        first.start()
        self.assertTrue(self.state.client.send_started.wait(2))
        second.start()
        second.join(0.2)
        self.assertTrue(second.is_alive())
        self.state.client.release_send.set()
        first.join(3)
        second.join(3)
        self.assertEqual([item[0] for item in results], [200, 200])
        self.assertEqual(len(supplier.calls), 1)
        self.assertEqual(len(self.state.client.sent), 1)

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

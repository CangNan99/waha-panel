import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from panel.app import init_db
try:
    from panel.monitoring_service import MonitoringService
except ImportError:
    MonitoringService = None

NOW = 1_792_022_400


class Client:
    health_failed = False
    sessions_failed = False
    calls = 0
    def get_health(self):
        self.calls += 1
        if self.health_failed:
            raise RuntimeError("synthetic health failure")
        return {"status": "ok"}
    def get_version(self):
        return {"version": "test-version"}
    def get_sessions(self):
        if self.sessions_failed:
            raise RuntimeError("synthetic session failure")
        return [{"name": "default", "status": "WORKING"}, {"name": "support", "status": "SCAN_QR_CODE"},
                {"name": "unmanaged", "status": "FAILED"}]


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(MonitoringService, "the read-only monitoring implementation is missing")
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / 'monitor.sqlite3'
        init_db(self.database, seed_business=False)
        self.client = Client()
        with closing(sqlite3.connect(self.database)) as connection, connection:
            for name in ('default', 'support'):
                connection.execute('INSERT OR IGNORE INTO managed_sessions(session_name,display_name,created_at,updated_at,is_active) VALUES (?,?,?,?,1)', (name, '测试会话', NOW, NOW))
            connection.executemany('INSERT OR REPLACE INTO session_settings(session_name,key,value,updated_at) VALUES (?,?,?,?)',
                [('default','auto_reply_all_day','1',NOW),('default','auto_reply_enabled','0',NOW),
                 ('default','ai_base_url','https://example.invalid',NOW),('default','ai_model','test',NOW),
                 ('default','ai_api_key','fixture-secret',NOW),('default','customer_memory_enabled','1',NOW)])
            connection.execute('DELETE FROM system_logs')
            connection.executemany('INSERT INTO system_logs(session_name,level,event,message,created_at) VALUES (?,?,?,?,?)', [
                ('default','INFO','session.start','测试启动',NOW-30),
                ('support','ERROR','session.failure','客服异常',NOW-20),
                ('default','ERROR','session.failure','边界异常',NOW-86400),
                ('default','ERROR','session.failure','过期异常',NOW-86401),
                ('default','WARNING','config.warning','配置提醒',NOW-10),
                ('default','INFO','test.literal','比例 50%_测试',NOW-5),
                ('default','ERROR','test.private','api_key=fixture-secret phone=60123456789 body=private conversation',NOW),
            ])
            for n,(session,state,due) in enumerate([
                ('default','PENDING',NOW-1),('default','PENDING',NOW+100),('default','RUNNING',NOW-1),
                ('default','FAILED',NOW-1),('default','UNKNOWN',NOW-1),('support','FAILED',NOW-1),
            ]):
                connection.execute('INSERT INTO follow_up_tasks(session_name,chat_key_hmac,chat_id_ciphertext,created_at,due_at,updated_at,delay_code,mode,state,client_request_id) VALUES (?,?,?,?,?,?,?,?,?,?)', (session,'test-hmac','synthetic',NOW,due,NOW,'24h','AI',state,f'request-{n}'))
            connection.executemany('INSERT INTO chat_takeovers(session_name,chat_key_hmac,state,paused_at,updated_at,auto_resume_at) VALUES (?,?,?,?,?,?)',[
                ('default','active','HUMAN_TAKEOVER',NOW-20,NOW,NOW+10),
                ('default','expired','HUMAN_TAKEOVER',NOW-20,NOW,NOW-1),
                ('default','indefinite','HUMAN_TAKEOVER',NOW-20,NOW,None),
            ])
        self.state = SimpleNamespace(database_path=self.database, client=self.client, api_key='fixture-secret')
        self.service = MonitoringService(self.state, clock=lambda: NOW)

    def tearDown(self):
        if hasattr(self,'temp'):
            self.temp.cleanup()

    def test_overview_counts_only_allowed_managed_sessions(self):
        result=self.service.overview(['default','support'],now=NOW)
        self.assertEqual(result['session_counts'],{'total':2,'connected':1,'scan_qr':1,'failed':0})
        self.assertEqual({x['name'] for x in result['sessions']},{'default','support'})
        self.assertEqual(result['recent_error_count'],3)
        self.assertEqual(result['sessions'][0]['auto_reply']['mode'],'all_day')
        self.assertTrue(result['sessions'][0]['auto_reply']['customer_memory_enabled'])

    def test_takeover_expiry_and_automation_states_are_separate(self):
        result=self.service.overview(['default','support'],'default',NOW)
        self.assertEqual(result['automation_counts'],{'human_takeover':2,'pending':2,'overdue':1,'running':1,'failed':1,'unknown':1})
        self.assertEqual(result['recent_error_count'],2)
        self.assertNotIn('test-hmac',json.dumps(result))

    def test_version_success_does_not_hide_health_failure(self):
        self.client.health_failed=True
        result=self.service.overview(['default'],now=NOW)
        self.assertFalse(result['services']['waha']['health']['ok'])
        self.assertTrue(result['services']['waha']['version_check']['ok'])
        self.assertEqual(result['services']['waha']['state'],'degraded')

    def test_failed_remote_session_query_returns_unknown_counts(self):
        self.client.sessions_failed=True
        result=self.service.overview(['default'],now=NOW)
        self.assertIsNone(result['session_counts']['connected'])
        self.assertEqual(result['sessions'][0]['state'],'UNKNOWN')

    def test_read_paths_do_not_seed_missing_settings_or_modify_database(self):
        before=hashlib.sha256(self.database.read_bytes()).digest()
        self.service.overview(['default','support'],now=NOW)
        self.service.query_logs(['default'],{'from':NOW-86400,'to':NOW})
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).digest(),before)

    def test_missing_database_is_not_created_and_counts_are_unavailable(self):
        absent=Path(self.temp.name)/'missing.sqlite3'
        service=MonitoringService(SimpleNamespace(database_path=absent,client=self.client,api_key=''),clock=lambda:NOW)
        result=service.overview(['default'],now=NOW)
        self.assertFalse(result['services']['database']['ok'])
        self.assertIsNone(result['automation_counts']['pending'])
        self.assertFalse(absent.exists())

    def test_shared_waha_checks_are_reused(self):
        self.service.overview(['default'],now=NOW)
        self.service.overview(['support'],now=NOW)
        self.assertEqual(self.client.calls,1)

    def test_cursor_pagination_is_stable_when_new_logs_arrive(self):
        first=self.service.query_logs(['default'],{'limit':2,'from':NOW-86400,'to':NOW})
        self.assertEqual([x['id'] for x in first['logs']],[7,6])
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('INSERT INTO system_logs(session_name,level,event,message,created_at) VALUES (?,?,?,?,?)',('default','INFO','test.new','新日志',NOW))
        second=self.service.query_logs(['default'],{'limit':2,'before_id':first['next_cursor'],'from':NOW-86400,'to':NOW})
        self.assertEqual([x['id'] for x in second['logs']],[5,3])

    def test_literal_search_and_session_isolation(self):
        result=self.service.query_logs(['default'],{'q':'%_','from':NOW-86400,'to':NOW})
        self.assertEqual([x['id'] for x in result['logs']],[6])
        errors=self.service.query_logs(['default'],{'level':'ERROR','from':NOW-86400,'to':NOW})
        self.assertEqual([x['id'] for x in errors['logs']],[7,3])

    def test_private_values_are_removed_from_read_and_search(self):
        result=self.service.query_logs(['default'],{'from':NOW-86400,'to':NOW})
        text=json.dumps(result,ensure_ascii=False)
        for private in ('fixture-secret','60123456789','private conversation'):
            self.assertNotIn(private,text)
            self.assertEqual(self.service.query_logs(['default'],{'q':private})['logs'],[])

    def test_structured_and_quoted_credentials_never_reach_read_or_search(self):
        messages = [
            '{"Cookie":"session=synthetic-cookie-value"}',
            '{"Authorization":"synthetic-credential"}',
            '{"nested":[{"password":"two secret words"}]}',
            'upstream "Set-Cookie": "session=synthetic-other-cookie; Path=/"',
            "password='many synthetic words' result=failed",
        ]
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.executemany('INSERT INTO system_logs(session_name,level,event,message,created_at) VALUES (?,?,?,?,?)',
                [('default','ERROR','test.structured',text,NOW) for text in messages])
        result=self.service.query_logs(['default'],{'event':'test.structured'})
        rendered=json.dumps(result,ensure_ascii=False)
        for private in ('synthetic-cookie-value','synthetic-credential','secret words','synthetic-other-cookie','synthetic words'):
            with self.subTest(private=private):
                self.assertNotIn(private,rendered)
                self.assertEqual(self.service.query_logs(['default'],{'q':private})['logs'],[])

    def test_incremental_log_cursor_catches_up_without_skipping(self):
        first=self.service.query_logs(['default'],{'after_id':1,'limit':2})
        self.assertEqual([x['id'] for x in first['logs']],[5,3])
        second=self.service.query_logs(['default'],{'after_id':first['next_cursor'],'limit':2})
        self.assertEqual([x['id'] for x in second['logs']],[7,6])

    def test_invalid_filters_cannot_expand_scope(self):
        for filters in ({'session':'support'},{'limit':101},{'limit':0},{'level':'SELECT'},{'before_id':1,'after_id':2},
                        {'from':NOW-2592001,'to':NOW},{'from':'NaN'},{'q':'x'*101},{'from':NOW,'to':NOW-1}):
            with self.subTest(filters=filters),self.assertRaises((ValueError,PermissionError)):
                self.service.query_logs(['default'],filters)
        with self.assertRaises(PermissionError):
            self.service.overview(['default'],'support',NOW)


if __name__=='__main__':
    unittest.main()

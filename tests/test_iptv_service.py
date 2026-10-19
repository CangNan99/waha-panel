"""Trial side effects use a fake supplier; SQLite and encryption are real."""
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from cryptography.fernet import Fernet
from panel.chat_security import DataCipher
from panel.iptv_service import ActivationClient, IptvError, IptvService, explicit_trial_request


ACCOUNT = {'status': 'true', 'user_id': '100', 'url':
           'http://stream.example/get.php?username=trial-user&password=trial-pass&type=m3u_plus&output=ts'}


class Response(io.BytesIO):
    status = 200


class Supplier:
    def __init__(self):
        self.calls = []
        self.fail = None
        self.entered = None
        self.release = None

    def create_trial(self, pack, note):
        self.calls.append((pack, note))
        if self.entered:
            self.entered.set()
            self.release.wait(4)
        if self.fail:
            raise self.fail
        return {'url': ACCOUNT['url'], 'username': 'trial-user', 'password': 'trial-pass',
                'server': 'http://stream.example', 'user_id': '100'}

    def check(self):
        return {'account_enabled': True, 'packages': []}


class ActivationClientTests(unittest.TestCase):
    def test_creation_is_one_fixed_demo_request(self):
        requests = []
        def transport(request, timeout):
            requests.append((request, timeout))
            return Response(json.dumps([ACCOUNT]).encode())
        client = ActivationClient('synthetic-reseller-key', transport=transport)
        account = client.create_trial('all', 'opaque-validation-id')
        query = parse_qs(urlparse(requests[0][0].full_url).query)
        self.assertEqual(query, {'action': ['new'], 'type': ['m3u'], 'sub': ['99'], 'pack': ['all'],
                                 'note': ['opaque-validation-id'], 'api_key': ['synthetic-reseller-key']})
        self.assertEqual(urlparse(requests[0][0].full_url).hostname, 'activationpanel.ru')
        self.assertEqual(account['username'], 'trial-user')
        self.assertEqual(account['server'], 'http://stream.example')
        self.assertEqual(len(requests), 1)

    def test_timeout_is_unknown_and_does_not_retry_or_expose_key(self):
        calls = []
        def transport(request, timeout):
            calls.append(request)
            raise TimeoutError(request.full_url)
        with self.assertRaises(IptvError) as caught:
            ActivationClient('synthetic-reseller-key', transport=transport).create_trial('all', 'id')
        self.assertTrue(caught.exception.uncertain)
        self.assertNotIn('synthetic-reseller-key', str(caught.exception))
        self.assertEqual(len(calls), 1)

    def test_success_without_valid_credentials_is_unknown(self):
        for value in ({'status': 'true', 'url': 'http://127.0.0.1/get.php'},
                      {'status': 'true', 'url': 'javascript:bad'},
                      {'status': 'true', 'url': 'http://stream.example/get.php?username=a&username=b&password=c'}):
            with self.subTest(value=value), self.assertRaises(IptvError) as caught:
                ActivationClient('key', transport=lambda *_args, **_kw: Response(json.dumps(value).encode())).create_trial('all', 'id')
            self.assertTrue(caught.exception.uncertain)

    def test_explicit_supplier_error_is_safe_and_definitive(self):
        with self.assertRaises(IptvError) as caught:
            ActivationClient('key', transport=lambda *_args, **_kw: Response(b'{"status":"error","result":"private diagnostic"}')).create_trial('all', 'id')
        self.assertFalse(caught.exception.uncertain)
        self.assertNotIn('private diagnostic', str(caught.exception))

    def test_read_check_accepts_empty_custom_bouquet(self):
        def transport(request, timeout):
            if parse_qs(urlparse(request.full_url).query)['action'] == ['reseller_info']:
                return Response(b'[{"status":"true","enabled":"1","username":"private","credits":"10"}]')
            return Response(b'{"status":"error","result":"Empty custom bouquet"}')
        self.assertEqual(ActivationClient('key', transport=transport).check(), {'account_enabled': True, 'packages': []})

    def test_request_intent_requires_a_current_explicit_request(self):
        for text in ('我想测试 IPTV', 'Can I get an IPTV trial?', 'Quero um teste IPTV',
                     'quiero probar IPTV', 'Je voudrais un test IPTV', 'أريد تجربة IPTV'):
            self.assertTrue(explicit_trial_request(text), text)
        for text in ('IPTV是什么', 'IPTV试用多少钱', 'IPTV trial price?', '不要给我开通IPTV试用',
                     'I do not want an IPTV trial', 'hello', 'My IPTV is not working'):
            self.assertFalse(explicit_trial_request(text), text)


class IptvServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'trials.sqlite3'
        self.cipher = DataCipher(Fernet.generate_key().decode())
        self.supplier = Supplier()
        self.now = 1800000000
        self.service = IptvService(self.path, self.cipher, b'h' * 32,
                                   clock=lambda: self.now, client_factory=lambda _key: self.supplier)
        self.service.save_settings('sales', {'api_key': 'synthetic-reseller-key', 'pack': 'all',
                    'daily_limit': 2, 'duration_confirmed': True, 'enabled': True})
        self.service.save_settings('support', {'pack': 'all', 'enabled': True})

    def create(self, customer='123456789@c.us', session='sales', allowed=lambda: True):
        return self.service.create_trial(session, customer, 'event-1', allowed=allowed)

    def rows(self):
        with closing(sqlite3.connect(self.path)) as c:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute('SELECT * FROM iptv_trials')]

    def test_encrypted_configuration_and_public_settings(self):
        public = self.service.settings_payload('sales')
        self.assertTrue(public['api_key_configured'])
        self.assertTrue(public['enabled'])
        self.assertEqual(public['sub'], 99)
        self.assertNotIn('synthetic-reseller-key', json.dumps(public))
        with closing(sqlite3.connect(self.path)) as c:
            raw = c.execute('SELECT config_ciphertext FROM iptv_provider_settings').fetchone()[0]
        self.assertNotIn('synthetic-reseller-key', raw)
        self.assertEqual(self.cipher.decrypt_json(raw)['api_key'], 'synthetic-reseller-key')

    def test_same_customer_across_sessions_reuses_encrypted_account(self):
        first = self.create()
        second = self.create('123456789@s.whatsapp.net', 'support')
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(second['account']['username'], 'trial-user')
        self.assertEqual(len(self.supplier.calls), 1)
        row = self.rows()[0]
        self.assertNotIn('123456789', json.dumps(row))
        self.assertNotIn('trial-pass', json.dumps(row))
        self.assertEqual(row['expires_at'], 1800043200)
        self.assertEqual(self.cipher.decrypt_json(row['credentials_ciphertext'])['password'], 'trial-pass')

    def test_unknown_creation_is_durable_and_never_retried(self):
        self.supplier.fail = IptvError('upstream_unknown', '供应商响应不确定', uncertain=True)
        with self.assertRaises(IptvError): self.create()
        self.supplier.fail = None
        reopened = IptvService(self.path, self.cipher, b'h' * 32,
                            clock=lambda: self.now, client_factory=lambda _key: self.supplier)
        with self.assertRaises(IptvError): reopened.create_trial('sales', '123456789@c.us', 'event-2')
        self.assertEqual(len(self.supplier.calls), 1)
        self.assertEqual(self.rows()[0]['state'], 'unknown')

    def test_expired_trial_does_not_create_another(self):
        first = self.create()
        for elapsed in (43201, 86400, 5 * 365 * 86400):
            with self.subTest(elapsed=elapsed):
                self.now = 1800000000 + elapsed
                reopened = IptvService(self.path, self.cipher, b'h' * 32,
                    clock=lambda: self.now, client_factory=lambda _key: self.supplier)
                reopened.save_settings('support', {'enabled': False})
                reopened.save_settings('support', {'enabled': True, 'api_key': 'replacement-reseller-key'})
                with self.assertRaises(IptvError) as caught:
                    reopened.create_trial('support', '123456789@s.whatsapp.net', 'new-event-' + str(elapsed))
                self.assertEqual(caught.exception.code, 'trial_expired')
                self.assertEqual(self.rows()[0]['id'], first['id'])
        self.assertEqual(len(self.supplier.calls), 1)
        self.assertEqual(len(self.rows()), 1)

    def test_unverified_lid_cannot_allocate_a_trial(self):
        with self.assertRaises(IptvError) as caught:
            self.create('987654321@lid')
        self.assertEqual(caught.exception.code, 'customer_unverified')
        self.assertEqual(self.supplier.calls, [])
        self.assertEqual(self.rows(), [])

    def test_daily_limit_is_global_across_sessions(self):
        self.create('111111111@c.us')
        self.create('222222222@c.us', 'support')
        with self.assertRaises(IptvError) as caught: self.create('333333333@c.us')
        self.assertEqual(caught.exception.code, 'daily_limit')
        self.assertEqual(len(self.supplier.calls), 2)

    def test_cancelled_policy_prevents_creation(self):
        with self.assertRaises(IptvError) as caught: self.create(allowed=lambda: False)
        self.assertEqual(caught.exception.code, 'cancelled')
        self.assertEqual(self.supplier.calls, [])

    def test_cancel_after_reservation_can_be_reclaimed_by_a_fresh_request(self):
        checks = iter([True, False])
        with self.assertRaises(IptvError): self.create(allowed=lambda: next(checks))
        self.assertEqual(self.rows()[0]['state'], 'cancelled')
        self.assertEqual(self.supplier.calls, [])
        result = self.service.create_trial('support', '123456789@c.us', 'fresh-event')
        self.assertEqual(result['account']['username'], 'trial-user')
        self.assertEqual(len(self.supplier.calls), 1)

    def test_concurrent_reclaim_after_cancel_still_has_one_creator(self):
        checks = iter([True, False])
        with self.assertRaises(IptvError): self.create(allowed=lambda: next(checks))
        self.supplier.entered, self.supplier.release = threading.Event(), threading.Event()
        completed = []
        def restore(): completed.append(self.service.create_trial('sales', '123456789@c.us', 'fresh-one'))
        thread = threading.Thread(target=restore); thread.start()
        try:
            self.assertTrue(self.supplier.entered.wait(2))
            with self.assertRaises(IptvError): self.service.create_trial('support', '123456789@c.us', 'fresh-two')
        finally:
            self.supplier.release.set(); thread.join(4)
        self.assertEqual(len(completed), 1)
        self.assertEqual(len(self.supplier.calls), 1)

    def test_disabling_a_session_or_unconfirmed_duration_blocks_creation(self):
        self.service.save_settings('sales', {'enabled': False})
        with self.assertRaises(IptvError): self.create()
        self.service.save_settings('support', {'enabled': False})
        self.service.save_settings('sales', {'enabled': False, 'duration_confirmed': False})
        with self.assertRaises(IptvError): self.service.save_settings('sales', {'enabled': True})
        self.assertEqual(self.supplier.calls, [])

    def test_pending_creation_is_not_duplicated_by_concurrent_session(self):
        self.supplier.entered = threading.Event()
        self.supplier.release = threading.Event()
        errors = []
        def create():
            try: self.create()
            except Exception as error: errors.append(error)
        thread = threading.Thread(target=create)
        thread.start()
        self.assertTrue(self.supplier.entered.wait(3))
        try:
            with self.assertRaises(IptvError): self.create(session='support')
        finally:
            self.supplier.release.set()
            thread.join(4)
        self.assertEqual(errors, [])
        self.assertEqual(len(self.supplier.calls), 1)

    def test_delivery_failure_reuses_created_result_and_public_records_are_safe(self):
        result = self.create()
        self.service.mark_delivery(result['id'], False)
        reused = self.create(session='support')
        self.service.mark_delivery(reused['id'], True)
        self.assertEqual(len(self.supplier.calls), 1)
        public = json.dumps(self.service.records('sales'))
        self.assertNotIn('trial-user', public)
        self.assertNotIn('trial-pass', public)
        self.assertNotIn('123456789', public)
        self.assertEqual(self.rows()[0]['state'], 'sent')

    def test_context_redacts_known_credentials_but_send_template_keeps_them(self):
        result = self.create()
        message = self.service.reply_text(result, 'en')
        self.assertIn('trial-user', message)
        self.assertIn('trial-pass', message)
        self.assertIn('12 hours', message)
        safe = self.service.sanitize_context('123456789@c.us', message)
        self.assertNotIn('trial-user', safe)
        self.assertNotIn('trial-pass', safe)
        self.assertNotIn(ACCOUNT['url'], safe)

    def test_clear_key_requires_disabling_and_cannot_leave_other_session_enabled(self):
        with self.assertRaises(IptvError): self.service.save_settings('sales', {'clear_api_key': True})
        self.assertTrue(self.service.settings_payload('sales')['api_key_configured'])


if __name__ == '__main__': unittest.main()

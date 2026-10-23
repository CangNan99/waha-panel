"""The real webhook gates a fake supplier and fake WAHA transport."""
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from cryptography.fernet import Fernet
from panel.app import PanelState, init_db
from panel.iptv_service import IptvError


class Client:
    def __init__(self): self.sent = []
    def get_sessions(self): return [{'name': 'default', 'status': 'WORKING'}, {'name': 'sales', 'status': 'WORKING'}]
    def get_messages(self, *args, **kwargs): return []
    def send_text(self, session, chat, text):
        self.sent.append((session, chat, text))
        return {'id': 'out-' + str(len(self.sent))}


class Supplier:
    def __init__(self): self.calls = []; self.during = None
    def create_trial(self, pack, note):
        self.calls.append((pack, note))
        if self.during: self.during()
        return {'url': 'http://stream.example/get.php?username=only-this-customer&password=private-trial-pass&type=m3u_plus', 'user_id': '100'}
    def check(self): return {'account_enabled': True, 'packages': []}


class IptvIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.now = 1800000000
        timer = patch('panel.app.time.time', side_effect=lambda: self.now); timer.start(); self.addCleanup(timer.stop)
        self.db = Path(self.temp.name) / 'panel.sqlite3'
        init_db(self.db, seed_business=False)
        self.client, self.supplier = Client(), Supplier()
        self.state = PanelState(self.db, self.client, 'waha-fixture-key', sleep_fn=lambda _: None,
                                data_encryption_key=Fernet.generate_key().decode())
        self.addCleanup(self.state.stop_background_services)
        self.now += 10
        self.state.save_settings({'auto_reply_all_day': True, 'iptv': {
            'api_key': 'synthetic-reseller-key', 'pack': 'all', 'duration_confirmed': True, 'enabled': True}})
        self.now += 2
        self.state.iptv.client_factory = lambda _key: self.supplier

    def event(self, body='Can I get an IPTV trial?', identity='customer@c.us', message_id='event-1', timestamp=None, session='default'):
        return json.dumps({'event': 'message', 'session': session, 'payload': {
            'id': message_id, 'from': identity, 'body': body, 'type': 'chat',
            'timestamp': self.now - 1 if timestamp is None else timestamp}}).encode()

    def run_event(self, *args, **kwargs):
        dispatch = kwargs.pop('dispatch', True)
        with patch.object(self.state, '_ai_reply', return_value=(json.dumps({'language': 'en'}), 'iptv_trial_tool')):
            return self.state.handle_webhook(self.event(*args, **kwargs), dispatch=dispatch)

    def test_trial_tool_creates_and_sends_to_the_triggering_customer(self):
        result = self.run_event()
        self.assertEqual(result['action'], 'replied')
        self.assertEqual(result['mode'], 'iptv_trial')
        self.assertEqual(self.client.sent[0][:2], ('default', 'customer@c.us'))
        self.assertIn('private-trial-pass', self.client.sent[0][2])
        self.assertNotIn('private-trial-pass', json.dumps(result))
        self.assertEqual(len(self.supplier.calls), 1)

    def test_duplicate_webhook_is_ignored_and_fresh_request_reuses_account(self):
        self.run_event()
        self.assertEqual(self.run_event()['action'], 'ignored')
        self.run_event(message_id='event-2')
        self.assertEqual(len(self.supplier.calls), 1)
        self.assertEqual(len(self.client.sent), 2)

    def system_labels(self):
        with closing(sqlite3.connect(self.db)) as c:
            return c.execute('SELECT session_name,label FROM customer_system_labels ORDER BY session_name').fetchall()

    def test_successful_trial_adds_system_label_after_delivery(self):
        events = []
        mark = self.state.iptv.mark_delivery
        label = self.state.chat.add_system_label_by_chat_id
        def mark_delivery(trial_id, success):
            events.append('delivered')
            return mark(trial_id, success)
        def add_label(*args):
            events.append('labelled')
            return label(*args)
        with patch.object(self.state.iptv, 'mark_delivery', side_effect=mark_delivery), patch.object(self.state.chat, 'add_system_label_by_chat_id', side_effect=add_label):
            self.assertEqual(self.run_event()['action'], 'replied')
        self.assertEqual(events, ['delivered', 'labelled'])
        self.assertEqual(self.system_labels(), [('default', 'IPTV测试过')])

    def test_delivery_failure_and_unknown_creation_do_not_add_system_label(self):
        with patch.object(self.state, '_send_text', side_effect=TimeoutError()):
            self.assertEqual(self.run_event()['action'], 'ignored')
        self.assertEqual(self.system_labels(), [])
        with patch.object(self.supplier, 'create_trial', side_effect=IptvError('upstream_unknown', '结果未知', uncertain=True)):
            self.assertEqual(self.run_event(identity='other@c.us', message_id='event-other')['action'], 'ignored')
        self.assertEqual(self.system_labels(), [])

    def test_separate_chat_window_trial_adds_current_session_label(self):
        self.assertEqual(self.run_event()['action'], 'replied')
        self.state.save_settings({'auto_reply_enabled': True, 'auto_reply_all_day': True, 'iptv': {'enabled': True}}, 'sales')
        self.now += 2
        self.assertEqual(self.run_event(session='sales', message_id='event-sales')['action'], 'replied')
        self.assertEqual(len(self.supplier.calls), 2)
        self.assertEqual(self.system_labels(), [('default', 'IPTV测试过'), ('sales', 'IPTV测试过')])

    def test_repeated_success_is_idempotent(self):
        self.run_event()
        self.run_event(message_id='event-2')
        self.assertEqual(self.system_labels(), [('default', 'IPTV测试过')])

    def test_system_label_failure_preserves_successful_delivery_and_redacts_error(self):
        with patch.object(self.state.chat, 'add_system_label_by_chat_id', side_effect=RuntimeError('password=private-trial-pass')):
            self.assertEqual(self.run_event()['action'], 'replied')
        self.assertEqual(self.state.iptv.records('default')['items'][0]['state'], 'sent')
        with closing(sqlite3.connect(self.db)) as c:
            logs = '\n'.join(r[0] for r in c.execute('SELECT message FROM system_logs'))
        self.assertNotIn('private-trial-pass', logs)

    def test_credentials_do_not_reach_archived_context_or_memory_input(self):
        self.run_event()
        with closing(sqlite3.connect(self.db)) as c:
            bodies = '\n'.join(r[0] for r in c.execute('SELECT content FROM conversation_messages'))
        self.assertNotIn('private-trial-pass', bodies)
        self.assertNotIn('only-this-customer', bodies)
        self.assertNotIn('private-trial-pass', self.state._conversation_context('customer@c.us'))
        # A later WAHA history backfill must apply the same credential filter.
        self.state._archive_message('default', 'customer@c.us', 'outbound', 'history-copy', self.client.sent[0][2])
        self.assertNotIn('private-trial-pass', self.state._conversation_context('customer@c.us'))

    def test_unrelated_or_price_question_cannot_allocate_even_with_a_bad_tool_call(self):
        for index, text in enumerate(('hello', 'IPTV trial price?', 'I do not want an IPTV trial')):
            self.assertEqual(self.run_event(text, message_id=str(index))['action'], 'ignored')
        self.assertEqual(self.supplier.calls, [])
        self.assertEqual(self.client.sent, [])

    def test_history_and_human_takeover_never_create(self):
        self.assertEqual(self.run_event(timestamp=self.now-100)['action'], 'ignored')
        with patch.object(self.state.chat, 'is_human_takeover', return_value=True):
            self.assertEqual(self.run_event(message_id='event-2')['action'], 'ignored')
        self.assertEqual(self.supplier.calls, [])

    def test_preview_does_not_create_an_external_account(self):
        self.assertEqual(self.run_event(dispatch=False)['action'], 'ignored')
        self.assertEqual(self.supplier.calls, [])

    def test_policy_changes_during_creation_keep_account_but_cancel_send(self):
        self.supplier.during = lambda: self.state.save_settings({'auto_reply_all_day': False, 'auto_reply_enabled': False})
        self.assertEqual(self.run_event()['action'], 'ignored')
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.state.iptv.records('default')['items'][0]['state'], 'created')

    def test_disable_then_reenable_iptv_does_not_revive_inflight_result(self):
        def switch():
            self.state.save_settings({'iptv': {'enabled': False}})
            self.state.save_settings({'iptv': {'enabled': True}})
        self.supplier.during = switch
        self.assertEqual(self.run_event()['action'], 'ignored')
        self.assertEqual(self.client.sent, [])

    def test_send_failure_retains_account_and_logs_no_credentials(self):
        with patch.object(self.state, '_send_text', side_effect=RuntimeError('password=private-trial-pass')):
            self.assertEqual(self.run_event()['action'], 'ignored')
        self.supplier.during = None
        self.run_event(message_id='event-2')
        self.assertEqual(len(self.supplier.calls), 1)
        self.assertEqual(len(self.client.sent), 1)
        with closing(sqlite3.connect(self.db)) as c:
            logs = '\n'.join(r[0] for r in c.execute('SELECT message FROM system_logs'))
        self.assertNotIn('private-trial-pass', logs)

    def test_invalid_iptv_settings_do_not_partially_save_other_fields(self):
        with self.assertRaises(ValueError): self.state.save_settings({'persona': 'must-not-save', 'iptv': {'pack': 'bad'}})
        self.assertNotEqual(self.state.settings_payload()['persona'], 'must-not-save')
        public = json.dumps(self.state.settings_payload())
        self.assertNotIn('synthetic-reseller-key', public)
        self.assertTrue(self.state.settings_payload()['iptv']['api_key_configured'])

    def test_real_ai_tool_protocol_works_without_commerce_and_rejects_extra_arguments(self):
        self.state.save_settings({'ai_base_url': 'https://ai.example/v1', 'ai_model': 'fixture', 'ai_api_key': 'fake-ai-key'})
        settings = self.state._settings()
        captured = []
        arguments = {'language': 'pt'}
        def transport(request, timeout):
            captured.append(json.loads(request.data))
            return io.BytesIO(json.dumps({'choices': [{'finish_reason': 'tool_calls', 'message': {
                'tool_calls': [{'function': {'name': 'create_iptv_trial', 'arguments': json.dumps(arguments)}}]}}]}).encode())
        with patch('panel.app.urlopen', side_effect=transport):
            result, mode = self.state._ai_reply(settings, 'Quero um teste IPTV', include_tools=True)
        self.assertEqual(mode, 'iptv_trial_tool')
        self.assertEqual(json.loads(result), {'language': 'pt'})
        tool = next(t for t in captured[0]['tools'] if t['function']['name'] == 'create_iptv_trial')
        self.assertEqual(set(tool['function']['parameters']['properties']), {'language'})
        self.assertNotIn('synthetic-reseller-key', json.dumps(captured[0]))
        arguments['sub'] = 12
        with patch('panel.app.urlopen', side_effect=transport):
            self.assertEqual(self.state._ai_reply(settings, 'test IPTV please', include_tools=True)[1], 'ai_invalid_tool')

    def test_all_chat_ai_entrypoints_filter_raw_waha_credentials_at_transport(self):
        self.run_event()
        raw = self.client.sent[0][2]
        history = [{'from_me': False, 'body': 'Can I get an IPTV trial?'}, {'from_me': True, 'body': raw}]
        service = self.state.translation
        service.resolver = lambda *args, **kwargs: [(None, None, None, None, ('93.184.216.34', 0))]
        service.save_settings({'base_url': 'https://ai.example.com', 'model': 'fixture', 'api_key': 'translation-fixture'})
        captured = []
        class ProbeStop(RuntimeError): pass
        def transport(request, timeout):
            captured.append(json.loads(request.data))
            raise ProbeStop()
        service.opener = transport
        calls = [lambda: service.summarize_conversation(history),
                 lambda: service.suggest_reply(history, '', '', []),
                 lambda: service.optimize_and_translate('请继续测试', history),
                 lambda: service.generate_follow_up(history),
                 lambda: service.translate_follow_up('请继续测试', history),
                 lambda: service.translate(raw, force=True)]
        for operation in calls:
            captured.clear()
            with self.assertRaises(ProbeStop): operation()
            self.assertEqual(len(captured), 1)
            body = json.dumps(captured[0], ensure_ascii=False)
            self.assertNotIn('private-trial-pass', body)
            self.assertNotIn('only-this-customer', body)
            self.assertNotIn(raw, body)
        self.state.save_settings({'ai_base_url': 'https://ai.example/v1', 'ai_model': 'fixture', 'ai_api_key': 'fake-ai-key'})
        for operation in [lambda: self.state._ai_reply(self.state._settings(), raw, conversation_context=raw),
                          lambda: self.state._customer_memory_completion(self.state._settings(), '整理摘要', raw)]:
            captured.clear()
            with patch('panel.app.urlopen', side_effect=transport):
                try: operation()
                except ProbeStop: pass
            self.assertEqual(len(captured), 1)
            self.assertNotIn('private-trial-pass', json.dumps(captured[0]))
            self.assertNotIn('only-this-customer', json.dumps(captured[0]))

    def test_machine_translation_filters_trial_credentials_at_sdk_boundary(self):
        self.run_event()
        captured = []
        class SDK:
            def translate_with_options(self, request, runtime):
                captured.append(request.source_text)
                return {'body': {'Code': 200, 'Data': {'Translated': '试用信息', 'DetectedLanguage': 'en'}}}
        service = self.state.aliyun_translation
        service.sdk_client_factory = lambda settings: SDK()
        service.save_settings({'access_key_id': 'fixture-id', 'access_key_secret': 'fixture-secret'})
        result = service.translate_batch('default', 'a' * 64,
                  [{'message_ref': 'trial-message', 'text': self.client.sent[0][2]}])
        self.assertEqual(result['items'][0]['status'], 'READY')
        self.assertEqual(len(captured), 1)
        self.assertNotIn('private-trial-pass', captured[0])
        self.assertNotIn('only-this-customer', captured[0])


if __name__ == '__main__': unittest.main()

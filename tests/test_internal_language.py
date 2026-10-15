import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from panel.app import init_db
from panel.chat_service import ChatService, ChatServiceError
from panel.translation_service import TranslationService, TranslationError
from test_chat_engagement import _Codec, _Client, _FakeAIOpener
try:
    from panel.internal_language import validate_internal_chinese, InternalLanguageError
except ImportError:
    validate_internal_chinese = None
    InternalLanguageError = ValueError

VALID_SUMMARY={key:'未提及' for key in ('summary','customer_need','intent','confirmed_items','unresolved_items','next_action')}


class InternalLanguageTests(unittest.TestCase):
    def test_chinese_descriptions_keep_product_codes_links_and_named_entities(self):
        self.assertIsNotNone(validate_internal_chinese)
        text='客户姓名 John，关注 iPhone 16 Pro，预算 800 USD，型号 AB-123，链接 https://example.invalid/p?id=42'
        self.assertEqual(validate_internal_chinese(text,'note'),text)

    def test_one_han_character_does_not_validate_foreign_sentences(self):
        self.assertIsNotNone(validate_internal_chinese)
        for text in ('Please send a quote','中文 Please send a quote','中文 Hola necesito precio','中文 مرحبا كيف حالك','中文 Hello','中文 日本語です','中文 PendingQuote123'):
            with self.subTest(text=text),self.assertRaises(InternalLanguageError):
                validate_internal_chinese(text,'note')

    def test_empty_note_is_allowed_but_empty_or_english_ai_labels_are_not(self):
        self.assertIsNotNone(validate_internal_chinese)
        self.assertEqual(validate_internal_chinese('','note'),'')
        for text in ('','warm lead','iPhone 16'):
            with self.subTest(text=text),self.assertRaises(InternalLanguageError):validate_internal_chinese(text,'label')
        self.assertEqual(validate_internal_chinese('高意向','label'),'高意向')

    def test_explicitly_named_foreign_names_can_contain_multiple_words(self):
        self.assertIsNotNone(validate_internal_chinese)
        text='客户姓名 John Smith，询问型号 RTX4090，订单 AB-123'
        self.assertEqual(validate_internal_chinese(text,'note'),text)

    def test_named_entity_prefix_cannot_hide_foreign_sentences(self):
        samples=('产品 Please send a quote','中文 品牌 do not contact me','型号 This is a sentence',
                 '姓名 Please Send A Quote','产品 Hola necesito precio','产品 Hello',
                 '产品 Buy Now','品牌 Never Call Again','产品 Interested Customer')
        for text in samples:
            with self.subTest(text=text),self.assertRaises(InternalLanguageError):
                validate_internal_chinese(text,'note')

    def test_frontend_and_backend_agree_on_named_entity_boundaries(self):
        import json
        import shutil
        import subprocess
        from panel.internal_language import frontend_validator_source
        if not shutil.which('node'):
            self.skipTest('Node is needed to execute the actual frontend validator')
        samples=['产品 Please send a quote','中文 品牌 do not contact me','型号 This is a sentence',
                 '产品 Buy Now','品牌 Never Call Again','姓名 Please Send A Quote','客户姓名 John Smith，询问型号 RTX4090，订单 AB-123',
                 '客户关注 iPhone 16 Pro，预算 800 USD']
        expected=[False,False,False,False,False,False,True,True]
        script=frontend_validator_source()+'\nconsole.log(JSON.stringify('+json.dumps(samples,ensure_ascii=False)+'.map(x=>isInternalChinese(x))));'
        result=subprocess.run(['node','--input-type=commonjs'],input=script,text=True,encoding='utf-8',capture_output=True,check=True)
        self.assertEqual(json.loads(result.stdout),expected)


class InternalLanguageServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.database=Path(self.temp.name)/'language.sqlite3'
        init_db(self.database,seed_business=False);self.codec=_Codec()
        self.chat=ChatService(self.database,_Client(),self.codec,b't'*32)
        self.ref=self.chat.overview('default')['items'][0]['chat_ref']
        with closing(sqlite3.connect(self.database)) as connection,connection:
            connection.execute("INSERT INTO translation_settings(id,base_url,model,api_key_ciphertext,key_fingerprint,last_test_status,updated_at) VALUES (1,'https://example.com','model','key','fixture','never',0)")
        self.codec.encrypted['key']={'api_key':'fixture-only'}

    def tearDown(self):self.temp.cleanup()

    def service(self,payloads):
        opener=_FakeAIOpener(payloads)
        service=TranslationService(self.database,self.codec,opener=opener,resolver=lambda *args,**kwargs:[(None,None,None,None,('93.184.216.34',0))])
        return service,opener

    def test_foreign_note_is_rejected_without_overwriting_previous_note(self):
        self.chat.save_note('default',self.ref,'已有中文备注')
        with self.assertRaises(ChatServiceError):self.chat.save_note('default',self.ref,'foreign note')
        self.assertEqual(self.chat.note('default',self.ref)['note'],'已有中文备注')

    def test_non_chinese_ai_labels_cannot_replace_valid_summary(self):
        self.chat.save_summary_and_ai_labels('default',self.ref,{'summary':'有效总结'},['高意向'])
        with self.assertRaises(ChatServiceError):self.chat.save_summary_and_ai_labels('default',self.ref,{'summary':'新的总结'},['warm lead'])
        self.assertEqual(self.chat.current_summary('default',self.ref)['summary'],'有效总结')
        self.assertEqual(self.chat.customer_labels('default',self.ref)['ai'],['高意向'])

    def test_nonlist_or_excess_ai_labels_cannot_replace_valid_summary(self):
        self.chat.save_summary_and_ai_labels('default',self.ref,{'summary':'有效总结'},['高意向'])
        for labels in ({'高意向':'invalid'},['标签'+str(n) for n in range(11)]):
            with self.subTest(labels=labels),self.assertRaises(ChatServiceError):
                self.chat.save_summary_and_ai_labels('default',self.ref,{'summary':'新总结'},labels)
        self.assertEqual(self.chat.current_summary('default',self.ref)['summary'],'有效总结')

    def test_summary_language_repair_is_bounded_to_two_calls(self):
        bad=dict(VALID_SUMMARY,summary='customer needs quote',ai_labels=['warm lead'])
        good=dict(VALID_SUMMARY,summary='客户需要报价',ai_labels=['待报价'])
        service,opener=self.service([bad,good])
        self.assertEqual(service.summarize_conversation([{'body':'Please quote'}]),good)
        self.assertEqual(len(opener.requests),2)
        self.assertIn('简体中文',opener.requests[0]['messages'][0]['content'])

    def test_summary_second_language_failure_does_not_trigger_third_call(self):
        bad=dict(VALID_SUMMARY,summary='中文 English sentence',ai_labels=['warm lead'])
        service,opener=self.service([bad,bad])
        with self.assertRaises(TranslationError):service.summarize_conversation([{'body':'Hello'}])
        self.assertEqual(len(opener.requests),2)

    def test_note_conversion_returns_preview_without_persistence(self):
        service,opener=self.service({'note_zh':'客户希望购买 iPhone 16，预算 800 USD'})
        self.assertTrue(hasattr(service,'normalize_internal_note'),'note preview API is missing')
        result=service.normalize_internal_note('Customer wants iPhone 16, budget 800 USD')
        self.assertEqual(result,'客户希望购买 iPhone 16，预算 800 USD')
        self.assertEqual(self.chat.note('default',self.ref)['note'],'')
        self.assertEqual(len(opener.requests),1)

    def test_note_conversion_repairs_changed_numbers_and_links_once(self):
        service,opener=self.service([{'note_zh':'客户预算 900 USD，链接 https://example.invalid/other'},
                                   {'note_zh':'客户预算 800 USD，链接 https://example.invalid/item'}])
        self.assertTrue(hasattr(service,'normalize_internal_note'))
        result=service.normalize_internal_note('Budget 800 USD, link https://example.invalid/item')
        self.assertIn('800',result);self.assertIn('/item',result);self.assertEqual(len(opener.requests),2)

    def test_note_conversion_rejects_added_numbers_or_links(self):
        service,opener=self.service([{'note_zh':'客户预算 800 USD，新增费用 900 USD'}]*2)
        with self.assertRaises(TranslationError):service.normalize_internal_note('Budget 800 USD')
        self.assertEqual(len(opener.requests),2)


if __name__=='__main__':unittest.main()

import unittest

from panel.customer_intent import classify_purchase_intent


class CustomerIntentTests(unittest.TestCase):
    def test_explicit_purchase_language_gets_purchase_intent(self):
        for text in (
            "我现在想购买并付款",
            "How can I buy this package?",
            "请发购买链接，我要下单",
        ):
            with self.subTest(text=text):
                self.assertEqual(classify_purchase_intent(text), ["购买意向"])

    def test_high_interest_language_gets_high_intent(self):
        self.assertEqual(
            classify_purchase_intent("这个套餐我很感兴趣，怎么买？"),
            ["高意向"],
        )

    def test_general_questions_do_not_create_an_intent_label(self):
        for text in ("你好", "请介绍一下功能", "谢谢，再见", "价格是多少"):
            with self.subTest(text=text):
                self.assertEqual(classify_purchase_intent(text), [])


if __name__ == "__main__":
    unittest.main()

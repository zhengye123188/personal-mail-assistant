import unittest

from maildesk.reply_language import language_context, strip_language_request, wrong_reply_language


class ReplyLanguageTests(unittest.TestCase):
    def test_new_message_outweighs_the_chinese_brief_subject_and_quoted_chain(self):
        body = ("Hi 小王,\nCould you confirm the review meeting for Friday?\n"
                "Best regards,\n技术支持中心\nOn Monday, 王工 wrote:\n周三是否方便评审？")
        result = language_context("请确认评审时间", body, "谢谢对方的邀请。", previous_draft="收到，我会确认。")
        self.assertEqual((result["target"], result["source"]), ("en", "body"))
        self.assertNotIn("技术支持", result["sample"])
        self.assertNotIn("周三", result["sample"])

    def test_quoted_english_and_code_are_excluded_from_chinese_latest_text(self):
        body = ("周五上午十点可以参加。\n> Can you confirm the review?\n"
                "```py\nprint('waiting for your reply')\n```\n--\nSent from my iPhone")
        result = language_context("Re: Friday meeting", body, "Thanks, please confirm")
        self.assertEqual(result["target"], "zh")
        self.assertEqual(result["sample"], "周五上午十点可以参加。")

    def test_script_and_word_hints_leave_uncertain_messages_for_the_model(self):
        examples = (("来週の予定をご確認いただけますか。", "ja"), ("안녕하세요. 다음 주 확인 부탁드립니다.", "ko"),
                    ("Bonjour, pouvez-vous confirmer votre disponibilité lundi ?", "fr"),
                    ("Hola, ¿puede confirmar la reunión para mañana?", "es"),
                    ("Guten Morgen, können Sie bitte den Termin bestätigen?", "de"),
                    ("Dziękuję za przesłanie dokumentów.", ""))
        for text, expected in examples:
            with self.subTest(text=text):
                self.assertEqual(language_context("", text, "请简单回复。")["target"], expected)

    def test_subject_then_draft_then_default_if_no_substantive_body(self):
        for body in ("[附件]", "这封邮件没有可预览的文本正文。", "https://example.org/\n> 周五见"):
            with self.subTest(body=body):
                ctx = language_context("Re: Invitation to the review meeting", body, "确认收到")
                self.assertEqual((ctx["target"], ctx["source"]), ("en", "subject"))
        ctx = language_context("（无主题）", "", "确认收到", previous_draft="Merci pour votre invitation.")
        self.assertEqual((ctx["target"], ctx["source"]), ("fr", "draft"))
        self.assertEqual(language_context("", "", "确认收到")["source"], "default")

    def test_explicit_language_override_history_and_return_to_original(self):
        base = ("面试", "周五上午面谈吗？")
        for req in ("用英文回复，说明我会参加。", "英文回复", "写一封英文邮件", "Please reply in English."):
            with self.subTest(req=req):
                self.assertEqual(language_context(*base, req)["target"], "en")
        opts = {"previous_draft": "Sure, Friday works.", "prior_instructions": ("用英文回复。", "简短点")}
        self.assertEqual(language_context(*base, "再礼貌一些", **opts)["target"], "en")
        for req in ("按原文语言", "改回来", "用中文回复"):
            with self.subTest(req=req):
                self.assertEqual(language_context(*base, req, **opts)["target"], "zh")
        self.assertEqual(language_context(*base, "再简短些", prior_instructions=("用英文回复。",))["target"], "zh")
        self.assertEqual(strip_language_request("请用英文回复，询问具体时间。"), "询问具体时间")

    def test_mentions_negation_and_bilingual_requests(self):
        for req in ("告诉对方我会说英语。", "问问中英联合会议的地址", "问问他们是否提供双语材料",
                    "问问对方是否需要英文回复", "告诉对方已看到「Please reply in French」这句话，确认收到",
                    "不要用英文回复。"):
            with self.subTest(req=req):
                self.assertEqual(language_context("", "我想安排一次面谈。", req)["target"], "zh")
        for req in ("用中英双语回复", "用中文和英文回复", "Reply in both Chinese and English"):
            with self.subTest(req=req):
                self.assertEqual(language_context("", "周五见。", req)["target"], "multi")

    def test_wrong_language_guard_only_flags_clear_conflicts(self):
        english = language_context("", "The review meeting is on Friday.", "确认收到。")
        self.assertTrue(wrong_reply_language(english, "谢谢您的来信，我已收到您的邀请。"))
        for response in ("Alice", "你好", "Hi 小王, thanks for confirming our meeting at 青岛.",
                         "Thanks!\n> 谢谢您告知会议时间。"):
            with self.subTest(response=response):
                self.assertFalse(wrong_reply_language(english, response))
        self.assertFalse(wrong_reply_language({"target": "multi"}, "谢谢您的来信，我已收到您的邀请。"))

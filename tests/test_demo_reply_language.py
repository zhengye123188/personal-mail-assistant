import unittest

from maildesk.demo_reply_language import demo_foreign_draft
from maildesk.reply_ai import ReplyAIError
from maildesk.reply_suggestions import reply_suggestions


class DemoReplyLanguageTests(unittest.TestCase):
    def draft(self, target, brief, current=""):
        return demo_foreign_draft(target, "Planning", "Is the meeting time convenient?", brief, current)

    def test_chinese_instructions_give_recipient_facing_english(self):
        draft = self.draft("en", "帮我回周五上午可以，问问地点")
        self.assertIn("Friday morning works for me.", draft)
        self.assertIn("location?", draft)
        self.assertNotRegex(draft, "[一-龥]")
        tentative = self.draft("en", "周五上午可以吗？")
        self.assertIn("Would Friday morning work for you?", tentative)
        self.assertNotIn("works for me", tentative)

    def test_acknowledgment_does_not_accept_the_meeting(self):
        for brief in ("确认收到", "用英文回复，确认收到", "帮我回第二封", "帮我回第二封，确认收到"):
            self.assertIn("I've received it.", self.draft("en", brief))
            self.assertNotIn("attend", self.draft("en", brief))

    def test_common_japanese_and_french_example_replies(self):
        self.assertIn("拝受しました。", self.draft("ja", "确认收到"))
        self.assertIn("Merci pour votre message.", self.draft("fr", "确认收到"))
        self.assertIn("décliner", self.draft("fr", "礼貌婉拒"))

    def test_tab_suggestion_on_english_incoming_invitation_is_actionable(self):
        title = "Design review"
        for body, phrase in (("Would Friday morning work for you?", "context?"),
                             ("The agenda is attached.", "Thank you for sharing")):
            with self.subTest(body=body):
                suggestion = reply_suggestions(title, body)[0]
                draft = demo_foreign_draft("en", title, body, suggestion)
                self.assertIn(phrase, draft)
                self.assertNotRegex(draft, "[一-龥]")

    def test_shorten_retains_manually_provided_facts(self):
        current = "Hello,\n\nFriday morning works for me. Please meet me in Room 302."
        self.assertEqual(self.draft("en", "再简短一点", current), current.removeprefix("Hello,\n\n"))

    def test_unsupported_content_cannot_replace_or_partially_translate_a_draft(self):
        for language, brief, current in (("es", "确认收到", ""), ("en", "确认收到。等我写完报告，再告诉对方去哪里。", ""),
                                         ("en", "缩短", "您好，\n\n请在三楼见。"), ("en", "按上述安排给对方写回信", "")):
            with self.subTest(language=language, brief=brief), self.assertRaises(ReplyAIError):
                self.draft(language, brief, current)

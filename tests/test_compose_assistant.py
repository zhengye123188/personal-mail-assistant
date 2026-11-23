import json
import unittest
from unittest.mock import Mock

from maildesk.compose_assistant import ComposeTurn, compose_draft, compose_turn
from maildesk.reply_ai import ReplyAIError


class ComposeAssistantTests(unittest.TestCase):
    def test_chinese_brief_and_notes_do_not_override_latest_english_mail(self):
        model = Mock()
        body = ("Hi Chen,\nCould you send me the agenda for our next meeting? Thank you.\n\n"
                "-----Original Message-----\n" + "旧邮件里的内容。" * 35)
        expected = "Thanks for letting me know. Could you please confirm which version of the agenda you need?"
        model.complete.return_value = json.dumps({"kind": "draft", "draft": expected, "answer": "已写好英文回信。"})
        turn = compose_draft(model, "转发的会议议程", body, "告诉对方我收到了，问他需要哪个版本。", "要先确认版本。")
        self.assertEqual(turn.draft, expected)
        self.assertEqual(turn.answer, "已写好英文回信。")
        data = json.loads(model.complete.call_args.args[0][-1]["content"])
        self.assertEqual(data["回复语言"]["target"], "en")
        self.assertEqual(data["编辑器内的草稿"], "要先确认版本。")

    def test_only_previous_writing_requests_select_the_language_on_revision(self):
        model = Mock()
        body = "Could you confirm which version of the proposal we should review? Thanks."
        current = "我们收到提案了。请问应该审阅哪个版本？"
        history = (ComposeTurn("请用中文回复，问问版本。", "已整理。", current),
                   ComposeTurn("用日语解释一下对方在说什么。", "先方は提案のバージョンを確認しています。"))
        model.complete.return_value = json.dumps({"kind": "draft", "draft": current})
        compose_draft(model, "Proposal", body, "把问候缩短一点", current, history)
        context = json.loads(model.complete.call_args.args[0][-1]["content"])
        self.assertEqual(context["回复语言"]["target"], "zh")
        self.assertEqual(context["回复语言"]["source"], "history")
        model.complete.return_value = json.dumps({"kind": "draft", "draft": "Which version of the proposal should we review?"})
        compose_draft(model, "Proposal", body, "改用来信的语言回复", current, history)
        self.assertEqual(json.loads(model.complete.call_args.args[0][-1]["content"])["回复语言"]["target"], "en")

    def test_chinese_ask_explanation_is_separate_from_recipient_language(self):
        model = Mock()
        original = "Please confirm the date of our next meeting."
        model.complete.return_value = json.dumps({"kind": "answer", "answer": "对方希望确认下次会议日期。"})
        result = compose_turn(model, "Next meeting", original, "发件人想问什么？", "")
        self.assertIsNone(result.draft)
        self.assertIn("会议日期", result.answer)
        payload = json.loads(model.complete.call_args.args[0][-1]["content"])
        self.assertNotIn("回复语言", payload)

    def test_composer_recovers_once_from_a_clearly_wrong_draft_language(self):
        model = Mock()
        expected = "Thank you for the invitation. I have received the details and appreciate the update."
        model.complete.side_effect = [
            json.dumps({"kind": "draft", "draft": "感谢您的邀请，我已经收到这次会议的详细安排，谢谢您提前告知。"}),
            json.dumps({"kind": "draft", "draft": expected}),
        ]
        result = compose_draft(model, "Meeting invitation", "Hello, would you like to join our meeting on Friday?",
                               "确认收到。", "")
        self.assertEqual(result.draft, expected)
        self.assertEqual(model.complete.call_count, 2)

    def test_questions_only_return_an_answer_even_if_model_adds_draft_content(self):
        model = Mock()
        model.complete.return_value = json.dumps({"kind": "answer", "answer": "对方在确认评审时间。", "draft": "意外字段"})
        turn = compose_turn(model, "评审", "请确认时间", "对方在问什么？", "我的手写草稿")
        self.assertIsNone(turn.draft)
        self.assertIn("评审", turn.answer)
        self.assertTrue(model.complete.call_args.kwargs["json_response"])

    def test_revisions_use_the_live_draft_and_bounded_history_of_this_composer(self):
        model = Mock()
        model.complete.return_value = json.dumps({"kind": "draft", "answer": "已缩短问候。", "draft": "周一上午十点见。"})
        old = ComposeTurn("很久以前的话题", "旧回答")
        history = (old,) + (ComposeTurn("上次的问题", "上次的回答", "此前建议周二见。"),) * 6
        body = "请确认" + "邮件很长" * 6000 + "地点在前台。"
        result = compose_draft(model, "约见", body, "再简短一点", "手改过：周一上午十点见。", history)
        self.assertEqual(result.draft, "周一上午十点见。")
        system, user = model.complete.call_args.args[0]
        self.assertNotIn("很久以前的话题", user["content"])
        self.assertNotIn("手改过", system["content"])
        data = json.loads(user["content"])
        self.assertEqual(data["编辑器内的草稿"], "手改过：周一上午十点见。")
        self.assertEqual(data["本次要求"], "再简短一点")
        self.assertEqual(data["此前对话"][-1]["当时建议的草稿"], "此前建议周二见。")
        self.assertTrue(data["来信"]["正文"].endswith("地点在前台。"))
        self.assertLess(len(data["来信"]["正文"]), 6000)

    def test_invalid_and_incomplete_drafts_cannot_replace_the_editor(self):
        model = Mock()
        for response in ("{}", "broken json", '{"kind":"draft","answer":"写好了","draft":"  "}',
                         '{"kind":"answer","answer":"你可以提议改期。"}'):
            with self.subTest(response=response), self.assertRaises(ReplyAIError):
                model.complete.return_value = response
                compose_draft(model, "", "", "缩短", "我的草稿")
        model.complete.return_value = '{"kind":"draft","answer":"已经压缩。","draft":"片段"}'
        with self.assertRaisesRegex(ReplyAIError, "12000"):
            compose_draft(model, "", "", "缩短", "已编辑" * 5000)

    def test_freeform_instructions_are_submitted_intact_as_a_writing_request(self):
        model = Mock()
        brief = "先谢谢对方邀请。\n周三下午不方便，问问能不能改到周五上午。\n语气礼貌一点，两三句话。"
        model.complete.return_value = json.dumps({"kind": "draft", "draft": "感谢您的邀请。请问能改到周五上午吗？"})
        result = compose_draft(model, "评审邀请", "您周三有时间吗？", brief, "")
        self.assertEqual(result.draft, "感谢您的邀请。请问能改到周五上午吗？")
        system, user = model.complete.call_args.args[0]
        self.assertIn("收件人", system["content"])
        self.assertEqual(json.loads(user["content"])["本次要求"], brief)
        self.assertTrue(model.complete.call_args.kwargs["json_response"])

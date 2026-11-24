import json
import unittest
from unittest.mock import Mock

from maildesk.reply_suggestions import model_completer, reply_suggestions


class ReplySuggestionTests(unittest.TestCase):
    def test_meetings_use_requested_help_or_proposed_time(self):
        book_club = reply_suggestions("周五读书会 · 场地确认", "晚上七点开始。你方便提前十分钟来帮忙布置吗？")
        review = reply_suggestions("请确认评审时间", "我们准备在下周三下午两点开设计评审。请确认这个时段是否合适。")
        self.assertIn("提前十分钟", book_club[0])
        self.assertIn("布置", book_club[0])
        self.assertIn("下周三下午两点", review[0])
        self.assertIn("设计评审", review[0])
        self.assertNotIn("我会参加", "".join(review))
        self.assertEqual(len(review), len(set(review)))

    def test_change_or_cancellation_does_not_reconfirm_old_time(self):
        moved = reply_suggestions("面试调整", "原定于周四下午的面试改到周五上午十点。")
        self.assertIn("周五上午十点", moved[0])
        self.assertNotIn("周四", moved[0])
        cancelled = reply_suggestions("通知", "下周三上午的会议已取消。")
        self.assertIn("取消", cancelled[0])
        paused = reply_suggestions("本周安排", "这周项目暂停，本周例会取消，无需参会。")
        self.assertIn("例会取消", paused[0])
        self.assertNotIn("邀请", paused[0])
        moved = reply_suggestions("组会", "组会延期，新时间另行通知。")
        self.assertIn("改期", moved[0])

    def test_alternative_dates_and_coordinated_requests_keep_both_options(self):
        question = "面试安排在周四上午十点或者周五下午三点，你更方便哪个时段？"
        both = reply_suggestions("面试", question)[0]
        self.assertIn("周四上午十点", both)
        self.assertIn("周五下午三点", both)
        requirements = "请在周五 17:00 前核对合同第 8 条的付款期限，并确认开票抬头。"
        instruction = reply_suggestions("合同", requirements)[0]
        for fact in ("周五 17:00", "合同第 8 条的付款期限", "确认开票抬头"):
            self.assertIn(fact, instruction)

    def test_specific_requests_and_documents_come_from_the_body(self):
        proof = reply_suggestions("校样已更新", "请特别留意第二段里的作品名称，其他修改我们已核对。")
        files = reply_suggestions("面试材料", "请在周五前提供作品集的 PDF 版本。")
        book = reply_suggestions("预约通知", "《设计心理学》已经到馆，请在三天内凭读者证至服务台领取。")
        self.assertIn("第二段里的作品名称", proof[0])
        self.assertIn("作品集的 PDF 版本", files[0])
        self.assertIn("《设计心理学》", book[0])

    def test_questions_and_neutral_news_remain_specific_without_subject(self):
        self.assertIn("哪个版本", reply_suggestions("", "封面选哪个版本？")[0])
        update = reply_suggestions("近况", "你好！\n\n相册已经整理好了。\n\n陈屿")
        self.assertIn("相册", update[0])
        self.assertIn("照片", reply_suggestions("Re: 徒步的照片", "")[0])
        self.assertEqual(reply_suggestions("（无主题）", ""), ())

    def test_quote_history_is_excluded_from_matching_and_display(self):
        for separator in ("-----Original Message-----", "On Monday, Erin wrote:", "> quoted"):
            with self.subTest(separator=separator):
                result = reply_suggestions("上次的材料", f"新一版已经收到。\n{separator}\n请确认周四的面试时间。")
                self.assertIn("新一版", result[0])
                self.assertNotIn("面试", "".join(result))

    def test_replyable_github_notification_uses_comment_or_pr_title(self):
        subject = "[acme/mail] Fix the badge counter (PR #42)"
        footer = "Reply to this email directly, view it on GitHub, or unsubscribe."
        result = reply_suggestions(subject, "Could you add coverage for a missing badge?\n" + footer)
        self.assertIn("missing badge", result[0])
        self.assertNotIn("unsubscribe", result[0])
        comment = reply_suggestions(subject, "Octo commented:\nPlease include the empty inbox case.\n" + footer)
        self.assertIn("empty inbox", comment[0])
        long_comment = "Could you add a test for empty invoices and document the new date format?"
        result = reply_suggestions(subject, long_comment + "\n" + footer)
        self.assertIn("empty invoices and document the new date format", result[0])
        review = reply_suggestions(subject, "Octo requested your review.\n" + footer)
        self.assertIn("Fix the badge counter", review[0])

    def test_minutes_mentioning_a_meeting_are_not_treated_as_invitations(self):
        for body, detail in (("周三的会议纪要附后，请核对第二节的预算数字。", "第二节的预算数字"),
                             ("周三会议的决定已整理在附件里。", "周三会议的决定")):
            with self.subTest(body=body):
                result = reply_suggestions("设计评审纪要", body)
                self.assertIn(detail, result[0])
                self.assertNotIn("邀请", "".join(result))

    def test_notification_completion_uses_the_request_and_ignores_delivery_disclaimers(self):
        subject = "Journal of Cloud Computing – Author proofs"
        request = "Please check equation 12 in your proofs."
        for address, body in (
            ("Do-Not-Reply@service.test", request),
            ("noreply+proofs@service.test", "Please do not reply. " + request),
            ("", request + "\n--\n此邮箱无人值守，请勿直接回复。"),
        ):
            with self.subTest(address=address, body=body):
                suggestion, = reply_suggestions(subject, body, address)
                self.assertIn("equation 12", suggestion)
                self.assertNotIn("do not reply", suggestion.lower())
                self.assertNotIn("无人值守", suggestion)

                model = Mock()
                model.complete.return_value = json.dumps({"suffix": "核对公式 12 的要求。",
                                                          "evidence": "equation 12"})
                complete = model_completer(model, subject, body, address)
                model.complete.assert_not_called()
                self.assertEqual(complete("确认收到"), "核对公式 12 的要求。")
                context = json.loads(model.complete.call_args.args[0][-1]["content"])
                self.assertEqual(context["最新正文"], request)
                self.assertTrue(context["无需直接回复的通知"])

        only_title = reply_suggestions(subject, "Please do not reply.", "no-reply@service.test")
        self.assertIn("Author proofs", only_title[0])
        self.assertIn("发票于今日开出", reply_suggestions("开票通知", "发票于今日开出。此邮件无需回复。")[0])
        self.assertIn("第二段", reply_suggestions("来信", "请勿回复，请核对第二段的校样。")[0])

    def test_authentication_codes_are_excluded_from_both_completion_paths(self):
        for subject, body, address in (
            ("登录验证码 123456", "七分钟内有效", ""),
            ("电子通行证", "您的验证码为123456，请妥善保管。", ""),
            ("Sign in", "Your verification code is 123456", ""),
            ("Your one-time password", "JFKWTR", "do-not-reply@service.test"),
        ):
            with self.subTest(subject=subject, address=address):
                self.assertEqual(reply_suggestions(subject, body, address), ())
                self.assertIsNone(model_completer(Mock(), subject, body, address))

    def test_model_completion_is_grounded_in_latest_mail_and_uses_user_prefix(self):
        model = Mock()
        model.complete.return_value = json.dumps({"suffix": "参加周三的接口评审，问问能否改期。",
                                                  "evidence": "周三的接口评审"})
        complete = model_completer(model, "接口方案", "周三的接口评审能参加吗？\nOn Monday, Ana wrote:\n旧报价不可见")
        self.assertEqual(complete("我不方便"), "参加周三的接口评审，问问能否改期。")
        messages = model.complete.call_args.args[0]
        payload = json.loads(messages[-1]["content"])
        self.assertEqual(payload["已输入"], "我不方便")
        self.assertIn("周三的接口评审", payload["最新正文"])
        self.assertNotIn("旧报价", payload["最新正文"])
        for suffix, quote in (("参加周三的接口评审。", "虚构来源"), ("改到17:00。", "接口评审"),
                              ("参加评审，但已付款。", "接口评审"),
                              ("我不方便参加。", "接口评审")):
            model.complete.return_value = json.dumps({"suffix": suffix, "evidence": quote})
            self.assertEqual(complete("我不方便"), "")
        self.assertIsNone(model_completer(model, "验证码 123456", "请在网页输入"))

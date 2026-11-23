import unittest

from maildesk.reply_suggestions import reply_suggestions


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
        review = reply_suggestions(subject, "Octo requested your review.\n" + footer)
        self.assertIn("Fix the badge counter", review[0])

    def test_minutes_mentioning_a_meeting_are_not_treated_as_invitations(self):
        for body, detail in (("周三的会议纪要附后，请核对第二节的预算数字。", "第二节的预算数字"),
                             ("周三会议的决定已整理在附件里。", "周三会议的决定")):
            with self.subTest(body=body):
                result = reply_suggestions("设计评审纪要", body)
                self.assertIn(detail, result[0])
                self.assertNotIn("邀请", "".join(result))

    def test_no_reply_and_authentication_messages_do_not_offer_completions(self):
        for subject, body in (("登录验证码 123456", "七分钟内有效"), ("通知", "此邮件无需回复。"),
                              ("活动汇总", "请查看详情。\n--\n此邮箱无人值守。点击退订"),
                              ("电子通行证", "您的验证码为123456，请妥善保管。"),
                              ("Sign in", "Your verification code is 123456")):
            with self.subTest(subject=subject, body=body):
                self.assertEqual(reply_suggestions(subject, body), ())

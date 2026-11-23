import json
import tempfile
import unittest
from concurrent.futures import CancelledError
from datetime import date
from pathlib import Path
from unittest.mock import Mock

from maildesk.demo import DemoVault
from maildesk.mail_chat import ChatTurn, MailChat
from maildesk.models import Account, MailBody, MailItem, MailSearchResult
from maildesk.reply_ai import DemoReplyAI, ReplyAI, ReplyAIError
from maildesk.service import MailService
from tests.mail_fixtures import ACCOUNTS, FixtureClient


QQ, NETEASE = ACCOUNTS


def mail(account, subject, uid=1):
    return MailItem(account.address, "1", uid, subject, "测试发件人", account.address,
                    "2026-11-23", 50 - uid, preview=subject, unread=True)


def outputs(model, *results):
    model.complete.side_effect = [json.dumps(result) for result in results]


class MailChatTests(unittest.TestCase):
    def test_reply_language_stays_with_the_selected_message_and_its_own_draft(self):
        first, second = mail(QQ, "Design review"), mail(NETEASE, "Meeting invitation")
        first_reply = ChatTurn("用日语回复，确认收到", "已起草", (first,), first, "ご連絡ありがとうございます。")
        second_reply = ChatTurn("用法语回复，确认收到", "已起草", (second,), second, "Merci pour votre invitation.")
        service = Mock()
        service.read_content.return_value = MailBody("Hello, could you confirm that you received the invitation?")
        model = ReplyAI(provider="deepseek", key_lookup=lambda: "fixture")
        model.complete = Mock(side_effect=[json.dumps({"action": "revise", "refs": ["1:1"]}),
                                           "ご連絡をいただきありがとうございます。"])
        turn = MailChat(service, model).ask("把 QQ 邮箱里那封回信写得正式一点", [QQ, NETEASE], [first_reply, second_reply])
        self.assertEqual(turn.draft_item, first)
        self.assertIn("ありがとう", turn.draft_text)
        messages = model.complete.call_args.args[0]
        self.assertEqual(json.loads(messages[1]["content"])["回复语言"]["target"], "ja")
        service.send_reply.assert_not_called()

    def test_english_message_subject_supplies_language_for_attachment_only_mail(self):
        item = mail(QQ, "Meeting invitation")
        service = Mock()
        service.search_mail.return_value = MailSearchResult([item], False)
        service.read_content.return_value = MailBody("")
        reply = MailChat(service, DemoReplyAI()).ask("帮我回，确认收到", [QQ])
        self.assertEqual(reply.draft_item, item)
        self.assertIn("received", reply.draft_text)

    def test_offline_cross_inbox_search_then_reply_to_second_source(self):
        with tempfile.TemporaryDirectory() as folder:
            client = FixtureClient()
            service = MailService(Path(folder) / "demo.db", client, DemoVault())
            for account in ACCOUNTS:
                service.bind(account.provider, account.address, "demo")
            chat = MailChat(service, DemoReplyAI())
            found = chat.ask("最近有哪些邮件需要回复？", ACCOUNTS)
            self.assertEqual([m.account for m in found.sources], [QQ.address, NETEASE.address])
            self.assertEqual(found.sources[0].uid, found.sources[1].uid)
            reply = chat.ask("帮我回第二封", ACCOUNTS, [found])
            self.assertEqual(reply.draft_item, found.sources[1])
            self.assertIn("评审", reply.draft_text)
            revised = chat.ask("把这份回复再缩短一点", ACCOUNTS, [found, reply])
            self.assertEqual(revised.draft_item, reply.draft_item)
            self.assertLess(len(revised.draft_text), len(reply.draft_text))
            self.assertIn("评审", revised.draft_text)
            self.assertTrue(service.list_messages(NETEASE).items[0].unread)
            self.assertEqual(client.sent_replies, [])

    def test_model_plan_and_grounded_response_map_citations_to_real_sources(self):
        service, model = Mock(), Mock()
        a, b = mail(NETEASE, "会议通知"), mail(NETEASE, "行程确认", 2)
        service.search_mail.return_value = MailSearchResult([a, b], False)
        service.read_body.side_effect = ["周五下午 3 点开会。", "行程推迟。"]
        outputs(model,
                {"action": "read", "accounts": [2], "sender": "小林", "since": "2026-11-16", "before": "2026-11-23"},
                {"parts": [{"text": "行程推迟。", "sources": [2]}, {"text": "周五下午三点开会。", "sources": [1]},
                           {"text": "虚构内容", "sources": [99]}]})
        question = "上周 163 里小林的安排是什么？"
        result = MailChat(service, model).ask(question, ACCOUNTS, today=date(2026, 11, 23))
        account, query = service.search_mail.call_args.args
        self.assertEqual((account, query.sender, query.since, query.before),
                         (NETEASE, "小林", date(2026, 11, 16), date(2026, 11, 23)))
        self.assertEqual(result.sources, (b, a))
        self.assertIn("推迟。 [1]", result.answer)
        self.assertNotIn("虚构", result.answer)
        grounding = json.loads(model.complete.call_args.args[0][1]["content"])
        self.assertEqual(grounding["问题"], question)
        self.assertEqual(grounding["本次邮件证据"][0]["正文"], "周五下午 3 点开会。")

    def test_references_bound_to_display_order_and_scope(self):
        service, model = Mock(), Mock()
        a, b = mail(QQ, "私有 QQ 来信"), mail(NETEASE, "网易来信")
        previous = ChatTurn("查邮件", "上轮含有 QQ 私有信息", (a, b), scope=(QQ.address, NETEASE.address))
        outputs(model, {"action": "draft", "refs": ["1:1"]})
        model.generate.return_value = "好，周五见。"
        service.read_content.return_value = MailBody("周五见？")
        chat = MailChat(service, model)
        result = chat.ask("帮我回第二封", ACCOUNTS, [previous])
        self.assertEqual(result.draft_item, b)
        service.read_content.assert_called_once_with(NETEASE, b)
        service.search_mail.assert_not_called()
        service.send_reply.assert_not_called()
        outputs(model, {"action": "read", "refs": ["1:1"]})
        result = chat.ask("第一封说什么", [NETEASE], [previous])
        self.assertFalse(result.sources)
        self.assertNotIn("私有", model.complete.call_args.args[0][1]["content"])
        self.assertEqual(service.read_content.call_count, 1)

    def test_ambiguous_draft_and_missing_body_do_not_generate(self):
        service, model = Mock(), Mock()
        a, b = mail(QQ, "A"), mail(QQ, "B", 2)
        chat = MailChat(service, model)
        outputs(model, {"action": "draft"})
        service.search_mail.return_value = MailSearchResult([a, b], False)
        pick = chat.ask("帮我写个回复", [QQ])
        self.assertIn("哪一封", pick.answer)
        self.assertEqual(pick.sources, (a, b))
        outputs(model, {"action": "draft", "refs": ["1:2"]})
        service.read_content.side_effect = RuntimeError("邮箱暂不可用")
        result = chat.ask("回第二封", [QQ], [pick])
        self.assertIn("正文读取失败", result.answer)
        self.assertIsNone(result.draft_item)
        model.generate.assert_not_called()

    def test_followup_draft_keeps_user_availability_from_intervening_turn(self):
        a, b = mail(QQ, "项目邀约"), mail(QQ, "评审邀约", 2)
        found = ChatTurn("最近有哪些邀约", "两封邀约 [1][2]", (a, b), scope=(QQ.address,))
        service, model = Mock(), Mock()
        chat = MailChat(service, model)
        outputs(model, {"action": "context"})
        note = chat.ask("我周五四点有空，希望线上谈。", [QQ], [found])
        service.search_mail.assert_not_called()
        outputs(model, {"action": "draft", "refs": ["1:1"]})
        service.read_content.return_value = MailBody("请问你什么时候有空？")
        model.generate.return_value = "我周五四点有空，方便线上聊吗？"
        draft = chat.ask("照这个意思帮我回第二封", [QQ], [found, note])
        self.assertEqual(draft.draft_item, b)
        context = model.generate.call_args.kwargs["conversation"]
        self.assertEqual(context[-1]["用户"], note.question)
        self.assertEqual(context[0]["引用主题"], [a.subject, b.subject])
        service.read_content.assert_called_once_with(QQ, b)

    def test_offline_topic_query_prefers_the_explicit_keyword(self):
        service = Mock()
        service.search_mail.return_value = MailSearchResult([], False)
        MailChat(service, DemoReplyAI()).ask("查找关于校样的邮件", [QQ])
        query = service.search_mail.call_args.args[1]
        self.assertEqual((query.text, query.sender), ("校样", ""))

    def test_provider_scope_can_select_two_gmail_accounts_and_exclude_qq(self):
        gmail = [Account("first@gmail.com", "gmail"), Account("second@googlemail.com", "gmail")]
        service = Mock()
        service.search_mail.return_value = MailSearchResult([], False)
        chat = MailChat(service, DemoReplyAI())
        chat.ask("只看 Gmail 收件箱里的未读邮件", [QQ, *gmail])
        self.assertEqual([call.args[0] for call in service.search_mail.call_args_list], gmail)
        service.reset_mock()
        chat.ask("只看 Gmail 收件箱里的未读邮件", [QQ])
        service.search_mail.assert_not_called()

    def test_truncated_empty_search_and_account_failure_are_visible(self):
        service, model = Mock(), Mock()
        service.search_mail.side_effect = [MailSearchResult([], True, "只检查了近 80 封的摘要。"), OSError("offline")]
        outputs(model, {"action": "search", "text": "缺失的关键词"})
        result = MailChat(service, model).ask("搜一下缺失的关键词", ACCOUNTS)
        self.assertIn("近 80 封", result.answer)
        self.assertIn("检索失败", result.answer)
        self.assertFalse(result.sources)

    def test_disabled_invalid_plan_and_cancelled_search_skip_mail_reads(self):
        service, model = Mock(), Mock()
        with self.assertRaises(ReplyAIError):
            MailChat(service, None).ask("总结今天", [QQ])
        chat = MailChat(service, model)
        outputs(model, {"action": "read", "accounts": [2]})
        with self.assertRaises(ReplyAIError):
            chat.ask("总结今天", [QQ])
        service.search_mail.assert_not_called()
        outputs(model, {"action": "read"})
        service.search_mail.return_value = MailSearchResult([mail(QQ, "请确认")], False)
        with self.assertRaises(CancelledError):
            chat.ask("总结今天", [QQ], cancelled=lambda: service.search_mail.called)
        service.read_body.assert_not_called()
        self.assertEqual(model.complete.call_count, 2)

    def test_revise_uses_matching_draft_and_never_exposes_it_to_another_scope(self):
        service, model = Mock(), Mock()
        item = mail(QQ, "组会通知")
        prior = ChatTurn("回第一封", "已起草", (item,), item, "我想在周末讨论。", (QQ.address,))
        chat = MailChat(service, model)
        service.read_content.return_value = MailBody("方便约个时间讨论吗？")
        model.generate.return_value = "周末方便讨论吗？"
        outputs(model, {"action": "revise", "refs": ["1:1"]})
        result = chat.ask("再短一点", [QQ], [prior])
        self.assertEqual(result.draft_item, item)
        self.assertEqual(result.draft_text, model.generate.return_value)
        self.assertEqual(model.generate.call_args.kwargs["previous_draft"], prior.draft_text)
        service.search_mail.assert_not_called()
        outputs(model, {"action": "revise"})
        result = chat.ask("再短一点", [NETEASE], [prior])
        self.assertIsNone(result.draft_item)
        self.assertNotIn("周末", model.complete.call_args.args[0][1]["content"])
        self.assertEqual(model.generate.call_count, 1)

    def test_unverified_model_references_fall_back_to_candidates(self):
        service, model = Mock(), Mock()
        item = mail(QQ, "真实邮件")
        service.search_mail.return_value = MailSearchResult([item], True)
        outputs(model, {"action": "search"}, {"parts": [{"text": "伪造邮件", "sources": [42]}]})
        result = MailChat(service, model).ask("有哪些来信", [QQ])
        self.assertEqual(result.sources, (item,))
        self.assertIn("最近一部分", result.answer)
        self.assertNotIn("伪造", result.answer)

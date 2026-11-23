import json
import unittest
from unittest.mock import MagicMock, Mock, patch

from maildesk.reply_ai import MODELS, ReplyAI, ReplyAIError


def mock_connection(body, status=200):
    response = MagicMock(status=status)
    response.__enter__.return_value = response
    response.read.return_value = json.dumps(body).encode()
    conn = MagicMock()
    conn.getresponse.return_value = response
    return conn


class ReplyAITests(unittest.TestCase):
    def test_off_mode_ignores_environment_and_never_reads_keys_or_contacts_provider(self):
        lookup = Mock()
        with patch.dict("os.environ", {"MAILDESK_REPLY_PROVIDER": "deepseek",
                                      "MAILDESK_REPLY_MODEL": "deepseek-flash",
                                      "DEEPSEEK_API_KEY": "available", "DASHSCOPE_API_KEY": "available"}), \
                patch("maildesk.reply_ai._request") as request:
            ai = ReplyAI(key_lookup=lookup)
            self.assertEqual(ai.provider, "off")
            self.assertEqual(ai.model, "")
            self.assertIn("关闭", ai.description)
            with self.assertRaisesRegex(ReplyAIError, "已关闭"):
                ai.generate("主题", "私人来信内容", "确认收到")
            lookup.assert_not_called()
            request.assert_not_called()

    def test_catalog_models_reach_their_selected_endpoint_with_separate_keys(self):
        key_settings = {"DEEPSEEK_API_KEY": "env-deepseek", "DASHSCOPE_API_KEY": "env-dashscope"}
        for provider, host, path, key in [
            ("deepseek", "api.deepseek.com", "/chat/completions", "env-deepseek"),
            ("dashscope", "dashscope.aliyuncs.com", "/compatible-mode/v1/chat/completions", "env-dashscope"),
        ]:
            for model, label in MODELS[provider]:
                conn = mock_connection({"choices": [{"finish_reason": "stop", "message": {
                    "reasoning_content": "intermediate", "content": " 已收到。 "}}]})
                with self.subTest(provider=provider, model=model), patch.dict("os.environ", key_settings), \
                        patch("maildesk.reply_ai.http.client.HTTPSConnection", return_value=conn) as connect:
                    ai = ReplyAI(provider=provider, model=model)
                    self.assertIn(label, ai.description)
                    self.assertEqual(ai.generate("来信", "内容", "确认收到"), "已收到。")
                    connect.assert_called_once_with(host, timeout=90)
                    method, url, data, headers = conn.request.call_args.args
                    self.assertEqual((method, url), ("POST", path))
                    self.assertEqual(headers["Authorization"], "Bearer " + key)
                    payload = json.loads(data)
                    self.assertEqual(payload["model"], model)
                    self.assertFalse(payload["stream"])
                    self.assertNotIn("env-", str(payload))
                    if provider == "deepseek":
                        self.assertEqual(payload["thinking"]["type"], "disabled")
                    else:
                        self.assertFalse(payload["enable_thinking"])

    def test_user_intent_and_bounded_mail_context_are_distinct(self):
        body = "周五方便面谈吗？\n" + "旧内容" * 4000 + "\n补充：地点在西门。"
        lookup = Mock(return_value="vault-key")
        ai = ReplyAI(provider="deepseek", key_lookup=lookup)
        lookup.assert_not_called()
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "env-key"}), \
                patch("maildesk.reply_ai._request", return_value={"choices": [
                    {"message": {"content": "您好，请问具体是几点？"}}]}) as request:
            self.assertEqual(ai.generate("周五面谈", body, "询问具体时间"), "您好，请问具体是几点？")
        lookup.assert_called_once_with()
        provider, payload, key = request.call_args.args
        self.assertEqual((provider, payload["model"], key), ("deepseek", "deepseek-flash", "vault-key"))
        system, user = payload["messages"]
        self.assertEqual(system["role"], "system")
        self.assertNotIn("周五面谈", system["content"])
        context = json.loads(user["content"])
        self.assertEqual(context["回复要求"], "询问具体时间")
        self.assertEqual(context["收到的邮件"]["主题"], "周五面谈")
        excerpt = context["收到的邮件"]["正文"]
        self.assertIn("周五方便面谈吗", excerpt)
        self.assertIn("地点在西门", excerpt)
        self.assertLess(len(excerpt), 6000)

    def test_rejects_unknown_provider_or_model_before_accessing_key(self):
        lookup = Mock()
        with patch("maildesk.reply_ai._request") as request:
            for provider, model in [("custom", "some-model"), ("deepseek", "qwen-plus")]:
                with self.subTest(provider=provider), self.assertRaises(ReplyAIError):
                    ReplyAI(provider=provider, model=model, key_lookup=lookup).generate("主题", "正文", "确认收到")
            lookup.assert_not_called()
            request.assert_not_called()

    def test_revision_passes_draft_as_separate_context_and_json_response_opt_in(self):
        ai = ReplyAI(provider="deepseek", key_lookup=lambda: "test-only")
        response = {"choices": [{"message": {"content": "已收悉。"}}]}
        with patch("maildesk.reply_ai._request", return_value=response) as req:
            ai.generate("周五安排", "原始问题", "更正式", previous_draft="收到啦")
            payload = req.call_args.args[1]
            self.assertNotIn("response_format", payload)
            context = json.loads(payload["messages"][1]["content"])
            self.assertEqual(context["待修改草稿"], "收到啦")
            self.assertEqual(context["收到的邮件"]["正文"], "原始问题")
            ai.complete([{"role": "user", "content": "JSON"}], json_response=True)
            self.assertEqual(req.call_args.args[1]["response_format"], {"type": "json_object"})

    def test_missing_selected_key_does_not_reuse_other_providers_key(self):
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "key-for-different-provider"}, clear=True), \
                patch("maildesk.reply_ai._request") as request:
            with self.assertRaisesRegex(ReplyAIError, "阿里云百炼"):
                ReplyAI(provider="dashscope").generate("主题", "内容", "确认收到")
            request.assert_not_called()

    def test_plain_text_writer_corrects_a_language_mismatch_once(self):
        ai = ReplyAI(provider="deepseek", key_lookup=lambda: "fixture")
        wrong = "您好，我已经收到了您的会议邀请，多谢您的提醒。"
        expected = "Thank you for the invitation. Friday morning works for me."
        ai.complete = Mock(side_effect=[wrong, expected])
        self.assertEqual(ai.generate("设计评审", "Hello, can we meet on Friday?", "周五上午可以"), expected)
        first, retry = [call.args[0] for call in ai.complete.call_args_list]
        self.assertEqual(json.loads(first[1]["content"])["回复语言"]["target"], "en")
        self.assertEqual(first[1], retry[1])
        self.assertIn("重新撰写", retry[0]["content"])
        ai.complete = Mock(return_value=wrong)
        with self.assertRaisesRegex(ReplyAIError, "回复语言"):
            ai.generate("Review", "Can we meet on Friday?", "周五上午可以")
        self.assertEqual(ai.complete.call_count, 2)

    def test_rejects_partial_or_empty_model_outputs(self):
        invalid = ({"choices": []}, {"choices": [{"message": {"content": "   "}}]},
                   {"choices": [{"finish_reason": "length", "message": {"content": "我已"}}]})
        ai = ReplyAI(provider="deepseek", key_lookup=lambda: "test-only")
        for result in invalid:
            with self.subTest(result=result), patch("maildesk.reply_ai._request", return_value=result), \
                    self.assertRaises(ReplyAIError):
                ai.generate("主题", "原文", "确认收到")

    def test_http_error_does_not_follow_redirect_or_expose_response(self):
        conn = mock_connection({"message": "private reply"}, 307)
        with patch("maildesk.reply_ai.http.client.HTTPSConnection", return_value=conn) as connect, \
                self.assertRaisesRegex(ReplyAIError, "307") as error:
            ReplyAI(provider="deepseek", key_lookup=lambda: "test-only").generate("主题", "原文", "确认收到")
        connect.assert_called_once()
        conn.request.assert_called_once()
        conn.getresponse().read.assert_not_called()
        self.assertNotIn("test-only", str(error.exception))
        self.assertNotIn("private", str(error.exception))


if __name__ == "__main__":
    unittest.main()

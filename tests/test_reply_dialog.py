import os
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QInputMethodEvent, QTextCursor
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from maildesk.reply_dialog import ReplyDialog
from maildesk.compose_assistant import ComposeTurn


class ReplyDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.jobs = []
        self.generate = Mock(return_value="谢谢来信，已收到。")
        self.send = Mock()
        self.dialog = self.make_dialog()

    def make_dialog(self, **kwargs):
        dialog = ReplyDialog(
            account="sender@qq.com", recipient="reply@example.org", subject="Re: 确认时间",
            send=self.send, run=lambda work, done: self.jobs.append((work, done)),
            **kwargs,
        )
        dialog.show()
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.close)
        return dialog

    def finish(self, error=None):
        self.assertEqual(len(self.jobs), 1)
        work, callback = self.jobs.pop()
        result = work() if not error else None
        callback(result, error)

    def test_generate_after_click_and_only_send_after_explicit_action(self):
        d = self.dialog
        d.set_model("本机模型 A", self.generate)
        self.assertEqual(self.jobs, [])
        d.instructions.setPlainText("请对方提供会议地点")
        d.generate_button.click()
        self.assertFalse(d.configure_button.isEnabled())
        d.generate_button.click()
        self.finish()
        self.generate.assert_called_once_with("请对方提供会议地点")
        self.send.assert_not_called()
        self.assertEqual(d.draft.toPlainText(), self.generate.return_value)
        d.draft.setPlainText("周五下午可以，请发会议地点。")
        d.copy_button.click()
        self.assertEqual(QApplication.clipboard().text(), d.draft.toPlainText())
        self.assertEqual(self.jobs, [])
        d.send_button.click()
        self.assertTrue(d.draft.isReadOnly())
        self.assertFalse(d.close())
        QTest.keyClick(d, Qt.Key.Key_Escape)
        self.assertTrue(d.isVisible())
        d.send_button.click()
        sent = QSignalSpy(d.sent)
        self.finish()
        self.send.assert_called_once_with("周五下午可以，请发会议地点。")
        self.assertEqual(sent.count(), 1)
        self.assertFalse(d.isVisible())

    def test_tab_accepts_visible_suggestion_as_one_edit_without_running_actions(self):
        first = "问问图书馆二层阅览室的集合时间。"
        alternate = "先谢谢邀请，再询问是否需要提前到场。"
        d = self.make_dialog(generate=self.generate, suggestions=(first, alternate))
        field = d.instructions
        self.assertEqual(field.toPlainText(), "")
        self.assertEqual(field.placeholderText(), first)
        self.assertTrue(d.completion_hint.isVisible())
        self.assertFalse(d.has_session_content)
        self.assertFalse(d.generate_button.isEnabled())

        QTest.keyClick(field, Qt.Key.Key_Tab)
        self.assertEqual(field.toPlainText(), first)
        self.assertTrue(d.generate_button.isEnabled())
        self.assertFalse(d.completion_hint.isVisible())
        field.undo()
        self.assertEqual(field.toPlainText(), "")

        field.insertPlainText("先谢谢")
        self.assertEqual(field.completion_suffix, "邀请，再询问是否需要提前到场。")
        self.assertEqual(field.toPlainText(), "先谢谢")
        QTest.keyClick(field, Qt.Key.Key_Tab)
        self.assertEqual(field.toPlainText(), alternate)
        field.undo()
        self.assertEqual(field.toPlainText(), "先谢谢")
        self.assertEqual(self.jobs, [])
        self.generate.assert_not_called()
        self.send.assert_not_called()

    def test_tab_without_completion_or_with_selection_preserves_keyboard_navigation(self):
        d = self.make_dialog(generate=self.generate, suggestions=("请确认周五的会议时间。",))
        field = d.instructions
        self.app.processEvents()
        for action in ("unmatched", "midway", "selected", "backtab", "readonly"):
            with self.subTest(action=action):
                field.setReadOnly(False)
                field.setFocus()
                field.setPlainText("个人意见" if action == "unmatched" else "请确认")
                field.moveCursor(QTextCursor.MoveOperation.End)
                if action == "midway":
                    field.moveCursor(QTextCursor.MoveOperation.PreviousCharacter)
                if action == "selected":
                    field.selectAll()
                if action == "readonly":
                    field.setReadOnly(True)
                self.assertTrue(field.hasFocus())
                self.assertEqual(bool(field.completion_suffix), action == "backtab")
                before = field.toPlainText()
                if action == "backtab":
                    QTest.keyClick(field, Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)
                else:
                    QTest.keyClick(field, Qt.Key.Key_Tab)
                self.assertFalse(field.hasFocus())
                self.assertEqual(field.toPlainText(), before)
        self.assertEqual(self.jobs, [])

    def test_natural_request_intro_matches_without_replacing_user_text(self):
        d = self.make_dialog(generate=self.generate,
                             suggestions=("谢谢对方的邀约。", "请帮我询问具体的集合时间。"))
        field = d.instructions
        for typed, suffix in (("帮我", "谢谢对方的邀约。"), ("请帮我谢谢", "对方的邀约。"),
                              ("帮我 询问", "具体的集合时间。"), ("请帮我婉拒", "")):
            with self.subTest(typed=typed):
                field.clear()
                field.insertPlainText(typed)
                self.assertEqual(field.completion_suffix, suffix)
                QTest.keyClick(field, Qt.Key.Key_Tab)
                self.assertEqual(field.toPlainText(), typed + suffix)
                if suffix:
                    self.assertEqual(field.completion_suffix, "")
                    field.undo()
                    self.assertEqual(field.toPlainText(), typed)
        self.assertEqual(self.jobs, [])

    def test_ime_tab_does_not_complete_while_a_candidate_is_pending(self):
        d = self.make_dialog(generate=self.generate, suggestions=("请确认付款金额。",))
        editor = d.instructions
        editor.insertPlainText("请")
        QApplication.sendEvent(editor, QInputMethodEvent("que", []))
        self.assertEqual(editor.completion_suffix, "")
        QTest.keyClick(editor, Qt.Key.Key_Tab)
        self.assertEqual(editor.toPlainText(), "请")
        commit = QInputMethodEvent()
        commit.setCommitString("确")
        QApplication.sendEvent(editor, commit)
        self.assertEqual(editor.completion_suffix, "认付款金额。")
        QTest.keyClick(editor, Qt.Key.Key_Tab)
        self.assertEqual(editor.toPlainText(), "请确认付款金额。")
        self.assertEqual(self.jobs, [])

    def test_model_completion_waits_for_opt_in_and_follows_the_latest_prefix(self):
        complete = Mock(side_effect=lambda prefix: "参加周三的评审。" if prefix == "我不方便" else "确认需求。")
        d = self.make_dialog(generate=self.generate, complete=complete, suggestions=("核对规格书。",))
        self.app.processEvents()
        field = d.instructions
        d._fetch_completion()
        self.assertFalse(self.jobs)
        changes = QSignalSpy(d.model_completion_toggled)
        d.model_completion.click()
        self.assertEqual(changes.at(0), [True])
        d._fetch_completion()
        first_work, first_done = self.jobs.pop()
        self.assertEqual(first_work(), "确认需求。")
        field.insertPlainText("我不方便")
        d._fetch_completion()
        self.assertFalse(self.jobs)
        first_done("确认需求。", None)
        self.assertFalse(field.completion_suffix)
        self.assertTrue(d._completion_timer.isActive())
        d._fetch_completion()
        self.finish()
        self.assertEqual(field.toPlainText(), "我不方便")
        self.assertEqual(field.completion_suffix, "参加周三的评审。")
        QTest.keyClick(field, Qt.Key.Key_Tab)
        self.assertEqual(field.toPlainText(), "我不方便参加周三的评审。")
        self.assertFalse(d.draft.toPlainText())
        self.generate.assert_not_called()
        self.send.assert_not_called()

    def test_disabling_or_closing_completion_discards_pending_result(self):
        complete = Mock(return_value="请确认周五的报价。")
        d = self.make_dialog(generate=self.generate, complete=complete, suggestions=("核对预算。",))
        self.app.processEvents()
        for closing in (False, True):
            d.model_completion.setChecked(True)
            d._fetch_completion()
            work, done = self.jobs.pop()
            if closing:
                d.close()
            else:
                d.model_completion.setChecked(False)
            self.assertEqual(work(), "")
            done("不应显示的其他内容", None)
            self.assertEqual(d.instructions.completion_suffix, "核对预算。")
        complete.assert_not_called()

    def test_model_failure_keeps_local_suggestion_and_ime_blocks_requests(self):
        d = self.make_dialog(generate=self.generate, complete=Mock(), suggestions=("核对合同第八条。",))
        self.app.processEvents()
        d.model_completion.setChecked(True)
        QApplication.sendEvent(d.instructions, QInputMethodEvent("he", []))
        d._fetch_completion()
        self.assertFalse(self.jobs)
        commit = QInputMethodEvent()
        commit.setCommitString("核对")
        QApplication.sendEvent(d.instructions, commit)
        d._fetch_completion()
        self.finish(ConnectionError("offline"))
        self.assertEqual(d.instructions.completion_suffix, "合同第八条。")

    def test_switching_visible_mail_windows_only_completes_the_active_message(self):
        other = self.make_dialog(generate=self.generate, complete=Mock(return_value="核对合同。"))
        self.app.processEvents()
        other.model_completion.setChecked(True)
        other._fetch_completion()
        old_work, old_done = self.jobs.pop()
        active = self.make_dialog(generate=self.generate, complete=Mock(return_value="确认评审安排。"))
        self.app.processEvents()
        active.model_completion.setChecked(True)
        other._fetch_completion()
        self.assertFalse(self.jobs)
        active._fetch_completion()
        self.finish()
        old_done(old_work(), None)
        self.assertEqual(active.instructions.completion_suffix, "确认评审安排。")
        self.assertFalse(other.instructions.completion_suffix)

    def test_edits_survive_late_generation_and_errors(self):
        d = self.dialog
        d.set_model("本机模型 A", self.generate)
        d.instructions.setPlainText("问问对方开会的地点")
        d.generate_button.click()
        d.draft.setPlainText("我写的答复")
        self.finish()
        self.assertEqual(d.draft.toPlainText(), "我写的答复")
        d.generate_button.click()
        self.finish(ValueError("模型服务暂不可用"))
        self.assertIn("模型服务暂不可用", d.status.text())
        self.assertEqual(d.draft.toPlainText(), "我写的答复")
        d.send_button.click()
        self.finish(ConnectionError("请先核对已发送邮箱"))
        self.assertEqual(self.jobs, [])
        self.assertIn("请先核对已发送邮箱", d.status.text())
        self.assertTrue(d.send_button.isEnabled())
        d.close()
        d.show()
        self.assertEqual(d.draft.toPlainText(), "我写的答复")

    def test_plain_reply_is_default_and_draft_survives_opt_in_and_opt_out(self):
        d = self.dialog
        self.assertEqual(d.windowTitle(), "回复")
        for widget in (d.instructions, d.generate_button, d.ask_button, d.configure_button, d.model_hint):
            self.assertFalse(widget.isVisible())
        d.draft.setPlainText("手写答复")
        d.generate_button.click()
        self.assertEqual(self.jobs, [])
        self.assertTrue(d.send_button.isEnabled())
        d.set_model("本机模型 A", self.generate)
        self.assertTrue(d.generate_button.isVisible())
        configure = QSignalSpy(d.configure_requested)
        d.configure_button.click()
        self.assertEqual(configure.count(), 1)
        d.instructions.setPlainText("保留意思，修改语气")
        d.generate_button.click()
        d.set_model("", None)
        self.finish()
        self.generate.assert_not_called()
        self.assertEqual(d.draft.toPlainText(), "手写答复")
        self.assertFalse(d.status.isVisible())
        d.send_button.click()
        d.set_model("本机模型 B", self.generate)
        self.assertTrue(d.sending)
        self.assertFalse(d.close())
        self.finish()
        self.send.assert_called_once_with("手写答复")

    def test_switch_model_ignores_old_result_and_new_model_can_finish_while_hidden(self):
        d = self.dialog
        d.set_model("本机模型 A", self.generate)
        d.draft.setPlainText("我写的答复")
        alternate = Mock(return_value="另一个模型的草稿")
        d.instructions.setPlainText("缩短一句")
        d.generate_button.click()
        old_work, old_done = self.jobs.pop()
        old_result = old_work()
        d.set_model("本机模型 B", alternate)
        d.generate_button.click()
        old_done(old_result, None)
        self.assertEqual(d.draft.toPlainText(), "我写的答复")
        self.assertFalse(d.send_button.isEnabled())
        d.close()
        self.finish()
        d.show()
        self.assertIn("本机模型 B", d.model_hint.text())
        self.assertEqual(d.draft.toPlainText(), alternate.return_value)
        d.draft.undo()
        self.assertEqual(d.draft.toPlainText(), "我写的答复")

    def test_ask_then_rewrite_the_manually_edited_draft_without_sending(self):
        question = "对方要我确认什么？"
        first = ComposeTurn(question, "需要确认周五的会议时间。", None)
        rewrite = ComposeTurn("语气亲切一点", "已调整语气。", "周五我有空，期待见面！")
        converse = Mock(return_value=first)
        writer = Mock(return_value=rewrite)
        d = self.make_dialog(model_description="DeepSeek", converse=converse, draft_with=writer)
        self.assertEqual(self.jobs, [])
        self.assertFalse(d.generate_button.isEnabled())
        self.assertEqual(d.generate_button.text(), "按要求起草")
        d.instructions.setPlainText(question)
        d.ask_button.click()
        self.finish()
        converse.assert_called_with(question, "", ())
        self.assertIn(first.answer, d.assistant_transcript.toPlainText())
        self.assertTrue(d.draft.document().isEmpty())
        self.assertFalse(d.has_pending_draft)
        self.assertTrue(d.has_session_content)
        d.draft.setPlainText("周五我有空。")
        self.assertEqual(d.generate_button.text(), "按要求修改")
        d.instructions.setPlainText(rewrite.question)
        QTest.keyClick(d.instructions, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        self.finish()
        writer.assert_called_once_with(rewrite.question, "周五我有空。", (first,))
        converse.assert_called_once()
        self.assertEqual(d.assistant_history, [first, rewrite])
        self.assertEqual(d.draft.toPlainText(), rewrite.draft)
        d.draft.undo()
        self.assertEqual(d.draft.toPlainText(), "周五我有空。")
        self.send.assert_not_called()
        self.assertEqual(self.jobs, [])

    def test_concurrent_edit_retains_suggested_draft_and_model_switch_clears_history(self):
        turn = ComposeTurn("确认收到", "拟了一封确认信。", "谢谢来信，已收到。")
        writer = Mock(return_value=turn)
        d = self.dialog
        d.set_model("模型 A", self.generate, draft_with=writer)
        d.instructions.setPlainText("确认收到")
        d.generate_button.click()
        d.draft.setPlainText("用户刚输入的正文")
        self.finish()
        self.assertEqual(d.draft.toPlainText(), "用户刚输入的正文")
        self.assertIn(turn.draft, d.assistant_transcript.toPlainText())
        self.generate.assert_not_called()
        d.instructions.setPlainText("更简洁一点")
        d.generate_button.click()
        work, done = self.jobs.pop()
        old_result = work()
        d.set_model("模型 B", self.generate, draft_with=writer)
        done(old_result, None)
        self.assertEqual(d.assistant_history, [])
        self.assertEqual(d.assistant_transcript.toPlainText(), "")
        self.assertEqual(d.draft.toPlainText(), "用户刚输入的正文")
        d.instructions.setPlainText("请对方补充信息")
        d.generate_button.click()
        d.set_model("", None)
        self.finish()
        self.assertEqual(writer.call_count, 2)
        self.assertFalse(d.ai_tools.isVisible())
        self.assertEqual(d.draft.toPlainText(), "用户刚输入的正文")
        self.send.assert_not_called()

    def test_ime_candidate_confirmation_does_not_ask_or_send(self):
        d = self.dialog
        instruction = "会议在哪天？\n用一句话问对方。"
        writer = Mock(return_value=ComposeTurn(instruction, "已草拟。", "请问会议在哪一天？"))
        converse = Mock()
        d.set_model("模型 A", None, converse, writer)
        d.draft.setPlainText("谢谢邀请")
        editor = d.instructions
        editor.insertPlainText("会议在哪")
        preedit = QInputMethodEvent("tian", [])
        QApplication.sendEvent(editor, preedit)
        QTest.keyClick(editor, Qt.Key.Key_Return)
        QTest.keyClick(editor, Qt.Key.Key_Return, Qt.KeyboardModifier.MetaModifier)
        self.assertEqual(self.jobs, [])
        commit = QInputMethodEvent()
        commit.setCommitString("天？")
        QApplication.sendEvent(editor, commit)
        self.assertEqual(self.jobs, [])
        self.assertEqual(editor.toPlainText(), "会议在哪天？")
        QTest.keyClick(editor, Qt.Key.Key_Return)
        editor.insertPlainText("用一句话问对方。")
        self.assertEqual(self.jobs, [])
        self.assertEqual(editor.toPlainText(), instruction)
        QTest.keyClick(editor, Qt.Key.Key_Return, Qt.KeyboardModifier.MetaModifier)
        self.finish()
        writer.assert_called_once_with(instruction, "谢谢邀请", ())
        converse.assert_not_called()
        self.send.assert_not_called()
        self.assertEqual(self.jobs, [])

    def test_restore_conversation_is_a_context_only_handoff_to_an_empty_chat(self):
        d = self.dialog
        turns = [ComposeTurn("帮我回复第一封", "答复会确认周五的日程。", "回复正文建议")]
        d.restore_conversation(turns)
        self.assertEqual(d.assistant_history, [])
        converse = Mock(return_value=ComposeTurn("指定了地点吗", "没有指定地点。"))
        d.set_model("模型 A", self.generate, converse)
        d.draft.setPlainText("我会参加")
        document = d.draft.document()
        document.setModified(False)
        revision = document.revision()
        d.restore_conversation(turns)
        transcript = d.assistant_transcript.toPlainText()
        self.assertIn("来自邮箱助手", transcript)
        self.assertIn(turns[0].answer, transcript)
        self.assertNotIn(turns[0].draft, transcript)
        self.assertEqual(d.draft.toPlainText(), "我会参加")
        self.assertEqual(document.revision(), revision)
        self.assertFalse(document.isModified())
        self.assertEqual(self.jobs, [])
        d.restore_conversation([ComposeTurn("重复导入", "不会覆盖")])
        self.assertEqual(d.assistant_transcript.toPlainText(), transcript)
        self.assertEqual(d.assistant_history, turns)
        d.instructions.setPlainText("指定了地点吗")
        d.ask_button.click()
        self.finish()
        converse.assert_called_once_with("指定了地点吗", "我会参加", tuple(turns))
        self.send.assert_not_called()

    def test_long_requirements_survive_close_and_model_changes_until_submitted(self):
        d = self.dialog
        d.set_model("模型 A", self.generate)
        text = "请保留报价。\n" + "蓝" * 2000
        d.instructions.setPlainText(text)
        self.assertTrue(d.has_session_content)
        self.assertTrue(d.instruction_limit.isVisible())
        self.assertFalse(d.generate_button.isEnabled())
        d.close()
        d.show()
        d.set_model("", None)
        self.assertFalse(d.instructions.isVisible())
        d.set_model("模型 B", self.generate)
        self.assertEqual(d.instructions.toPlainText(), text)
        d.instructions.setPlainText(text[:2000])
        self.assertTrue(d.generate_button.isEnabled())
        self.assertFalse(d.instruction_limit.isVisible())
        self.assertEqual(self.jobs, [])

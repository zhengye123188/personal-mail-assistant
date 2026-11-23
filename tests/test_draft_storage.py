import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from maildesk.demo import DemoVault
from maildesk.models import MailItem, ReplyDraft, ReplyEnvelope
from maildesk.service import MailService


class DraftStorageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "accounts.sqlite"
        self.client = Mock()
        self.service = MailService(self.path, self.client, DemoVault())
        self.qq = self.service.bind("qq", "me@qq.com", "test")
        self.other = self.service.bind("163", "me@163.com", "test")

    def draft(self, address, validity="1"):
        item = MailItem(address, validity, 27, "会议安排", "李老师", address, "", 3.0)
        envelope = ReplyEnvelope("sender@example.org", "Re: 会议安排", "<id@example.org>",
                                 ("<id@example.org>",), (address, validity, 27))
        return ReplyDraft(item, envelope, "周五还是周六方便？", "周六上午可以。", "请用中文回复")

    def test_restart_restores_latest_text_and_reply_thread_without_mailbox_login(self):
        draft = self.draft(self.qq.address)
        self.service.save_draft(draft)
        saved = self.service.save_draft(replace(draft, text="周六下午更方便。"))
        offline = MailService(self.path, Mock(), DemoVault())
        self.assertEqual(offline.list_drafts(), [saved])
        self.assertEqual(offline.list_drafts(include_original=False), [replace(saved, original_text="")])
        self.assertEqual(offline.original_for_draft(draft.item), draft.original_text)
        self.assertGreater(saved.updated_at, 0)
        self.client.send_reply.assert_not_called()

    def test_same_uid_in_two_accounts_and_a_new_generation_stay_independent(self):
        drafts = [self.draft(self.qq.address), self.draft(self.other.address), self.draft(self.qq.address, "2")]
        for draft in drafts:
            self.service.save_draft(draft)
        self.service.delete_draft(drafts[0].item)
        self.service.delete_draft(drafts[0].item)
        self.assertCountEqual([d.item for d in self.service.list_drafts()], [d.item for d in drafts[1:]])
        with self.assertRaises(ValueError):
            self.service.save_draft(replace(drafts[1], envelope=drafts[0].envelope))

    def test_unfinished_instruction_survives_without_reply_address_and_clears_when_empty(self):
        draft = replace(self.draft(self.qq.address), envelope=None, text="", original_text="邮" * 50_000)
        self.service.save_draft(draft)
        restored, = self.service.list_drafts()
        self.assertIsNone(restored.envelope)
        self.assertEqual((restored.instructions, restored.original_text), (draft.instructions, "邮" * 32_768))
        self.service.save_draft(replace(restored, instructions=""))
        self.assertEqual(self.service.list_drafts(), [])

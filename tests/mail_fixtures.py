"""Minimal messages for account isolation, pagination, filter, and reply tests."""

from base64 import b64decode

from maildesk.demo import DemoClient
from maildesk.models import Account


ACCOUNTS = (Account("reader@qq.com", "qq"), Account("reviewer@163.com", "163"))
# A valid two-pixel PNG lets the reader tests distinguish loaded images from placeholders.
PNG = b64decode("iVBORw0KGgoAAAANSUhEUgAAAAIAAAABCAIAAAB7QOjdAAAAD0lEQVR4nGP4z8DA8J8BAAf/Af8Bf4mnAAAAAElFTkSuQmCC")


class FixtureClient(DemoClient):
    def __init__(self):
        # The same UIDs in different inboxes exercise the account portion of message IDs.
        super().__init__(
            examples={
                "qq": ((3, 1, "Reader", "读书会", "二楼阅览室。请确认能否提前十分钟布置。"),
                       (2, 3, "Reader", "QQ 2", "第二封。"),
                       (1, 5, "Reader", "QQ 1", "第一封。")),
                "163": ((3, 2, "Reviewer", "设计评审", "下周三下午两点设计评审，请确认时间。"),
                        (2, 4, "Reviewer", "校样核对", "请核对第二段里的作品名称。"),
                        (1, 6, "Reviewer", "163 1", "第一封。")),
            },
            senders={"Reader": "reader", "Reviewer": "reviewer"},
            arrival_details={"qq": ("Reader", "测试请求"), "163": ("Reviewer", "测试请求")},
        )

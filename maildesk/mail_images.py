"""Decode supported mail images within the reader's size limits."""

import base64
import binascii
import re

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QSize, Qt
from PySide6.QtGui import QImageReader


_MAX_BYTES = 6 * 1024 * 1024
_MAX_PIXELS = 16_000_000


def data_image(source):
    header, _, data = source.partition(",")
    if (len(data) > _MAX_BYTES * 4 // 3 + 8 or not re.fullmatch(
            r"data:image/(?:png|jpe?g|gif|webp);base64", header, re.I)):
        return b""
    try:
        return base64.b64decode("".join(data.split()), validate=True)
    except (ValueError, binascii.Error):
        return b""


def decode_raster(raw):
    if not raw or len(raw) > _MAX_BYTES:
        return None
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        image_format = b"png"
    elif raw.startswith(b"\xff\xd8\xff"):
        image_format = b"jpeg"
    elif raw.startswith((b"GIF87a", b"GIF89a")):
        image_format = b"gif"
    elif raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        image_format = b"webp"
    else:
        return None
    device = QBuffer()
    device.setData(QByteArray(raw))
    device.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(device, QByteArray(image_format))
    reader.setAutoDetectImageFormat(False)
    size = reader.size()
    if size.isEmpty() or max(size.width(), size.height()) > 16384 or size.width() * size.height() > _MAX_PIXELS:
        return None
    if max(size.width(), size.height()) > 1800:
        reader.setScaledSize(size.scaled(QSize(1800, 1800), Qt.AspectRatioMode.KeepAspectRatio))
    image = reader.read()
    return None if image.isNull() else image

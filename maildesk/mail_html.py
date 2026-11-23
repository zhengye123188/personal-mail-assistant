"""Prepare static HTML for Qt rich-text mail reading."""

import re
from collections.abc import Callable
from html import escape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from .models import MailBody, normalize_cid


_DEFAULT_STYLE = "img { max-width: 100%; } pre { white-space: pre-wrap; }"
_HIDDEN_STYLE = re.compile(r"(?:^|;)\s*display\s*:\s*none\s*(?:!\s*important\s*)?(?:;|$)", re.I)
_CLASS_RULE = re.compile(r"(?:^|(?<=}))\s*((?:\.[\w-]+\s*,\s*)*\.[\w-]+)\s*\{([^{}]*)}", re.MULTILINE)
_TAGS = set("""
    a abbr address article aside b bdi bdo big blockquote br button caption center cite code
    col colgroup dd del details dfn div dl dt em figure figcaption font footer h1 h2 h3 h4 h5 h6
    header hr i img ins kbd li main mark ol p pre q rp rt ruby s samp section small span strike
    strong sub summary sup table tbody td tfoot th thead time tr tt u ul var wbr
""".split())
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
_OMIT_CONTENT = set("""
    applet audio canvas datalist frameset iframe math noembed noscript object script select
    svg template textarea title video xml xmp
""".split())
# Legacy attributes such as cellpadding and bgcolor are still common in email.
_ATTRS = set("""
    align alt bgcolor border bordercolor cellpadding cellspacing char charoff class clear color
    cols colspan compact dir face frame headers height hidden id lang nowrap reversed role rows
    rowspan rules scope size span start style summary title type valign value width
""".split())
_WEB_IN_TEXT = re.compile(r"""https?://[^\s<>"'。，、；！？《》“”]+""", re.IGNORECASE)
_MAX_HTML = 8 * 1024 * 1024
_MAX_PARTS = 100_000


def safe_link(value: str, base: str = "") -> str | None:
    """Resolve a website link without admitting local files or executable schemes."""
    value = (value or "").strip()
    if (not value or len(value) > 8192 or "\\" in value or value.startswith("#")
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
        return None
    try:
        if value.startswith("//") and not base:
            value = "https:" + value
        url = urljoin(base, value) if base else value
        parts = urlsplit(url)
        if (parts.scheme not in {"https", "http"} or not parts.hostname or parts.username is not None
                or any(ch.isspace() for ch in parts.netloc)):
            return None
        _ = parts.port  # Reject malformed or out-of-range ports.
        return url
    except ValueError:
        return None


def _attributes(fields):
    return "".join(f' {name}="{escape(value, quote=True)}"' for name, value in fields.items())


def _layout_attrs(fields):
    attrs = {name: value or "" for name, value in fields.items() if name in _ATTRS or name == "aria-label"}
    for key in ("rowspan", "colspan"):
        if key in attrs and not re.fullmatch(r"[1-9][0-9]?", attrs[key]):
            del attrs[key]
    return attrs


def _tag_name(tag):
    prefix, sep, name = tag.partition(":")
    return name if sep and prefix in {"html", "xhtml"} else tag


class _MailParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.styles = []
        self.html_attrs = {}
        self.body_attrs = {}
        self.base = ""
        self.in_head = False
        self.in_style = False
        self.omitted = []
        self.hidden_classes = set()
        self.depth = 0
        self.has_content = False
        self.has_image = False

    def handle_starttag(self, tag, attrs):
        tag = _tag_name(tag)
        if len(self.parts) + len(self.styles) >= _MAX_PARTS:
            raise ValueError("mail markup exceeds preview limits")
        fields = dict(attrs)
        if tag not in _VOID:
            self.depth += 1
            if self.depth > 256:
                raise ValueError("mail markup nesting exceeds preview limits")
        if self.omitted or tag in _OMIT_CONTENT or (
                "hidden" in fields or _HIDDEN_STYLE.search(fields.get("style") or "")
                or self.hidden_classes.intersection((fields.get("class") or "").split())):
            if tag not in _VOID:
                self.omitted.append(tag)
            return
        if tag == "head":
            self.in_head = True
        elif tag == "html":
            self.html_attrs = _layout_attrs(fields)
        elif tag == "body":
            self.in_head = False
            self.body_attrs = _layout_attrs(fields)
            for name in ("text", "link", "vlink", "alink"):
                if fields.get(name):
                    self.body_attrs[name] = fields[name]
        elif tag == "style":
            if (fields.get("type") or "text/css").lower() == "text/css":
                media = {"media": fields["media"]} if fields.get("media") else {}
                self.styles.append(f"<style{_attributes(media)}>")
                self.in_style = True
            else:
                self.omitted.append("style")
        elif tag == "base" and not self.base:
            self.base = fields.get("href") or ""
        elif not self.in_head and tag in _TAGS:
            clean = _layout_attrs(fields)
            if tag == "a":
                if fields.get("name"):
                    clean["name"] = fields["name"]
                self.parts.append((tag, clean, fields.get("href") or ""))
            elif tag == "img":
                src = fields.get("src") or fields.get("data-src") or ""
                self.parts.append((tag, clean, src))
                self.has_image = True
            else:
                if tag == "button":
                    clean["type"] = "button"
                self.parts.append(f"<{tag}{_attributes(clean)}>")

    def handle_endtag(self, tag):
        tag = _tag_name(tag)
        if tag not in _VOID:
            self.depth = max(0, self.depth - 1)
        if self.omitted:
            if self.omitted[-1] == tag:
                self.omitted.pop()
            return
        if tag == "style" and self.in_style:
            self.styles.append("</style>")
            self.in_style = False
        elif tag == "head":
            self.in_head = False
        elif not self.in_head and tag in _TAGS and tag not in _VOID:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        if self.omitted:
            return
        if self.in_style:
            for rule in _CLASS_RULE.finditer(data):
                if _HIDDEN_STYLE.search(rule[2]):
                    self.hidden_classes.update(name.strip()[1:] for name in rule[1].split(","))
            self.styles.append(data.replace("</", r"\3c /"))
        elif not self.in_head:
            self.parts.append(escape(data, quote=False))
            if data.strip():
                self.has_content = True


def _image_source(raw, base, inline):
    value = normalize_cid(raw)
    if not value:
        return None
    if value.startswith("cid:") or value in inline:
        return value
    if re.match(r"data:image/(?:png|jpe?g|gif|webp);base64,", value, re.I):
        return value
    return safe_link(value, base)


def plain_body_html(text):
    chunks = []
    offset = 0
    for match in _WEB_IN_TEXT.finditer(text):
        value = match.group().rstrip(".,;:!?")
        if value.endswith(")"):
            trailing = len(value) - len(value.rstrip(")"))
            excess = min(trailing, value.count(")") - value.count("("))
            if excess > 0:
                value = value[:-excess]
        link = safe_link(value)
        if not link:
            continue
        chunks.append(escape(text[offset:match.start()]))
        chunks.append(f'<a href="{escape(link)}" rel="noreferrer">{escape(value)}</a>')
        offset = match.start() + len(value)
    chunks.append(escape(text[offset:]))
    return '<div style="white-space:pre-wrap">' + "".join(chunks) + "</div>"


def build_mail_html(content: MailBody, image_url: Callable[[str], str | None]) -> str:
    """Map mail images to the current document and preserve basic sender formatting."""
    parser = _MailParser()
    if content.html and len(content.html) <= _MAX_HTML:
        try:
            parser.feed(content.html)
            parser.close()
        except (ValueError, AssertionError, RecursionError):
            parser = _MailParser()

    body = []
    if parser.has_content or parser.has_image:
        mime_base = safe_link(content.base_url) or ""
        base = safe_link(parser.base, mime_base) or mime_base
        mapped = {}
        for part in parser.parts:
            if isinstance(part, str):
                body.append(part)
                continue
            tag, attrs, raw = part
            if tag == "a":
                target = raw.strip()
                fragment = (target.startswith("#") and len(target) <= 8192
                            and all(ord(ch) >= 32 for ch in target))
                link = target if fragment else safe_link(target, base)
                if link:
                    attrs.update(href=link, rel="noreferrer")
            else:
                source = _image_source(raw, base, content.inline_images)
                if source:
                    if source not in mapped:
                        mapped[source] = image_url(source)
                    url = mapped[source]
                    if url and url.startswith("maildesk:"):
                        attrs["src"] = url
                if "src" not in attrs:
                    body.append(escape(attrs.get("alt") or "［图片］"))
                    continue
                # Qt parses HTML percentage widths as negative pixel sizes. Its
                # native max-width property resizes the decoded image with the viewport.
                width = attrs.get("width", "").strip()
                if re.fullmatch(r"[0-9]{1,3}%", width):
                    attrs.pop("width")
                    percent = min(100, int(width[:-1]))
                    attrs["style"] = f'{attrs.get("style", "")};max-width:{percent}%'
                if width:
                    attrs.pop("height", None)
            body.append(f"<{tag}{_attributes(attrs)}>")
    else:
        body.append(plain_body_html(content.text))
    return (f'<!doctype html><html{_attributes(parser.html_attrs)}><head><meta charset="utf-8">'
            f'<style>{_DEFAULT_STYLE}</style>{"".join(parser.styles)}</head>'
            f'<body{_attributes(parser.body_attrs)}>{"".join(body)}</body></html>')

"""Build a static browser document from an HTML or plain-text email."""

import re
from collections.abc import Callable
from html import escape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from .models import MailBody, normalize_cid


# Chromium can use the sender's CSS, but all image bytes must come from the
# reader's per-message handler. This also applies to URLs inside stylesheets.
_CSP = ("default-src 'none'; img-src maildesk:; style-src 'unsafe-inline'; "
        "base-uri 'none'; form-action 'none'; object-src 'none'; frame-src 'none'")
_DEFAULT_STYLE = """
:root { color-scheme: light; }
:where(body) { margin: 16px 20px; font: 15px/1.5 -apple-system, BlinkMacSystemFont, sans-serif;
               overflow-wrap: break-word; }
:where(body:not([bgcolor])) { background: white; }
:where(body:not([text])) { color: #24292f; }
:where(body:not([link]) a[href]) { color: #0969da; }
:where(table, img, svg) { max-width: 100%; }
:where(img[data-maildesk-sized]) {
    aspect-ratio: attr(width type(<number>)) / attr(height type(<number>)); height: auto;
}
img[data-maildesk-image="pending"] { max-height: 500px; object-fit: contain; }
img[data-maildesk-image="failed"] { width: auto !important; height: auto !important;
    min-width: 0 !important; min-height: 0 !important; max-width: 100% !important;
    max-height: 2em !important; }
:where(pre) { white-space: pre-wrap; }
"""
_TAGS = set("""
    a abbr address article aside b bdi bdo big blockquote br button caption center cite code
    col colgroup dd del details dfn div dl dt em figure figcaption font footer h1 h2 h3 h4 h5 h6
    header hr i img ins kbd li main mark ol p pre q rp rt ruby s samp section small span strike
    strong sub summary sup table tbody td tfoot th thead time tr tt u ul var wbr
""".split())
_VOID = {"br", "col", "hr", "img", "wbr"}
_OMIT_CONTENT = set("""
    applet audio canvas datalist frameset iframe math noembed noscript object script select
    template textarea title video xml xmp
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
_PIXEL_SIZE = re.compile(r"[1-9][0-9]{0,4}")
_SVG_TAGS = {"svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "title", "desc"}
_SVG_ATTRS = set("""
    aria-hidden aria-label class color cx cy d fill fill-opacity fill-rule height opacity points
    r role rx ry stroke stroke-linecap stroke-linejoin stroke-opacity stroke-width style transform
    width x x1 x2 y y1 y2
""".split())


def _svg_attrs(fields):
    result = {name: value or "" for name, value in fields.items() if name in _SVG_ATTRS}
    for name in ("fill", "stroke"):
        if name in result and not re.fullmatch(r"[#\w\s.,%()+/-]+", result[name]):
            del result[name]
        elif name in result and "url(" in result[name].lower():
            del result[name]
    for name in ("viewBox", "preserveAspectRatio"):
        if fields.get(name.lower()):
            result[name] = fields[name.lower()]
    return result


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
    return {name: value or "" for name, value in fields.items() if name in _ATTRS or name == "aria-label"}


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
        self.vector = []
        self.has_content = False

    def handle_starttag(self, tag, attrs):
        tag = _tag_name(tag)
        if len(self.parts) + len(self.styles) >= _MAX_PARTS:
            raise ValueError("mail markup exceeds preview limits")
        if self.vector or (tag == "svg" and not self.omitted and not self.in_head):
            shown = (not self.vector or self.vector[-1][1]) and tag in _SVG_TAGS
            self.vector.append((tag, shown))
            if len(self.vector) > 64:
                raise ValueError("vector nesting exceeds preview limits")
            if shown:
                self.parts.append(f"<{tag}{_attributes(_svg_attrs(dict(attrs)))}>")
                self.has_content = True
            return
        if tag in _OMIT_CONTENT:
            self.omitted.append(tag)
        if self.omitted:
            return
        fields = dict(attrs)
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
                self.has_content = True
            else:
                if tag == "button":
                    clean["type"] = "button"
                self.parts.append(f"<{tag}{_attributes(clean)}>")

    def handle_endtag(self, tag):
        tag = _tag_name(tag)
        if self.vector:
            for index in range(len(self.vector) - 1, -1, -1):
                if self.vector[index][0] == tag:
                    for name, shown in reversed(self.vector[index:]):
                        if shown:
                            self.parts.append(f"</{name}>")
                    del self.vector[index:]
                    break
            return
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
        if self.vector:
            if self.vector[-1] in (("title", True), ("desc", True)):
                self.parts.append(escape(data, quote=False))
            return
        if self.omitted:
            return
        if self.in_style:
            # A permissive browser also accepts end tags like </style foo>.
            # Never let parser differences reintroduce markup through CSS text.
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


def _plain_html(text):
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
    """Keep template layout, mapping each unique image source to a reader-owned URL."""
    parser = _MailParser()
    if content.html and len(content.html) <= _MAX_HTML:
        try:
            parser.feed(content.html)
            parser.close()
        except (ValueError, AssertionError, RecursionError):
            parser = _MailParser()

    body = []
    if parser.has_content:
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
                        if source not in content.inline_images and safe_link(source):
                            attrs["data-maildesk-image"] = "pending"
                if "alt" not in attrs and "src" not in attrs:
                    attrs["alt"] = "图片未能显示"
                if all(_PIXEL_SIZE.fullmatch(attrs.get(dim, "").strip()) for dim in ("width", "height")):
                    attrs["data-maildesk-sized"] = ""
            body.append(f"<{tag}{_attributes(attrs)}>")
    else:
        body.append(_plain_html(content.text))
    return (f'<!doctype html><html{_attributes(parser.html_attrs)}><head><meta charset="utf-8">'
            f'<meta http-equiv="Content-Security-Policy" content="{_CSP}">'
            '<meta name="referrer" content="no-referrer">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<style>{_DEFAULT_STYLE}</style>{"".join(parser.styles)}</head>'
            f'<body{_attributes(parser.body_attrs)}>{"".join(body)}</body></html>')

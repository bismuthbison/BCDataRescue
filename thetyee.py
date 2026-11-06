"""The Tyee news support for bcdata_tui: RSS feed listing and a plain-text article reader.

No UI code here, so it can be tested on its own.

How it works
  * The list of articles comes from the public RSS feed (https://thetyee.ca/rss2.xml). The feed only carries
    a one-sentence summary per article, so the full text is fetched from the article page itself, and only
    when the reader asks for it (never in bulk), then converted to plain text.
  * Each article's lead image (the feed's <enclosure>) is fetched the same way: only when the reader opens the
    article, only from thetyee.ca, and kept in memory.
  * Nothing is written to disk. Articles are shown for reading, not archived: they are The Tyee's copyrighted
    work (see https://thetyee.ca), unlike the open-licensed government data the rest of this tool handles.
  * The feed and the article pages are untrusted input from the network. The XML is parsed with entity
    resolution and network access switched off, and article URLs are only fetched when they point at
    thetyee.ca, so a tampered feed can't make this tool request other (e.g. internal) addresses.
"""
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import requests
from lxml import etree, html as lxml_html

FEED_URL = "https://thetyee.ca/rss2.xml"
ALLOWED_HOSTS = {"thetyee.ca", "www.thetyee.ca"}
HEADERS = {"User-Agent": "BCDataRescue/1.0 (open-data archiver)"}
MAX_PAGE_BYTES = 5 * 1024 ** 2  # refuse absurdly large responses; a normal article page is ~100 KB
DC = "{http://purl.org/dc/elements/1.1/}"  # Dublin Core namespace used by the feed (creator, subject)

# Parts of an article page that are not article text: promos, figures/captions, navigation, forms, scripts.
SKIP_TAGS = {"aside", "figure", "nav", "form", "script", "style", "noscript", "footer", "header", "iframe", "button"}


def is_allowed_url(url: str) -> bool:
    """Only https URLs on thetyee.ca may be fetched (guards against a feed pointing somewhere else)."""
    p = urlparse(url)
    return p.scheme == "https" and (p.hostname or "").lower() in ALLOWED_HOSTS


def _get(url: str, timeout=(10, 30)) -> bytes:
    """GET with a size cap, following redirects but re-checking that we are still on an allowed host."""
    with requests.get(url, headers=HEADERS, timeout=timeout, stream=True) as r:
        r.raise_for_status()
        if not is_allowed_url(r.url):
            raise ValueError(f"redirected away from thetyee.ca ({urlparse(r.url).hostname})")
        data = b""
        for chunk in r.iter_content(1 << 16):
            data += chunk
            if len(data) > MAX_PAGE_BYTES:
                raise ValueError("response too large")
        return data


def fetch_feed() -> list[dict]:
    """The latest articles, newest first. Each is a dict:
    key, title, link, summary, author, published (datetime, local time), topics (list of str),
    image (URL of the lead image, or None)."""
    # Hardened parser: no entity expansion, no network access, no huge documents.
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)
    root = etree.fromstring(_get(FEED_URL), parser)
    items = []
    for it in root.iter("item"):
        link = (it.findtext("link") or "").strip()
        title = " ".join((it.findtext("title") or "").split())
        if not link or not title or not is_allowed_url(link):
            continue  # skip anything that isn't a normal thetyee.ca article
        try:
            published = parsedate_to_datetime(it.findtext("pubDate")).astimezone()  # to the local time zone
        except (TypeError, ValueError):
            published = None
        enc = it.find("enclosure")  # the feed attaches one lead picture to each article
        image = enc.get("url") if enc is not None and (enc.get("type") or "").startswith("image/") else None
        items.append({
            "image": image if image and is_allowed_url(image) else None,  # same host rule as the articles
            "key": "news:" + link, "title": title, "link": link,
            "summary": " ".join((it.findtext("description") or "").split()),
            "author": " ".join((it.findtext(DC + "creator") or "").split()),
            "published": published,
            "topics": [t.text.strip() for t in it.findall(DC + "subject") if t.text and t.text.strip()],
        })
    items.sort(key=lambda a: a["published"] or datetime.min.astimezone(), reverse=True)
    return items


def fetch_image(url: str) -> bytes:
    """The raw bytes of an article image (size-capped). Only thetyee.ca URLs are fetched."""
    if not is_allowed_url(url):
        raise ValueError("not a thetyee.ca URL")
    return _get(url)


def fetch_article(url: str) -> list[tuple[str, str]]:
    """The article's text as (kind, text) blocks. kind is "h" (heading), "p" (paragraph), "quote" or "li".
    Raises ValueError for a disallowed URL or if no article body can be found (e.g. the page layout changed)."""
    if not is_allowed_url(url):
        raise ValueError("not a thetyee.ca URL")
    doc = lxml_html.fromstring(_get(url))
    bodies = doc.xpath("//article[contains(@class, 'article__body')]")
    if not bodies:
        raise ValueError("couldn't find the article text on the page (the site layout may have changed)")
    body = bodies[0]

    blocks: list[tuple[str, str]] = []
    for el in body.iter("p", "h2", "h3", "h4", "li", "blockquote"):
        ancestors = {a.tag for a in el.iterancestors() if a is not body}
        if ancestors & SKIP_TAGS:
            continue  # inside a promo box, caption, etc.
        if el.tag == "blockquote" and el.find(".//p") is not None:
            continue  # its <p> children are emitted individually, as quotes
        text = " ".join(el.text_content().split())
        if not text or text.startswith("Read more:"):  # "Read more: Topic, Topic" is a row of site links
            continue
        if el.tag in ("h2", "h3", "h4"):
            kind = "h"
        elif el.tag == "li":
            kind = "li"
        elif el.tag == "blockquote" or "blockquote" in ancestors:
            kind = "quote"
        else:
            kind = "p"
        if not blocks or blocks[-1] != (kind, text):  # drop immediate duplicates
            blocks.append((kind, text))
    if not blocks:
        raise ValueError("the article page had no readable text")
    return blocks

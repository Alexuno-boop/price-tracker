"""Fetch and parse Amazon / Flipkart product pages.

Only hosts on the allow-list are ever fetched, and every redirect is re-checked,
so users cannot make the server request arbitrary URLs (SSRF protection).
"""
from __future__ import annotations

import html as htmllib
import json
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}
MAX_BYTES = 6 * 1024 * 1024
MAX_REDIRECTS = 6
TIMEOUT = 20

AMAZON_TLDS = ("in", "com", "co.uk", "de", "fr", "it", "es", "ca", "com.au", "co.jp",
               "ae", "sa", "sg", "nl", "se", "pl", "com.tr", "com.mx", "com.br", "eg", "com.be")
AMAZON_SHORT = {"amzn.in", "amzn.to", "amzn.eu", "amzn.com", "a.co"}
FLIPKART_SHORT = {"fkrt.it", "fkrt.cc", "fkrt.co", "fkrt.to"}


class ScrapeError(Exception):
    """A user-facing error explaining why a price could not be read."""


@dataclass
class ProductInfo:
    site: str
    url: str
    title: str | None
    price: float
    image: str | None


def site_for_host(host: str | None) -> str | None:
    host = (host or "").lower().rstrip(".")
    if host in AMAZON_SHORT:
        return "amazon"
    for tld in AMAZON_TLDS:
        if host == f"amazon.{tld}" or host.endswith(f".amazon.{tld}"):
            return "amazon"
    if host in FLIPKART_SHORT or host == "flipkart.com" or host.endswith(".flipkart.com"):
        return "flipkart"
    return None


def check_url(url: str) -> tuple[str, str]:
    """Return (normalised url, site) or raise ScrapeError if the URL is not allowed."""
    url = (url or "").strip()
    if not url:
        raise ScrapeError("Paste a product link.")
    if len(url) > 2000:
        raise ScrapeError("That link is too long.")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or parts.port not in (None, 80, 443) or parts.username:
        raise ScrapeError("Only Amazon and Flipkart product links are supported.")
    site = site_for_host(parts.hostname)
    if not site:
        raise ScrapeError("Only Amazon and Flipkart product links are supported.")
    return url, site


def normalize_url(url: str, site: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if site == "amazon":
        m = re.search(r"/(?:dp|gp/product|gp/aw/d|exec/obidos/ASIN)/([A-Z0-9]{10})", url)
        if m and host.startswith(("www.amazon.", "amazon.")):
            return f"https://{host}/dp/{m.group(1)}"
    if site == "flipkart" and host.endswith("flipkart.com") and "/p/" in parts.path:
        m = re.search(r"(?:^|&)pid=([A-Za-z0-9]+)", parts.query)
        q = f"?pid={m.group(1)}" if m else ""
        return f"https://www.flipkart.com{parts.path}{q}"
    return url


def fetch(url: str) -> tuple[str, str]:
    """GET a page following redirects manually so each hop is allow-list checked."""
    session = requests.Session()
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        check_url(current)
        try:
            r = session.get(current, headers=HEADERS, timeout=TIMEOUT, allow_redirects=False, stream=True)
        except requests.RequestException as e:
            raise ScrapeError(f"Could not reach the store ({type(e).__name__}). Try again later.")
        if r.is_redirect or r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("Location")
            r.close()
            if not loc:
                break
            current = urljoin(current, loc)
            continue
        if r.status_code in (403, 429, 503, 529):
            r.close()
            raise ScrapeError(f"The store blocked this check (HTTP {r.status_code}). It will be retried later.")
        if r.status_code == 404:
            r.close()
            raise ScrapeError("Product page not found (404). The link may be wrong or the listing removed.")
        if r.status_code >= 400:
            r.close()
            raise ScrapeError(f"The store returned an error (HTTP {r.status_code}).")
        chunks, size = [], 0
        for chunk in r.iter_content(64 * 1024):
            size += len(chunk)
            if size > MAX_BYTES:
                r.close()
                raise ScrapeError("The product page was unexpectedly large.")
            chunks.append(chunk)
        enc = r.encoding if r.encoding and r.encoding.lower() != "iso-8859-1" else "utf-8"
        return b"".join(chunks).decode(enc, errors="replace"), current
    raise ScrapeError("Too many redirects.")


def _num(s: str | None) -> float | None:
    if not s:
        return None
    clean = re.sub(r"[^\d.]", "", s)
    try:
        return float(clean) if clean else None
    except ValueError:
        return None


def _clean(s: str | None) -> str | None:
    if s is None:
        return None
    return re.sub(r"\s+", " ", htmllib.unescape(str(s))).strip() or None


def parse_amazon(page: str) -> tuple[str | None, float, str | None]:
    if re.search(r"validateCaptcha|Type the characters you see in this image|api-services-support@amazon\.com", page):
        raise ScrapeError("Amazon showed a bot-check page instead of the product. It will be retried later.")
    title = price = image = None
    m = re.search(r'id="productTitle"[^>]*>\s*([^<]+)', page) or re.search(r'<meta name="title" content="([^"]+)"', page)
    if m:
        title = _clean(m.group(1))
    for anchor in ("apex-pricetopay-value", "corePriceDisplay_desktop_feature_div", "corePrice_feature_div",
                   "corePrice_desktop", "apex_desktop"):
        i = page.find(anchor)
        if i >= 0:
            m = re.search(r'a-price-whole">([\d,]+)', page[i:i + 6000])
            if m:
                price = _num(m.group(1))
                break
    if not price:
        m = re.search(r'id="priceblock_(?:ourprice|dealprice|saleprice)"[^>]*>([^<]+)', page) \
            or re.search(r'a-price-whole">([\d,]+)', page)
        if m:
            price = _num(m.group(1))
    for pat in (r'"hiRes":"(https://[^"]+)"', r'data-old-hires="(https://[^"]+)"',
                r'id="landingImage"[^>]*src="(https://[^"]+)"'):
        m = re.search(pat, page)
        if m:
            image = m.group(1)
            break
    if not price:
        if re.search(r'id="outOfStock"|Currently unavailable', page):
            raise ScrapeError("Product is currently unavailable on Amazon.")
        raise ScrapeError("Could not find a price on the Amazon page.")
    return title, price, image


def parse_flipkart(page: str) -> tuple[str | None, float, str | None]:
    title = price = image = None
    for m in re.finditer(r'(?s)<script[^>]*application/ld\+json[^>]*>(.*?)</script>', page):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if not isinstance(item, dict) or item.get("@type") != "Product":
                continue
            title = title or _clean(item.get("name"))
            img = item.get("image")
            if not image and img:
                image = img[0] if isinstance(img, list) else img
            offer = item.get("offers")
            offer = offer[0] if isinstance(offer, list) and offer else offer
            if isinstance(offer, dict):
                price = _num(str(offer.get("price") or offer.get("lowPrice") or ""))
        if price:
            break
    if not title:
        m = re.search(r'<meta property="og:title" content="([^"]+)"', page)
        title = _clean(m.group(1)) if m else None
    if not image:
        m = re.search(r'<meta property="og:image" content="([^"]+)"', page)
        image = m.group(1) if m else None
    if not price:
        m = re.search(r">₹([\d,]+)<", page)
        price = _num(m.group(1)) if m else None
    if not price:
        if re.search(r"Sold Out|Currently Unavailable|Coming Soon", page):
            raise ScrapeError("Product is sold out / unavailable on Flipkart.")
        raise ScrapeError("Could not find a price on the Flipkart page.")
    return title, price, image


def get_product_info(url: str) -> ProductInfo:
    url, site = check_url(url)
    page, final_url = fetch(url)
    site = site_for_host(urlsplit(final_url).hostname) or site
    title, price, image = parse_amazon(page) if site == "amazon" else parse_flipkart(page)
    if image and not image.startswith("https://"):
        image = None
    return ProductInfo(site=site, url=normalize_url(final_url, site), title=title, price=price, image=image)

#!/usr/bin/env python3
"""Read-only catalogue reader for the Al-Nukhba store.

It opens the PUBLIC pages of the store website (the same pages every visitor sees) with plain GET
requests, one page at a time, slowly, and writes the result as static JSON files that the phone app
reads. It never logs in, never submits a form and never writes anything to the website or to the ERP.

Output (folder given by --out):
    v1/catalog.json       compact list of products + categories   (what the app downloads first)
    v1/products/<id>.json full details of one product              (description, gallery, status)
    v1/state.json         what this run remembers for the next run (statuses, mapping)
    index.html, .nojekyll

Availability follows exactly what the website shows: the page of a product that is sold out says
"OutOfStock", otherwise "InStock".
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from bs4 import BeautifulSoup

USER_AGENT = "AlNukhbaCatalogBot/1.0 (read-only catalogue sync for the store's own mobile app)"

# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def parse_number(text: str) -> Optional[float]:
    """'1,590.000 ل.د' -> 1590.0 ; Arabic digits and separators are understood."""
    if text is None:
        return None
    s = str(text).translate(_ARABIC_DIGITS)
    s = s.replace("٬", ",").replace("٫", ".").replace(" ", " ")
    m = re.search(r"\d[\d,\s]*(?:\.\d+)?", s)
    if not m:
        return None
    raw = m.group(0).replace(",", "").replace(" ", "")
    try:
        return float(raw)
    except ValueError:
        return None


def trailing_id(path: str) -> Optional[int]:
    """'/ar/shop/some-name-75?x=1' -> 75 ; '/shop/75' -> 75"""
    p = urllib.parse.unquote(urllib.parse.urlparse(path).path).rstrip("/")
    m = re.search(r"-(\d+)$", p) or re.search(r"/(\d+)$", p)
    return int(m.group(1)) if m else None


def clean_text(s: str) -> str:
    s = (s or "").replace("\r", "")
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in s.split("\n")]
    out: List[str] = []
    for ln in lines:
        if ln or (out and out[-1]):
            out.append(ln)
    return "\n".join(out).strip()


def absolute(site: str, path: str) -> str:
    return urllib.parse.urljoin(site.rstrip("/") + "/", path)


# ---------------------------------------------------------------------------------------------
# Parsers (pure functions: HTML in, data out). Covered by tests/test_parsers.py.
# ---------------------------------------------------------------------------------------------
@dataclass
class Card:
    id: int
    name: str
    price: float
    old_price: Optional[float]
    href: str


def parse_listing(html: str) -> List[Card]:
    soup = BeautifulSoup(html, "lxml")
    cards: List[Card] = []
    for el in soup.select(".oe_product"):
        link = el.select_one("a.oe_product_image_link") or el.select_one("a[href*='/shop/']")
        if not link or not link.get("href"):
            continue
        href = link["href"].split("?")[0]
        pid = trailing_id(href)
        if pid is None:
            continue
        name = (el.get("data-name") or "").strip()
        if not name:
            title = el.select_one(".o_wsale_products_item_title") or el.select_one("h6") or link
            name = title.get_text(" ", strip=True)
        price_box = el.select_one(".product_price") or el
        old_price: Optional[float] = None
        price: Optional[float] = None
        for val in price_box.select(".oe_currency_value"):
            number = parse_number(val.get_text())
            if number is None:
                continue
            if val.find_parent("del") is not None:
                old_price = number
            else:
                price = number
        cards.append(Card(pid, name, price if price is not None else 0.0, old_price, href))
    return cards


def parse_last_page(html: str) -> int:
    """Highest page number mentioned by the pager (1 when there is no pager)."""
    soup = BeautifulSoup(html, "lxml")
    best = 1
    for a in soup.select(".pagination a"):
        href = a.get("href") or ""
        m = re.search(r"/page/(\d+)", href) or re.search(r"[?&]page=(\d+)", href)
        if m:
            best = max(best, int(m.group(1)))
        text = (a.get_text(" ", strip=True) or "").translate(_ARABIC_DIGITS)
        if re.fullmatch(r"\d+", text):
            best = max(best, int(text))
    return best


@dataclass
class Category:
    id: int
    name: str
    href: str
    depth: int


def parse_categories(html: str) -> List[Category]:
    """Top-level categories of the shop sidebar (children are folded into their parent by the shop)."""
    soup = BeautifulSoup(html, "lxml")
    found: Dict[int, Category] = {}
    for a in soup.select("a[href*='/shop/category/']"):
        href = a["href"].split("?")[0]
        cid = trailing_id(href)
        name = a.get_text(" ", strip=True)
        if cid is None or not name:
            continue
        depth = len(a.find_parents(["ul", "ol"]))
        if cid not in found or depth < found[cid].depth:
            found[cid] = Category(cid, name, href, depth)
    if not found:
        return []
    top = min(c.depth for c in found.values())
    return [c for c in found.values() if c.depth == top]


@dataclass
class BrandFilter:
    value: str
    name: str


BRAND_TITLES = ("العلامة التجارية", "العلامه التجاريه", "الماركة", "brand")


def parse_brand_filters(html: str) -> List[BrandFilter]:
    soup = BeautifulSoup(html, "lxml")
    out: List[BrandFilter] = []
    seen = set()
    for item in soup.select(".accordion-item"):
        title = item.select_one(".accordion-header")
        if not title:
            continue
        t = title.get_text(" ", strip=True).lower()
        if not any(k in t for k in BRAND_TITLES):
            continue
        for inp in item.select("input[name='attribute_value']"):
            value = inp.get("value", "")
            label = item.select_one(f"label[for='{inp.get('id', '')}']") if inp.get("id") else None
            if label is None:
                label = inp.find_next("label")
            name = label.get_text(" ", strip=True) if label else ""
            if value and name and value not in seen:
                seen.add(value)
                out.append(BrandFilter(value, name))
    return out


AVAILABILITY_MAP = {
    "instock": "available",
    "onlineonly": "available",
    "outofstock": "out",
    "soldout": "out",
    "discontinued": "out",
    "limitedavailability": "low",
    "preorder": "on_request",
    "backorder": "on_request",
    "presale": "on_request",
}


def map_availability(value: str) -> Optional[str]:
    tail = (value or "").rsplit("/", 1)[-1].strip().lower()
    return AVAILABILITY_MAP.get(tail)


def _iter_ld_objects(soup: BeautifulSoup) -> Iterable[Dict[str, Any]]:
    for s in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(s.string or s.get_text() or "")
        except (ValueError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        for obj in stack:
            if isinstance(obj, dict):
                yield obj


def _image_key(path: str) -> str:
    base = urllib.parse.urlparse(path).path
    m = re.match(r"(/web/image/[^/]+/\d+)", base)
    return m.group(1) if m else base


def parse_product(html: str, site: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    detail: Dict[str, Any] = {"stock": None, "description": "", "images": [], "name": ""}

    for obj in _iter_ld_objects(soup):
        if obj.get("@type") != "Product":
            continue
        detail["name"] = str(obj.get("name") or "").strip()
        desc = obj.get("description") or ""
        detail["description"] = clean_text(BeautifulSoup(str(desc), "lxml").get_text("\n"))
        offers = obj.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        if isinstance(offers, dict):
            detail["stock"] = map_availability(str(offers.get("availability") or ""))
            detail["ld_price"] = parse_number(str(offers.get("price") or ""))
        break

    seen = set()
    images: List[str] = []
    for img in soup.select("#o-carousel-product img, .o_wsale_product_images img, #product_detail_main img"):
        src = img.get("src") or img.get("data-src") or ""
        if "/web/image/product." not in src:
            continue
        key = _image_key(src)
        if key in seen:
            continue
        seen.add(key)
        path = urllib.parse.urlparse(src).path
        path = re.sub(r"/image_\d+(?=/|$)", "/image_1024", path)
        images.append(absolute(site, path))
    detail["images"] = images

    return detail


# ---------------------------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------------------------
class Blocked(Exception):
    """The website asked us to stop (403 / 429). We stop too and keep the previous data."""


class Http:
    def __init__(self, site: str, min_interval: float = 1.1, timeout: float = 30.0, retries: int = 3):
        self.site = site.rstrip("/")
        self.min_interval = min_interval
        self.timeout = timeout
        self.retries = retries
        self._last = 0.0
        self.requests = 0

    def get(self, path_or_url: str) -> str:
        url = absolute(self.site, path_or_url)
        for attempt in range(1, self.retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.requests += 1
            req = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/json", "Accept-Language": "ar"},
                method="GET",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                if e.code in (403, 429):
                    raise Blocked(f"{e.code} from {url}") from e
                if e.code == 404:
                    raise
                if attempt == self.retries:
                    raise
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt == self.retries:
                    raise
            time.sleep(3 * attempt)
        raise RuntimeError("unreachable")

    def robots_allow(self, path: str) -> bool:
        if not hasattr(self, "_robots"):
            self._robots = None
            try:
                rp = urllib.robotparser.RobotFileParser()
                rp.parse(self.get("/robots.txt").splitlines())
                self._robots = rp
            except Exception:
                self._robots = None
        if self._robots is None:
            return True
        return self._robots.can_fetch(USER_AGENT, absolute(self.site, path))


# ---------------------------------------------------------------------------------------------
# Crawl steps
# ---------------------------------------------------------------------------------------------
def shop_page_url(prefix: str, page: int, category_href: Optional[str] = None, query: str = "") -> str:
    if category_href:
        base = category_href.rstrip("/")
    else:
        base = f"{prefix}/shop"
    path = base if page <= 1 else f"{base}/page/{page}"
    return path + (f"?{query}" if query else "")


def crawl_listing(http: Http, prefix: str, category_href: Optional[str] = None, query: str = "",
                  max_pages: int = 200) -> List[Card]:
    cards: List[Card] = []
    seen = set()
    last = 1
    per_page = 0
    for page in range(1, max_pages + 1):
        html = http.get(shop_page_url(prefix, page, category_href, query))
        batch = parse_listing(html)
        if page == 1:
            last = parse_last_page(html)
            per_page = len(batch)
        fresh = [c for c in batch if c.id not in seen]
        for c in fresh:
            seen.add(c.id)
        cards.extend(fresh)
        if not fresh:
            break
        if last > 1:
            if page >= last:
                break
        elif len(batch) < max(per_page, 12):
            break  # no pager and a short page: this was the only page
    return cards


def now_iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def build_outputs(
    site: str,
    cards: List[Card],
    cat_of: Dict[int, Tuple[int, str]],
    brand_of: Dict[int, str],
    details: Dict[int, Dict[str, Any]],
    first_seen: Dict[int, float],
    bootstrap: bool,
    now: float,
    new_days: int = 14,
) -> Tuple[Dict[str, Any], Dict[int, Dict[str, Any]]]:
    products: List[Dict[str, Any]] = []
    full: Dict[int, Dict[str, Any]] = {}
    cat_counts: Dict[int, int] = {}
    cat_names: Dict[int, str] = {}
    cat_image: Dict[int, str] = {}

    for c in cards:
        d = details.get(c.id, {})
        stock = d.get("stock") or "available"
        cid, cname = cat_of.get(c.id, (0, ""))
        seen_at = first_seen.get(c.id)
        watched = bool(seen_at) and seen_at > 0  # 0 means "already there when we started watching"
        is_new = watched and (now - seen_at) < new_days * 86400
        image = absolute(site, f"/web/image/product.template/{c.id}/image_512")
        item = {
            "id": c.id,
            "name": c.name or d.get("name") or "",
            "price": c.price,
            "old_price": c.old_price if (c.old_price and c.old_price > c.price) else None,
            "image_url": image,
            "category_id": cid,
            "category_name": cname,
            "brand": brand_of.get(c.id, ""),
            "sku": "",
            "stock": stock,
            "url": c.href,
            "is_new": bool(is_new),
            "added_at": now_iso(seen_at) if watched else None,
        }
        products.append(item)
        if cid:
            cat_counts[cid] = cat_counts.get(cid, 0) + 1
            cat_names[cid] = cname
            if stock != "out" and cid not in cat_image:
                cat_image[cid] = image
        gallery = d.get("images") or [image]
        full[c.id] = dict(item, description=d.get("description", ""), images=gallery, specifications={})

    categories = [
        {"id": cid, "name": cat_names[cid], "image_url": cat_image.get(cid), "count": n}
        for cid, n in sorted(cat_counts.items(), key=lambda kv: -kv[1])
    ]
    catalog = {"built_at": now_iso(now), "source": "public-website", "products": products, "categories": categories}
    return catalog, full


def pick_refresh(ids: List[int], checked_at: Dict[int, float], budget: int) -> List[int]:
    """Products never checked first, then the ones checked longest ago."""
    ordered = sorted(ids, key=lambda i: (checked_at.get(i, 0.0), i))
    return ordered[:budget]


def load_json(path_or_url: str, http: Optional[Http] = None) -> Optional[Dict[str, Any]]:
    try:
        if path_or_url.startswith("http"):
            req = urllib.request.Request(path_or_url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        with open(path_or_url, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def write_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))


def run(args: argparse.Namespace, log=print) -> int:
    site = args.site.rstrip("/")
    prefix = args.lang_prefix.rstrip("/")
    http = Http(site, min_interval=args.interval)
    started = time.time()

    if not http.robots_allow(f"{prefix}/shop"):
        log("robots.txt of the website does not allow reading /shop: nothing was read.")
        return 3

    prev_state = load_json(args.prev_state) if args.prev_state else None
    prev_state = prev_state if isinstance(prev_state, dict) else {}
    bootstrap = not prev_state.get("products")
    pstate: Dict[str, Dict[str, Any]] = prev_state.get("products", {}) if not bootstrap else {}
    details: Dict[int, Dict[str, Any]] = {int(k): v.get("detail", {}) for k, v in pstate.items()}
    checked_at: Dict[int, float] = {int(k): float(v.get("checked_at", 0)) for k, v in pstate.items()}
    first_seen: Dict[int, float] = {int(k): float(v.get("first_seen", 0)) for k, v in pstate.items()}
    cat_of: Dict[int, Tuple[int, str]] = {int(k): (int(v[0]), v[1]) for k, v in prev_state.get("category_of", {}).items()}
    brand_of: Dict[int, str] = {int(k): v for k, v in prev_state.get("brand_of", {}).items()}
    mapped_at = float(prev_state.get("mapped_at", 0))

    try:
        log("Reading the shop list ...")
        cards = crawl_listing(http, prefix, max_pages=args.max_pages)
        if len(cards) < args.min_products:
            log(f"Only {len(cards)} products found (expected at least {args.min_products}): keeping the previous data.")
            return 2
        ids = [c.id for c in cards]
        log(f"{len(cards)} products on the shop list.")

        for pid in ids:
            first_seen.setdefault(pid, 0.0 if bootstrap else started)

        new_ids = [i for i in ids if i not in cat_of]
        remap = bootstrap or bool(new_ids) or (started - mapped_at) > args.remap_hours * 3600
        if remap:
            log("Reading categories and brands ...")
            shop_html = http.get(f"{prefix}/shop")
            cats = parse_categories(shop_html)
            new_cat_of: Dict[int, Tuple[int, str]] = {}
            for cat in cats:
                for card in crawl_listing(http, prefix, category_href=cat.href, max_pages=args.max_pages):
                    new_cat_of.setdefault(card.id, (cat.id, cat.name))
            brands = parse_brand_filters(shop_html)
            if brands and not http.robots_allow(f"{prefix}/shop?attribute_value={brands[0].value}"):
                log("robots.txt does not allow the brand filter pages: brands are skipped.")
                brands = []
            new_brand_of: Dict[int, str] = {}
            for b in brands:
                q = urllib.parse.urlencode({"attribute_value": b.value})
                for card in crawl_listing(http, prefix, query=q, max_pages=args.max_pages):
                    new_brand_of.setdefault(card.id, b.name)
            if new_cat_of:
                cat_of = new_cat_of
            brand_of = new_brand_of or brand_of
            mapped_at = started
            log(f"{len(cats)} categories, {len(brands)} brands read.")

        budget = len(ids) if bootstrap else args.budget
        by_id = {c.id: c for c in cards}
        todo = pick_refresh(ids, {i: checked_at.get(i, 0.0) for i in ids}, budget)
        log(f"Checking the availability of {len(todo)} product pages ...")
        done = 0
        for pid in todo:
            card = by_id[pid]
            try:
                html = http.get(card.href)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    continue
                raise
            parsed = parse_product(html, site)
            if parsed.get("stock") is None and details.get(pid, {}).get("stock"):
                parsed["stock"] = details[pid]["stock"]
            details[pid] = parsed
            checked_at[pid] = time.time()
            done += 1
            if done % 100 == 0:
                log(f"  {done}/{len(todo)}")
            if time.time() - started > args.max_minutes * 60:
                log("Time limit reached: the rest is checked in the next run.")
                break
    except Blocked as e:
        log(f"The website asked us to slow down ({e}). Stopping; the previous data stays online.")
        return 4

    catalog, full = build_outputs(site, cards, cat_of, brand_of, details, first_seen, bootstrap, time.time())
    out = args.out
    write_json(os.path.join(out, "v1", "catalog.json"), catalog)
    for pid, item in full.items():
        write_json(os.path.join(out, "v1", "products", f"{pid}.json"), item)
    state = {
        "built_at": catalog["built_at"],
        "mapped_at": mapped_at,
        "category_of": {str(k): [v[0], v[1]] for k, v in cat_of.items() if k in set(ids)},
        "brand_of": {str(k): v for k, v in brand_of.items() if k in set(ids)},
        "products": {
            str(i): {"detail": details.get(i, {}), "checked_at": checked_at.get(i, 0), "first_seen": first_seen.get(i, 0.0)}
            for i in ids
        },
    }
    write_json(os.path.join(out, "v1", "state.json"), state)
    with open(os.path.join(out, ".nojekyll"), "w") as fh:
        fh.write("")
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(
            "<!doctype html><meta charset=utf-8><title>Al-Nukhba catalogue</title>"
            f"<p>Catalogue data for the Al-Nukhba Electronics app. Built {catalog['built_at']}. "
            f"{len(ids)} products. <a href='v1/catalog.json'>v1/catalog.json</a></p>"
        )
    out_count = sum(1 for p in catalog["products"] if p["stock"] == "out")
    log(f"Done: {len(ids)} products ({out_count} sold out), {http.requests} page requests, "
        f"{int(time.time() - started)} seconds.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site", default=os.environ.get("SITE", "https://www.alnukhbastore.com"))
    ap.add_argument("--lang-prefix", default=os.environ.get("LANG_PREFIX", "/ar"))
    ap.add_argument("--out", default="site")
    ap.add_argument("--prev-state", default=os.environ.get("PREV_STATE", ""),
                    help="URL or file of the state.json written by the previous run")
    ap.add_argument("--budget", type=int, default=int(os.environ.get("BUDGET", "300")),
                    help="product pages to re-check per run after the first full run")
    ap.add_argument("--interval", type=float, default=float(os.environ.get("INTERVAL", "1.1")),
                    help="minimum seconds between two requests")
    ap.add_argument("--max-pages", type=int, default=200)
    ap.add_argument("--min-products", type=int, default=int(os.environ.get("MIN_PRODUCTS", "50")))
    ap.add_argument("--remap-hours", type=float, default=6.0)
    ap.add_argument("--max-minutes", type=float, default=float(os.environ.get("MAX_MINUTES", "150")))
    args = ap.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())

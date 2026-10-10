"""비비빈스와 일렉샵의 공개 기기 상품 정보를 수집한다."""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from urllib.parse import urljoin

import aiohttp
from bs4 import BeautifulSoup, Comment


PRICE_MARKUP = 3_000
USER_AGENT = "Mozilla/5.0 (compatible; V4P3-SP0T-ProductSync/1.0)"
BIBIBINS_BASE = "https://xn--jk1bo8sa06werixle.com"
ELECSHOP_BASE = "https://xn--352bl9av7r2tn.com"
ELECSHOP_DEVICES = f"{ELECSHOP_BASE}/product-category/%EA%B0%9C%EB%B0%A9%ED%98%95-%EA%B8%B0%EA%B8%B0/"


@dataclass(slots=True)
class ScrapedProduct:
    source_site: str
    source_key: str
    source_url: str
    category: str
    name: str
    description: str
    source_price: int
    price: int
    image_url: str
    option_label: str
    options: list[str]
    stock: int


def _number(text: str) -> int:
    match = re.search(r"([0-9][0-9,]*(?:\.[0-9]+)?)", text or "")
    return int(float(match.group(1).replace(",", ""))) if match else 0


def _unique(values) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _image_url(node, base_url: str) -> str:
    image = node.select_one("img")
    if image:
        for attr in ("data-large_image", "data-src", "data-lazy-src", "ec-data-src", "src"):
            value = image.get(attr)
            if value and "prod_loading.gif" not in value and "adult.png" not in value:
                return urljoin(base_url, value)
    for comment in node.find_all(string=lambda value: isinstance(value, Comment)):
        match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', str(comment), re.I)
        if match:
            return urljoin(base_url, match.group(1))
    return urljoin(base_url, image.get("src")) if image and image.get("src") else ""


def _bibibins_card(node, category: str, page_url: str) -> ScrapedProduct | None:
    link = node.select_one('a[href*="/product/"]')
    image = node.select_one(".prdImg img")
    name = (image.get("alt") if image else "") or ""
    if not name:
        name_spans = node.select(".name a > span:not(.title)")
        name = name_spans[-1].get_text(" ", strip=True) if name_spans else ""
    price_node = node.select_one('[column_name="product_price"]')
    if not link or not name or not price_node:
        return None
    product_match = re.search(r"anchorBoxId_(\d+)", node.get("id", ""))
    if not product_match:
        product_match = re.search(r"/(\d+)/category/", link.get("href", ""))
    source_key = product_match.group(1) if product_match else link.get("href", "")
    source_price = _number(price_node.get_text(" ", strip=True))
    if not source_price:
        return None
    summary = node.select_one('[column_name="summary_desc"]')
    summary_values = summary.select("span:not(.title)") if summary else []
    option_nodes = node.select(".option option[value], .option li")
    options = _unique(item.get_text(" ", strip=True) for item in option_nodes)
    if not options:
        options = ["색상은 구매티켓에서 확인"]
    return ScrapedProduct(
        source_site="bibibins",
        source_key=source_key,
        source_url=urljoin(page_url, link.get("href")),
        category=category,
        name=name.strip(),
        description=summary_values[-1].get_text(" ", strip=True) if summary_values else "",
        source_price=source_price,
        price=source_price + PRICE_MARKUP,
        image_url=_image_url(node, page_url),
        option_label="색상",
        options=options,
        stock=0 if "soldout" in " ".join(node.get("class", [])).lower() else 99,
    )


async def _soup(session: aiohttp.ClientSession, url: str) -> BeautifulSoup:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
        response.raise_for_status()
        return BeautifulSoup(await response.read(), "html.parser")


async def scrape_bibibins(session: aiohttp.ClientSession) -> list[ScrapedProduct]:
    products: dict[str, ScrapedProduct] = {}
    categories = (("입호흡 기기", 50), ("폐호흡 기기", 52))
    for category, category_number in categories:
        category_keys: set[str] = set()
        for page in range(1, 21):
            url = f"{BIBIBINS_BASE}/product/list.html?cate_no={category_number}&page={page}"
            soup = await _soup(session, url)
            cards = soup.select(".xans-product-listnormal .prdList > li")
            if not cards:
                break
            new_on_page = 0
            for card in cards:
                product = _bibibins_card(card, category, url)
                if product:
                    if product.source_key not in category_keys:
                        category_keys.add(product.source_key)
                        new_on_page += 1
                    existing = products.get(product.source_key)
                    if existing is None or category == "입호흡 기기":
                        products[product.source_key] = product
            if not new_on_page:
                break
            await asyncio.sleep(0.12)
    if not products:
        raise RuntimeError("비비빈스 공개 상품을 찾지 못했습니다.")
    return list(products.values())


DTL_WORDS = (
    "폐호흡", "모드기기", "모드 기기", "박스모드", "박스 모드", "200w", "220w",
    "젠200", "클라우드 플라스크", "드래그 5", "드래그5", "아르거스 mt",
)


def _device_category(name: str) -> str:
    lowered = name.lower()
    return "폐호흡 기기" if any(word in lowered for word in DTL_WORDS) else "입호흡 기기"


def _elec_options(soup: BeautifulSoup) -> list[str]:
    options = []
    for select in soup.select("form.variations_form .variations select"):
        options.extend(option.get_text(" ", strip=True) for option in select.select("option[value]") if option.get("value"))
    if options:
        return _unique(options)
    form = soup.select_one("form.variations_form[data-product_variations]")
    if form:
        try:
            variations = json.loads(form.get("data-product_variations", "[]"))
            return _unique(value for item in variations for value in item.get("attributes", {}).values())
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return ["색상은 구매티켓에서 확인"]


COLOR_WORDS = (
    "블랙", "화이트", "그레이", "실버", "골드", "레드", "오렌지", "옐로우", "그린",
    "블루", "네이비", "퍼플", "핑크", "브라운", "카키", "민트", "투명", "클리어",
)


def _option_label(options: list[str]) -> str:
    color_count = sum(any(word in option for word in COLOR_WORDS) for option in options)
    return "색상" if color_count * 2 > len(options) else "맛"


def _elec_currency(soup: BeautifulSoup) -> str:
    for script in soup.select('script[type="application/ld+json"]'):
        match = re.search(r'"priceCurrency"\s*:\s*"([A-Z]{3})"', script.get_text(" ", strip=True))
        if match:
            return match.group(1)
    return "USD" if "$" in (soup.select_one(".summary .price") or soup).get_text(" ", strip=True) else "KRW"


async def _usd_krw_rate(session: aiohttp.ClientSession) -> float:
    configured = os.getenv("ELECSHOP_USD_KRW_RATE", "").strip()
    if configured:
        return float(configured)
    try:
        async with session.get("https://open.er-api.com/v6/latest/USD", timeout=aiohttp.ClientTimeout(total=10)) as response:
            response.raise_for_status()
            rate = float((await response.json())["rates"]["KRW"])
            if rate > 0:
                return rate
    except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, TypeError, ValueError):
        pass
    return 1_400.0


async def _elec_detail(
    session: aiohttp.ClientSession, card, usd_krw_rate: float,
) -> ScrapedProduct | None:
    link = card.select_one('a[href*="/product/"]')
    product_id = card.get("data-product_id")
    if not link or not product_id:
        return None
    url = urljoin(ELECSHOP_BASE, link.get("href"))
    soup = await _soup(session, url)
    name_node = soup.select_one("h1.product_title")
    price_node = soup.select_one(".summary .price")
    if not name_node or not price_node:
        return None
    raw_price = float(re.search(r"[0-9][0-9,.]*", price_node.get_text(" ", strip=True)).group().replace(",", ""))
    currency = _elec_currency(soup)
    source_price = round(raw_price * usd_krw_rate) if currency == "USD" else round(raw_price)
    description_node = soup.select_one(".woocommerce-product-details__short-description")
    in_stock = "outofstock" not in " ".join(card.get("class", [])).lower()
    options = _elec_options(soup)
    return ScrapedProduct(
        source_site="elecshop",
        source_key=str(product_id),
        source_url=url,
        category=_device_category(name_node.get_text(" ", strip=True)),
        name=name_node.get_text(" ", strip=True),
        description=description_node.get_text(" ", strip=True) if description_node else "",
        source_price=source_price,
        price=source_price + PRICE_MARKUP,
        image_url=_image_url(soup.select_one(".woocommerce-product-gallery") or soup, url),
        option_label=_option_label(options),
        options=options,
        stock=99 if in_stock else 0,
    )


async def scrape_elecshop(session: aiohttp.ClientSession) -> list[ScrapedProduct]:
    soup = await _soup(session, ELECSHOP_DEVICES)
    cards = soup.select(".products .product[data-product_id]")
    if not cards:
        raise RuntimeError("일렉샵 공개 상품을 찾지 못했습니다.")
    rate = await _usd_krw_rate(session)
    semaphore = asyncio.Semaphore(4)

    async def load(card):
        async with semaphore:
            return await _elec_detail(session, card, rate)

    results = await asyncio.gather(*(load(card) for card in cards), return_exceptions=True)
    products = [result for result in results if isinstance(result, ScrapedProduct)]
    if not products:
        raise RuntimeError("일렉샵 상품 상세정보를 읽지 못했습니다.")
    return products


async def scrape_device_stores() -> tuple[dict[str, list[ScrapedProduct]], dict[str, str]]:
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5"}
    connector = aiohttp.TCPConnector(limit_per_host=4)
    async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
        results = await asyncio.gather(
            scrape_bibibins(session), scrape_elecshop(session), return_exceptions=True,
        )
    products: dict[str, list[ScrapedProduct]] = {}
    errors: dict[str, str] = {}
    for site, result in zip(("bibibins", "elecshop"), results):
        if isinstance(result, Exception):
            errors[site] = str(result)
        else:
            products[site] = result
    if not products:
        raise RuntimeError("두 쇼핑몰의 상품 정보를 모두 불러오지 못했습니다.")
    return products, errors


async def sync_scraped_products(conn, guild_id: int, grouped: dict[str, list[ScrapedProduct]]) -> dict:
    """수집 결과를 상품 DB에 반영하고 출처별로 사라진 상품은 판매 중지한다."""
    inserted = updated = hidden = 0
    async with conn.transaction():
        category_ids = {}
        for sort_order, name in enumerate(("입호흡 기기", "폐호흡 기기"), start=1):
            category = await conn.fetchrow(
                "SELECT id FROM web_categories WHERE guild_id=$1 AND name=$2", guild_id, name,
            )
            if category:
                category_ids[name] = int(category["id"])
            else:
                category_ids[name] = await conn.fetchval(
                    "INSERT INTO web_categories (guild_id,name,sort_order) VALUES ($1,$2,$3) RETURNING id",
                    guild_id, name, sort_order * 10,
                )

        for site, products in grouped.items():
            seen_keys = []
            for product in products:
                seen_keys.append(product.source_key)
                source = await conn.fetchrow(
                    """
                    SELECT product_id, disabled_by_admin FROM web_product_sources
                    WHERE guild_id=$1 AND source_site=$2 AND source_key=$3
                    """,
                    guild_id, site, product.source_key,
                )
                category_id = category_ids[product.category]
                if source:
                    product_id = int(source["product_id"])
                    await conn.execute(
                        """
                        UPDATE web_products
                        SET category_id=$3,name=$4,description=$5,price=$6,stock=$7,image_url=$8,
                            option_label=$9,options=$10,is_active=$11,updated_at=CURRENT_TIMESTAMP
                        WHERE id=$1 AND guild_id=$2
                        """,
                        product_id, guild_id, category_id, product.name, product.description,
                        product.price, product.stock, product.image_url,
                        product.option_label, product.options, not bool(source["disabled_by_admin"]),
                    )
                    updated += 1
                else:
                    product_id = await conn.fetchval(
                        """
                        INSERT INTO web_products
                            (guild_id,category_id,name,description,price,stock,image_url,option_label,options,is_active)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,TRUE)
                        RETURNING id
                        """,
                        guild_id, category_id, product.name, product.description,
                        product.price, product.stock, product.image_url,
                        product.option_label, product.options,
                    )
                    inserted += 1
                await conn.execute(
                    """
                    INSERT INTO web_product_sources
                        (guild_id,source_site,source_key,product_id,source_url,source_price,synced_at)
                    VALUES ($1,$2,$3,$4,$5,$6,CURRENT_TIMESTAMP)
                    ON CONFLICT (guild_id,source_site,source_key) DO UPDATE SET
                        product_id=EXCLUDED.product_id, source_url=EXCLUDED.source_url,
                        source_price=EXCLUDED.source_price, synced_at=CURRENT_TIMESTAMP
                    """,
                    guild_id, site, product.source_key, product_id,
                    product.source_url, product.source_price,
                )

            hidden_result = await conn.execute(
                """
                UPDATE web_products p SET is_active=FALSE, updated_at=CURRENT_TIMESTAMP
                FROM web_product_sources s
                WHERE s.product_id=p.id AND s.guild_id=$1 AND s.source_site=$2
                  AND NOT (s.source_key = ANY($3::text[])) AND p.is_active=TRUE
                """,
                guild_id, site, seen_keys,
            )
            hidden += int(hidden_result.rsplit(" ", 1)[-1])
    return {"inserted": inserted, "updated": updated, "hidden": hidden}

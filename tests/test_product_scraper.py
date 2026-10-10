import unittest

from bs4 import BeautifulSoup

from product_scraper import (
    _bibibins_card,
    _device_category,
    _option_label,
    _parse_elec_detail,
    _source_site_for_url,
)


class ProductScraperTests(unittest.TestCase):
    def test_bibibins_card_uses_sale_price_and_adds_markup(self):
        soup = BeautifulSoup(
            """
            <li id="anchorBoxId_520">
              <div class="prdImg"><a href="/product/sample/520/category/50/display/1/">
                <img src="//example.com/adult.png" alt="[브랜드] 테스트 기기">
                <!--<img src="//example.com/real-product.png">-->
              </a></div>
              <div class="name"><a href="/product/sample/520/category/50/display/1/">
                <span class="title">상품명</span><span>[브랜드] 테스트 기기</span>
              </a></div>
              <li column_name="summary_desc"><span class="title">상품요약정보</span><span>테스트 설명</span></li>
              <li column_name="product_price"><span>53,000원</span></li>
            </li>
            """,
            "html.parser",
        )
        product = _bibibins_card(
            soup.select_one("li"), "입호흡 기기", "https://example.com/list",
        )
        self.assertEqual(product.name, "[브랜드] 테스트 기기")
        self.assertEqual(product.category, "입호흡 기기")
        self.assertEqual(product.source_price, 53_000)
        self.assertEqual(product.price, 56_000)
        self.assertEqual(product.description, "테스트 설명")
        self.assertEqual(product.image_url, "https://example.com/real-product.png")

    def test_device_category_and_option_label(self):
        self.assertEqual(_device_category("베이포레소 젠200 모드 기기"), "폐호흡 기기")
        self.assertEqual(_device_category("헬베이프 젤로 킷"), "입호흡 기기")
        self.assertEqual(_option_label(["블랙", "화이트", "퍼플"]), "색상")
        self.assertEqual(_option_label(["망고 아이스", "포도 민트"]), "맛")
        self.assertEqual(_option_label(["색상은 구매티켓에서 확인"]), "색상")

    def test_link_import_allows_only_supported_store_hosts(self):
        self.assertEqual(
            _source_site_for_url("https://xn--jk1bo8sa06werixle.com/product/list.html?cate_no=50"),
            "bibibins",
        )
        self.assertEqual(
            _source_site_for_url("https://xn--352bl9av7r2tn.com/product-category/devices/"),
            "elecshop",
        )
        with self.assertRaisesRegex(RuntimeError, "비비빈스 또는 일렉샵"):
            _source_site_for_url("https://xn--352bl9av7r2tn.com.example.com/product/test/")

    def test_elecshop_detail_adds_markup_and_reads_options(self):
        soup = BeautifulSoup(
            """
            <html><body class="single-product postid-321">
              <h1 class="product_title">테스트 팟 기기</h1>
              <div class="summary"><p class="price">$20.00</p>
                <div class="woocommerce-product-details__short-description">테스트 설명</div>
                <form class="variations_form" data-product_id="321">
                  <table class="variations"><select><option value="">선택</option>
                    <option value="black">블랙</option><option value="white">화이트</option>
                  </select></table>
                </form>
              </div>
              <div class="woocommerce-product-gallery"><img src="/sample.jpg"></div>
              <script type="application/ld+json">{"priceCurrency":"USD"}</script>
            </body></html>
            """,
            "html.parser",
        )
        product = _parse_elec_detail(
            soup, "https://xn--352bl9av7r2tn.com/product/sample/", "321", 1_400,
        )
        self.assertEqual(product.source_price, 28_000)
        self.assertEqual(product.price, 31_000)
        self.assertEqual(product.options, ["블랙", "화이트"])
        self.assertEqual(product.option_label, "색상")


if __name__ == "__main__":
    unittest.main()

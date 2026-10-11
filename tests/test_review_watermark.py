import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from PIL import Image

from review_watermark import watermark_review_image


class ReviewWatermarkTests(unittest.TestCase):
    def test_single_watermark_is_scaled_over_the_entire_photo(self):
        source_buffer = BytesIO()
        Image.new("RGB", (120, 80), (0, 0, 255)).save(source_buffer, format="PNG")

        with tempfile.TemporaryDirectory() as directory:
            watermark_path = Path(directory) / "watermark.png"
            Image.new("RGBA", (10, 10), (255, 0, 0, 128)).save(watermark_path)
            result = watermark_review_image(source_buffer.getvalue(), watermark_path)

        with Image.open(BytesIO(result)) as rendered:
            self.assertEqual(rendered.size, (120, 80))
            for point in ((2, 2), (60, 40), (117, 77)):
                red, green, blue = rendered.getpixel(point)
                self.assertGreater(red, 110)
                self.assertLess(green, 15)
                self.assertGreater(blue, 110)


if __name__ == "__main__":
    unittest.main()

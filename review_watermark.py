"""후기 첨부 이미지에 고정 저작권 로고를 합성합니다."""

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError


WATERMARK_PATH = Path(__file__).with_name("review_watermark.png")
MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_OUTPUT_SIDE = 4096
MAX_OUTPUT_BYTES = 9 * 1024 * 1024


class ReviewImageError(ValueError):
    pass


def watermark_review_image(source: bytes, watermark_path: Path = WATERMARK_PATH) -> bytes:
    if not source:
        raise ReviewImageError("첨부된 이미지가 비어 있습니다.")
    if len(source) > MAX_SOURCE_BYTES:
        raise ReviewImageError("후기 이미지는 20MB 이하만 업로드할 수 있습니다.")
    if not watermark_path.is_file():
        raise ReviewImageError("후기 저작권 로고 파일을 찾을 수 없습니다.")

    try:
        with Image.open(BytesIO(source)) as opened:
            if opened.width * opened.height > MAX_IMAGE_PIXELS:
                raise ReviewImageError("후기 이미지의 해상도가 너무 큽니다.")
            opened.load()
            base = ImageOps.exif_transpose(opened).convert("RGBA")
        with Image.open(watermark_path) as opened_watermark:
            opened_watermark.load()
            watermark = opened_watermark.convert("RGBA")
    except ReviewImageError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as error:
        raise ReviewImageError("정상적인 이미지 파일인지 확인해 주세요.") from error

    base.thumbnail((MAX_OUTPUT_SIDE, MAX_OUTPUT_SIDE), Image.Resampling.LANCZOS)
    shortest_side = min(base.size)
    target_side = max(1, round(shortest_side * 0.24))
    scale = min(target_side / watermark.width, target_side / watermark.height)
    target_size = (
        max(1, round(watermark.width * scale)),
        max(1, round(watermark.height * scale)),
    )
    watermark = watermark.resize(target_size, Image.Resampling.LANCZOS)

    margin = max(1, round(shortest_side * 0.02))
    position = (max(0, base.width - watermark.width - margin), margin)
    base.alpha_composite(watermark, position)

    flattened = Image.new("RGB", base.size, "white")
    flattened.paste(base, mask=base.getchannel("A"))
    output = BytesIO()
    flattened.save(output, format="JPEG", quality=90, optimize=True, progressive=True)
    result = output.getvalue()
    if len(result) > MAX_OUTPUT_BYTES:
        raise ReviewImageError("처리된 후기 이미지가 너무 큽니다. 더 작은 이미지로 다시 시도해 주세요.")
    return result

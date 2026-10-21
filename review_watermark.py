"""후기 첨부 이미지에 고정 저작권 로고를 합성합니다."""

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError


LOGO_WATERMARK_PATH = Path(__file__).with_name("review_watermark.png")
PATTERN_WATERMARK_PATH = Path(__file__).with_name("review_watermark_pattern.png")
BADGE_WATERMARK_PATH = Path(__file__).with_name("review_watermark_badge.png")
MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_OUTPUT_SIDE = 4096
MAX_OUTPUT_BYTES = 9 * 1024 * 1024


class ReviewImageError(ValueError):
    pass


def watermark_review_image(
    source: bytes,
    logo_path: Path = LOGO_WATERMARK_PATH,
    pattern_path: Path = PATTERN_WATERMARK_PATH,
    badge_path: Path = BADGE_WATERMARK_PATH,
) -> bytes:
    if not source:
        raise ReviewImageError("첨부된 이미지가 비어 있습니다.")
    if len(source) > MAX_SOURCE_BYTES:
        raise ReviewImageError("후기 이미지는 20MB 이하만 업로드할 수 있습니다.")
    if not logo_path.is_file():
        raise ReviewImageError("1번 후기 워터마크 파일을 찾을 수 없습니다.")
    if not pattern_path.is_file():
        raise ReviewImageError("2번 후기 워터마크 파일을 찾을 수 없습니다.")
    if not badge_path.is_file():
        raise ReviewImageError("3번 후기 워터마크 파일을 찾을 수 없습니다.")

    try:
        with Image.open(BytesIO(source)) as opened:
            if opened.width * opened.height > MAX_IMAGE_PIXELS:
                raise ReviewImageError("후기 이미지의 해상도가 너무 큽니다.")
            opened.load()
            base = ImageOps.exif_transpose(opened).convert("RGBA")
        with Image.open(logo_path) as opened_logo:
            opened_logo.load()
            logo = opened_logo.convert("RGBA")
        with Image.open(pattern_path) as opened_pattern:
            opened_pattern.load()
            pattern = opened_pattern.convert("RGBA")
        with Image.open(badge_path) as opened_badge:
            opened_badge.load()
            badge = opened_badge.convert("RGBA")
    except ReviewImageError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as error:
        raise ReviewImageError("정상적인 이미지 파일인지 확인해 주세요.") from error

    base.thumbnail((MAX_OUTPUT_SIDE, MAX_OUTPUT_SIDE), Image.Resampling.LANCZOS)

    # 2번 워터마크는 후기 이미지 크기에 정확히 맞춰 전체를 덮습니다.
    pattern = pattern.resize(base.size, Image.Resampling.LANCZOS)
    base.alpha_composite(pattern, (0, 0))

    # 1번 워터마크는 왼쪽 위, 3번 워터마크는 오른쪽 아래에 표시합니다.
    shortest_side = min(base.size)
    target_side = max(1, round(shortest_side * 0.34))
    scale = min(target_side / logo.width, target_side / logo.height)
    target_size = (
        max(1, round(logo.width * scale)),
        max(1, round(logo.height * scale)),
    )
    logo = logo.resize(target_size, Image.Resampling.LANCZOS)
    badge_scale = min(target_side / badge.width, target_side / badge.height)
    badge = badge.resize(
        (
            max(1, round(badge.width * badge_scale)),
            max(1, round(badge.height * badge_scale)),
        ),
        Image.Resampling.LANCZOS,
    )

    margin = max(1, round(shortest_side * 0.02))
    top_left = (margin, margin)
    bottom_right = (
        max(0, base.width - badge.width - margin),
        max(0, base.height - badge.height - margin),
    )
    base.alpha_composite(logo, top_left)
    base.alpha_composite(badge, bottom_right)

    flattened = Image.new("RGB", base.size, "white")
    flattened.paste(base, mask=base.getchannel("A"))
    output = BytesIO()
    flattened.save(output, format="JPEG", quality=90, optimize=True, progressive=True)
    result = output.getvalue()
    if len(result) > MAX_OUTPUT_BYTES:
        raise ReviewImageError("처리된 후기 이미지가 너무 큽니다. 더 작은 이미지로 다시 시도해 주세요.")
    return result

"""Run only in a disposable child process; never import PDFium in the API process."""

import sys
import warnings
from pathlib import Path

from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = 25_000_000
warnings.simplefilter("error", Image.DecompressionBombWarning)


def save(image, dest, rotate):
    image = ImageOps.exif_transpose(image)
    if rotate:
        image = image.rotate(-rotate, expand=True)
    image.thumbnail((1568, 1568))
    image.convert("RGB").save(dest, "JPEG", quality=85)


def render(source, output, pages, rotate):
    header = source.read_bytes()[:8]
    if header.startswith(b"%PDF-"):
        import pypdfium2 as pdfium

        with pdfium.PdfDocument(source) as pdf:
            if not 1 <= len(pdf) <= pages:
                raise ValueError("page_limit")
            for index in range(len(pdf)):
                page = pdf[index]
                try:
                    width, height = page.get_size()
                    if width <= 0 or height <= 0:
                        raise ValueError("invalid_page")
                    scale = min(2, 1568 / max(width, height))
                    bitmap = page.render(scale=scale)
                    try:
                        save(bitmap.to_pil(), output / f"{index:03}.jpg", rotate)
                    finally:
                        bitmap.close()
                finally:
                    page.close()
    else:
        with Image.open(source) as img:
            if img.format not in {"PNG", "JPEG"} or getattr(img, "n_frames", 1) != 1:
                raise ValueError("unsupported_image")
            if img.width * img.height > Image.MAX_IMAGE_PIXELS:
                raise ValueError("pixel_limit")
            img.load()
            save(img, output / "000.jpg", rotate)


if __name__ == "__main__":
    try:
        render(Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]))
    except Exception:
        sys.exit(2)  # Never expose parser errors or document text.

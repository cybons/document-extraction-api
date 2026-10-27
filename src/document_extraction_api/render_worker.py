"""PDF・画像をページごとの PNG に変換する子プロセス。

親（render.py）が別プロセスで起動する。親が強制終了されても残らないよう、自分でも制限時間を持つ。
終了コード: 0 成功 / 2 読めないファイル / 3 ページ数が上限を超える / 124 制限時間切れ
"""

import io
import os
import sys
import threading
from pathlib import Path

EXIT_UNREADABLE = 2
EXIT_TOO_MANY_PAGES = 3
EXIT_TIMEOUT = 124
PNG_LIMIT_BYTES = 1_500_000


class _TooManyPages(Exception):
    pass


def main(argv: list[str]) -> int:
    source, out_dir, max_pages, dpi, long_edge, limit = argv
    watchdog = threading.Timer(float(limit), os._exit, args=(EXIT_TIMEOUT,))
    watchdog.daemon = True
    watchdog.start()
    try:
        render(Path(source), Path(out_dir), int(max_pages), int(dpi), int(long_edge))
    except _TooManyPages:
        return EXIT_TOO_MANY_PAGES
    except Exception:
        return EXIT_UNREADABLE
    return 0


def render(source: Path, out_dir: Path, max_pages: int, dpi: int, long_edge: int) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as f:
        head = f.read(5)
    images = (
        _pdf_pages(source, max_pages, dpi, long_edge)
        if head == b"%PDF-"
        else _image_pages(source, max_pages)
    )
    for number, image in enumerate(images, start=1):
        _save(_fit(_on_white(image), long_edge), out_dir / f"page-{number:03d}")


def _save(image, stem: Path) -> None:
    """文字の輪郭を保つため PNG を基本にし、スキャンなどで重くなるページだけ JPEG にする。"""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    if buffer.tell() <= PNG_LIMIT_BYTES:
        stem.with_suffix(".png").write_bytes(buffer.getvalue())
    else:
        image.save(stem.with_suffix(".jpg"), format="JPEG", quality=90)


def _pdf_pages(source: Path, max_pages: int, dpi: int, long_edge: int):
    import pypdfium2

    pdf = pypdfium2.PdfDocument(source)
    try:
        if len(pdf) > max_pages:
            raise _TooManyPages()
        for index in range(len(pdf)):
            page = pdf[index]
            width, height = page.get_size()
            scale = min(dpi / 72, long_edge / max(width, height))
            # ページの /Rotate は pdfium が描画時に反映する
            yield page.render(scale=scale).to_pil()
    finally:
        pdf.close()


def _image_pages(source: Path, max_pages: int):
    from PIL import Image, ImageOps, ImageSequence

    with Image.open(source) as image:
        frames = getattr(image, "n_frames", 1)
        if frames > max_pages:
            raise _TooManyPages()
        for frame in ImageSequence.Iterator(image):
            yield ImageOps.exif_transpose(frame.copy())


def _on_white(image):
    """透過を白い背景に合成して RGB にする。"""
    from PIL import Image

    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(background, rgba).convert("RGB")
    return image.convert("RGB")


def _fit(image, long_edge: int):
    if max(image.size) <= long_edge:
        return image
    image = image.copy()
    image.thumbnail((long_edge, long_edge))
    return image


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

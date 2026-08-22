"""
Self-hosted PDF compression for generated travel-expense reports.

Why self-hosted instead of a third-party API (e.g. iLovePDF): these PDFs
contain client financial data and, in the receipts appendix, personal
documents (flight tickets, invoices) that may carry passport-adjacent
details. Routing them through an external compression service means that
data leaves the firm's infrastructure. This module gets most of the
practical size reduction (recompressing/downsampling embedded receipt
photos, which are almost always the actual size driver -- report text/
tables are already tiny) without sending anything off-server.

See ilovepdf_compress.py for the third-party alternative, with the same
trade-off spelled out there.
"""

from __future__ import annotations

import io
from pathlib import Path

import pikepdf
from PIL import Image

DEFAULT_JPEG_QUALITY = 70
DEFAULT_MAX_DIMENSION = 2000  # px, long edge -- plenty for legibility/OCR of a receipt


def compress(
    input_path: str | Path,
    output_path: str | Path,
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
    max_dimension: int = DEFAULT_MAX_DIMENSION,
) -> dict:
    """Recompress embedded raster images (the usual size driver for
    phone-photographed receipts) and re-save with object streams enabled.
    Vector/text pages are left untouched -- they're already small."""
    input_path = Path(input_path)
    output_path = Path(output_path)
    original_size = input_path.stat().st_size

    images_processed = 0
    images_skipped = 0

    with pikepdf.open(input_path) as pdf:
        for page in pdf.pages:
            for name, raw_image in page.get_images().items():
                pdf_image = pikepdf.PdfImage(raw_image)
                try:
                    pil_image = pdf_image.as_pil_image()
                except Exception:
                    images_skipped += 1
                    continue

                if pil_image.mode not in ("RGB", "L"):
                    pil_image = pil_image.convert("RGB")

                w, h = pil_image.size
                if max(w, h) > max_dimension:
                    scale = max_dimension / max(w, h)
                    pil_image = pil_image.resize(
                        (int(w * scale), int(h * scale)), Image.LANCZOS
                    )

                buf = io.BytesIO()
                pil_image.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
                buf.seek(0)

                raw_image.write(buf.read(), filter=pikepdf.Name("/DCTDecode"))
                raw_image.ColorSpace = pikepdf.Name(
                    "/DeviceGray" if pil_image.mode == "L" else "/DeviceRGB"
                )
                raw_image.Width = pil_image.width
                raw_image.Height = pil_image.height
                if "/SMask" in raw_image:
                    del raw_image["/SMask"]  # drop alpha; receipts don't need transparency
                images_processed += 1

        pdf.save(
            output_path,
            compress_streams=True,
            object_stream_mode=pikepdf.ObjectStreamMode.generate,
            linearize=True,
        )

    new_size = output_path.stat().st_size
    return {
        "original_bytes": original_size,
        "compressed_bytes": new_size,
        "reduction_pct": round(100 * (1 - new_size / original_size), 1) if original_size else 0,
        "images_processed": images_processed,
        "images_skipped": images_skipped,
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("Usage: python compress_pdf.py input.pdf output.pdf [quality] [max_dimension]")
        sys.exit(1)
    q = int(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_JPEG_QUALITY
    d = int(sys.argv[4]) if len(sys.argv) > 4 else DEFAULT_MAX_DIMENSION
    result = compress(sys.argv[1], sys.argv[2], q, d)
    print(result)

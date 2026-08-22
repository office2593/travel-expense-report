"""Builds the @font-face CSS block for PDF templates from the base64 font
data in static/fonts/ (kept as .b64 text files, not the binary .woff2, so
they're diff-friendly and don't need a build step)."""

from functools import lru_cache
from pathlib import Path

FONTS_DIR = Path(__file__).parent / "static" / "fonts"

_FACES = [
    # (family, filename, unicode-range)
    ("Assistant", "assistant-he.b64", "U+0590-05FF, U+200C-2010, U+20AA, U+25CC, U+FB1D-FB4F"),
    ("Assistant", "assistant-latin.b64", "U+0000-00FF, U+2000-206F, U+20AC, U+2212"),
    ("Frank Ruhl Libre", "frl-he.b64", "U+0590-05FF, U+200C-2010, U+20AA, U+25CC, U+FB1D-FB4F"),
    ("Frank Ruhl Libre", "frl-latin.b64", "U+0000-00FF, U+2000-206F, U+20AC, U+2212"),
]


@lru_cache(maxsize=1)
def font_faces_css() -> str:
    blocks = []
    for family, filename, unicode_range in _FACES:
        b64 = (FONTS_DIR / filename).read_text(encoding="ascii").strip()
        blocks.append(
            f"""@font-face {{
  font-family: '{family}';
  src: url(data:font/woff2;base64,{b64}) format('woff2');
  unicode-range: {unicode_range};
  font-weight: 100 900;
  font-display: swap;
}}"""
        )
    return "\n".join(blocks)

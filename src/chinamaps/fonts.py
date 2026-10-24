"""CJK fonts for map labels, fetched from fonts.gstatic.com into data/fonts/.

Falls back to the system WenQuanYi Zen Hei when a download fails.
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

import requests

from chinamaps.net import DATA_DIR, download, session

FONT_DIR = DATA_DIR / "fonts"
GSTATIC = "https://fonts.gstatic.com/s"
CSS_API = "https://fonts.googleapis.com/css2"
FALLBACK = (Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),)

# key -> (file name, gstatic URL, Google Fonts css2 family spec used if the URL goes stale)
FONTS: dict[str, tuple[str, str, str]] = {
    "serif": ("NotoSerifSC-Regular.ttf",
              f"{GSTATIC}/notoserifsc/v36/H4cyBXePl9DZ0Xe7gG9cyOj7uK2-n-D2rd4FY7SCqyWv.ttf",
              "Noto+Serif+SC:wght@400"),
    "serif-bold": ("NotoSerifSC-Bold.ttf",
                   f"{GSTATIC}/notoserifsc/v36/H4cyBXePl9DZ0Xe7gG9cyOj7uK2-n-D2rd4FY7RlrCWv.ttf",
                   "Noto+Serif+SC:wght@700"),
    "serif-black": ("NotoSerifSC-Black.ttf",
                    f"{GSTATIC}/notoserifsc/v36/H4cyBXePl9DZ0Xe7gG9cyOj7uK2-n-D2rd4FY7QrrCWv.ttf",
                    "Noto+Serif+SC:wght@900"),
    "sans": ("NotoSansSC-Regular.ttf",
             f"{GSTATIC}/notosanssc/v41/k3kCo84MPvpLmixcA63oeAL7Iqp5IZJF9bmaG9_FnYw.ttf",
             "Noto+Sans+SC:wght@400"),
    "sans-bold": ("NotoSansSC-Bold.ttf",
                  f"{GSTATIC}/notosanssc/v41/k3kCo84MPvpLmixcA63oeAL7Iqp5IZJF9bmaGzjCnYw.ttf",
                  "Noto+Sans+SC:wght@700"),
    "latin-italic": ("NotoSerif-Italic.ttf",
                     f"{GSTATIC}/notoserif/v33/ga6saw1J5X9T9RW6j9bNfFIMZhhWnFTyNZIQD1-_FXP0RgnaOg9MYBNLg8cP.ttf",
                     "Noto+Serif:ital,wght@1,400"),
}


def css_font_url(family_spec: str) -> str:
    """Resolve a css2 family spec to a TTF URL (a curl-like UA gets truetype)."""
    r = session().get(f"{CSS_API}?family={family_spec}", headers={"User-Agent": "curl/8.5.0"}, timeout=60)
    r.raise_for_status()
    m = re.search(r"url\((https://[^)]+\.ttf)\)", r.text)
    if not m:
        raise RuntimeError(f"no ttf url for {family_spec}")
    return m.group(1)


def fallback_font() -> Path:
    for p in FALLBACK:
        if p.exists():
            return p
    raise FileNotFoundError("no CJK fallback font found")


@cache
def font_path(key: str) -> Path:
    """Local path of a font by key (downloaded on first use)."""
    name, url, spec = FONTS[key]
    dest = FONT_DIR / name
    for get_url in (lambda: url, lambda: css_font_url(spec)):
        try:
            return download(get_url(), dest)
        except (OSError, RuntimeError, requests.RequestException) as e:  # stale URL / offline: next source
            print(f"font {key}: {e!r}")
    return fallback_font()


def fetch_all() -> dict[str, Path]:
    return {k: font_path(k) for k in FONTS}


if __name__ == "__main__":
    for k, p in fetch_all().items():
        print(f"{k:13s} {p} {p.stat().st_size / 1e6:.1f} MB")

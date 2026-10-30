#!/usr/bin/env python3
"""Download the Tilda landing page and all of its static assets for a local 1:1 copy."""
from __future__ import annotations

import hashlib
import html
import mimetypes
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "index-source.html"
ASSETS = ROOT / "assets"
SITE_BASE = "https://xn----8sbhhcq4afdfepg6a.xn--p1ai/"
ASSET_HOSTS = {
    "static.tildacdn.com",
    "thb.tildacdn.com",
    "neo.tildacdn.com",
    "use.fontawesome.com",
    "kit.fontawesome.com",
}
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}
sess = requests.Session()
sess.headers.update(HEADERS)

url_map: dict[str, Path] = {}
failures: list[tuple[str, str]] = []


def clean_candidate(url: str) -> str:
    """Trim HTML entity junk that a regex can swallow after a URL."""
    for marker in ("&quot;", "&#34;", "&#39;", "&amp;", "\\\\", "\\", "&lt;", "&gt;", "<", ">", " "):
        idx = url.find(marker)
        if idx != -1:
            url = url[:idx]
    return url.rstrip(".,;:)]}")


def candidate_urls(text: str) -> set[str]:
    out: set[str] = set()
    # Tilda serializes image lists with &quot; entities, so unescape the document
    # before extracting asset URLs. Otherwise a greedy regex only sees the first
    # image in each JSON attribute.
    for document in (text, html.unescape(text)):
        for raw in re.findall(r'https?://[^\s"\'<>\\]+', document):
            raw = clean_candidate(raw)
            if not raw:
                continue
            try:
                parts = urlsplit(raw)
            except ValueError:
                continue
            host = parts.netloc.lower()
            # The Font Awesome kit is not needed and rejects server-side fetches.
            if host == "kit.fontawesome.com":
                continue
            # Never map a bare host: it is used as a prefix in inline scripts.
            if host in ASSET_HOSTS and parts.path.strip("/"):
                out.add(raw)
    return out


def url_extension(url: str, content_type: str = "") -> str:
    path = unquote(urlsplit(url).path)
    ext = Path(path).suffix
    if ext and len(ext) <= 8:
        return ext
    guessed = mimetypes.guess_extension(content_type.split(";")[0].strip()) if content_type else None
    return guessed or ".bin"


def local_path_for(url: str) -> Path:
    """Stable local path for a remote URL, preserving its human-readable name."""
    if url in url_map:
        return url_map[url]
    parts = urlsplit(url)
    host = parts.netloc.lower().replace(":", "_")
    path = unquote(parts.path).lstrip("/")
    segments = [seg for seg in path.split("/") if seg not in ("", ".", "..")]
    if not segments:
        segments = ["index"]
    name = segments.pop()
    parent = ASSETS / host
    for seg in segments:
        parent = parent / seg
    stem, ext = os.path.splitext(name)
    if not ext or len(ext) > 8:
        stem, ext = name, ""
    if parts.query:
        qhash = hashlib.md5(parts.query.encode()).hexdigest()[:8]
        stem = f"{stem}.q{qhash}"
    if not ext:
        ext = ".bin"
    local = parent / f"{stem}{ext}"
    url_map[url] = local
    return local


def download(url: str) -> tuple[str, bool, str]:
    local = local_path_for(url)
    if local.exists() and local.stat().st_size > 0:
        return url, True, "cached"
    local.parent.mkdir(parents=True, exist_ok=True)
    last_error = ""
    for attempt in range(4):
        try:
            r = sess.get(url, timeout=45)
            if r.status_code == 404:
                last_error = "HTTP 404"
                break
            r.raise_for_status()
            ctype = r.headers.get("content-type", "")
            # Correct a missing/incorrect extension using the response type.
            if local.suffix == ".bin":
                ext = url_extension(url, ctype)
                local = local.with_suffix(ext)
                url_map[url] = local
            local.write_bytes(r.content)
            return url, True, f"{r.status_code} {len(r.content)}"
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(0.6 * (attempt + 1))
    failures.append((url, last_error))
    return url, False, last_error


def relative_for_html(local: Path) -> str:
    return local.relative_to(ROOT).as_posix()


def relative_for_css(local: Path, css_local: Path) -> str:
    return Path(os.path.relpath(local, start=css_local.parent)).as_posix()


def replace_urls_in_text(text: str, replacer) -> str:
    """Replace absolute asset URLs anywhere in a text file."""
    # Longest first avoids replacing a prefix of a longer matching URL.
    for remote in sorted(url_map, key=len, reverse=True):
        local = url_map[remote]
        replacement = replacer(local)
        if remote in text:
            text = text.replace(remote, replacement)
        escaped = remote.replace("&", "&amp;")
        if escaped != remote and escaped in text:
            text = text.replace(escaped, replacement)
    return text


def process_css(css_url: str, css_local: Path) -> None:
    text = css_local.read_text(encoding="utf-8", errors="replace")
    refs: set[str] = set()
    for match in re.finditer(r"url\(\s*(['\"]?)(.*?)\1\s*\)", text, re.I | re.S):
        ref = match.group(2).strip()
        if ref and not ref.startswith(("data:", "#", "about:", "blob:")):
            refs.add(urljoin(css_url, ref))
    for match in re.finditer(r"@import\s+(?:url\(\s*)?(['\"]?)(.*?)\1\s*\)?", text, re.I):
        ref = match.group(2).strip()
        if ref and not ref.startswith(("data:", "#")):
            refs.add(urljoin(css_url, ref))
    for ref in refs:
        if urlsplit(ref).netloc.lower() in ASSET_HOSTS:
            download(ref)
    new_text = replace_urls_in_text(text, lambda p: relative_for_css(p, css_local))
    css_local.write_text(new_text, encoding="utf-8")


def apply_branding(text: str) -> str:
    """Replace the developer credit with the Exponent Digital logo/link and
    update canonical URLs to the GitHub Pages address."""
    text = text.replace(
        "https://xn----8sbhhcq4afdfepg6a.xn--p1ai",
        "https://voroninded1987-dot.github.io/23region-remonta/",
    )
    header_elem = (
        "<div class='t396__elem tn-elem tn-elem__6724442791701100000001' "
        "data-elem-id='1701100000001' data-elem-type='html' "
        "data-field-top-value=\"8\" data-field-left-value=\"720\" "
        "data-field-height-value=\"36\" data-field-width-value=\"270\" "
        "data-field-axisy-value=\"top\" data-field-axisx-value=\"left\" "
        "data-field-container-value=\"grid\" "
        "data-field-topunits-value=\"px\" data-field-leftunits-value=\"px\" "
        "data-field-heightunits-value=\"px\" data-field-widthunits-value=\"px\" "
        "data-field-top-res-320-value=\"-500\" data-field-left-res-320-value=\"49\" "
        "data-field-top-res-480-value=\"-500\" data-field-left-res-480-value=\"60\" "
        "data-field-top-res-640-value=\"8\" data-field-left-res-640-value=\"720\" "
        "data-field-top-res-960-value=\"8\" data-field-left-res-960-value=\"700\"> "
        "<div class='tn-atom'> "
        "<a href=\"https://voroninded1987-dot.github.io/exponent-digital/\" "
        "target=\"_blank\" rel=\"noopener\" "
        "title=\"Exponent Digital — разработка сайта и маркетинг\" "
        "style=\"display:inline-flex;align-items:center;gap:10px;text-decoration:none;color:inherit;\"> "
        "<img src='assets/exponent-logo.png' alt='Exponent Digital' "
        "style='display:block;height:30px;width:auto;border-radius:6px;'/> "
        "<span style=\"font-family:'Gilroy',Arial,sans-serif;font-size:12px;line-height:1.25;color:#0d0d0d;\">"
        "разработка сайта<br>и маркетинг</span> </a> </div> </div>"
    )
    marker = (
        "<script>t_onReady(function() {t_onFuncLoad('t396_init',function() "
        "{t396_init('672444279');});});</script>"
    )
    assert marker in text, "header t396 marker not found"
    # Insert inside the artboard: right after the last element's closing div.
    idx = text.index(marker)
    anchor_end = '/> </div> </div>'
    pos = text.rfind(anchor_end, 0, idx)
    assert pos != -1, "header artboard tail not found"
    insert_at = pos + len(anchor_end)
    text = text[:insert_at] + header_elem + " " + text[insert_at:]
    old_credit = (
        "<div class='tn-atom'><a style=\"color: inherit\" "
        "href=\"https://leeda.tech\"target=\"_blank\">Создание сайта: LEADA</a></div>"
    )
    new_credit = (
        "<div class='tn-atom'><a href=\"https://voroninded1987-dot.github.io/exponent-digital/\" "
        "target=\"_blank\" rel=\"noopener\" "
        "title=\"Exponent Digital — разработка сайта и маркетинг\" "
        "style=\"color:inherit;text-decoration:none;display:inline-flex;align-items:center;gap:10px;\">"
        "<img src='assets/exponent-logo.png' alt='Exponent Digital' "
        "style='display:block;height:32px;width:auto;border-radius:6px;'/>"
        "<span style=\"line-height:1.3;font-size:12px;\">разработка сайта<br>и маркетинг</span></a></div>"
    )
    assert old_credit in text, "footer LEADA credit not found"
    text = text.replace(old_credit, new_credit, 1)
    # Remove the company phone numbers completely (header, menu bar, contacts).
    for class_token in (
        "6724442791687463834967",  # hero: phone number text
        "6724442791687463835009",  # hero: phone icon
        "6724443001594882485684",  # contacts: phone number 603-19-99
        "6724443001701099055439",  # contacts: phone number 603-89-99
        "6724443011635760131487",  # menu bar: phone number button
        "6724443011701099142877",  # menu bar: phone number button
    ):
        text = _remove_t396_element(text, class_token)
    return text


def _remove_t396_element(text: str, class_token: str) -> str:
    """Remove one Tilda zero-block element by its tn-elem__ class token."""
    marker = f"tn-elem__{class_token}'"
    idx = text.find(marker)
    if idx == -1:
        return text
    start = text.rfind("<div", 0, idx)
    if start == -1:
        return text
    tag_end = text.find(">", idx)
    if tag_end == -1:
        return text
    depth = 1
    pos = tag_end + 1
    while depth > 0:
        nxt_open = re.search(r"<\s*div\b", text[pos:])
        nxt_close = re.search(r"</\s*div\s*>", text[pos:])
        if nxt_open is None and nxt_close is None:
            break
        open_pos = pos + nxt_open.start() if nxt_open else len(text)
        close_pos = pos + nxt_close.start() if nxt_close else len(text)
        if close_pos < open_pos:
            depth -= 1
            pos = close_pos + len(nxt_close.group(0))
        else:
            depth += 1
            pos = open_pos + len(nxt_open.group(0))
    return text[:start] + text[pos:]


def main() -> int:
    if not SOURCE.exists():
        print(f"Missing {SOURCE}", file=sys.stderr)
        return 1
    source = SOURCE.read_text(encoding="utf-8")
    ASSETS.mkdir(exist_ok=True)

    urls = candidate_urls(source)
    # Rules from Tilda append plugin scripts to the page after load.
    extra = {
        "https://static.tildacdn.com/css/tilda-img-select-1.0.min.css",
        "https://static.tildacdn.com/js/tilda-img-select-1.0.min.js",
        "https://static.tildacdn.com/css/tilda-zero-form-errorbox.min.css",
        "https://static.tildacdn.com/lib/flags/flags7.png",
    }
    urls |= extra
    custom_css_url = urljoin(SITE_BASE, "/custom.css?t=1790842308")
    custom_css_local = ASSETS / "custom.css"

    print(f"Downloading {len(urls)} referenced assets + custom.css ...")
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(download, u) for u in sorted(urls)]
        for i, fut in enumerate(as_completed(futures), 1):
            url, ok, detail = fut.result()
            if not ok:
                print(f"  FAILED {url} ({detail})")
            elif i % 25 == 0 or i == len(futures):
                print(f"  {i}/{len(futures)}")

    # custom.css is a same-origin stylesheet.
    if not custom_css_local.exists():
        r = sess.get(custom_css_url, timeout=45)
        r.raise_for_status()
        custom_css_local.write_bytes(r.content)

    # Download resources referenced by downloaded stylesheets (fonts and icons).
    css_files: list[tuple[str, Path]] = []
    for remote, local in list(url_map.items()):
        if local.suffix.lower() == ".css":
            css_files.append((remote, local))
    # custom.css has no absolute assets, but process it for future edits.
    css_files.append((custom_css_url, custom_css_local))

    print(f"Processing {len(css_files)} stylesheets ...")
    for css_url, css_local in css_files:
        process_css(css_url, css_local)

    # Any resources discovered by CSS processing still need downloading.
    pending = [u for u, p in url_map.items() if not p.exists()]
    if pending:
        print(f"Downloading {len(pending)} stylesheet dependencies ...")
        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(download, pending))
        # Rewrite nested CSS again if necessary.
        for css_url, css_local in css_files:
            if css_local.exists():
                process_css(css_url, css_local)

    # Strip analytics / anti-flicker scripts. They are irrelevant for a local copy and
    # can keep the whole page hidden when opened from file://.
    source = re.sub(
        r"\s*<!-- Yandex\.Metrika counter.*?<!-- /Yandex\.Metrika counter -->",
        "",
        source,
        flags=re.S,
    )
    source = re.sub(
        r"\s*<script[^>]*>if\(!window\.mainTracker\).*?</script>",
        "",
        source,
        flags=re.S,
    )
    source = re.sub(
        r"\s*<script[^>]*>\(function\(\) \{var ttt='cebo';.*?</script>",
        "",
        source,
        flags=re.S,
    )
    # The Font Awesome kit rejects server-side downloads (403); the regular
    # Font Awesome CSS is already included and the page does not use its icons.
    source = source.replace(
        '<script src="https://kit.fontawesome.com/69e467919f.js" crossorigin="anonymous"></script>',
        "",
    )
    source = source.replace(
        '<link rel="stylesheet" type="text/css" href="/custom.css?t=1790842308">',
        '<link rel="stylesheet" type="text/css" href="assets/custom.css">',
    )
    # Use the original local images instead of Tilda's on-the-fly resizer. The
    # resizer would point at optim.tildacdn.com, which is unavailable offline.
    source = source.replace(
        'data-tilda-lazy="yes"',
        'data-tilda-lazy="yes" data-tilda-imgoptimoff="yes"',
    )
    # Local favicon/OG asset paths + all CDN references.
    source = replace_urls_in_text(source, relative_for_html)
    source = source.replace("assets/custom.css", "assets/custom.css")  # no-op clarity

    # Developer credit → Exponent Digital logo, canonical → GitHub Pages.
    source = apply_branding(source)

    # Set the generated file and keep a diagnostic copy of the source markup.
    (ROOT / "index.html").write_text(source, encoding="utf-8")
    print(f"Wrote index.html ({len(source):,} chars)")
    print(f"Assets on disk: {sum(1 for p in ASSETS.rglob('*') if p.is_file())}")
    if failures:
        print(f"Failures: {len(failures)}")
        for url, err in failures:
            print(f"  {url} -> {err}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

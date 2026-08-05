"""fetch_firm_logos — download each firm's own mark, once, into our media.

    python manage.py fetch_firm_logos --dry-run
    python manage.py fetch_firm_logos
    python manage.py fetch_firm_logos --slug bofa --force

WHY FETCH RATHER THAN HOTLINK
-----------------------------
The obvious version of this feature is an `<img src="https://some-logo-api/…">`
in the template. That would tell a third party, on every single page load,
which firms this student is researching — a live feed of somebody's recruiting
strategy to a company with no reason to have it. The rest of this product
refuses that kind of leak, so this one does too: fetch once here, store in our
own media, serve from our own origin. It also means the board still renders
with no network, and costs no per-view latency for the ~13 firms on a page.

SOURCES, AND WHY FOUR
---------------------
Clearbit's logo API was the standard answer and is GONE (dead DNS since the
HubSpot acquisition — verified 2026-08-05). What is left disagrees wildly, so
four sources are tried per candidate domain and the best result wins:

  1. Icons the site DECLARES (apple-touch-icon and friends). Composed to be
     an icon, so square and padded — up to 513px for Point72.
  2. <img> assets in the page that call themselves a logo. This is the
     header WORDMARK, and for the last stragglers it was the only thing
     there: State Street and Franklin Templeton serve a 16px favicon and
     nothing else, while their real marks sit in the header at 256px+.
  3. DuckDuckGo's favicon service.
  4. Google's favicon service.

Ranked by (melts into the page, resolution) IN THAT ORDER — a 128px mark on
white beats a 512px mark baked onto a brand-coloured square, because the
second cannot be un-tiled and reads as a sticker.

SVG is handled, because SVG is exactly what the firms with no usable raster
publish. Pillow cannot read it, so it is rasterised through cairosvg in a
SUBPROCESS — see `_svg_to_png` for why a subprocess and not an import.

WHAT IT REFUSES
---------------
- The "not found" placeholder. Both services answer 200 with a generic glyph
  for a domain they don't know, so a status code proves nothing. The
  DuckDuckGo placeholder is byte-identical across unrelated bogus domains
  (verified against two), and that hash is rejected by name below.
- Anything under MIN_SOURCE_PX. A 16x16 favicon blown up to 44px is mush, and
  mush is worse than the monogram it would replace — blackrock.com is that
  case at both services today, and it correctly keeps its "BL".
- ATS and careers-vendor domains. `blackstone.wd1.myworkdayjobs.com` would
  otherwise fetch WORKDAY's logo and put it on Blackstone's card. The
  blocklist below is derived from the connector catalog's own hosts.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import subprocess
import sys

import requests
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.db.models import Q
from PIL import Image

from directory.models import Firm

# Stored square, at 2x the largest place a logo renders (44px on a firm
# cluster header), so it stays crisp on a retina display and no larger.
STORE_PX = 128

# Below this, upscaling produces mush. The monogram is the better answer.
MIN_SOURCE_PX = 32

TIMEOUT = 12

# Applicant-tracking and careers-platform hosts. A firm's `domains` list holds
# whatever the board connector needed, which for many firms is the VENDOR's
# host — fetching a logo from it would brand the firm with its ATS.
VENDOR_DOMAINS = frozenset({
    "myworkdayjobs.com", "workday.com", "avature.net", "tal.net",
    "greenhouse.io", "lever.co", "oraclecloud.com", "icims.com",
    "successfactors.com", "taleo.net", "brassring.com", "phenompeople.com",
    "eightfold.ai", "beisen.com", "smartrecruiters.com", "jobvite.com",
})

# Multi-label public suffixes this directory actually meets. Not a full PSL —
# that would be a dependency for a handful of hosts — but enough that
# `hsbc.com.hk` resolves to itself rather than to `com.hk`.
TWO_PART_TLDS = frozenset({
    "com.cn", "com.hk", "com.sg", "com.au", "co.uk", "co.jp", "com.tw",
})

# The DuckDuckGo "I don't know this domain" glyph, byte-identical across
# unrelated unknown domains (verified 2026-08-05 against two bogus hosts).
# A 200 with this body means NOT FOUND.
PLACEHOLDER_SHA256 = frozenset({
    "e5db88ea2322863ca17817b99d60006c625a31cff0dad49cf05d3c6d16a75c17",
})


def root_domain(host: str) -> str:
    """`careers.bcg.com` -> `bcg.com`. The careers subdomain often has no
    favicon of its own, or a worse one, than the firm's front door."""
    host = (host or "").strip().lower().strip("/")
    labels = [p for p in host.split(".") if p]
    if len(labels) < 2:
        return ""
    if ".".join(labels[-2:]) in TWO_PART_TLDS and len(labels) >= 3:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def candidates(firm: Firm) -> list[str]:
    """The firm's own domains, root-normalized, vendors dropped, order kept."""
    out: list[str] = []
    for raw in firm.domains or []:
        root = root_domain(raw)
        if root and root not in VENDOR_DOMAINS and root not in out:
            out.append(root)
    return out


UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"}

# Icon rels worth reading off a page, apple-touch-icon first: it exists to be
# a home-screen tile, so it is square, padded, and composed — which is exactly
# what a 38px board tile needs.
_ICON_LINK = re.compile(
    r'<link[^>]+rel=["\'][^"\']*(?:apple-touch-icon|shortcut icon|icon)[^"\']*["\'][^>]*>',
    re.I,
)
_HREF = re.compile(r'href=["\']([^"\']+)', re.I)


def site_icons(domain: str) -> list[str]:
    """Icons the site itself declares.

    Worth the extra request because the favicon services return whatever is
    at /favicon.ico, which is drawn for 16px. KKR's is a wordmark CROPPED
    mid-letter — it shipped to the board reading "KKR" with the R sliced off,
    and the owner called it out. The same site's declared apple-touch-icon is
    the whole wordmark, composed, at 180px. Point72 declares 513px.
    """
    try:
        resp = requests.get(f"https://{domain}/", headers=UA, timeout=TIMEOUT)
        html = resp.text
    except requests.RequestException:
        return []
    urls: list[str] = []
    for tag in _ICON_LINK.findall(html):
        href = _HREF.search(tag)
        if not href:
            continue
        u = href.group(1).strip()
        if u.startswith("//"):
            u = "https:" + u
        elif u.startswith("/"):
            u = f"https://{domain}{u}"
        elif not u.lower().startswith("http"):
            u = f"https://{domain}/{u}"
        if u not in urls:
            urls.append(u)
    return urls


_IMG_TAG = re.compile(r"<img[^>]+>", re.I)
_SRC = re.compile(r'src=["\']([^"\']+)', re.I)


def page_logos(domain: str) -> list[str]:
    """<img> assets on the homepage that call themselves a logo.

    The source that finally reached the last stragglers. A firm's best mark is
    often its HEADER WORDMARK, a plain image in the page, while its favicon is
    a 16px afterthought: Franklin Templeton's header logo is 394x76 on their
    DAM, and State Street's is a full SVG wordmark, while both serve 16px
    favicons and nothing else. Matching on "logo" appearing anywhere in the
    tag catches src, alt and class, which between them cover how these are
    marked up in practice.
    """
    html = ""
    for host in (f"https://www.{domain}/", f"https://{domain}/"):
        try:
            resp = requests.get(host, headers=UA, timeout=TIMEOUT)
            if len(resp.text) > 2000:
                html = resp.text
                break
        except requests.RequestException:
            continue
    if not html:
        return []

    out: list[str] = []
    for tag in _IMG_TAG.findall(html):
        if "logo" not in tag.lower():
            continue
        src = _SRC.search(tag)
        if not src:
            continue
        u = src.group(1).strip()
        if u.startswith("data:"):
            continue
        if u.startswith("//"):
            u = "https:" + u
        elif u.startswith("/"):
            u = f"https://{domain}{u}"
        elif not u.lower().startswith("http"):
            u = f"https://{domain}/{u}"
        if u not in out:
            out.append(u)
    return out[:6]


def sources(domain: str) -> list[str]:
    return [
        *site_icons(domain),
        *page_logos(domain),
        f"https://icons.duckduckgo.com/ip3/{domain}.ico",
        f"https://www.google.com/s2/favicons?domain={domain}&sz=256",
    ]


# Where Homebrew and the common Linux distros put cairo. cairocffi resolves
# it by leaf name through dlopen, which does NOT search Homebrew's prefix, so
# on a stock macOS `import cairosvg` fails with the library sitting right
# there. Loading it by full path first puts it in the process, and the later
# by-name dlopen then resolves against the already-loaded image — which saves
# every caller from having to remember DYLD_FALLBACK_LIBRARY_PATH.
_CAIRO_PATHS = (
    "/opt/homebrew/lib/libcairo.2.dylib",   # Apple silicon Homebrew
    "/usr/local/lib/libcairo.2.dylib",      # Intel Homebrew
    "/usr/lib/x86_64-linux-gnu/libcairo.so.2",
    "/usr/lib/aarch64-linux-gnu/libcairo.so.2",
)


def _cairo_dir() -> str | None:
    """The directory holding cairo, when it is somewhere dlopen won't look."""
    import ctypes.util
    if ctypes.util.find_library("cairo"):
        return ""                      # already resolvable; no help needed
    for path in _CAIRO_PATHS:
        if os.path.exists(path):
            return os.path.dirname(path)
    return None


def _rasterise_svg(raw: bytes) -> Image.Image | None:
    """An SVG mark at STORE_PX, or None when cairo is not installed.

    Imported HERE rather than at module scope on purpose: cairosvg needs
    native cairo, and a missing system library must not break every other
    `manage.py` command in the project. Without it the command simply loses
    its SVG sources and says so.
    """
    png = _svg_to_png(raw)
    if png is None:
        return None
    try:
        img = Image.open(io.BytesIO(png))
        img.load()
        return img
    except Exception:
        return None


def _svg_to_png(raw: bytes) -> bytes | None:
    """SVG bytes -> PNG bytes, in a SUBPROCESS.

    cairocffi resolves libcairo by leaf name through dlopen, which on macOS
    does not search Homebrew's prefix and cannot be satisfied by preloading
    the library — dlopen matches on path, not on already-loaded images
    (verified: a full-path CDLL succeeds and the later import still fails).
    The only thing that works is DYLD_FALLBACK_LIBRARY_PATH set BEFORE the
    process starts, which we cannot do to ourselves.

    So the conversion runs in a child with that variable set. It costs one
    process per SVG — a handful per full refresh — and it means the command
    works out of the box wherever cairo is installed, rather than depending
    on whoever runs it remembering an environment variable.
    """
    cairo_dir = _cairo_dir()
    if cairo_dir is None:
        return None                    # cairo genuinely absent

    env = dict(os.environ)
    if cairo_dir:
        for var in ("DYLD_FALLBACK_LIBRARY_PATH", "LD_LIBRARY_PATH"):
            env[var] = os.pathsep.join(filter(None, (cairo_dir, env.get(var))))

    script = (
        "import sys, cairosvg;"
        f"sys.stdout.buffer.write(cairosvg.svg2png("
        f"bytestring=sys.stdin.buffer.read(), output_width={STORE_PX * 2}))"
    )
    try:
        done = subprocess.run(
            [sys.executable, "-c", script], input=raw, env=env,
            capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 and done.stdout else None


def _fetch(url: str) -> Image.Image | None:
    """One candidate image, or None for every failure mode."""
    try:
        resp = requests.get(url, headers=UA, timeout=TIMEOUT)
    except requests.RequestException:
        return None
    if resp.status_code != 200 or not resp.content:
        return None
    if hashlib.sha256(resp.content).hexdigest() in PLACEHOLDER_SHA256:
        return None
    try:
        img = Image.open(io.BytesIO(resp.content))
        img.load()
    except Exception:
        # Pillow cannot read SVG, and SVG is exactly what the firms with no
        # usable raster mark publish — State Street's wordmark is SVG-only.
        # Rasterise it if cairo is available, otherwise fall through.
        if b"<svg" not in resp.content[:2048].lower():
            return None
        img = _rasterise_svg(resp.content)
        if img is None:
            return None
    if min(img.size) < MIN_SOURCE_PX:
        return None
    return img


def melts_in(img: Image.Image) -> bool:
    """Does this sit on the page, or on its own coloured tile?

    The board wants the mark itself — transparent or white behind it — so a
    logo reads as part of the page rather than as a sticker on it. Many
    apple-touch-icons are the opposite: KKR's is white-on-purple, a solid
    plum square that shouts next to fifteen quiet ones.

    Judged from the four CORNERS, not the whole image: a wordmark fills the
    middle, so averaging everything would call a dark logo a dark background.
    A corner is background by construction.
    """
    img = img.convert("RGBA")

    # Real transparency settles it before any sampling. A wide wordmark can
    # run edge to edge — State Street's waves reach the left corners — so
    # corner sampling alone called a transparent SVG a coloured tile.
    alpha = img.getchannel("A")
    transparent = sum(c for v, c in zip(range(256), alpha.histogram()) if v < 32)
    if transparent > 0.15 * (img.width * img.height):
        return True

    w, h = img.size
    inset = max(1, min(w, h) // 16)
    corners = [
        (inset, inset), (w - 1 - inset, inset),
        (inset, h - 1 - inset), (w - 1 - inset, h - 1 - inset),
    ]
    for x, y in corners:
        r, g, b, a = img.getpixel((x, y))
        if a < 32:
            continue          # transparent: melts in
        if r > 235 and g > 235 and b > 235:
            continue          # white-ish: melts in
        return False
    return True


def best_logo(firm: Firm) -> tuple[Image.Image | None, str]:
    """The best image across every candidate domain and source.

    Ranked by (melts into the page, resolution) — in that order, deliberately.
    A 128px mark on white beats a 512px mark baked onto a brand-coloured
    square, because the second one cannot be un-tiled and looks like a sticker
    on a page of logos that aren't.
    """
    best: Image.Image | None = None
    best_from = ""
    best_rank: tuple[int, int] = (-1, -1)
    for domain in candidates(firm):
        for url in sources(domain):
            img = _fetch(url)
            if img is None:
                continue
            rank = (1 if melts_in(img) else 0, min(img.size))
            if rank > best_rank:
                best, best_from, best_rank = img, url, rank
    return best, best_from


def trim(img: Image.Image) -> Image.Image:
    """Drop a fully transparent border.

    Icons carry wildly different built-in padding — some are edge-to-edge,
    some float a small mark in a big transparent field. Trimming first is what
    makes every tile on the board carry the same visual weight instead of one
    firm's mark reading half the size of its neighbour's.
    """
    bbox = img.convert("RGBA").getchannel("A").getbbox()
    return img.crop(bbox) if bbox else img


def to_png(img: Image.Image) -> bytes:
    """A square RGBA PNG at STORE_PX, the mark FILLING it, aspect preserved.

    The scale is computed and applied with `resize`, not `thumbnail`, because
    thumbnail only ever shrinks. That is the bug the owner saw as "too small":
    a 32px favicon came back, was left at 32px, and got pasted into the middle
    of a 128px canvas — so Macquarie, RBC, Raymond James and TPG rendered as a
    speck occupying a quarter of their tile while their neighbours filled
    theirs. Upscaling a small source is soft, but soft-and-legible beats
    sharp-and-tiny, and MIN_SOURCE_PX still keeps the truly hopeless out.
    """
    img = trim(img.convert("RGBA"))
    scale = STORE_PX / max(img.size)
    img = img.resize(
        (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
        Image.LANCZOS,
    )
    canvas = Image.new("RGBA", (STORE_PX, STORE_PX), (0, 0, 0, 0))
    canvas.paste(img, ((STORE_PX - img.width) // 2, (STORE_PX - img.height) // 2), img)
    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


class Command(BaseCommand):
    help = "Fetch each firm's logo into local media. Monogram stays the fallback."

    def add_arguments(self, parser):
        parser.add_argument("--slug", help="One firm only.")
        parser.add_argument("--force", action="store_true",
                            help="Re-fetch firms that already have a logo.")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        firms = Firm.objects.exclude(domains=[]).order_by("name")
        if opts["slug"]:
            firms = firms.filter(slug=opts["slug"])
        if not opts["force"]:
            # Both spellings of empty: rows predating the column are NULL,
            # rows written since are "". Filtering on one silently selected
            # nothing at all.
            firms = firms.filter(Q(logo="") | Q(logo__isnull=True))

        tag = "[dry-run] " if opts["dry_run"] else ""
        got = missed = 0
        for firm in firms:
            if not candidates(firm):
                missed += 1
                self.stdout.write(f"{tag}—    {firm.name}: only vendor domains, keeping monogram")
                continue

            img, url = best_logo(firm)
            if img is None:
                missed += 1
                self.stdout.write(
                    f"{tag}—    {firm.name}: nothing usable at "
                    f"{MIN_SOURCE_PX}px+, keeping monogram")
                continue

            got += 1
            # Three sources now, so a two-way branch mislabels the one that
            # matters most — the site's own icon was being reported as
            # "google" whenever it won.
            if "duckduckgo.com" in url:
                source = "duckduckgo"
            elif "google.com/s2/favicons" in url:
                source = "google"
            else:
                source = "site"
            bg = "clean" if melts_in(img) else "TILED"
            self.stdout.write(
                f"{tag}LOGO {firm.name}: {img.size[0]}x{img.size[1]} {bg} from {source}")
            if opts["dry_run"]:
                continue
            firm.logo.save(f"{firm.slug}.png", ContentFile(to_png(img)), save=True)

        self.stdout.write(self.style.SUCCESS(
            f"{tag}{got} logo{'' if got == 1 else 's'}, "
            f"{missed} keeping their monogram"))

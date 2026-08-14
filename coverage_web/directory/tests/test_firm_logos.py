"""Firm logos: what may reach a board tile, and what must not.

The board showed two-letter monograms. The owner asked for the real marks,
then for the highest definition available, then for ones that sit on white so
they melt into the page rather than reading as stickers. These pin the rules
that came out of that.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from directory.management.commands.fetch_firm_logos import (
    MIN_SOURCE_PX, VENDOR_DOMAINS, candidates, melts_in, root_domain, to_png, trim,
)
from directory.models import Firm

pytestmark = pytest.mark.django_db


def _img(size, bg, fg=(200, 0, 0, 255)):
    im = Image.new("RGBA", (size, size), bg)
    # A mark in the middle, so corners stay background — the thing melts_in reads.
    for x in range(size // 4, 3 * size // 4):
        for y in range(size // 4, 3 * size // 4):
            im.putpixel((x, y), fg)
    return im


# ---------------------------------------------------------------------------
# Which domain a logo may be fetched from
# ---------------------------------------------------------------------------
def test_a_careers_subdomain_resolves_to_the_firms_front_door():
    """careers.bcg.com has a worse favicon than bcg.com, and often none."""
    assert root_domain("careers.bcg.com") == "bcg.com"
    assert root_domain("https://search.jobs.barclays/") == "jobs.barclays"


def test_a_two_part_tld_is_not_mistaken_for_the_domain():
    assert root_domain("careers.hsbc.com.hk") == "hsbc.com.hk"
    assert root_domain("htsc.com.cn") == "htsc.com.cn"


def test_an_ats_host_never_supplies_the_logo():
    """`blackstone.wd1.myworkdayjobs.com` would brand Blackstone with
    WORKDAY's logo — the firm's `domains` list holds whatever the board
    connector needed, which is often the vendor."""
    firm = Firm.objects.create(
        slug="blackstone", name="Blackstone",
        domains=["blackstone.wd1.myworkdayjobs.com", "blackstone.com"],
    )
    assert candidates(firm) == ["blackstone.com"]
    assert "myworkdayjobs.com" in VENDOR_DOMAINS


def test_a_firm_with_only_vendor_domains_gets_no_candidate(): 
    firm = Firm.objects.create(slug="x", name="X", domains=["x.tal.net"])
    assert candidates(firm) == []


# ---------------------------------------------------------------------------
# Which image wins
# ---------------------------------------------------------------------------
def test_transparent_and_white_backgrounds_melt_into_the_page():
    assert melts_in(_img(64, (0, 0, 0, 0))) is True
    assert melts_in(_img(64, (255, 255, 255, 255))) is True


def test_a_brand_coloured_tile_does_not():
    """KKR's apple-touch-icon is white-on-purple: a solid plum square that
    shouts next to fifteen quiet ones."""
    assert melts_in(_img(64, (77, 20, 84, 255))) is False


def test_the_mark_itself_never_decides_the_verdict():
    """A dark wordmark on white is CLEAN. Judged from the whole image it
    would average dark and be called a tile."""
    assert melts_in(_img(64, (255, 255, 255, 255), fg=(0, 0, 0, 255))) is True


# ---------------------------------------------------------------------------
# What gets stored
# ---------------------------------------------------------------------------
def test_stored_logos_are_a_uniform_square():
    """Tiles sit in a row; a non-square logo would make one of them jump."""
    out = Image.open(io.BytesIO(to_png(_img(200, (0, 0, 0, 0)))))
    assert out.size == (128, 128)
    assert out.mode == "RGBA", "transparency survives, or nothing melts in"


def test_a_wide_wordmark_is_fitted_not_cropped():
    """`contain`, never `cover`: half these marks are wordmarks, and cropping
    one cuts the words off — which is exactly how KKR shipped reading 'KK'."""
    wide = Image.new("RGBA", (400, 100), (255, 255, 255, 255))
    out = Image.open(io.BytesIO(to_png(wide)))
    assert out.size == (128, 128)
    # Letterboxed: the source's own aspect ratio is preserved inside the square.
    assert out.getpixel((2, 2))[3] == 0, "padding is transparent, not stretched pixels"


def test_the_minimum_source_is_big_enough_to_not_be_mush():
    """A 16px favicon upscaled to a 38px tile is mush, and mush is worse than
    the monogram it would replace."""
    assert MIN_SOURCE_PX >= 32


# ---------------------------------------------------------------------------
# Sizing and centering — the "too small" bug the owner reported
# ---------------------------------------------------------------------------
def test_a_small_source_is_scaled_up_to_fill_its_tile():
    """The shipped bug. `thumbnail` only ever SHRINKS, so a 32px favicon was
    left at 32px and pasted into the middle of a 128px canvas — Macquarie,
    RBC, Raymond James and TPG each rendered as a speck occupying a quarter
    of their tile while their neighbours filled theirs."""
    out = Image.open(io.BytesIO(to_png(_img(32, (255, 255, 255, 255)))))
    assert out.size == (128, 128)
    bbox = out.getchannel("A").getbbox()
    assert max(bbox[2] - bbox[0], bbox[3] - bbox[1]) == 128, "fills, not floats"


def test_built_in_transparent_padding_is_trimmed_first():
    """Icons pad themselves by wildly different amounts. Without trimming,
    one firm's mark reads half the size of its neighbour's on the same row."""
    padded = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    for x in range(56, 72):
        for y in range(56, 72):
            padded.putpixel((x, y), (200, 0, 0, 255))
    out = Image.open(io.BytesIO(to_png(padded)))
    bbox = out.getchannel("A").getbbox()
    assert max(bbox[2] - bbox[0], bbox[3] - bbox[1]) == 128


def test_the_mark_lands_centred_on_both_axes():
    wide = Image.new("RGBA", (400, 100), (0, 0, 0, 0))
    for x in range(400):
        for y in range(100):
            wide.putpixel((x, y), (0, 0, 200, 255))
    out = Image.open(io.BytesIO(to_png(wide)))
    left, top, right, bottom = out.getchannel("A").getbbox()
    assert abs((left + right) / 2 - 64) <= 1, "horizontally centred"
    assert abs((top + bottom) / 2 - 64) <= 1, "vertically centred"


def test_aspect_ratio_survives_the_fit():
    """A 4:1 wordmark squeezed into a square would be unreadable."""
    wide = Image.new("RGBA", (400, 100), (0, 0, 0, 0))
    for x in range(400):
        for y in range(100):
            wide.putpixel((x, y), (0, 0, 200, 255))
    out = Image.open(io.BytesIO(to_png(wide)))
    left, top, right, bottom = out.getchannel("A").getbbox()
    assert round((right - left) / (bottom - top)) == 4


# ---------------------------------------------------------------------------
# The sources that reached the last stragglers
# ---------------------------------------------------------------------------
def test_a_header_wordmark_is_a_candidate_not_just_the_favicon():
    """State Street and Franklin Templeton serve 16px favicons and nothing
    else — their real marks are plain <img> assets in the page header."""
    from directory.management.commands import fetch_firm_logos as cmd
    # Padded past the 2000-char floor `page_logos` uses to skip JS shells —
    # Huatai's homepage is 940 chars of loader and no markup worth reading.
    html = (
        '<html><header>'
        '<img class="site-logo" src="/assets/ft-global-logo-header.png">'
        '<img src="/assets/hero-photo.jpg" alt="office">'
        '</header>' + ('<p>filler</p>' * 200) + '</html>'
    )

    class _Resp:
        status_code = 200
        text = html

    def fake_get(url, **kw):
        return _Resp()

    orig, cmd.requests.get = cmd.requests.get, fake_get
    try:
        found = cmd.page_logos("example.com")
    finally:
        cmd.requests.get = orig

    assert found == ["https://example.com/assets/ft-global-logo-header.png"]
    assert not any("hero-photo" in u for u in found), "only images that say logo"


def test_an_svg_only_firm_is_not_a_dead_end():
    """Pillow cannot read SVG, and SVG is exactly what the firms with no
    usable raster publish. Rasterised when cairo is present; when it is not,
    the command must degrade to PNG sources rather than crash."""
    from directory.management.commands.fetch_firm_logos import _rasterise_svg
    svg = (b'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="60">'
           b'<rect width="200" height="60" fill="#0000ff"/></svg>')
    out = _rasterise_svg(svg)
    assert out is None or out.width > 0, "either rasterised, or cleanly absent"


def test_transparency_alone_proves_it_melts_in():
    """A wide wordmark can run edge to edge — State Street's waves reach the
    left corners — so corner sampling alone called a transparent SVG a
    coloured tile."""
    edge_to_edge = Image.new("RGBA", (200, 60), (0, 0, 0, 0))
    for x in range(200):
        for y in range(20, 40):
            edge_to_edge.putpixel((x, y), (0, 0, 200, 255))
    assert melts_in(edge_to_edge) is True


# ---------------------------------------------------------------------------
# Cycle labels — the raw slug that leaked into body copy
# ---------------------------------------------------------------------------
def test_a_cycle_slug_is_rendered_as_english():
    """`SA2028_IB` sat in the product's own copy on the Jefferies page. The
    column holds two spellings of one vocabulary (importers wrote the slug,
    seeds wrote the prose), so this formats on READ — rewriting the stored
    value would break the importer's own matching for a display bug."""
    from directory.views import cycle_label
    assert cycle_label("sa2028_ib") == "SA 2028 · IB"
    assert cycle_label("insight") == "Insight"


def test_a_region_suffix_is_not_mistaken_for_a_track():
    """`sa2028_hk` used to expand to "SA 2028 · Hong Kong", seating a MARKET
    in the slot that means DESK — and _timeline.html then printed the row's
    own region after it ("SA 2028 · HONG KONG · HK"). Naming the market is
    `cycle_region`'s job, and it happens once."""
    from directory.views import cycle_label, cycle_region
    assert cycle_label("sa2028_hk") == "SA 2028"
    assert cycle_region("sa2028_hk") == "hk"
    assert cycle_region("sa2028_ib") == ""
    assert cycle_region("SA 2028") == ""


def test_an_already_human_cycle_is_left_alone():
    from directory.views import cycle_label
    assert cycle_label("SA 2028") == "SA 2028"
    assert cycle_label("") == ""

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
    MIN_SOURCE_PX, VENDOR_DOMAINS, candidates, melts_in, root_domain, to_png,
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

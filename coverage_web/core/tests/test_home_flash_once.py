"""The landing page prints a flash message once, not twice.

base.html renders `.page-msgs` inside <main> for every page in the product.
core/home.html used to render its OWN second copy of the same loop in
`.home-msgs`, left behind when the loop was hoisted into base.html — so on
the one page a just-deleted user can still see, `delete_account`'s receipt
("Deleted 137 contacts, 138 touches, …") rendered twice in a row.

Django's message storage caches `_loaded_messages`, so a second iteration in
the same render yields the same messages again rather than an empty list;
nothing about the double render was self-correcting.
"""

import pytest
from django.template.loader import render_to_string


class _Msg:
    """Stands in for a real `Message`: `tags` plus a `__str__`, which is all
    the template touches."""

    tags = "success"

    def __str__(self):
        return "SENTINEL-DELETION-RECEIPT"


@pytest.mark.django_db
def test_landing_page_renders_a_flash_message_exactly_once():
    body = render_to_string(
        "core/home.html",
        {
            "messages": [_Msg()],
            "open_count": 1,
            "firm_count": 1,
            "strip_firms": [],
        },
    )

    assert body.count("SENTINEL-DELETION-RECEIPT") == 1, (
        "base.html already renders the flash loop for every page; a second "
        "copy in home.html prints the deletion receipt twice"
    )

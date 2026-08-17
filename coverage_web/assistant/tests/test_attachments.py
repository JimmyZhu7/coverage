"""assistant.attachments: turning an upload into a content block, or a
plain-English reason it can't be one. No Django test client here — these
are pure functions over Django's own SimpleUploadedFile, no network,
no database.
"""

from __future__ import annotations

import base64

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from assistant import attachments

pytestmark = pytest.mark.django_db


def _file(name, content_type, size=None, content=b"x"):
    body = content * (size or len(content)) if size else content
    return SimpleUploadedFile(name, body, content_type=content_type)


def test_a_small_png_becomes_an_image_block():
    upload = _file("logo.png", "image/png", content=b"\x89PNG fake bytes")

    blocks, errors = attachments.blocks_for([upload])

    assert errors == []
    assert len(blocks) == 1
    assert blocks[0]["type"] == "image"
    assert blocks[0]["source"]["media_type"] == "image/png"
    assert blocks[0]["source"]["data"] == base64.b64encode(b"\x89PNG fake bytes").decode("ascii")
    assert blocks[0]["_filename"] == "logo.png"


def test_a_pdf_becomes_a_document_block():
    upload = _file("resume.pdf", "application/pdf", content=b"%PDF-1.4 fake")

    blocks, errors = attachments.blocks_for([upload])

    assert errors == []
    assert blocks[0]["type"] == "document"
    assert blocks[0]["source"]["media_type"] == "application/pdf"
    assert blocks[0]["_filename"] == "resume.pdf"


def test_a_pdf_is_recognised_by_extension_even_with_a_generic_content_type():
    """Some browsers hand a PDF over as application/octet-stream — the
    filename is the fallback signal, not just the browser's own guess."""
    upload = _file("notes.pdf", "application/octet-stream", content=b"%PDF fake")

    blocks, errors = attachments.blocks_for([upload])

    assert errors == []
    assert blocks[0]["type"] == "document"


def test_a_text_file_is_inlined_as_a_text_block_with_its_name():
    upload = _file("contacts.csv", "text/csv", content=b"name,firm\nJane,GS\n")

    blocks, errors = attachments.blocks_for([upload])

    assert errors == []
    assert blocks[0]["type"] == "text"
    assert "[Attached file: contacts.csv]" in blocks[0]["text"]
    assert "name,firm" in blocks[0]["text"]
    # Plain text blocks carry no _filename — there is nothing to strip
    # before an API call, since there's no separate "source" payload.
    assert "_filename" not in blocks[0]


def test_a_long_text_file_is_truncated_not_rejected():
    big = ("line\n" * 20_000).encode("utf-8")  # ~100,000 chars, under the 2MB size cap
    upload = _file("log.txt", "text/plain", content=big)

    blocks, errors = attachments.blocks_for([upload])

    assert errors == []
    assert len(blocks[0]["text"]) <= attachments.MAX_TEXT_INLINE_CHARS + len("[Attached file: log.txt]\n\n")


def test_an_unsupported_file_type_is_a_clear_error_not_an_exception():
    upload = _file("resume.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")

    blocks, errors = attachments.blocks_for([upload])

    assert blocks == []
    assert len(errors) == 1
    assert "resume.docx" in errors[0]
    assert "isn't supported" in errors[0]


def test_an_oversized_image_is_rejected_with_its_actual_size_named():
    upload = _file("huge.png", "image/png", size=6 * 1024 * 1024)  # over the 5 MB image cap

    blocks, errors = attachments.blocks_for([upload])

    assert blocks == []
    assert "huge.png" in errors[0]
    assert "6.0 MB" in errors[0]


def test_an_oversized_pdf_is_rejected():
    upload = _file("deck.pdf", "application/pdf", size=11 * 1024 * 1024)  # over the 10 MB PDF cap

    blocks, errors = attachments.blocks_for([upload])

    assert blocks == []
    assert "deck.pdf" in errors[0]


def test_an_oversized_text_file_is_rejected():
    upload = _file("dump.txt", "text/plain", size=3 * 1024 * 1024)  # over the 2 MB text cap

    blocks, errors = attachments.blocks_for([upload])

    assert blocks == []
    assert "dump.txt" in errors[0]


def test_more_than_three_files_is_rejected_outright():
    uploads = [_file(f"f{i}.png", "image/png") for i in range(4)]

    blocks, errors = attachments.blocks_for(uploads)

    assert blocks == []
    assert "Only 3 files" in errors[0]


def test_three_files_that_individually_fit_can_still_blow_the_combined_cap():
    each = 7 * 1024 * 1024  # 3 x 7MB = 21MB, over the 20MB combined cap, none over the 10MB PDF cap alone
    uploads = [_file(f"f{i}.pdf", "application/pdf", size=each) for i in range(3)]

    blocks, errors = attachments.blocks_for(uploads)

    assert any("combined" in e for e in errors)


def test_a_file_that_is_not_valid_utf8_text_is_a_clear_error():
    upload = _file("mystery.txt", "text/plain", content=b"\xff\xfe\x00\x01 not utf8")

    blocks, errors = attachments.blocks_for([upload])

    assert blocks == []
    assert "couldn't read this as text" in errors[0]


def test_no_files_is_not_an_error():
    assert attachments.blocks_for([]) == ([], [])


def test_strip_private_fields_removes_filename_but_keeps_everything_else():
    blocks = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "abc"}, "_filename": "x.png"},
        {"type": "text", "text": "hello"},
    ]

    cleaned = attachments.strip_private_fields(blocks)

    assert cleaned[0] == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "abc"}}
    assert cleaned[1] == {"type": "text", "text": "hello"}
    # The original list is untouched — a replay builder calling this on
    # every request must never mutate what's still going to be persisted.
    assert "_filename" in blocks[0]

from __future__ import annotations

from flakedoctor._fingerprint import CRASH, HANG, Fingerprint, normalize_message


def test_normalize_masks_hex_addresses():
    assert (
        normalize_message("<Foo object at 0x7f3a2b109c40> != expected")
        == "<Foo object at 0xADDR> != expected"
    )


def test_normalize_masks_tmp_paths():
    msg = "could not open /private/var/folders/ab/xyz123/T/pytest-42/file.txt for reading"
    out = normalize_message(msg)
    assert "/var/folders" not in out
    assert "<tmpdir>" in out


def test_normalize_masks_long_numbers_but_keeps_small_ones():
    out = normalize_message("expected 3 items, got 4 (request id 1234567890)")
    assert "expected 3 items, got 4" in out
    assert "1234567890" not in out
    assert "<n>" in out


def test_normalize_takes_first_line_and_truncates():
    out = normalize_message("first line\nsecond line\nthird")
    assert out == "first line"
    assert len(normalize_message("x" * 500)) == 200


def test_normalize_empty():
    assert normalize_message("") == ""
    assert normalize_message("   \n  ") == ""


def test_fingerprint_equality_and_digest():
    a = Fingerprint("call", "AssertionError", "assert 1 == 2", "test_x.py:10")
    b = Fingerprint("call", "AssertionError", "assert 1 == 2", "test_x.py:10")
    c = Fingerprint("call", "AssertionError", "assert 1 == 2", "test_x.py:11")
    assert a.key() == b.key()
    assert a.digest() == b.digest()
    assert a.key() != c.key()
    assert a.digest() != c.digest()
    assert len(a.digest()) == 12


def test_fingerprint_describe():
    fp = Fingerprint("setup", "myapp.errors.BoomError", "kaboom", "conftest.py:5")
    text = fp.describe()
    assert "myapp.errors.BoomError" in text
    assert "setup" in text
    assert "conftest.py:5" in text


def test_process_outcome_fingerprints_are_distinct():
    assert HANG.key() != CRASH.key()
    assert HANG.exc_type == "<hang>"

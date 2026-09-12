"""Byte-range parsing for BMC ISO image serving."""
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.virtual_media import _parse_byte_range, _range_response


def test_parse_byte_range_suffix():
    start, end = _parse_byte_range("bytes=-1024", 5000)
    assert start == 3976
    assert end == 4999


def test_parse_byte_range_open_end():
    start, end = _parse_byte_range("bytes=100-", 500)
    assert start == 100
    assert end == 499


def test_parse_byte_range_invalid():
    with pytest.raises(HTTPException) as exc:
        _parse_byte_range("bytes=0-1,2-3", 100)
    assert exc.value.status_code == 416


def test_range_response_head_full(tmp_path):
    iso = tmp_path / "a.iso"
    iso.write_bytes(b"abcdef")
    request = Request({"type": "http", "method": "HEAD", "headers": [], "path": "/"})
    resp = _range_response(request, iso, "a.iso")
    assert resp.status_code == 200
    assert resp.headers["Content-Length"] == "6"

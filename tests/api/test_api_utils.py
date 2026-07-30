import string

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.utils import PasswordGenerateRequest, generate_password


@pytest.mark.asyncio
@pytest.mark.parametrize("charset", ["alphanumeric", "alphanumeric_symbols"])
@pytest.mark.parametrize("length", [4, 5, 8, 12, 16, 32, 64, 128])
@pytest.mark.parametrize("exclude_ambiguous", [True, False])
async def test_generate_password_has_requested_length_and_composition(
    charset, length, exclude_ambiguous
):
    result = await generate_password(
        PasswordGenerateRequest(
            charset=charset, length=length, exclude_ambiguous=exclude_ambiguous
        )
    )
    assert len(result.password) == length
    assert any(char.isdigit() for char in result.password)
    assert any(char.islower() for char in result.password)
    assert any(char.isupper() for char in result.password)
    allowed = string.ascii_letters + string.digits
    if charset == "alphanumeric_symbols":
        allowed += "!@#$%^&*()_+-=[]{}|;:,.<>?"
    assert set(result.password).issubset(set(allowed))
    if exclude_ambiguous:
        # Base charset filter excludes these (lowercase "i" is not in the set).
        assert not set(result.password).intersection("0O1Il5S2Z")


@pytest.mark.asyncio
@pytest.mark.parametrize("charset, detail", [
    ("numeric", "No lowercase"),
    ("alphabetic", "No digits"),
])
async def test_generate_password_rejects_charsets_without_required_composition(
    charset, detail
):
    with pytest.raises(HTTPException, match=detail) as exc:
        await generate_password(PasswordGenerateRequest(charset=charset))
    assert exc.value.status_code == 400


@pytest.mark.parametrize("length", [0, 1, 2, 3, 129, 1000])
def test_password_request_validates_length_bounds(length):
    with pytest.raises(ValidationError):
        PasswordGenerateRequest(length=length)


@pytest.mark.parametrize("charset", ["", "symbols", "ALPHANUMERIC", "unicode"])
def test_password_request_validates_charset(charset):
    with pytest.raises(ValidationError):
        PasswordGenerateRequest(charset=charset)


def test_password_request_defaults_are_safe():
    request = PasswordGenerateRequest()
    assert request.length == 16
    assert request.charset == "alphanumeric"
    assert request.exclude_ambiguous is True

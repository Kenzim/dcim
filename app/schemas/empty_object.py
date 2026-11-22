"""Accept an empty JSON list where a request expects a JSON object.

PHP's ``json_encode(array())`` writes ``[]``, so older WHMCS module copies send
an empty option set as a list. ``EmptyListIsObject`` turns that one case into
``{}`` for a field; ``EmptyListBody`` does the same for a whole request body.
Non-empty lists are left alone, so they still fail validation.
"""
from typing import Any

from pydantic import BaseModel, BeforeValidator, model_validator


def empty_list_as_object(value: Any) -> Any:
    if isinstance(value, list) and not value:
        return {}
    return value


EmptyListIsObject = BeforeValidator(empty_list_as_object)


class EmptyListBody(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _empty_list_is_object(cls, data: Any) -> Any:
        return empty_list_as_object(data)

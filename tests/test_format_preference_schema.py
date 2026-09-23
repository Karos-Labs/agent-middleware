"""0008 (2026-09-23): the post-type preference a person sets, validated before it is stored."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.schemas.learning import PreferencesWrite
from app.services.learning_store import _formats_of


def test_a_known_preference_round_trips_by_alias() -> None:
    body = PreferencesWrite.model_validate(
        {
            "formats": {
                "instagram": {
                    "postModes": ["news_flash"],
                    "pictureDensity": "photo-first",
                    "series": "the_list",
                }
            }
        }
    )
    assert body.formats is not None
    dumped = body.formats["instagram"].model_dump(by_alias=True, exclude_none=True)
    assert dumped == {
        "postModes": ["news_flash"],
        "pictureDensity": "photo-first",
        "series": "the_list",
    }


@pytest.mark.parametrize(
    "bad",
    [
        {"instagram": {"pictureDensity": "all-photos"}},
        {"instagram": {"format": "reel"}},
        {"instagram": {"postModes": ["breaking"]}},
        {"instagram": {"series": "The List!"}},
        {"instagram": {"colour": "red"}},
        {"myspace": {"format": "single"}},
    ],
)
def test_an_unknown_value_platform_or_field_is_refused(bad: dict) -> None:
    with pytest.raises(ValidationError):
        PreferencesWrite.model_validate({"formats": bad})


def test_absent_formats_leaves_the_stored_map_alone() -> None:
    assert PreferencesWrite.model_validate({"neverTopics": ["x"]}).formats is None


def test_the_reader_tolerates_what_a_database_might_hand_back() -> None:
    assert _formats_of({"instagram": {"format": "single"}}) == {"instagram": {"format": "single"}}
    assert _formats_of('{"instagram": {"format": "single"}}') == {"instagram": {"format": "single"}}
    assert _formats_of(None) == {}
    assert _formats_of("not json") == {}
    assert _formats_of([1, 2]) == {}

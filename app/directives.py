from collections.abc import Mapping
from math import isfinite
from typing import Any


class DirectiveValidationError(ValueError):
    """Raised when a directive interpretation violates the GridWise rules."""


def validate_directive_interpretation(
    directives: list[Mapping[str, Any]],
) -> bool:
    """Validate parsed directive interpretations and return ``True`` when valid."""
    if not isinstance(directives, list):
        raise DirectiveValidationError("directive_interpretation must be a list")

    for expected_index, directive in enumerate(directives):
        if not isinstance(directive, Mapping):
            raise DirectiveValidationError("each directive must be an object")

        note_index = directive.get("note_index")
        if isinstance(note_index, bool) or not isinstance(note_index, int):
            raise DirectiveValidationError("note_index must be an integer")
        if note_index != expected_index:
            raise DirectiveValidationError("note_index values must be sequential")

        directive_type = directive.get("directive_type")
        applies = directive.get("applies")
        structured_adjustment = directive.get("structured_adjustment")

        if structured_adjustment is not None and not isinstance(
            structured_adjustment, Mapping
        ):
            raise DirectiveValidationError(
                "structured_adjustment must be an object or null"
            )

        if directive_type == "solar_reduction":
            if structured_adjustment is None:
                raise DirectiveValidationError(
                    "solar_reduction requires a structured adjustment"
                )
            factor = structured_adjustment.get("factor")
            if (
                isinstance(factor, bool)
                or not isinstance(factor, float)
                or not isfinite(factor)
                or not 0.0 <= factor <= 1.0
            ):
                raise DirectiveValidationError(
                    "solar_reduction factor must be a float from 0 to 1"
                )

        if directive_type == "no_op":
            if applies is not False or structured_adjustment is not None:
                raise DirectiveValidationError(
                    "no_op directives must not apply or contain an adjustment"
                )
        elif applies is not True:
            raise DirectiveValidationError("non-no_op directives must apply")

        if structured_adjustment is not None:
            if "hours" in structured_adjustment:
                hours = structured_adjustment["hours"]
                if not isinstance(hours, list):
                    raise DirectiveValidationError(
                        "structured_adjustment hours must be a list"
                    )
                if any(isinstance(hour, bool) or not isinstance(hour, int) for hour in hours):
                    raise DirectiveValidationError("structured_adjustment hours must be integers")
                if any(hour < 0 or hour > 23 for hour in hours):
                    raise DirectiveValidationError(
                        "structured_adjustment hours must be from 0 to 23"
                    )
                if any(current >= following for current, following in zip(hours, hours[1:])):
                    raise DirectiveValidationError(
                        "structured_adjustment hours must be unique and ascending"
                    )

    return True

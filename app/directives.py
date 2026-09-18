"""Deterministic guardrails between the LLM (untrusted) and the optimizer (trusted)."""

from math import isfinite

from app.models import DirectiveInterpretation, StructuredAdjustment

DIRECTIVE_TYPES = [
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
]
VALUE_FIELDS = ["factor", "minimum_energy_kwh", "max_grid_kwh"]
NO_OP_EXPLANATION = "This note does not affect today's 24-hour energy schedule."
MAX_EXPLANATION_CHARS = 300


class DirectiveValidationError(ValueError):
    """Raised when a directive violates the Problem Statement guardrails."""


def value_field_for(directive_type: str) -> str | None:
    if directive_type == "solar_reduction":
        return "factor"
    if directive_type == "minimum_battery_reserve":
        return "minimum_energy_kwh"
    if directive_type == "max_grid_window":
        return "max_grid_kwh"
    return None


def no_op(note_index: int, explanation: str = NO_OP_EXPLANATION) -> DirectiveInterpretation:
    return DirectiveInterpretation(
        note_index=note_index,
        applies=False,
        directive_type="no_op",
        structured_adjustment=None,
        explanation=explanation,
    )


def clean_explanation(text, default: str) -> str:
    if not isinstance(text, str) or not text.strip():
        return default
    return " ".join(text.split())[:MAX_EXPLANATION_CHARS]


def _number(raw, name: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not isfinite(raw):
        raise DirectiveValidationError(f"{name} must be a finite number")
    return float(raw)


def _whole_hour(raw) -> int:
    value = _number(raw, "window hour")
    if not value.is_integer():
        raise DirectiveValidationError("window hours must be whole hours")
    return int(value)


def hours_from_windows(windows) -> list[int]:
    """Expand [start, end) windows into sorted unique hours.

    End is exclusive ("1 PM to 3 PM" -> [13, 15] -> [13, 14]); end 24 means midnight;
    a window whose end precedes its start wraps past midnight.
    """
    if not isinstance(windows, list) or not windows:
        raise DirectiveValidationError("at least one time window is required")
    hours = []
    for window in windows:
        if not isinstance(window, list) or len(window) != 2:
            raise DirectiveValidationError("each window must be [start_hour, end_hour]")
        start, end = _whole_hour(window[0]), _whole_hour(window[1])
        if not (0 <= start <= 23 and 0 <= end <= 24) or start == end:
            raise DirectiveValidationError(f"invalid window [{start}, {end}]")
        if start < end:
            hours.extend(range(start, end))
        else:
            hours.extend(range(start, 24))
            hours.extend(range(0, end))
    return sorted(set(hours))


def solar_factor(percent, percent_meaning) -> float:
    """Normalise the note's percentage into the usable fraction that remains.

    The LLM reports the number exactly as written plus whether it describes what is
    lost ("80% reduction") or what is left ("drops to 20%"); only losses are inverted.
    """
    value = _number(percent, "percent")
    if 0 < value < 1:
        value *= 100  # the model returned a fraction instead of a percentage
    if not 0 <= value <= 100:
        raise DirectiveValidationError("solar percent must be within 0-100")
    if percent_meaning == "reduction":
        value = 100 - value
    elif percent_meaning != "remaining":
        raise DirectiveValidationError("percent_meaning must be 'remaining' or 'reduction'")
    return round(value / 100, 4)


def reserve_kwh(amount, unit, capacity_kwh: float) -> float:
    value = _number(amount, "reserve amount")
    if unit == "percent":
        value = value / 100 * capacity_kwh
    elif unit != "kwh":
        raise DirectiveValidationError("reserve unit must be 'kwh' or 'percent'")
    return round(value, 4)


def build_directive(note_index: int, raw, capacity_kwh: float) -> DirectiveInterpretation:
    """Convert one raw LLM entry into a validated directive.

    Unsupported directive types are coerced to no_op; a supported type with a malformed
    payload raises DirectiveValidationError so the caller can recover that note.
    """
    if not isinstance(raw, dict):
        raise DirectiveValidationError("directive entry must be an object")
    directive_type = raw.get("directive_type")
    if directive_type not in DIRECTIVE_TYPES:
        return no_op(note_index, "Model proposed an unsupported directive type; treated as no_op.")
    if directive_type == "no_op":
        return no_op(note_index, clean_explanation(raw.get("explanation"), NO_OP_EXPLANATION))

    adjustment = StructuredAdjustment(hours=hours_from_windows(raw.get("windows")))
    if directive_type == "solar_reduction":
        adjustment.factor = solar_factor(raw.get("percent"), raw.get("percent_meaning"))
    elif directive_type == "minimum_battery_reserve":
        adjustment.minimum_energy_kwh = reserve_kwh(raw.get("amount"), raw.get("amount_unit"), capacity_kwh)
    elif directive_type == "max_grid_window":
        adjustment.max_grid_kwh = round(_number(raw.get("max_grid_kwh"), "max_grid_kwh"), 4)

    directive = DirectiveInterpretation(
        note_index=note_index,
        applies=True,
        directive_type=directive_type,
        structured_adjustment=adjustment,
        explanation=clean_explanation(raw.get("explanation"), f"Interpreted as {directive_type}."),
    )
    validate_directive(directive, capacity_kwh)
    return directive


def validate_directive(directive: DirectiveInterpretation, capacity_kwh: float) -> None:
    """Final guardrail applied to every directive, whatever produced it."""
    if directive.directive_type == "no_op":
        if directive.applies or directive.structured_adjustment is not None:
            raise DirectiveValidationError("no_op must have applies=false and a null adjustment")
        return
    if not directive.applies:
        raise DirectiveValidationError("non-no_op directives must have applies=true")

    adjustment = directive.structured_adjustment
    if adjustment is None:
        raise DirectiveValidationError(f"{directive.directive_type} requires a structured adjustment")
    hours = adjustment.hours
    if not hours or any(hour < 0 or hour > 23 for hour in hours):
        raise DirectiveValidationError("hours must be a non-empty list of integers 0-23")
    if any(current >= following for current, following in zip(hours, hours[1:])):
        raise DirectiveValidationError("hours must be unique and ascending")

    required = value_field_for(directive.directive_type)
    for field in VALUE_FIELDS:
        present = getattr(adjustment, field) is not None
        if present != (field == required):
            raise DirectiveValidationError(f"{directive.directive_type} has the wrong adjustment shape")
    if required is None:
        return

    amount = _number(getattr(adjustment, required), required)
    if required == "factor" and not 0 <= amount <= 1:
        raise DirectiveValidationError("solar factor must be within 0-1")
    if required == "minimum_energy_kwh" and not 0 <= amount <= capacity_kwh:
        raise DirectiveValidationError("reserve must be within 0 and battery capacity")
    if required == "max_grid_kwh" and amount < 0:
        raise DirectiveValidationError("max_grid_kwh must be non-negative")


def validate_interpretations(
    directives: list[DirectiveInterpretation], note_count: int, capacity_kwh: float
) -> None:
    if len(directives) != note_count:
        raise DirectiveValidationError("exactly one interpretation is required per note")
    for expected_index, directive in enumerate(directives):
        if directive.note_index != expected_index:
            raise DirectiveValidationError("interpretations must follow note_index order")
        validate_directive(directive, capacity_kwh)

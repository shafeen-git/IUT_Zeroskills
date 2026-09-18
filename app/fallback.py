"""Rule-based safety net used only when the LLM is unavailable or returns an unusable entry.

It is deliberately conservative: when a value cannot be read it prefers no_op (or zero usable
solar) over guessing a limit that could make the schedule infeasible.
"""

import re

from app.directives import hours_from_windows, no_op, reserve_kwh
from app.models import DirectiveInterpretation, StructuredAdjustment

_TIME = r"(\d{1,2})(?::\d{2})?\s*(a\.?m\.?|p\.?m\.?)?"
_RANGE = re.compile(_TIME + r"\s*(?:to|until|till|til|through|thru|and|-|–)\s*" + _TIME)
_OPEN_END = re.compile(r"(?:from|after|starting at|beginning at|onwards? from)\s+" + _TIME)
_OPEN_START = re.compile(r"(?:until|till|before|up to)\s+" + _TIME)
_NUMBER = r"(\d+(?:\.\d+)?)"

_NEGATION = r"\b(?:not|no|never|forbid\w*|prohibit\w*|disabl\w*|unavailable|block\w*|suspend\w*|isolat\w*|lock\w*|prevent\w*|avoid\w*|halt\w*|off|offline|out of service|pause\w*|stop\w*)\b"
_SOLAR = r"\b(?:solar|pv|photovoltaic|panels?|rooftop array)\b"
_SOLAR_LOSS = r"(?:reduc|drop|cut|clean|wash|dust|cloud|shad|inverter|inspect|mainten|offline|lower|fall|haz|smog|curtail|%|percent|half|quarter|third|fifth)"
_GRID = r"\b(?:grid|feeder|transformer|substation|utility|intake|import)\b"
_RESERVE = r"(?:reserve|keep at least|maintain at least|at least .* (?:in|stored)|remain in the battery|state of charge|\bsoc\b)"

_FRACTION_WORDS = [
    ["three[- ]quarters?", 75.0],
    ["two[- ]thirds?", 66.67],
    ["half", 50.0],
    ["(?:a|one)[- ]third", 33.33],
    ["(?:a|one)[- ]quarter", 25.0],
    ["(?:a|one)[- ]fifth", 20.0],
    ["(?:a|one)[- ]tenth", 10.0],
]


def _normalise(note: str) -> str:
    text = note.lower()
    text = re.sub(r"\bstate[- ]of[- ]charge\b", "soc", text)  # otherwise matches the charging rule
    text = re.sub(r"\b(?:noon|midday)\b", "12 pm", text)
    text = re.sub(r"\bmidnight\b", "12 am", text)
    text = re.sub(r"(\d{1,2})(?::\d{2})?\s+in the morning\b", r"\1 am", text)
    return re.sub(r"(\d{1,2})(?::\d{2})?\s+(?:in the afternoon|in the evening|at night)\b", r"\1 pm", text)


def _to_24h(hour: int, meridiem: str | None, is_end: bool) -> int:
    if meridiem:
        hour %= 12
        if meridiem.startswith("p"):
            hour += 12
    if is_end and hour == 0:
        return 24
    return hour


def _windows(text: str) -> list[list[int]]:
    match = _RANGE.search(text)
    if match:
        start, start_mer, end, end_mer = int(match[1]), match[2], int(match[3]), match[4]
        if start_mer is None and end_mer is not None:
            start_mer = end_mer
            if _to_24h(start, start_mer, False) >= _to_24h(end, end_mer, True):
                start_mer = "am" if end_mer.startswith("p") else "pm"
        return [[_to_24h(start, start_mer, False), _to_24h(end, end_mer, True)]]
    match = _OPEN_END.search(text)
    if match:
        return [[_to_24h(int(match[1]), match[2], False), 24]]
    match = _OPEN_START.search(text)
    if match:
        return [[0, _to_24h(int(match[1]), match[2], True)]]
    return [[0, 24]]


def _solar_factor(text: str) -> float:
    percent = None
    match = re.search(_NUMBER + r"\s*(?:%|percent)", text)
    if match:
        percent = float(match[1])
    else:
        for pattern, value in _FRACTION_WORDS:
            if re.search(r"\b" + pattern + r"\b", text):
                percent = value
                break
    if percent is None:
        return 0.0  # unknown remaining share: using no solar can never overdraw the true limit
    lost = re.search(r"\b(?:reduction|reduced by|cut\w*(?: \w+){0,4} by|drop\w* by|decrease\w* by|lower\w* by|down by|los[et]\w*|loss)\b", text)
    remaining = 100 - percent if lost else percent
    return round(min(max(remaining, 0.0), 100.0) / 100, 4)


def _directive(note_index, directive_type, hours, explanation, **value) -> DirectiveInterpretation:
    return DirectiveInterpretation(
        note_index=note_index,
        applies=True,
        directive_type=directive_type,
        structured_adjustment=StructuredAdjustment(hours=hours, **value),
        explanation=f"{explanation} (rule-based fallback)",
    )


def rule_based_directive(note_index: int, note: str, capacity_kwh: float) -> DirectiveInterpretation:
    text = _normalise(note)
    hours = hours_from_windows(_windows(text))
    negated = re.search(_NEGATION, text) is not None

    if re.search(r"discharg", text) and negated:
        return _directive(note_index, "no_discharge_window", hours, "Battery discharging is blocked in this window.")
    if re.search(r"(?<!dis)charg", text) and negated:
        return _directive(note_index, "no_charge_window", hours, "Battery charging is blocked in this window.")
    if re.search(_SOLAR, text) and re.search(_SOLAR_LOSS, text):
        return _directive(note_index, "solar_reduction", hours, "Usable solar is reduced in this window.",
                          factor=_solar_factor(text))

    kwh = re.search(_NUMBER + r"\s*kwh?\b", text)
    megawatts = re.search(_NUMBER + r"\s*mwh?\b", text)
    if re.search(_GRID, text) and (kwh or megawatts):
        limit = float(kwh[1]) if kwh else float(megawatts[1]) * 1000
        return _directive(note_index, "max_grid_window", hours, "Grid import is capped in this window.",
                          max_grid_kwh=limit)

    if re.search(r"batter|storage", text) and re.search(_RESERVE, text):
        percent = re.search(_NUMBER + r"\s*(?:%|percent)", text)
        if kwh:
            minimum = reserve_kwh(float(kwh[1]), "kwh", capacity_kwh)
        elif percent:
            minimum = reserve_kwh(float(percent[1]), "percent", capacity_kwh)
        else:
            return no_op(note_index, "Reserve level could not be read; no constraint applied (rule-based fallback).")
        return _directive(note_index, "minimum_battery_reserve", hours, "Battery reserve is raised in this window.",
                          minimum_energy_kwh=minimum)

    return no_op(note_index)

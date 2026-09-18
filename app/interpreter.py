"""LLM interpretation of operator notes (Groq-hosted model), with per-note safe recovery."""

import json
import logging

from groq import NOT_GIVEN, Groq
from pydantic import ValidationError

from app.config import get_config, get_runtime_secret
from app.directives import DirectiveValidationError, build_directive, no_op, validate_directive
from app.fallback import rule_based_directive
from app.models import DirectiveInterpretation

logger = logging.getLogger("gridwise.interpreter")

SYSTEM_PROMPT = """You extract machine-readable directives from campus energy operator notes.
The schedule covers ONE day split into 24 hourly intervals: hour h runs from h:00 to h+1:00, h = 0..23.

For EACH note choose exactly one directive_type:
- "solar_reduction": usable solar/PV output is reduced or lost (cleaning, washing, dust, clouds, haze, shading, inverter work, panel inspection, panels offline, ...).
- "minimum_battery_reserve": the battery/storage must keep AT LEAST some energy stored (reserve, backup, state of charge floor, "keep at least X in the battery").
- "no_charge_window": the battery must not charge (charger isolated/unavailable/disabled, charging circuit down, no top-ups).
- "no_discharge_window": the battery must not discharge (must not supply energy, no drawing from it, discharge blocked).
- "max_grid_window": energy imported/drawn from the grid must stay at or below a limit (grid import/intake/draw, feeder, substation, transformer or utility limit).
- "no_op": the note does not impose any of the above on today's schedule (unrelated announcements, menus, bookings, deadlines, events next week or month, billing, staffing, general information).

Return JSON only, in exactly this form:
{"directives": [{"note_index": 0, "directive_type": "...", "windows": [[start, end]], "percent": null, "percent_meaning": null, "amount": null, "amount_unit": null, "max_grid_kwh": null, "explanation": "one short sentence"}]}

Time windows ("windows"):
- A list of [start, end] pairs in 24-hour clock. start is INCLUDED, end is EXCLUDED.
- "1 PM to 3 PM", "13:00-15:00", "between 1 PM and 3 PM", "from 1 PM until 3 PM", "1 PM through 3 PM" -> [13, 15].
- noon = 12. Midnight at the END of a window = 24; at the START = 0. "from 10 PM until midnight" -> [22, 24].
- One named hour ("at 5 PM", "during the 5 PM hour") -> [17, 18].
- Open-ended: "from 6 PM onward"/"for the rest of the day" -> [18, 24]; "until 7 AM" with no start -> [0, 7].
- No time mentioned at all -> [[0, 24]] (the whole day).
- For no_op use [].

Values - copy numbers from the note; never invent a value that the note does not state:
- solar_reduction: "percent" = the percentage number written in the note (0-100) and "percent_meaning":
    "remaining" if it is how much solar is still usable ("drops to 20%", "treated as 25% of the forecast", "only 30% available", "leave about half" -> 50),
    "reduction" if it is how much is lost ("cut by 70%", "an 80% reduction", "loses three quarters" -> 75).
  Convert words to numbers: half = 50, a third = 33.33, a quarter = 25, three quarters = 75, a fifth = 20. Panels fully offline / no solar -> percent 0, "remaining".
- minimum_battery_reserve: "amount" and "amount_unit" = "kwh" for an energy amount, or "percent" when it is a share of battery capacity or state of charge.
- max_grid_window: "max_grid_kwh" = the per-hour limit in kWh (a kW limit held for one hour equals the same number of kWh; MW/MWh x 1000).
- no_charge_window, no_discharge_window, no_op: leave all value fields null.

Return exactly one object per note, with note_index 0..N-1 in the same order as the notes."""

CACHE_SIZE = 128
_client: Groq | None = None
_cache: list = []  # [[notes, capacity_kwh], directives] entries, newest last


class LLMUnavailableError(RuntimeError):
    pass


def _groq_client() -> Groq:
    global _client
    if _client is None:
        api_key = get_runtime_secret("GROQ_API_KEY")
        if not api_key:
            raise LLMUnavailableError("GROQ_API_KEY is not configured")
        _client = Groq(api_key=api_key, timeout=get_config("LLM_TIMEOUT_SECONDS"), max_retries=1)
    return _client


def _ask_llm(notes: list[str], capacity_kwh: float) -> list:
    user_content = (
        f"Battery capacity: {capacity_kwh} kWh\n"
        f"Operator notes (note_index: text):\n"
        + "\n".join(f"{index}: {note}" for index, note in enumerate(notes))
    )
    model = get_config("GROQ_MODEL")
    response = _groq_client().chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        response_format={"type": "json_object"},
        temperature=0,
        reasoning_effort="low" if "gpt-oss" in model else NOT_GIVEN,
    )
    payload = json.loads(response.choices[0].message.content or "")
    entries = payload.get("directives") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise ValueError("LLM response has no directives list")
    return entries


def _entry_for(entries: list, note_index: int, note_count: int):
    for entry in entries:
        if isinstance(entry, dict) and entry.get("note_index") == note_index:
            return entry
    if len(entries) == note_count and isinstance(entries[note_index], dict):
        return entries[note_index]
    return None


def _fallback(note_index: int, note: str, capacity_kwh: float) -> DirectiveInterpretation:
    try:
        directive = rule_based_directive(note_index, note, capacity_kwh)
        validate_directive(directive, capacity_kwh)
        return directive
    except (DirectiveValidationError, ValidationError):
        return no_op(note_index, "Note could not be interpreted safely; no constraint applied.")


def _cached(key: list):
    for cached_key, directives in _cache:
        if cached_key == key:
            return directives
    return None


def _remember(key: list, directives: list[DirectiveInterpretation]) -> None:
    _cache.append([key, directives])
    if len(_cache) > CACHE_SIZE:
        _cache.pop(0)


def interpret_notes(notes: list[str], capacity_kwh: float) -> tuple[list[DirectiveInterpretation], str]:
    """Return one validated directive per note and the interpretation source (llm, llm-cache, llm+fallback, fallback)."""
    key = [list(notes), capacity_kwh]
    cached = _cached(key)
    if cached is not None:
        return cached, "llm-cache"

    try:
        entries = _ask_llm(notes, capacity_kwh)
    except Exception as exc:
        logger.warning("LLM interpretation unavailable (%s); using rule-based fallback", type(exc).__name__)
        return [_fallback(index, note, capacity_kwh) for index, note in enumerate(notes)], "fallback"

    directives = []
    recovered = 0
    for index, note in enumerate(notes):
        entry = _entry_for(entries, index, len(notes))
        try:
            if entry is None:
                raise DirectiveValidationError("LLM returned no entry for this note")
            directives.append(build_directive(index, entry, capacity_kwh))
        except (DirectiveValidationError, ValidationError) as exc:
            logger.warning("LLM entry for note %d rejected by guardrails: %s", index, exc)
            directives.append(_fallback(index, note, capacity_kwh))
            recovered += 1

    if recovered:
        return directives, "llm+fallback"
    _remember(key, directives)
    return directives, "llm"

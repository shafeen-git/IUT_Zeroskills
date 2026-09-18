import json
import os
import re
from typing import Any, Dict, List

from groq import Groq

from app.models import DirectiveInterpretation

# ----------------- GROQ CLIENT SETUP -----------------

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "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")
groq_client = Groq(api_key=GROQ_API_KEY)

# ----------------- INTERPRETER & GUARDRAILS -----------------

SYSTEM_PROMPT = """You are an expert energy scheduling assistant for BUP Smart Campus.
Your job is to interpret natural-language operator notes into a structured JSON array of directives for a 24-hour horizon (hours 0 to 23).

### SUPPORTED DIRECTIVE TYPES:
1. "solar_reduction": Usable solar drops.
   Required adjustment: {"hours": [int, ...], "factor": float}
   * factor is the REMAINING usable fraction (0.0 to 1.0). E.g., an 80% reduction means factor = 0.2. "Drops to 25%" means factor = 0.25.
2. "minimum_battery_reserve": Battery energy must stay at or above a threshold.
   Required adjustment: {"hours": [int, ...], "minimum_energy_kwh": float}
   * If given as a percentage, multiply by the battery capacity provided in the context. E.g., 50% of 200 kWh = 100.0.
3. "no_charge_window": Battery cannot charge.
   Required adjustment: {"hours": [int, ...]}
4. "no_discharge_window": Battery cannot discharge.
   Required adjustment: {"hours": [int, ...]}
5. "max_grid_window": Grid import must stay at or below a limit.
   Required adjustment: {"hours": [int, ...], "max_grid_kwh": float}
6. "no_op": The note is an unrelated distractor (e.g., cafeteria, events, general campus announcements).
   Required adjustment: null

### RULES:
- Return exactly one entry per note, strictly matching the note_index (0, 1, ... N-1).
- Time windows are start-inclusive and end-exclusive (e.g., 1 PM to 3 PM -> [13, 14]; noon to 2 PM -> [12, 13]).
- hours array must contain unique integers from 0 to 23 in ascending order.
- applies must be true for all directives EXCEPT "no_op", where applies must be false.
- structured_adjustment must be null for "no_op".
- Output MUST be valid JSON with top-level key "directives": an array of objects with keys: note_index, applies, directive_type, structured_adjustment, explanation.
"""


def parse_time_window(text: str) -> List[int]:
    t = text.lower()
    m = re.search(r'(\d{1,2})(?::00)?\s*(am|pm)?\s*(?:to|until|-)\s*(\d{1,2})(?::00)?\s*(am|pm)?', t)
    start, end = None, None
    if "noon" in t and ("until 2 pm" in t or "to 2 pm" in t):
        start, end = 12, 14
    elif "noon" in t and ("until 1 pm" in t or "to 1 pm" in t):
        start, end = 12, 13
    elif m:
        s_val = int(m.group(1))
        s_mer = m.group(2)
        e_val = int(m.group(3))
        e_mer = m.group(4)

        if not s_mer and e_mer:
            s_mer = e_mer
        if s_mer == "pm" and s_val != 12:
            s_val += 12
        elif s_mer == "am" and s_val == 12:
            s_val = 0
        if e_mer == "pm" and e_val != 12:
            e_val += 12
        elif e_mer == "am" and e_val == 12:
            e_val = 0
        start, end = s_val, e_val

    if start is not None and end is not None and start < end:
        return list(range(start, end))
    return []


def rule_based_fallback(note: str, idx: int, battery_cap: float) -> DirectiveInterpretation:
    t = note.lower()
    hours = parse_time_window(t)

    # Solar reduction
    if any(k in t for k in ["solar", "pv", "panel", "sun"]):
        if any(k in t for k in ["reduc", "drop", "wash", "clean", "cloud", "inverter", "inspection"]):
            pct_match = re.search(r'(\d+)\s*%', t)
            factor = 0.5
            if pct_match:
                val = float(pct_match.group(1)) / 100.0
                if "drop to" in t or "leave" in t:
                    factor = val
                elif "reduction" in t or "cut" in t:
                    factor = round(1.0 - val, 2)
            elif "half" in t or "one-half" in t:
                factor = 0.5
            elif "one-fifth" in t:
                factor = 0.2
            return DirectiveInterpretation(
                note_index=idx,
                applies=True,
                directive_type="solar_reduction",
                structured_adjustment={"hours": sorted(list(set(hours))), "factor": factor},
                explanation="Solar availability adjusted based on note.",
            )

    # No charge
    if ("charge" in t or "charging" in t) and any(k in t for k in ["do not", "not charge", "disabled", "unavailable", "isolated", "outage"]):
        return DirectiveInterpretation(
            note_index=idx,
            applies=True,
            directive_type="no_charge_window",
            structured_adjustment={"hours": sorted(list(set(hours)))},
            explanation="Battery charging disabled during window.",
        )

    # No discharge
    if ("discharge" in t or "discharging" in t) and any(k in t for k in ["do not", "not discharge", "disabled", "unavailable", "must not"]):
        return DirectiveInterpretation(
            note_index=idx,
            applies=True,
            directive_type="no_discharge_window",
            structured_adjustment={"hours": sorted(list(set(hours)))},
            explanation="Battery discharge disabled during window.",
        )

    # Minimum battery reserve
    if any(k in t for k in ["reserve", "remain in the battery", "stored in the battery", "keep at least"]):
        kwh_match = re.search(r'(\d+)\s*kwh', t)
        pct_match = re.search(r'(\d+)\s*%', t)
        min_kwh = 0.0
        if kwh_match:
            min_kwh = float(kwh_match.group(1))
        elif pct_match:
            min_kwh = (float(pct_match.group(1)) / 100.0) * battery_cap
        return DirectiveInterpretation(
            note_index=idx,
            applies=True,
            directive_type="minimum_battery_reserve",
            structured_adjustment={"hours": sorted(list(set(hours))), "minimum_energy_kwh": min_kwh},
            explanation="Battery reserve set.",
        )

    # Max grid window
    if any(k in t for k in ["grid import", "grid intake", "grid limit", "transformer limit", "feeder"]):
        kwh_match = re.search(r'(\d+)\s*kwh', t)
        max_kwh = 150.0
        if kwh_match:
            max_kwh = float(kwh_match.group(1))
        return DirectiveInterpretation(
            note_index=idx,
            applies=True,
            directive_type="max_grid_window",
            structured_adjustment={"hours": sorted(list(set(hours))), "max_grid_kwh": max_kwh},
            explanation="Grid import capped during window.",
        )

    # Distractor / No-op
    return DirectiveInterpretation(
        note_index=idx,
        applies=False,
        directive_type="no_op",
        structured_adjustment=None,
        explanation="This note does not affect today's energy schedule.",
    )


def call_llm_interpreter(notes: List[str], battery_cap: float) -> List[DirectiveInterpretation]:
    if not notes:
        return []

    try:
        user_content = f"Campus Battery Capacity: {battery_cap} kWh\nOperator Notes:\n{json.dumps(notes, indent=2)}"
        response = groq_client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        content = response.choices[0].message.content
        if not content:
            raise ValueError("Empty LLM response")
        data = json.loads(content)
        raw_list = data.get("directives", data.get("directive_interpretation", []))
        if isinstance(raw_list, list) and len(raw_list) == len(notes):
            return [DirectiveInterpretation(**item) for item in raw_list]
    except Exception:
        pass

    # Safety fallback to regex rules if API fails or network drops
    return [rule_based_fallback(note, i, battery_cap) for i, note in enumerate(notes)]

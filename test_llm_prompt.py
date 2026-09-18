import json
import os

# --- LLM SYSTEM PROMPT ---
SYSTEM_PROMPT = """You are an expert energy scheduling assistant for BUP Smart Campus.
Your job is to interpret natural-language operator notes into a structured JSON array of directives for a 24-hour horizon (hours 0 to 23).

### SUPPORTED DIRECTIVE TYPES:
1. "solar_reduction": Usable solar drops.
   Required adjustment: {"hours": [int, ...], "factor": float}
   * factor is the REMAINING usable fraction (0.0 to 1.0). E.g., an 80% reduction means factor = 0.2. "Drops to 25%" means factor = 0.25.
2. "minimum_battery_reserve": Battery energy must stay at or above a threshold.
   Required adjustment: {"hours": [int, ...], "minimum_energy_kwh": float}
   * If given as a percentage, multiply by the battery capacity provided in the context. E.g., 50% of 200 kWh = 100.
3. "no_charge_window": Battery cannot charge.
   Required adjustment: {"hours": [int, ...]}
4. "no_discharge_window": Battery cannot discharge.
   Required adjustment: {"hours": [int, ...]}
5. "max_grid_window": Grid import must stay at or below a limit.
   Required adjustment: {"hours": [int, ...], "max_grid_kwh": float}
6. "no_op": The note is an unrelated distractor (e.g. cafeteria, events, general campus announcements).
   Required adjustment: null

### RULES:
- Return exactly one entry per note, strictly matching the note_index (0, 1, ... N-1).
- Time windows are start-inclusive and end-exclusive (e.g., 1 PM to 3 PM -> [13, 14]; noon to 2 PM -> [12, 13]).
- hours array must contain unique integers from 0 to 23 in ascending order.
- applies must be true for all directives EXCEPT "no_op", where applies must be false.
- structured_adjustment must be null for "no_op".
- Output MUST be a valid JSON array of objects with keys: note_index, applies, directive_type, structured_adjustment, explanation.
"""

def generate_user_payload(notes, capacity_kwh):
    return f"""Campus Battery Capacity: {capacity_kwh} kWh
Operator Notes:
{json.dumps(notes, indent=2)}

Produce the JSON directive_interpretation array:"""

# Test cases from official sample pack
sample_cases = [
    {
        "name": "SAMPLE-01: Solar cleaning + distractor",
        "capacity_kwh": 220,
        "notes": [
            "Facilities will wash the rooftop solar panels from noon until 2 PM. During cleaning, usable solar should be treated as roughly 25% of the forecast.",
            "The sports office moved next month's registration deadline."
        ]
    },
    {
        "name": "SAMPLE-03: Relative percentage reserve",
        "capacity_kwh": 200,
        "notes": [
            "Keep at least 50% of the battery capacity stored in the battery from 6 PM until 9 PM for emergency operations."
        ]
    }
]

if __name__ == "__main__":
    print("=== SYSTEM PROMPT READY FOR EVALUATION ===")
    for case in sample_cases:
        print(f"\n--- {case['name']} ---")
        print(generate_user_payload(case["notes"], case["capacity_kwh"]))
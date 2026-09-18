import os
import json
import re
from typing import List, Optional, Literal, Dict, Any
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import pulp

app = FastAPI(title="GridWise Energy Optimizer")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------- SCHEMAS -----------------

class HourInput(BaseModel):
    hour: int
    demand_kwh: float
    solar_kwh: float
    tariff_bdt_per_kwh: float

class BatteryInput(BaseModel):
    capacity_kwh: float
    initial_energy_kwh: float
    minimum_energy_kwh: float
    max_charge_kwh_per_hour: float
    max_discharge_kwh_per_hour: float

class OptimizeRequest(BaseModel):
    scenario_id: str
    operator_notes: List[str]
    hours: List[HourInput]
    battery: BatteryInput

class DirectiveInterpretation(BaseModel):
    note_index: int
    applies: bool
    directive_type: Literal[
        "solar_reduction",
        "minimum_battery_reserve",
        "no_charge_window",
        "no_discharge_window",
        "max_grid_window",
        "no_op"
    ]
    structured_adjustment: Optional[Dict[str, Any]] = None
    explanation: str

class HourlyPlanEntry(BaseModel):
    hour: int
    grid_kwh: float
    solar_used_kwh: float
    battery_action: Literal["charge", "discharge", "idle"]
    battery_kwh: float
    battery_energy_after_kwh: float

class OptimizeResponse(BaseModel):
    scenario_id: str
    directive_interpretation: List[DirectiveInterpretation]
    hourly_plan: List[HourlyPlanEntry]
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float
    plan_summary: str


# ----------------- HEALTH ENDPOINT -----------------

@app.get("/health")
def health():
    return {"status": "ok"}


# ----------------- INTERPRETER & GUARDRAILS -----------------

SYSTEM_PROMPT = """You are an expert energy scheduling assistant. Convert operator notes into structured directives for a 24-hour horizon (hours 0..23).
Rules:
1. Supported directives:
   - "solar_reduction": {"hours": [int], "factor": float (0..1 remaining usable fraction)}
   - "minimum_battery_reserve": {"hours": [int], "minimum_energy_kwh": float}
   - "no_charge_window": {"hours": [int]}
   - "no_discharge_window": {"hours": [int]}
   - "max_grid_window": {"hours": [int], "max_grid_kwh": float}
   - "no_op": structured_adjustment is null, applies is false.
2. Hours are start-inclusive, end-exclusive. E.g., 1 PM to 3 PM -> [13, 14]. Whole hours only. Sorted ascending unique integers.
3. For irrelevant notes, directive_type="no_op", applies=false, structured_adjustment=null.
4. Output MUST be valid JSON: an array of objects matching DirectiveInterpretation for each note in note_index order (0..N-1).
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
                elif "reduction" in t:
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
                explanation="Solar availability adjusted based on note."
            )

    # No charge
    if ("charge" in t or "charging" in t) and any(k in t for k in ["do not", "not charge", "disabled", "unavailable", "isolated", "outage"]):
        return DirectiveInterpretation(
            note_index=idx,
            applies=True,
            directive_type="no_charge_window",
            structured_adjustment={"hours": sorted(list(set(hours)))},
            explanation="Battery charging disabled during window."
        )

    # No discharge
    if ("discharge" in t or "discharging" in t) and any(k in t for k in ["do not", "not discharge", "disabled", "unavailable", "must not"]):
        return DirectiveInterpretation(
            note_index=idx,
            applies=True,
            directive_type="no_discharge_window",
            structured_adjustment={"hours": sorted(list(set(hours)))},
            explanation="Battery discharge disabled during window."
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
            explanation="Battery reserve set."
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
            explanation="Grid import capped during window."
        )

    # Distractor / No-op
    return DirectiveInterpretation(
        note_index=idx,
        applies=False,
        directive_type="no_op",
        structured_adjustment=None,
        explanation="This note does not affect today's energy schedule."
    )

def call_llm_interpreter(notes: List[str], battery_cap: float) -> List[DirectiveInterpretation]:
    api_key = os.getenv("OPENAI_API_KEY")
    if api_key:
        import requests
        try:
            prompt = f"Capacity: {battery_cap} kWh\nNotes:\n" + "\n".join([f"[{i}]: {n}" for i, n in enumerate(notes)])
            resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": "gpt-4o-mini",
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt}
                    ],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.0
                },
                timeout=5
            )
            if resp.status_code == 200:
                data = resp.json()
                content = json.loads(data["choices"][0]["message"]["content"])
                items = content if isinstance(content, list) else content.get("directives", content.get("directive_interpretation", []))
                results = [DirectiveInterpretation(**item) for item in items]
                if len(results) == len(notes):
                    return results
        except Exception:
            pass

    return [rule_based_fallback(n, i, battery_cap) for i, n in enumerate(notes)]

def validate_guardrails(directives: List[DirectiveInterpretation], notes_count: int, battery_cap: float) -> List[DirectiveInterpretation]:
    validated = []
    seen_indices = set()
    for idx, d in enumerate(directives):
        if d.note_index in seen_indices or d.note_index < 0 or d.note_index >= notes_count:
            d.note_index = idx
        seen_indices.add(d.note_index)

        if d.directive_type == "no_op":
            d.applies = False
            d.structured_adjustment = None
        else:
            d.applies = True
            adj = d.structured_adjustment or {}
            hours = sorted(list(set(adj.get("hours", []))))
            hours = [h for h in hours if 0 <= h <= 23]
            adj["hours"] = hours

            if d.directive_type == "solar_reduction":
                adj["factor"] = max(0.0, min(1.0, float(adj.get("factor", 1.0))))
            elif d.directive_type == "minimum_battery_reserve":
                adj["minimum_energy_kwh"] = max(0.0, min(battery_cap, float(adj.get("minimum_energy_kwh", 0.0))))
            elif d.directive_type == "max_grid_window":
                adj["max_grid_kwh"] = max(0.0, float(adj.get("max_grid_kwh", 0.0)))
            d.structured_adjustment = adj
        validated.append(d)
    validated.sort(key=lambda x: x.note_index)
    return validated


# ----------------- OPTIMIZER (PuLP) -----------------

def solve_schedule(req: OptimizeRequest, directives: List[DirectiveInterpretation]) -> Dict[str, Any]:
    hours_data = req.hours
    bat = req.battery

    effective_solar = [h.solar_kwh for h in hours_data]
    for d in directives:
        if d.applies and d.directive_type == "solar_reduction" and d.structured_adjustment:
            factor = d.structured_adjustment["factor"]
            for hr in d.structured_adjustment["hours"]:
                effective_solar[hr] = round(effective_solar[hr] * factor, 4)

    min_reserve = [bat.minimum_energy_kwh] * 24
    allow_charge = [True] * 24
    allow_discharge = [True] * 24
    grid_cap = [None] * 24

    for d in directives:
        if not d.applies or not d.structured_adjustment:
            continue
        hrs = d.structured_adjustment.get("hours", [])
        if d.directive_type == "minimum_battery_reserve":
            for hr in hrs:
                min_reserve[hr] = max(min_reserve[hr], d.structured_adjustment["minimum_energy_kwh"])
        elif d.directive_type == "no_charge_window":
            for hr in hrs:
                allow_charge[hr] = False
        elif d.directive_type == "no_discharge_window":
            for hr in hrs:
                allow_discharge[hr] = False
        elif d.directive_type == "max_grid_window":
            for hr in hrs:
                val = d.structured_adjustment["max_grid_kwh"]
                grid_cap[hr] = val if grid_cap[hr] is None else min(grid_cap[hr], val)

    prob = pulp.LpProblem("GridWise_Optimization", pulp.LpMinimize)

    grid = [pulp.LpVariable(f"grid_{t}", lowBound=0) for t in range(24)]
    solar_used = [pulp.LpVariable(f"solar_used_{t}", lowBound=0, upBound=effective_solar[t]) for t in range(24)]
    b_charge = [pulp.LpVariable(f"b_charge_{t}", lowBound=0, upBound=bat.max_charge_kwh_per_hour if allow_charge[t] else 0) for t in range(24)]
    b_discharge = [pulp.LpVariable(f"b_discharge_{t}", lowBound=0, upBound=bat.max_discharge_kwh_per_hour if allow_discharge[t] else 0) for t in range(24)]
    e_after = [pulp.LpVariable(f"e_after_{t}", lowBound=min_reserve[t], upBound=bat.capacity_kwh) for t in range(24)]

    prob += pulp.lpSum([grid[t] * hours_data[t].tariff_bdt_per_kwh for t in range(24)])

    for t in range(24):
        prob += grid[t] + solar_used[t] + b_discharge[t] == hours_data[t].demand_kwh + b_charge[t]
        e_prev = bat.initial_energy_kwh if t == 0 else e_after[t - 1]
        prob += e_after[t] == e_prev + b_charge[t] - b_discharge[t]
        if grid_cap[t] is not None:
            prob += grid[t] <= grid_cap[t]

    prob += e_after[23] == bat.initial_energy_kwh

    prob.solve()

    if prob.status != pulp.constants.LpStatusOptimal:
        raise HTTPException(status_code=500, detail="Optimization problem infeasible under given constraints")

    plan = []
    total_grid = 0.0
    total_cost = 0.0
    peak_grid = 0.0

    for t in range(24):
        g_val = round(float(pulp.value(grid[t])), 2)
        s_val = round(float(pulp.value(solar_used[t])), 2)
        c_val = round(float(pulp.value(b_charge[t])), 2)
        d_val = round(float(pulp.value(b_discharge[t])), 2)
        e_val = round(float(pulp.value(e_after[t])), 2)

        action = "idle"
        kwh = 0.0
        if c_val > 0.01:
            action = "charge"
            kwh = c_val
        elif d_val > 0.01:
            action = "discharge"
            kwh = d_val

        plan.append(HourlyPlanEntry(
            hour=t,
            grid_kwh=g_val,
            solar_used_kwh=s_val,
            battery_action=action,
            battery_kwh=kwh,
            battery_energy_after_kwh=e_val
        ))

        total_grid += g_val
        total_cost += g_val * hours_data[t].tariff_bdt_per_kwh
        if g_val > peak_grid:
            peak_grid = g_val

    return {
        "hourly_plan": plan,
        "total_grid_kwh": round(total_grid, 2),
        "total_cost_bdt": round(total_cost, 2),
        "peak_grid_kwh": round(peak_grid, 2),
        "plan_summary": "Operates under interpreted operator directives and minimizes total grid import cost."
    }


# ----------------- MAIN ENDPOINT -----------------

@app.post("/optimize-energy", response_model=OptimizeResponse)
def optimize_energy(req: OptimizeRequest):
    if len(req.hours) != 24:
        raise HTTPException(status_code=400, detail="Hours array must contain exactly 24 entries.")

    raw_directives = call_llm_interpreter(req.operator_notes, req.battery.capacity_kwh)
    directives = validate_guardrails(raw_directives, len(req.operator_notes), req.battery.capacity_kwh)
    result = solve_schedule(req, directives)

    return OptimizeResponse(
        scenario_id=req.scenario_id,
        directive_interpretation=directives,
        hourly_plan=result["hourly_plan"],
        total_grid_kwh=result["total_grid_kwh"],
        total_cost_bdt=result["total_cost_bdt"],
        peak_grid_kwh=result["peak_grid_kwh"],
        plan_summary=result["plan_summary"]
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
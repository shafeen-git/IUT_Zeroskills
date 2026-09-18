"""GridWise end-to-end simulation harness.

Checks the running API the way the judge does: interpretation vs ground truth, an independent
hour-by-hour replay of the plan against the TRUE directives, recomputed totals, cost vs the public
reference optimum, HTTP error handling, LLM-failure recovery, and latency (p95).

    python run_simulations.py                               # in-process app (LLM live if GROQ_API_KEY is set)
    python run_simulations.py --url http://localhost:8000   # any running deployment
    python run_simulations.py --samples path/to/BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json
"""

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path
from unittest import mock

TOL = 0.01
SAMPLE_FILE = "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"
VALUE_KEYS = ["factor", "minimum_energy_kwh", "max_grid_kwh"]

# ---------------------------------------------------------------- paraphrase stress scenarios
BASE_DEMAND = [95, 90, 85, 85, 90, 100, 115, 135, 155, 170, 180, 185, 190, 185, 175, 170, 175, 190, 205, 212, 205, 180, 140, 110]
BASE_SOLAR = [0, 0, 0, 0, 0, 0, 5, 25, 55, 95, 135, 165, 185, 175, 145, 95, 50, 12, 0, 0, 0, 0, 0, 0]
BASE_TARIFF = [6, 6, 5, 5, 5, 6, 8, 10, 12, 14, 16, 16, 15, 14, 13, 14, 18, 22, 28, 30, 26, 18, 10, 7]
BASE_BATTERY = {"capacity_kwh": 250, "initial_energy_kwh": 125, "minimum_energy_kwh": 40,
                "max_charge_kwh_per_hour": 60, "max_discharge_kwh_per_hour": 60}


def truth(directive_type, adjustment=None):
    return {"applies": directive_type != "no_op", "directive_type": directive_type, "structured_adjustment": adjustment}


PARAPHRASE_CASES = [
    ["PARA-1 grid cap as 'substation load'",
     ["Keep the substation load under 160 kWh from 19:00 to 21:00 - utility dispatch request."],
     [truth("max_grid_window", {"hours": [19, 20], "max_grid_kwh": 160})]],
    ["PARA-2 solar loss as a fraction",
     ["Firmware patching on the inverters means the rooftop array will lose roughly three quarters of its expected yield from 10 in the morning till 1 in the afternoon."],
     [truth("solar_reduction", {"hours": [10, 11, 12], "factor": 0.25})]],
    ["PARA-3 reserve as state of charge",
     ["Ops wants the storage bank held at no less than 40 percent state of charge from 5 PM until 8 PM in case the feeder trips."],
     [truth("minimum_battery_reserve", {"hours": [17, 18, 19], "minimum_energy_kwh": 100})]],
    ["PARA-4 no-discharge without the word 'discharge'",
     ["Protection engineers are testing the relays, so nothing may be drawn out of the battery bank between 4 and 6 PM."],
     [truth("no_discharge_window", {"hours": [16, 17]})]],
    ["PARA-5 no-charge at midnight + keyword-trap distractor",
     ["Charger firmware is being flashed, so the battery can't take on any energy from midnight to 4 AM.",
      "The solar panel supplier's invoice is due at the end of next month."],
     [truth("no_charge_window", {"hours": [0, 1, 2, 3]}), truth("no_op")]],
]


def base_request(scenario_id, notes):
    return {
        "scenario_id": scenario_id,
        "operator_notes": notes,
        "hours": [{"hour": h, "demand_kwh": BASE_DEMAND[h], "solar_kwh": BASE_SOLAR[h], "tariff_bdt_per_kwh": BASE_TARIFF[h]}
                  for h in range(24)],
        "battery": dict(BASE_BATTERY),
    }


# ---------------------------------------------------------------- transport
class Api:
    def __init__(self, url):
        self.url = url.rstrip("/") if url else None
        if self.url:
            import requests
            self.session = requests.Session()
        else:
            from fastapi.testclient import TestClient
            from app.main import app
            self.session = TestClient(app)

    def request(self, method, path, **kwargs):
        target = f"{self.url}{path}" if self.url else path
        if self.url:
            kwargs.setdefault("timeout", 35)
            if "content" in kwargs:
                kwargs["data"] = kwargs.pop("content")  # requests names the raw body 'data'
        started = time.perf_counter()
        response = self.session.request(method, target, **kwargs)
        return response, time.perf_counter() - started


# ---------------------------------------------------------------- independent judge checks
def check_interpretation(expected, got):
    errors = []
    if not isinstance(got, list) or len(got) != len(expected):
        return [f"expected {len(expected)} interpretation entries, got {len(got) if isinstance(got, list) else got!r}"]
    for index, (want, have) in enumerate(zip(expected, got)):
        label = f"note {index}"
        if have.get("note_index") != index:
            errors.append(f"{label}: note_index {have.get('note_index')!r}")
        for field in ["applies", "directive_type"]:
            if have.get(field) != want[field]:
                errors.append(f"{label}: {field} {have.get(field)!r} != {want[field]!r}")
        if not isinstance(have.get("explanation"), str):
            errors.append(f"{label}: explanation missing")
        want_adj, have_adj = want["structured_adjustment"], have.get("structured_adjustment")
        if want_adj is None or have_adj is None:
            if want_adj != have_adj:
                errors.append(f"{label}: structured_adjustment {have_adj!r} != {want_adj!r}")
            continue
        if sorted(have_adj) != sorted(want_adj):
            errors.append(f"{label}: adjustment shape {sorted(have_adj)} != {sorted(want_adj)}")
        if have_adj.get("hours") != want_adj["hours"]:
            errors.append(f"{label}: hours {have_adj.get('hours')} != {want_adj['hours']}")
        for key in VALUE_KEYS:
            if key in want_adj and not (isinstance(have_adj.get(key), (int, float))
                                        and abs(have_adj[key] - want_adj[key]) <= TOL):
                errors.append(f"{label}: {key} {have_adj.get(key)!r} != {want_adj[key]}")
    return errors


def replay_plan(request_body, true_directives, response):
    """Replay hourly_plan against the TRUE directives and all GridWise rules; returns (errors, recomputed_cost)."""
    errors = []
    hours_in = sorted(request_body["hours"], key=lambda entry: entry["hour"])
    battery = request_body["battery"]
    plan = response.get("hourly_plan")
    if not isinstance(plan, list) or sorted(step.get("hour") for step in plan) != list(range(24)):
        return ["hourly_plan must contain hours 0-23 exactly once"], None
    plan = sorted(plan, key=lambda step: step["hour"])

    solar = [entry["solar_kwh"] for entry in hours_in]
    floor = [battery["minimum_energy_kwh"]] * 24
    no_charge, no_discharge, grid_cap = [False] * 24, [False] * 24, [math.inf] * 24
    for directive in true_directives:
        adjustment = directive["structured_adjustment"]
        for h in (adjustment or {}).get("hours", []):
            kind = directive["directive_type"]
            if kind == "solar_reduction":
                solar[h] *= adjustment["factor"]
            elif kind == "minimum_battery_reserve":
                floor[h] = max(floor[h], adjustment["minimum_energy_kwh"])
            elif kind == "no_charge_window":
                no_charge[h] = True
            elif kind == "no_discharge_window":
                no_discharge[h] = True
            elif kind == "max_grid_window":
                grid_cap[h] = min(grid_cap[h], adjustment["max_grid_kwh"])

    energy = battery["initial_energy_kwh"]
    total_grid = total_cost = peak = 0.0
    for step, entry in zip(plan, hours_in):
        h = step["hour"]
        numbers = [step.get(key) for key in ["grid_kwh", "solar_used_kwh", "battery_kwh", "battery_energy_after_kwh"]]
        if not all(isinstance(n, (int, float)) and math.isfinite(n) and n >= -TOL for n in numbers):
            errors.append(f"h{h}: non-finite or negative value")
            continue
        grid, used, amount, after = numbers
        action = step.get("battery_action")
        if action not in ["charge", "discharge", "idle"]:
            errors.append(f"h{h}: battery_action {action!r}")
        charged = amount if action == "charge" else 0.0
        discharged = amount if action == "discharge" else 0.0
        if action == "idle" and amount > TOL:
            errors.append(f"h{h}: idle but battery_kwh={amount}")
        if abs(grid + used + discharged - entry["demand_kwh"] - charged) > TOL:
            errors.append(f"h{h}: energy balance off by {grid + used + discharged - entry['demand_kwh'] - charged:.4f}")
        if used > solar[h] + TOL:
            errors.append(f"h{h}: solar_used {used} > effective {solar[h]}")
        if charged > battery["max_charge_kwh_per_hour"] + TOL or discharged > battery["max_discharge_kwh_per_hour"] + TOL:
            errors.append(f"h{h}: rate limit")
        if abs(energy + charged - discharged - after) > TOL:
            errors.append(f"h{h}: battery transition {energy} -> {after}")
        if after < floor[h] - TOL or after > battery["capacity_kwh"] + TOL:
            errors.append(f"h{h}: battery {after} outside [{floor[h]}, {battery['capacity_kwh']}]")
        if no_charge[h] and charged > TOL:
            errors.append(f"h{h}: charged during no-charge window")
        if no_discharge[h] and discharged > TOL:
            errors.append(f"h{h}: discharged during no-discharge window")
        if grid > grid_cap[h] + TOL:
            errors.append(f"h{h}: grid {grid} > cap {grid_cap[h]}")
        energy = after
        total_grid += grid
        total_cost += grid * entry["tariff_bdt_per_kwh"]
        peak = max(peak, grid)

    if abs(energy - battery["initial_energy_kwh"]) > TOL:
        errors.append(f"end-of-day energy {energy} != initial {battery['initial_energy_kwh']}")
    for field, recomputed in [["total_grid_kwh", total_grid], ["total_cost_bdt", total_cost], ["peak_grid_kwh", peak]]:
        reported = response.get(field)
        if not isinstance(reported, (int, float)) or abs(reported - recomputed) > TOL:
            errors.append(f"{field} {reported!r} != recomputed {recomputed:.4f}")
    if response.get("scenario_id") != request_body["scenario_id"]:
        errors.append("scenario_id not echoed")
    if not isinstance(response.get("plan_summary"), str):
        errors.append("plan_summary missing")
    return errors, total_cost


# ---------------------------------------------------------------- runners
def run_case(api, name, body, true_directives, reference_cost, results, latencies):
    response, elapsed = api.request("POST", "/optimize-energy", json=body)
    latencies.append(elapsed)
    if response.status_code != 200:
        results.append([name, False, f"HTTP {response.status_code}: {response.text[:160]}"])
        return
    payload = response.json()
    interpretation_errors = check_interpretation(true_directives, payload.get("directive_interpretation"))
    plan_errors, cost = replay_plan(body, true_directives, payload)
    detail = f"{elapsed * 1000:.0f} ms, cost {cost:.2f}" if cost is not None else f"{elapsed * 1000:.0f} ms"
    if reference_cost is not None and cost is not None:
        detail += f" vs ref {reference_cost:.2f} (quality {min(1.0, reference_cost / cost) if cost > TOL else 1.0:.4f})"
        if cost > reference_cost + TOL:
            plan_errors.append(f"cost {cost:.2f} above reference optimum {reference_cost:.2f}")
    errors = [f"[interpretation] {e}" for e in interpretation_errors] + [f"[plan] {e}" for e in plan_errors]
    results.append([name, not errors, detail + ("" if not errors else "\n      " + "\n      ".join(errors[:8]))])


def find_samples(explicit):
    candidates = [explicit] if explicit else [os.getenv("GRIDWISE_SAMPLES"), SAMPLE_FILE, str(Path(__file__).parent / SAMPLE_FILE)]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def run_public_samples(api, path, results, latencies):
    cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
    for case in cases:
        expected = case["expected_output"]
        run_case(api, f"{case['id']} {case['label']}", case["input"], expected["directive_interpretation"],
                 expected["total_cost_bdt"], results, latencies)


def run_paraphrases(api, results, latencies):
    for index, (name, notes, expected) in enumerate(PARAPHRASE_CASES, start=1):
        run_case(api, name, base_request(f"PARA-{index}", notes), expected, None, results, latencies)


def run_http_contract(api, results):
    valid = base_request("HTTP-OK", ["The cafeteria menu changes tomorrow."])
    too_few_hours = base_request("HTTP-23", ["x"])
    too_few_hours["hours"] = too_few_hours["hours"][:23]
    duplicate_hour = base_request("HTTP-DUP", ["x"])
    duplicate_hour["hours"][5]["hour"] = 4
    bad_battery = base_request("HTTP-BAT", ["x"])
    bad_battery["battery"]["initial_energy_kwh"] = 999
    negative_demand = base_request("HTTP-NEG", ["x"])
    negative_demand["hours"][3]["demand_kwh"] = -5
    missing_battery = base_request("HTTP-MISS", ["x"])
    del missing_battery["battery"]
    json_header = {"content-type": "application/json"}

    checks = [
        ["GET /health -> 200 {status: ok}", "GET", "/health", {}, [200], lambda r: r.json() == {"status": "ok"}],
        ["valid distractor-only request -> 200", "POST", "/optimize-energy", {"json": valid}, [200], None],
        ["malformed JSON -> 400", "POST", "/optimize-energy", {"content": b'{"scenario_id": "x", ', "headers": json_header}, [400], None],
        ["JSON array body -> 400", "POST", "/optimize-energy", {"json": [1, 2]}, [400], None],
        ["missing battery -> 400", "POST", "/optimize-energy", {"json": missing_battery}, [400], None],
        ["23 hourly entries -> 400", "POST", "/optimize-energy", {"json": too_few_hours}, [400], None],
        ["duplicate hour -> 400", "POST", "/optimize-energy", {"json": duplicate_hour}, [400], None],
        ["4 operator notes -> 400", "POST", "/optimize-energy", {"json": base_request("HTTP-4", ["a", "b", "c", "d"])}, [400], None],
        ["blank operator note -> 400", "POST", "/optimize-energy", {"json": base_request("HTTP-BLANK", ["   "])}, [400], None],
        ["negative demand -> 400", "POST", "/optimize-energy", {"json": negative_demand}, [400], None],
        ["NaN tariff -> 400", "POST", "/optimize-energy",
         {"content": json.dumps(base_request("HTTP-NAN", ["x"])).replace('"tariff_bdt_per_kwh": 6', '"tariff_bdt_per_kwh": NaN', 1), "headers": json_header}, [400], None],
        ["initial energy above capacity -> 422", "POST", "/optimize-energy", {"json": bad_battery}, [422], None],
    ]
    for name, method, path, kwargs, statuses, extra in checks:
        response, _ = api.request(method, path, **kwargs)
        ok = response.status_code in statuses and (extra is None or extra(response))
        leaked = "Traceback" in response.text or "gsk_" in response.text
        results.append([name, ok and not leaked, f"HTTP {response.status_code}" + (" (leaks internals!)" if leaked else "")])


def run_llm_failure_injection(api, results, latencies):
    """In-process only: replace the model call with broken outputs and confirm the service degrades safely."""
    import app.interpreter as interpreter

    notes = ["Solar output will drop to about 20% from 1 PM to 3 PM.", "Do not charge the battery between 2 PM and 4 PM."]
    reserve_note = ["Hold 9999 kWh in the battery from 6 PM to 9 PM."]
    correct = [truth("solar_reduction", {"hours": [13, 14], "factor": 0.2}), truth("no_charge_window", {"hours": [14, 15]})]
    good_second = {"note_index": 1, "directive_type": "no_charge_window", "windows": [[14, 16]], "explanation": "ok"}
    scenarios = [
        ["provider timeout -> rule-based fallback", TimeoutError("provider timeout"), notes, correct],
        ["unparseable model output -> fallback", json.JSONDecodeError("bad", "{", 0), notes, correct],
        ["unsupported directive type -> no_op",
         [{"note_index": 0, "directive_type": "boost_solar", "windows": [[13, 15]], "applies": True}, good_second],
         notes, [truth("no_op"), correct[1]]],
        ["applies=false on a real directive -> forced true, end-exclusive hours",
         [{"note_index": 0, "directive_type": "solar_reduction", "applies": False, "windows": [[13, 15]],
           "percent": 20, "percent_meaning": "remaining"}, good_second], notes, correct],
        ["'80% reduction' -> factor 0.2 (inverted only for losses)",
         [{"note_index": 0, "directive_type": "solar_reduction", "windows": [[13, 15]],
           "percent": 80, "percent_meaning": "reduction"}, good_second], notes, correct],
        ["out-of-range hours rejected -> per-note fallback",
         [{"note_index": 0, "directive_type": "solar_reduction", "windows": [[25, 27]],
           "percent": 20, "percent_meaning": "remaining"}, good_second], notes, correct],
        ["reserve above capacity rejected -> safe no_op",
         [{"note_index": 0, "directive_type": "minimum_battery_reserve", "windows": [[18, 21]],
           "amount": 9999, "amount_unit": "kwh"}], reserve_note, [truth("no_op")]],
    ]
    for name, behaviour, case_notes, expected in scenarios:
        interpreter._cache.clear()
        stub = mock.Mock(side_effect=behaviour) if isinstance(behaviour, Exception) else mock.Mock(return_value=behaviour)
        with mock.patch.object(interpreter, "_ask_llm", stub):
            run_case(api, f"LLM-FAIL {name}", base_request("LLM-FAIL", case_notes), expected, None, results, latencies)
    interpreter._cache.clear()


def print_section(title, results):
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n== {title}: {passed}/{len(results)} passed")
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name} - {detail}")
    return passed == len(results)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", help="base URL of a running service (default: in-process app)")
    parser.add_argument("--samples", help=f"path to {SAMPLE_FILE}")
    args = parser.parse_args()

    api = Api(args.url)
    in_process = args.url is None
    print(f"Target: {args.url or 'in-process app (app.main:app)'}")
    if in_process and not os.getenv("GROQ_API_KEY"):
        print("WARNING: GROQ_API_KEY is not set - interpretation results below come from the rule-based "
              "fallback, NOT the LLM. Set the key to measure real LLM accuracy and latency.")

    all_ok = True
    latencies = []
    samples = find_samples(args.samples)
    if samples:
        results = []
        run_public_samples(api, samples, results, latencies)
        all_ok &= print_section(f"Public samples ({samples.name})", results)
    else:
        print(f"\n== Public samples: SKIPPED ({SAMPLE_FILE} not found; pass --samples PATH)")

    results = []
    run_paraphrases(api, results, latencies)
    all_ok &= print_section("Paraphrase stress test", results)

    results = []
    run_http_contract(api, results)
    all_ok &= print_section("HTTP contract & malformed input", results)

    if in_process:
        results = []
        run_llm_failure_injection(api, results, latencies)
        all_ok &= print_section("LLM failure injection & guardrails", results)

    if latencies:
        ordered = sorted(latencies)
        p95 = ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]
        print(f"\n== Latency over {len(ordered)} optimize calls: p50 {ordered[len(ordered) // 2]:.2f}s, "
              f"p95 {p95:.2f}s, max {ordered[-1]:.2f}s (rubric: p95 <= 5s for full marks)")
    print("\nRESULT:", "ALL CHECKS PASSED" if all_ok else "FAILURES FOUND")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())

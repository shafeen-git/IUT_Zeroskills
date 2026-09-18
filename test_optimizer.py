"""
Smoke test for app/optimizer.py with synthetic data.
Run from project root:  python test_optimizer.py
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from app.optimizer import optimize_energy


def build_synthetic_request():
    # 24-element load profile (kW): daytime peak, low overnight
    load = [
        30, 28, 26, 25, 25, 28,            # 0-5  (overnight low)
        40, 60, 80, 95, 100, 105,          # 6-11 (morning ramp + peak)
        110, 108, 100, 95, 90, 85,         # 12-17 (afternoon)
        80, 70, 60, 50, 40, 35,            # 18-23 (evening drop)
    ]

    # Solar production: zero at night, peak midday
    solar = [0.0] * 6 + [10, 30, 60, 90, 110, 120, 115, 100, 80, 50, 20, 5] + [0.0] * 6

    # Grid price: cheap overnight, expensive during peak
    price = [
        0.08, 0.07, 0.07, 0.07, 0.08, 0.10,
        0.15, 0.22, 0.28, 0.30, 0.32, 0.30,
        0.28, 0.26, 0.25, 0.27, 0.30, 0.32,
        0.30, 0.25, 0.20, 0.15, 0.12, 0.10,
    ]

    battery = [
        ["capacity_kwh", 100.0],
        ["initial_soc_kwh", 50.0],
        ["max_charge_kw", 50.0],
        ["max_discharge_kw", 50.0],
        ["charge_efficiency", 0.95],
        ["discharge_efficiency", 0.95],
    ]

    # Directives: solar-reduction cap during 10-15, and force reserve at end-of-day
    directives = [
        ["type", "solar_reduction", "start_hour", 10, "end_hour", 15, "max_kw", 40.0],
        ["type", "minimum_battery_reserve", "start_hour", 22, "end_hour", 23, "reserve_kwh", 20.0],
    ]

    return [
        ["load", load],
        ["solar", solar],
        ["grid_price", price],
        ["battery", battery],
        ["directives", directives],
    ]


def assert_balance(schedule, request):
    rd = dict(request)
    load = rd["load"]
    solar = rd["solar"]
    bat = dict(rd["battery"])
    eta_c = bat.get("charge_efficiency", 1.0)
    eta_d = bat.get("discharge_efficiency", 1.0)

    for row in schedule:
        h, ch, dis, gi, soc = row
        balance = gi + solar[h] + dis - ch
        assert abs(balance - load[h]) < 1e-6, (
            f"Hour {h}: energy balance violated ({balance} vs {load[h]})"
        )

    # SoC dynamics
    soc0 = bat["initial_soc_kwh"]
    prev = soc0
    for row in schedule:
        h, ch, dis, gi, soc = row
        expected = prev + ch * eta_c - dis / eta_d
        assert abs(soc - expected) < 1e-6, (
            f"Hour {h}: SoC dynamics violated ({soc} vs {expected})"
        )
        prev = soc

    # End-of-day neutrality
    assert abs(schedule[-1][4] - soc0) < 1e-6, (
        f"End-of-day SoC {schedule[-1][4]} != initial {soc0}"
    )


def main():
    req = build_synthetic_request()
    print("Solving 24-hour LP with synthetic data...")
    status, schedule = optimize_energy(req)

    print(f"LP status: {status}")
    assert status == "Optimal", f"Expected Optimal, got {status}"
    assert len(schedule) == 24, f"Expected 24 rows, got {len(schedule)}"

    assert_balance(schedule, req)

    # Print a compact summary table
    print("\nh  charge  discharge  grid_imp  soc")
    for row in schedule:
        print(f"{int(row[0]):2d}  {row[1]:6.2f}  {row[2]:9.2f}  {row[3]:8.2f}  {row[4]:6.2f}")

    # Compute total cost
    rd = dict(req)
    price = rd["grid_price"]
    total_cost = sum(float(row[3]) * float(price[int(row[0])]) for row in schedule)
    print(f"\nTotal grid cost: {total_cost:.2f}")

    print("\nAll assertions passed. ✓")


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# Validation tests: bad directive parameters must raise OptimizerInputError,
# NOT silently produce wrong schedules.
# ---------------------------------------------------------------------------

from app.optimizer import OptimizerInputError


def assert_raises(req, label):
    try:
        optimize_energy(req)
    except OptimizerInputError as exc:
        print(f"  ✓ {label}: {exc}")
        return
    raise AssertionError(f"{label}: expected OptimizerInputError, got none")


def base_request_with_directives(directives):
    base = build_synthetic_request()
    rd = dict(base)
    # Preserve the list-of-pairs structure, only override the directives entry
    out = []
    for pair in base:
        if pair[0] == "directives":
            out.append(["directives", directives])
        else:
            out.append(pair)
    return out


def test_validation():
    print("\nValidation tests:")

    # 1. start_hour > end_hour
    bad = base_request_with_directives([
        ["type", "minimum_battery_reserve",
         "start_hour", 22, "end_hour", 5, "reserve_kwh", 10.0],
    ])
    assert_raises(bad, "start > end (reserve)")

    # 2. Hour out of range
    bad = base_request_with_directives([
        ["type", "no_charge_window", "start_hour", -1, "end_hour", 5],
    ])
    assert_raises(bad, "start < 0")

    bad = base_request_with_directives([
        ["type", "no_discharge_window", "start_hour", 20, "end_hour", 24],
    ])
    assert_raises(bad, "end > 23")

    # 3. Non-integer hour
    bad = base_request_with_directives([
        ["type", "max_grid_window", "start_hour", "noon", "end_hour", 18],
    ])
    assert_raises(bad, "non-integer hour")

    # 4. Negative numeric param
    bad = base_request_with_directives([
        ["type", "max_grid_window",
         "start_hour", 10, "end_hour", 15, "max_kw", -5.0],
    ])
    assert_raises(bad, "negative max_kw")

    bad = base_request_with_directives([
        ["type", "minimum_battery_reserve",
         "start_hour", 0, "end_hour", 23, "reserve_kwh", -1.0],
    ])
    assert_raises(bad, "negative reserve_kwh")

    bad = base_request_with_directives([
        ["type", "solar_reduction",
         "start_hour", 10, "end_hour", 15, "max_kw", -1.0],
    ])
    assert_raises(bad, "negative solar cap")

    # 5. Non-numeric cap
    bad = base_request_with_directives([
        ["type", "solar_reduction",
         "start_hour", 10, "end_hour", 15, "max_kw", "high"],
    ])
    assert_raises(bad, "non-numeric solar cap")

    # 6. Valid edge case: window of 1 hour at boundary
    ok = base_request_with_directives([
        ["type", "no_charge_window", "start_hour", 23, "end_hour", 23],
    ])
    status, _ = optimize_energy(ok)
    assert status == "Optimal"
    print("  ✓ single-hour window at h=23: OK")

    print("Validation: all assertions passed. ✓")


if __name__ == "__main__":
    main()
    test_validation()

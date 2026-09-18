"""Final replay: re-check the finished plan hour by hour against the rules and every applied directive."""

from app.models import DirectiveInterpretation, HourlyPlan, OptimizeRequest
from app.optimizer import effective_solar

TOLERANCE = 0.01


def find_violations(
    request: OptimizeRequest, directives: list[DirectiveInterpretation], plan: list[HourlyPlan]
) -> list[str]:
    battery = request.battery
    solar = effective_solar(request, directives)
    violations = []
    energy_before = battery.initial_energy_kwh

    for step, demand in zip(plan, request.hours):
        h = step.hour
        charged = step.battery_kwh if step.battery_action == "charge" else 0.0
        discharged = step.battery_kwh if step.battery_action == "discharge" else 0.0
        if min(step.grid_kwh, step.solar_used_kwh, step.battery_kwh) < 0:
            violations.append(f"hour {h}: negative energy value")
        if step.battery_action == "idle" and step.battery_kwh > TOLERANCE:
            violations.append(f"hour {h}: idle with non-zero battery_kwh")
        if abs(step.grid_kwh + step.solar_used_kwh + discharged - demand.demand_kwh - charged) > TOLERANCE:
            violations.append(f"hour {h}: energy balance")
        if step.solar_used_kwh > solar[h] + TOLERANCE:
            violations.append(f"hour {h}: solar above effective solar")
        if charged > battery.max_charge_kwh_per_hour + TOLERANCE or discharged > battery.max_discharge_kwh_per_hour + TOLERANCE:
            violations.append(f"hour {h}: battery rate limit")
        if abs(energy_before + charged - discharged - step.battery_energy_after_kwh) > TOLERANCE:
            violations.append(f"hour {h}: battery transition")
        if not battery.minimum_energy_kwh - TOLERANCE <= step.battery_energy_after_kwh <= battery.capacity_kwh + TOLERANCE:
            violations.append(f"hour {h}: battery bounds")
        energy_before = step.battery_energy_after_kwh

    for directive in directives:
        if not directive.applies:
            continue
        adjustment = directive.structured_adjustment
        for h in adjustment.hours:
            step = plan[h]
            if directive.directive_type == "minimum_battery_reserve" and step.battery_energy_after_kwh < adjustment.minimum_energy_kwh - TOLERANCE:
                violations.append(f"hour {h}: reserve directive")
            if directive.directive_type == "no_charge_window" and step.battery_action == "charge" and step.battery_kwh > TOLERANCE:
                violations.append(f"hour {h}: no-charge directive")
            if directive.directive_type == "no_discharge_window" and step.battery_action == "discharge" and step.battery_kwh > TOLERANCE:
                violations.append(f"hour {h}: no-discharge directive")
            if directive.directive_type == "max_grid_window" and step.grid_kwh > adjustment.max_grid_kwh + TOLERANCE:
                violations.append(f"hour {h}: grid-cap directive")

    if abs(plan[-1].battery_energy_after_kwh - battery.initial_energy_kwh) > TOLERANCE:
        violations.append("end-of-day battery neutrality")
    return violations

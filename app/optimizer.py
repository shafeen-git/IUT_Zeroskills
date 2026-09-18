"""
Math Optimizer: 24-hour energy scheduling via Linear Programming (PuLP).

Decision variables (per hour t in 0..23):
    charge[t]        >= 0      battery charge power (kW)
    discharge[t]     >= 0      battery discharge power (kW)
    grid_import[t]   >= 0      power drawn from grid (kW)

State:
    soc[t]           >= 0      battery state of charge at end of hour t (kWh)

Objective:
    minimize  sum_t ( grid_import[t] * grid_price[t] )

Hard constraints (GridWise base):
    1. Energy balance per hour:
         grid_import[t] + effective_solar[t] + discharge[t] - charge[t] == load[t]
    2. SoC dynamics:
         soc[t] == soc[t-1] + charge[t] - discharge[t]   (with soc[-1] = soc0)
    3. SoC bounds: 0 <= soc[t] <= capacity
    4. Power rate limits: charge[t] <= max_charge, discharge[t] <= max_discharge
    5. End-of-day neutrality: soc[23] == soc0

Operator directives (applied AFTER validation):
    - solar_reduction      : set effective_solar[t] to solar[t] * factor
    - minimum_battery_reserve : soc[t] >= reserve_kwh for hours in window
    - no_charge_window      : charge[t] == 0 for hours in window
    - no_discharge_window   : discharge[t] == 0 for hours in window
    - max_grid_window       : grid_import[t] <= cap_kw for hours in window
    - no_op                 : ignored
"""

from pulp import (
    LpProblem,
    LpMinimize,
    LpVariable,
    lpSum,
    LpStatus,
    value,
    PULP_CBC_CMD,
)
from app.models import DirectiveInterpretation, HourlyPlan, OptimizeRequest

HOURS = list(range(24))  # 0..23


class OptimizerInputError(ValueError):
    """Raised when operator directive parameters are mathematically invalid."""


def _validate_window(params, dtype):
    """
    Validate that [start_hour, end_hour] is a non-empty window inside 0..23.
    Raises OptimizerInputError if not. Returns (start, end) on success.
    """
    try:
        start = int(params.get('start_hour', 0))
        end = int(params.get('end_hour', 23))
    except (TypeError, ValueError):
        raise OptimizerInputError(
            f"{dtype}: start_hour and end_hour must be integers"
        )
    if not (0 <= start <= 23) or not (0 <= end <= 23):
        raise OptimizerInputError(
            f"{dtype}: hours must be in 0..23 (got start={start}, end={end})"
        )
    if start > end:
        raise OptimizerInputError(
            f"{dtype}: start_hour ({start}) must be <= end_hour ({end})"
        )
    return start, end


def _validate_non_negative(params, dtype, *keys):
    """Ensure named numeric params are >= 0."""
    for k in keys:
        if k in params:
            try:
                v = float(params[k])
            except (TypeError, ValueError):
                raise OptimizerInputError(
                    f"{dtype}: {k} must be numeric (got {params[k]!r})"
                )
            if v < 0:
                raise OptimizerInputError(
                    f"{dtype}: {k} must be >= 0 (got {v})"
                )


def _parse_directive_pairs(directive):
    """
    Convert a directive list-of-pairs into a dict. The directive is shaped:
        ['type', <type_name>, <k1>, <v1>, <k2>, <v2>, ...]
    which becomes:
        {'type': <type_name>, <k1>: <v1>, <k2>: <v2>, ...}

    This replaces the buggy `dict(directive[1:])` pattern which collapsed
    pairs incorrectly across the [type, name] boundary.
    """
    if len(directive) < 2:
        return {}
    params = {str(directive[0]): directive[1]}
    i = 2
    while i + 1 < len(directive):
        params[str(directive[i])] = directive[i + 1]
        i += 2
    return params



def _empty_schedule():
    """Return a 24-hour schedule as a list of [hour, charge, discharge, grid_import, soc] pairs."""
    return [
        [h, 0.0, 0.0, 0.0, 0.0]
        for h in HOURS
    ]


def _apply_directive(prob, vars_, effective_solar, solar, directive):
    """
    Apply a single validated directive to the LP.
    `vars_` is a list of [hour, charge, discharge, grid_import, soc] variables.
    `effective_solar` is the solar series used by the energy-balance constraints.
    `directive` is a list-of-pairs dict matching the data-structure rule:
        ["type", "solar_reduction"] etc., plus type-specific params.
    """
    if not directive:
        return
    params = _parse_directive_pairs(directive)
    dtype = params.get("type")

    if dtype == "no_op":
        return

    if dtype == "solar_reduction":
        # Apply the reduction to solar generation, not to grid imports.
        try:
            factor = float(params["factor"])
        except (TypeError, ValueError, KeyError):
            raise OptimizerInputError(
                f"{dtype}: factor must be numeric (got {params.get('factor')!r})"
            )
        if not 0 <= factor <= 1:
            raise OptimizerInputError(
                f"{dtype}: factor must be between 0 and 1 (got {factor})"
            )
        for h in HOURS:
            prob += effective_solar[h] == solar[h] * factor

    elif dtype == "minimum_battery_reserve":
        start, end = _validate_window(params, dtype)
        _validate_non_negative(params, dtype, "reserve_kwh")
        reserve = float(params.get("reserve_kwh", 0.0))
        for h, ch, dis, gi, soc in vars_:
            if start <= h <= end:
                prob += soc >= reserve

    elif dtype == "no_charge_window":
        start, end = _validate_window(params, dtype)
        for h, ch, dis, gi, soc in vars_:
            if start <= h <= end:
                prob += ch == 0

    elif dtype == "no_discharge_window":
        start, end = _validate_window(params, dtype)
        for h, ch, dis, gi, soc in vars_:
            if start <= h <= end:
                prob += dis == 0

    elif dtype == "max_grid_window":
        start, end = _validate_window(params, dtype)
        _validate_non_negative(params, dtype, "max_kw")
        cap = float(params.get("max_kw", 0.0))
        for h, ch, dis, gi, soc in vars_:
            if start <= h <= end:
                prob += gi <= cap


def _directive_to_pairs(directive: DirectiveInterpretation):
    """Convert a standard directive interpretation to the internal pair format."""
    if not directive.applies:
        return ["type", "no_op"]

    adjustment = directive.structured_adjustment
    params = ["type", directive.directive_type]
    if adjustment is not None and adjustment.hours:
        params.extend([
            "start_hour", min(adjustment.hours),
            "end_hour", max(adjustment.hours),
        ])
    if adjustment is not None and adjustment.factor is not None:
        parameter_name = {
            "solar_reduction": "factor",
            "minimum_battery_reserve": "reserve_kwh",
            "max_grid_window": "max_kw",
        }.get(directive.directive_type)
        if parameter_name is not None:
            params.extend([parameter_name, adjustment.factor])
    return params


def _request_to_pairs(request: OptimizeRequest, directives):
    """Convert standard Pydantic inputs into the optimizer's internal pairs."""
    return [
        ["load", [hour.demand_kwh for hour in request.hours]],
        ["solar", [hour.solar_kwh for hour in request.hours]],
        ["grid_price", [hour.tariff_bdt_per_kwh for hour in request.hours]],
        ["battery", [
            ["capacity_kwh", request.battery.capacity_kwh],
            ["initial_soc_kwh", request.battery.initial_energy_kwh],
            ["max_charge_kw", request.battery.max_charge_kwh_per_hour],
            ["max_discharge_kw", request.battery.max_discharge_kwh_per_hour],
        ]],
        ["directives", [_directive_to_pairs(directive) for directive in directives]],
    ]


def _format_hourly_plan(schedule, request, directives):
    """Convert internal LP rows into the public HourlyPlan schema."""
    solar_factor = 1.0
    for directive in directives:
        if (
            directive.applies
            and directive.directive_type == "solar_reduction"
            and directive.structured_adjustment is not None
            and directive.structured_adjustment.factor is not None
        ):
            solar_factor *= directive.structured_adjustment.factor

    plans = []
    for row, hour_input in zip(schedule, request.hours):
        hour, charge, discharge, grid_import, soc = row
        if charge > 1e-9:
            battery_action = "charge"
            battery_kwh = charge
        elif discharge > 1e-9:
            battery_action = "discharge"
            battery_kwh = discharge
        else:
            battery_action = "idle"
            battery_kwh = 0.0
        effective_solar = hour_input.solar_kwh * solar_factor
        plans.append(HourlyPlan(
            hour=int(hour),
            grid_kwh=float(grid_import),
            solar_used_kwh=float(effective_solar),
            battery_action=battery_action,
            battery_kwh=float(battery_kwh),
            battery_energy_after_kwh=float(soc),
        ))
    return plans


def _optimize_internal(request):
    """
    Solve the 24-hour energy scheduling LP.

    `request` is the internal list-of-pairs representation:
        [
          ["load", [kW for h in 0..23]],
          ["solar", [kW for h in 0..23]],
          ["grid_price", [per-kWh for h in 0..23]],
          ["battery", [
              ["capacity_kwh", 100.0],
              ["initial_soc_kwh", 50.0],
              ["max_charge_kw", 50.0],
              ["max_discharge_kw", 50.0],
          ]],
          ["directives", [ list-of-directives, ... ]],   # each is list-of-pairs
        ]

    Returns (status, schedule) where schedule is a 24-row list of internal
    [hour, charge, discharge, grid_import, soc] numerical pairs.
    """
    rd = dict(request)

    load = rd["load"]
    solar = rd["solar"]
    price = rd["grid_price"]
    bat_list = rd["battery"]
    bat = dict(bat_list)

    capacity = float(bat["capacity_kwh"])
    soc0 = float(bat["initial_soc_kwh"])
    max_ch = float(bat["max_charge_kw"])
    max_dis = float(bat["max_discharge_kw"])

    directives = rd.get("directives", [])

    prob = LpProblem("energy_schedule", LpMinimize)

    charge = [LpVariable(f"charge_{h}", lowBound=0, upBound=max_ch) for h in HOURS]
    discharge = [LpVariable(f"discharge_{h}", lowBound=0, upBound=max_dis) for h in HOURS]
    grid_imp = [LpVariable(f"grid_{h}", lowBound=0) for h in HOURS]
    soc = [LpVariable(f"soc_{h}", lowBound=0, upBound=capacity) for h in HOURS]
    effective_solar = [
        LpVariable(f"effective_solar_{h}", lowBound=0)
        for h in HOURS
    ]

    # Pack vars as list-of-pairs for directive dispatch
    vars_ = [[h, charge[h], discharge[h], grid_imp[h], soc[h]] for h in HOURS]

    # Apply directives before building the balance so effective_solar is fixed
    # by a solar directive when one is present.
    solar_reduction_applied = False
    for d in directives:
        if _parse_directive_pairs(d).get("type") == "solar_reduction":
            solar_reduction_applied = True
        _apply_directive(prob, vars_, effective_solar, solar, d)

    if not solar_reduction_applied:
        for h in HOURS:
            prob += effective_solar[h] == solar[h]

    # Objective: minimize total grid cost
    prob += lpSum(grid_imp[h] * price[h] for h in HOURS)

    # Energy balance per hour
    for h in HOURS:
        prob += grid_imp[h] + effective_solar[h] + discharge[h] - charge[h] == load[h]

    # SoC dynamics
    for h in HOURS:
        prev = soc0 if h == 0 else soc[h - 1]
        prob += soc[h] == prev + charge[h] - discharge[h]

    # End-of-day neutrality
    prob += soc[23] == soc0

    prob.solve(PULP_CBC_CMD(msg=0))

    status = LpStatus[prob.status]
    schedule = []
    for h in HOURS:
        schedule.append([
            h,
            float(value(charge[h]) or 0.0),
            float(value(discharge[h]) or 0.0),
            float(value(grid_imp[h]) or 0.0),
            float(value(soc[h]) or 0.0),
        ])

    return status, schedule


def optimize_energy(
    request: OptimizeRequest,
    directive_interpretations: list[DirectiveInterpretation] | None = None,
):
    """Solve a standard request and return rows matching the HourlyPlan schema."""
    directives = directive_interpretations or []
    internal_request = _request_to_pairs(request, directives)
    status, schedule = _optimize_internal(internal_request)
    return status, _format_hourly_plan(schedule, request, directives)

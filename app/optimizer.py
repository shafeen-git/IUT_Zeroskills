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
       grid_import[t] + solar[t] + discharge[t] - charge[t] == load[t]
    2. SoC dynamics:
       soc[t] == soc[t-1] + charge[t] * eta_c - discharge[t] / eta_d   (with soc[-1] = soc0)
    3. SoC bounds: 0 <= soc[t] <= capacity
    4. Power rate limits: charge[t] <= max_charge, discharge[t] <= max_discharge
    5. End-of-day neutrality: soc[23] == soc0

Operator directives (applied AFTER validation):
    - solar_reduction      : cap grid_import during solar-peak hours
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


def _apply_directive(prob, vars_, directive):
    """
    Apply a single validated directive to the LP.
    `vars_` is a list of [hour, charge, discharge, grid_import, soc] variables.
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
        # Cap grid import during solar hours (default 10..15 if no window given)
        # Inject defaults BEFORE validating so the window default is also checked
        params.setdefault("start_hour", 10)
        params.setdefault("end_hour", 15)
        params.setdefault("max_kw", 0.0)
        start, end = _validate_window(params, dtype)
        _validate_non_negative(params, dtype, "max_kw")
        cap = float(params.get("max_kw"))
        for h, ch, dis, gi, soc in vars_:
            if start <= h <= end:
                prob += gi <= cap

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


def optimize_energy(request):
    """
    Solve the 24-hour energy scheduling LP.

    `request` shape (list-of-pairs per data-structure rule):
        [
          ["load", [kW for h in 0..23]],
          ["solar", [kW for h in 0..23]],
          ["grid_price", [per-kWh for h in 0..23]],
          ["battery", [
              ["capacity_kwh", 100.0],
              ["initial_soc_kwh", 50.0],
              ["max_charge_kw", 50.0],
              ["max_discharge_kw", 50.0],
              ["charge_efficiency", 0.95],
              ["discharge_efficiency", 0.95],
          ]],
          ["directives", [ list-of-directives, ... ]],   # each is list-of-pairs
        ]

    Returns (status, schedule) where schedule is a 24-row list of
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
    eta_c = float(bat.get("charge_efficiency", 1.0))
    eta_d = float(bat.get("discharge_efficiency", 1.0))

    directives = rd.get("directives", [])

    prob = LpProblem("energy_schedule", LpMinimize)

    charge = [LpVariable(f"charge_{h}", lowBound=0, upBound=max_ch) for h in HOURS]
    discharge = [LpVariable(f"discharge_{h}", lowBound=0, upBound=max_dis) for h in HOURS]
    grid_imp = [LpVariable(f"grid_{h}", lowBound=0) for h in HOURS]
    soc = [LpVariable(f"soc_{h}", lowBound=0, upBound=capacity) for h in HOURS]

    # Pack vars as list-of-pairs for directive dispatch
    vars_ = [[h, charge[h], discharge[h], grid_imp[h], soc[h]] for h in HOURS]

    # Objective: minimize total grid cost
    prob += lpSum(grid_imp[h] * price[h] for h in HOURS)

    # Energy balance per hour
    for h in HOURS:
        prob += grid_imp[h] + solar[h] + discharge[h] - charge[h] == load[h]

    # SoC dynamics
    for h in HOURS:
        prev = soc0 if h == 0 else soc[h - 1]
        prob += soc[h] == prev + charge[h] * eta_c - discharge[h] / eta_d

    # End-of-day neutrality
    prob += soc[23] == soc0

    # Apply directives
    for d in directives:
        _apply_directive(prob, vars_, d)

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

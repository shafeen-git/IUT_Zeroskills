"""24-hour linear program: minimise grid cost subject to GridWise rules and validated directives.

Per hour h (all >= 0):  grid[h], solar_used[h] <= effective_solar[h], charge[h], discharge[h], energy[h]
    grid + solar_used + discharge == demand + charge          (energy balance)
    energy[h] == energy[h-1] + charge - discharge             (battery transition, energy[-1] = initial)
    reserve_floor[h] <= energy[h] <= capacity                 (base minimum raised by reserve directives)
    charge <= charge_cap[h], discharge <= discharge_cap[h]    (rate limits, zeroed by no-charge/no-discharge windows)
    grid <= grid_cap[h]                                       (max_grid_window)
    energy[23] == initial                                     (end-of-day neutrality)
"""

from pulp import HiGHS, LpMinimize, LpProblem, LpStatus, LpVariable, lpSum, value

from app.config import get_config
from app.models import DirectiveInterpretation, HourlyPlan, OptimizeRequest

HOURS = range(24)
DECIMALS = 6
ZERO_TOLERANCE = 1e-6


class InfeasibleScheduleError(RuntimeError):
    """No schedule satisfies the battery rules together with the interpreted directives."""


def _active(directives: list[DirectiveInterpretation], directive_type: str) -> list[DirectiveInterpretation]:
    return [d for d in directives if d.applies and d.directive_type == directive_type]


def effective_solar(request: OptimizeRequest, directives: list[DirectiveInterpretation]) -> list[float]:
    solar = [entry.solar_kwh for entry in request.hours]
    for directive in _active(directives, "solar_reduction"):
        for hour in directive.structured_adjustment.hours:
            solar[hour] *= directive.structured_adjustment.factor
    return solar


def _solved(variable: LpVariable) -> float:
    return float(value(variable) or 0.0)


def _clean(amount: float) -> float:
    rounded = round(amount, DECIMALS)
    return 0.0 if abs(rounded) < ZERO_TOLERANCE else rounded


def solve_schedule(request: OptimizeRequest, directives: list[DirectiveInterpretation]) -> list[HourlyPlan]:
    battery = request.battery
    demand = [entry.demand_kwh for entry in request.hours]
    tariff = [entry.tariff_bdt_per_kwh for entry in request.hours]
    solar = effective_solar(request, directives)

    reserve_floor = [battery.minimum_energy_kwh] * 24
    # Moving more than the full capacity in one hour is impossible; capping keeps the LP well scaled.
    charge_cap = [min(battery.max_charge_kwh_per_hour, battery.capacity_kwh)] * 24
    discharge_cap = [min(battery.max_discharge_kwh_per_hour, battery.capacity_kwh)] * 24
    grid_cap = [None] * 24
    for directive in _active(directives, "minimum_battery_reserve"):
        for hour in directive.structured_adjustment.hours:
            reserve_floor[hour] = max(reserve_floor[hour], directive.structured_adjustment.minimum_energy_kwh)
    for directive in _active(directives, "no_charge_window"):
        for hour in directive.structured_adjustment.hours:
            charge_cap[hour] = 0.0
    for directive in _active(directives, "no_discharge_window"):
        for hour in directive.structured_adjustment.hours:
            discharge_cap[hour] = 0.0
    for directive in _active(directives, "max_grid_window"):
        limit = directive.structured_adjustment.max_grid_kwh
        for hour in directive.structured_adjustment.hours:
            grid_cap[hour] = limit if grid_cap[hour] is None else min(grid_cap[hour], limit)

    problem = LpProblem("gridwise_schedule", LpMinimize)
    grid = [LpVariable(f"grid_{h}", 0, grid_cap[h]) for h in HOURS]
    solar_used = [LpVariable(f"solar_{h}", 0, solar[h]) for h in HOURS]
    charge = [LpVariable(f"charge_{h}", 0, charge_cap[h]) for h in HOURS]
    discharge = [LpVariable(f"discharge_{h}", 0, discharge_cap[h]) for h in HOURS]
    energy = [LpVariable(f"energy_{h}", reserve_floor[h], battery.capacity_kwh) for h in HOURS]

    problem += lpSum(tariff[h] * grid[h] for h in HOURS)
    for h in HOURS:
        problem += grid[h] + solar_used[h] + discharge[h] == demand[h] + charge[h]
        previous = battery.initial_energy_kwh if h == 0 else energy[h - 1]
        problem += energy[h] == previous + charge[h] - discharge[h]
    problem += energy[23] == battery.initial_energy_kwh

    # HiGHS runs in-process and returns full double precision (CBC via PuLP reports 8 significant digits).
    problem.solve(HiGHS(msg=False, timeLimit=get_config("SOLVER_TIME_LIMIT_SECONDS")))
    if LpStatus[problem.status] != "Optimal":
        raise InfeasibleScheduleError(LpStatus[problem.status])

    # Rebuild the plan from the solution so every reported number replays exactly:
    # simultaneous charge/discharge is netted (valid because the battery is lossless),
    # grid is derived from the balance equation, and the day closes at the initial energy.
    plan = []
    energy_before = battery.initial_energy_kwh
    for h in HOURS:
        if h == 23:
            net = _clean(battery.initial_energy_kwh - energy_before)
        else:
            net = _clean(_solved(charge[h]) - _solved(discharge[h]))
        used = _clean(min(max(_solved(solar_used[h]), 0.0), solar[h]))
        grid_kwh = _clean(demand[h] + net - used)
        if grid_kwh < 0:
            used = _clean(used + grid_kwh)
            grid_kwh = 0.0
        energy_after = _clean(energy_before + net)
        plan.append(HourlyPlan(
            hour=h,
            grid_kwh=grid_kwh,
            solar_used_kwh=used,
            battery_action="charge" if net > 0 else "discharge" if net < 0 else "idle",
            battery_kwh=abs(net),
            battery_energy_after_kwh=energy_after,
        ))
        energy_before = energy_after
    return plan

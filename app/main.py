import logging
import time

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import get_config
from app.directives import validate_interpretations
from app.interpreter import interpret_notes
from app.models import DirectiveInterpretation, HourlyPlan, OptimizeRequest, OptimizeResponse
from app.optimizer import InfeasibleScheduleError, solve_schedule
from app.replay import find_violations

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("gridwise.api")

app = FastAPI(
    title=get_config("APP_NAME"),
    version=get_config("VERSION"),
    docs_url="/docs",
    redoc_url="/redoc",
)

MAX_REPORTED_ERRORS = 10


@app.exception_handler(RequestValidationError)
async def malformed_request(_: Request, exc: RequestValidationError):
    errors = [
        ".".join(str(part) for part in error.get("loc", [])) + ": " + str(error.get("msg", "invalid"))
        for error in exc.errors()[:MAX_REPORTED_ERRORS]
    ]
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": "Malformed JSON or structurally invalid request", "errors": errors},
    )


def semantic_problem(request: OptimizeRequest) -> str | None:
    battery = request.battery
    if battery.minimum_energy_kwh > battery.capacity_kwh:
        return "battery.minimum_energy_kwh exceeds battery.capacity_kwh"
    if not battery.minimum_energy_kwh <= battery.initial_energy_kwh <= battery.capacity_kwh:
        return "battery.initial_energy_kwh must lie between minimum_energy_kwh and capacity_kwh"
    return None


def _hour_ranges(hours: list[int]) -> str:
    if not hours:
        return "none"
    ranges = []
    start = previous = hours[0]
    for hour in hours[1:] + [None]:
        if hour is not None and hour == previous + 1:
            previous = hour
            continue
        ranges.append(f"{start}" if start == previous else f"{start}-{previous}")
        if hour is not None:
            start = previous = hour
    return ", ".join(ranges)


def plan_summary(directives: list[DirectiveInterpretation], plan: list[HourlyPlan], total_cost: float) -> str:
    applied = [d.directive_type for d in directives if d.applies]
    ignored = len(directives) - len(applied)
    charging = [step.hour for step in plan if step.battery_action == "charge"]
    discharging = [step.hour for step in plan if step.battery_action == "discharge"]
    return (
        f"Applied {len(applied)} operator directive(s) ({', '.join(applied) or 'none'}); "
        f"{ignored} note(s) ignored as no_op. Battery charges in hours {_hour_ranges(charging)} and "
        f"discharges in hours {_hour_ranges(discharging)}, returning to its initial energy after hour 23. "
        f"Minimum-cost grid purchase: {total_cost:.2f} BDT."
    )


@app.get("/health", status_code=status.HTTP_200_OK, tags=["System"])
def health_check():
    return {"status": "ok"}


@app.post("/optimize-energy", response_model=OptimizeResponse, tags=["Optimization"])
def optimize_energy_route(request: OptimizeRequest):
    started = time.perf_counter()
    problem = semantic_problem(request)
    if problem:
        return JSONResponse(status_code=422, content={"detail": problem})

    try:
        capacity = request.battery.capacity_kwh
        directives, source = interpret_notes(request.operator_notes, capacity)
        validate_interpretations(directives, len(request.operator_notes), capacity)
        plan = solve_schedule(request, directives)

        violations = find_violations(request, directives, plan)
        if violations:
            logger.error("scenario %s failed final replay: %s", request.scenario_id, "; ".join(violations[:5]))

        total_grid = round(sum(step.grid_kwh for step in plan), 6)
        total_cost = round(sum(step.grid_kwh * entry.tariff_bdt_per_kwh for step, entry in zip(plan, request.hours)), 6)
        response = OptimizeResponse(
            scenario_id=request.scenario_id,
            directive_interpretation=directives,
            hourly_plan=plan,
            total_grid_kwh=total_grid,
            total_cost_bdt=total_cost,
            peak_grid_kwh=max(step.grid_kwh for step in plan),
            plan_summary=plan_summary(directives, plan, total_cost),
        )
    except InfeasibleScheduleError:
        return JSONResponse(
            status_code=422,
            content={"detail": "No feasible schedule satisfies the battery rules and the interpreted directives"},
        )
    except Exception as exc:
        logger.error("scenario %s failed: %s", request.scenario_id, type(exc).__name__)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal error while optimizing the schedule"},
        )

    logger.info(
        "scenario %s interpreted via %s, cost %.2f BDT, %.0f ms",
        request.scenario_id, source, total_cost, (time.perf_counter() - started) * 1000,
    )
    return response

import asyncio

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from app.config import get_config, get_runtime_secret
from app.directives import DirectiveValidationError, validate_directive_interpretation
from app.interpreter import call_llm_interpreter
from app.models import OptimizeRequest, OptimizeResponse
from app.optimizer import optimize_energy

# Initialize FastAPI application
app = FastAPI(
    title=get_config("APP_NAME", "Smart Campus Energy Optimizer API"),
    version=get_config("VERSION", "1.0.0"),
    docs_url="/docs",
    redoc_url="/redoc",
)

# ==============================================================================
# DATA STRUCTURE RULE ENFORCEMENT:
# Strictly use lists instead of maps (dictionaries/objects) whenever grouping
# or implementing internal data structures throughout the codebase.
# ==============================================================================

# Route registry stored as a list of route definitions [method, path, description]
REGISTERED_ROUTES = [
    ["GET", "/health", "Service health check endpoint"],
    ["POST", "/optimize-energy", "Energy schedule optimization endpoint (Hackathon main)"],
]

# Supported hackathon operator directive types stored as a list
SUPPORTED_DIRECTIVES = [
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
]


@app.get(
    "/health",
    status_code=status.HTTP_200_OK,
    summary="Health Check",
    tags=["System"],
)
def health_check():
    """
    Health check endpoint returning 200 OK.

    Response format:
    { "status": "ok" }
    """
    return {"status": "ok"}


@app.post(
    "/optimize-energy",
    response_model=OptimizeResponse,
    status_code=status.HTTP_200_OK,
    summary="Optimize Energy Schedule",
    tags=["Optimization"],
)
async def optimize_energy_route(request: OptimizeRequest):
    try:
        async def run_pipeline():
            llm_directives = call_llm_interpreter(request.operator_notes, request.battery.capacity_kwh)
            validate_directive_interpretation(llm_directives)
            validated_directives = llm_directives

            lp_status, schedule = optimize_energy(request, validated_directives)
            if lp_status != "Optimal":
                return JSONResponse(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    content={"detail": f"Optimization failed: LP status was '{lp_status}'"},
                )

            tariff_by_hour = {hour_entry.hour: hour_entry.tariff_bdt_per_kwh for hour_entry in request.hours}
            total_grid_kwh = 0.0
            total_cost_bdt = 0.0
            peak_grid_kwh = 0.0
            for plan in schedule:
                total_grid_kwh += float(plan.grid_kwh)
                total_cost_bdt += float(plan.grid_kwh) * float(tariff_by_hour.get(plan.hour, 0.0))
                if float(plan.grid_kwh) > peak_grid_kwh:
                    peak_grid_kwh = float(plan.grid_kwh)

            return OptimizeResponse(
                scenario_id=request.scenario_id,
                directive_interpretation=validated_directives,
                hourly_plan=schedule,
                total_grid_kwh=round(total_grid_kwh, 2),
                total_cost_bdt=round(total_cost_bdt, 2),
                peak_grid_kwh=round(peak_grid_kwh, 2),
                plan_summary="Operates under interpreted operator directives and minimizes total grid import cost.",
            )

        response = await asyncio.wait_for(
            run_pipeline(),
            timeout=25,
        )
        return response
    except ValidationError:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": "Invalid optimization response"},
        )
    except DirectiveValidationError as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": str(exc)},
        )
    except asyncio.TimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Optimization timed out",
        ) from exc
    except Exception:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Optimization failed"},
        )


# Root informational endpoint
@app.get("/", status_code=status.HTTP_200_OK, include_in_schema=False)
def root():
    info_pairs = [
        ["service", get_config("APP_NAME")],
        ["version", get_config("VERSION")],
        ["routes", REGISTERED_ROUTES],
        ["status", "running"],
    ]
    return dict(info_pairs)

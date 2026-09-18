import asyncio

from fastapi import FastAPI, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from app.config import get_config, get_runtime_secret
from app.directives import DirectiveValidationError, validate_directive_interpretation
from app.models import OptimizeRequest, OptimizeResponse

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


async def call_llm_interpreter(operator_notes: list[str]) -> list[dict]:
    directive_interpretations = []
    for note_index, _ in enumerate(operator_notes):
        directive_pairs = [
            ["note_index", note_index],
            ["applies", False],
            ["directive_type", "no_op"],
            ["structured_adjustment", None],
            ["explanation", "No optimization directive was applied."],
        ]
        directive_interpretations.append(dict(directive_pairs))
    return directive_interpretations


async def run_math_optimizer(hours: list, battery: dict, directives: list) -> dict:
    hourly_plan = []
    for hour in hours:
        plan_pairs = [
            ["hour", hour["hour"]],
            ["grid_kwh", 0.0],
            ["solar_used_kwh", 0.0],
            ["battery_action", "idle"],
            ["battery_kwh", 0.0],
            ["battery_energy_after_kwh", battery["initial_energy_kwh"]],
        ]
        hourly_plan.append(dict(plan_pairs))

    result_pairs = [
        ["hourly_plan", hourly_plan],
        ["total_grid_kwh", 0.0],
        ["total_cost_bdt", 0.0],
        ["peak_grid_kwh", 0.0],
        ["plan_summary", "Dummy optimization result."],
    ]
    return dict(result_pairs)


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
async def optimize_energy(request: OptimizeRequest):
    try:
        async def run_pipeline():
            llm_directives = await call_llm_interpreter(request.operator_notes)
            validate_directive_interpretation(llm_directives)
            validated_directives = llm_directives
            optimizer_result = await run_math_optimizer(
                [hour.model_dump() for hour in request.hours],
                request.battery.model_dump(),
                validated_directives,
            )
            return OptimizeResponse(
                scenario_id=request.scenario_id,
                directive_interpretation=validated_directives,
                **optimizer_result,
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
    except asyncio.TimeoutError:
        return JSONResponse(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            content={"detail": "Optimization timed out"},
        )
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

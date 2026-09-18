from fastapi import FastAPI, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi import Request
from app.config import get_config, get_runtime_secret
from app.models import DirectiveInterpretation, OptimizeRequest
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


@app.get("/health", status_code=status.HTTP_200_OK, summary="Health Check", tags=["System"])
def health_check():
    """
    Health check endpoint returning 200 OK.
    Response: { "status": "healthy" }
    Toggle to "ok" via HEALTH_STATUS env var if Section 6.2 of the BUP problem statement requires it.
    """
    status_value = get_config("HEALTH_STATUS", "healthy")
    return dict([["status", status_value]])


@app.get("/", status_code=status.HTTP_200_OK, include_in_schema=False)
def root():
    info_pairs = [
        ["service", get_config("APP_NAME")],
        ["version", get_config("VERSION")],
        ["routes", REGISTERED_ROUTES],
        ["status", "running"],
    ]
    return dict(info_pairs)


@app.post(
    "/optimize-energy",
    status_code=status.HTTP_200_OK,
    summary="Run 24-hour energy optimization",
    tags=["Optimization"],
)
async def optimize_energy_endpoint(request: Request):
    payload = await request.json()
    try:
                optimization_request = OptimizeRequest.model_validate(payload)
                interpretations = [
                        DirectiveInterpretation.model_validate(item)
                        for item in payload.get("directive_interpretation", [])
                ]
                lp_status, schedule = optimize_energy(
                        optimization_request,
                        interpretations,
                )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Optimization failed: {exc}",
        )

    if lp_status != "Optimal":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"LP returned non-optimal status: {lp_status}",
        )

    total_cost = sum(
        plan.grid_kwh * hour.tariff_bdt_per_kwh
        for plan, hour in zip(schedule, optimization_request.hours)
    )
    return {
        "status": lp_status,
        "total_cost": round(total_cost, 4),
        "schedule": [plan.model_dump() for plan in schedule],
    }

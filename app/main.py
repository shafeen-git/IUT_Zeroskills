from fastapi import FastAPI, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi import Request
from app.config import get_config, get_runtime_secret
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
    """
    Body shape (list-of-pairs per data-structure rule):
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
        ["directives", [ ...list of directive list-of-pairs... ]],
      ]

    Response shape:
      {
        "status": "Optimal" | "Infeasible" | ...,
        "total_cost": <float>,
        "schedule": [
          [hour, charge_kw, discharge_kw, grid_import_kw, soc_kwh],
          ...
        ]
      }
    """
    try:
        lp_status, schedule = optimize_energy(payload)
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

    total_cost = 0.0
    # Re-derive cost for the response using grid_price from the request
    rd = dict(payload)
    price = rd.get("grid_price", [0.0] * 24)
    for row in schedule:
        h = int(row[0])
        grid_import = float(row[3])
        if 0 <= h < len(price):
            total_cost += grid_import * float(price[h])

    response_pairs = [
        ["status", lp_status],
        ["total_cost", round(total_cost, 4)],
        ["schedule", schedule],
    ]
    return dict(response_pairs)

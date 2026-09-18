from fastapi import FastAPI, status
from fastapi.responses import JSONResponse
from app.config import get_config, get_runtime_secret

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
    { "status": "healthy" }
    
    Note: Can be toggled to "ok" via HEALTH_STATUS environment variable
    to strictly match Section 6.2 of the BUP Hackathon Problem Statement if required.
    """
    # Build response payload strictly using a list of [key, value] pairs
    status_value = get_config("HEALTH_STATUS", "healthy")
    payload_pairs = [
        ["status", status_value],
    ]
    
    # Convert list of pairs to JSON-compliant dictionary for HTTP transmission
    return dict(payload_pairs)


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

from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, field_serializer, model_validator

NonNegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Finite = Annotated[float, Field(allow_inf_nan=False)]
NoteText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

DirectiveType = Literal[
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
]


class HourlyInput(BaseModel):
    hour: Annotated[int, Field(ge=0, le=23)]
    demand_kwh: NonNegative
    solar_kwh: NonNegative
    tariff_bdt_per_kwh: Finite


class BatteryConfig(BaseModel):
    capacity_kwh: NonNegative
    initial_energy_kwh: NonNegative
    minimum_energy_kwh: NonNegative
    max_charge_kwh_per_hour: NonNegative
    max_discharge_kwh_per_hour: NonNegative


class OptimizeRequest(BaseModel):
    scenario_id: str
    operator_notes: Annotated[list[NoteText], Field(min_length=1, max_length=3)]
    hours: Annotated[list[HourlyInput], Field(min_length=24, max_length=24)]
    battery: BatteryConfig

    @model_validator(mode="after")
    def _one_entry_per_hour(self):
        if sorted(entry.hour for entry in self.hours) != list(range(24)):
            raise ValueError("hours must contain each hour 0-23 exactly once")
        self.hours = sorted(self.hours, key=lambda entry: entry.hour)
        return self


class StructuredAdjustment(BaseModel):
    hours: list[int]
    factor: float | None = None
    minimum_energy_kwh: float | None = None
    max_grid_kwh: float | None = None


class DirectiveInterpretation(BaseModel):
    note_index: int
    applies: bool
    directive_type: DirectiveType
    structured_adjustment: StructuredAdjustment | None
    explanation: str

    @field_serializer("structured_adjustment")
    def _only_required_fields(self, adjustment: StructuredAdjustment | None):
        # The contract fixes one exact shape per directive type, so unused value fields are omitted.
        return None if adjustment is None else adjustment.model_dump(exclude_none=True)


class HourlyPlan(BaseModel):
    hour: int
    grid_kwh: float
    solar_used_kwh: float
    battery_action: Literal["charge", "discharge", "idle"]
    battery_kwh: float
    battery_energy_after_kwh: float


class OptimizeResponse(BaseModel):
    scenario_id: str
    directive_interpretation: list[DirectiveInterpretation]
    hourly_plan: Annotated[list[HourlyPlan], Field(min_length=24, max_length=24)]
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float
    plan_summary: str

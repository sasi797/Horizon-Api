from pydantic import BaseModel


class TrendPoint(BaseModel):
    date: str
    received: int
    completed: int


class PrioritySlice(BaseModel):
    name: str
    value: int
    color: str


class DailySummaryRow(BaseModel):
    date: str
    received: int
    completed: int
    pending: int
    rate: int


class ReportStats(BaseModel):
    total_bookings: int
    completed: int
    pending: int
    sla_breach: int
    completion_rate: float
    total_bookings_change: float = 0.0
    completed_change: float = 0.0
    pending_change: float = 0.0
    sla_breach_change: float = 0.0


class HourlyPoint(BaseModel):
    hour: int
    label: str
    received: int
    completed: int
    open: int = 0


class AvgCompletionByPriority(BaseModel):
    priority: str
    avg_hours: float
    count: int


class AvgCompletionReport(BaseModel):
    overall_avg_hours: float
    overall_count: int
    by_priority: list[AvgCompletionByPriority]


class StatusBreakdownRow(BaseModel):
    priority: str
    pending: int = 0
    in_progress: int = 0
    completed: int = 0


class DashboardStats(BaseModel):
    total_bookings: int
    pending: int
    in_progress: int
    completed: int
    ignored: int = 0
    da_numbers_count: int = 0
    at_risk: int = 0

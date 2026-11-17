from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

from .config import SITES
from .utils import TIME_RE

AttendanceStatus = Literal["P", "A", "HD"]
PfStatus = Literal["Verified", "Pending"]
PP_PATTERN = r"^PP\d{10}$"
MOBILE_PATTERN = r"^\d{10}$"


def _check_site(v: str) -> str:
    if v not in SITES:
        raise ValueError(f"Site must be one of: {', '.join(SITES)}")
    return v


def _check_time(v: str | None) -> str | None:
    if v in (None, ""):
        return None
    if not TIME_RE.match(v):
        raise ValueError("Time must be HH:MM (24-hour)")
    return v


# ---------- Auth ----------
class AdminLogin(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6)


class AdminSignup(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    email: EmailStr
    site: str
    password: str = Field(min_length=8, max_length=128)

    @field_validator("site")
    @classmethod
    def site_ok(cls, v: str) -> str:
        if v not in SITES + ["Both Sites (Head Office)"]:
            raise ValueError("Invalid site")
        return v


class ForgotPassword(BaseModel):
    email: EmailStr


class ResetPassword(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=128)


class OtpRequest(BaseModel):
    pp_number: str = Field(pattern=PP_PATTERN)
    mobile: str = Field(pattern=MOBILE_PATTERN)

    @field_validator("pp_number", mode="before")
    @classmethod
    def upper(cls, v: str) -> str:
        return v.strip().upper() if isinstance(v, str) else v


class EmployeeLogin(OtpRequest):
    name: str = Field(min_length=2, max_length=80)
    site: str
    otp: str = Field(pattern=r"^\d{6}$")
    selfie: str = Field(description="data:image/...;base64,... check-in selfie")

    @field_validator("site")
    @classmethod
    def site_ok(cls, v: str) -> str:
        return _check_site(v)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    role: Literal["admin", "employee"]
    user: dict


# ---------- Employees ----------
class EmployeeBase(BaseModel):
    emp_code: str = Field(min_length=2, max_length=20)
    pp_number: str = Field(pattern=PP_PATTERN)
    name: str = Field(min_length=2, max_length=80)
    trade: str = Field(min_length=2, max_length=40)
    site: str
    shift: str = Field("Shift A", max_length=30)
    daily_rate: int = Field(850, ge=0, le=100000)
    mobile: str = Field(pattern=MOBILE_PATTERN)
    uan: str | None = Field(None, max_length=20)
    esic: str | None = Field(None, max_length=20)
    pf_status: PfStatus = "Pending"

    @field_validator("site")
    @classmethod
    def site_ok(cls, v: str) -> str:
        return _check_site(v)

    @field_validator("emp_code", "pp_number", mode="before")
    @classmethod
    def upper(cls, v: str) -> str:
        return v.strip().upper() if isinstance(v, str) else v


class EmployeeCreate(EmployeeBase):
    initial_status: AttendanceStatus | None = None


class EmployeeUpdate(BaseModel):
    emp_code: str | None = Field(None, min_length=2, max_length=20)
    pp_number: str | None = Field(None, pattern=PP_PATTERN)
    name: str | None = Field(None, min_length=2, max_length=80)
    trade: str | None = None
    site: str | None = None
    shift: str | None = Field(None, max_length=30)
    daily_rate: int | None = Field(None, ge=0, le=100000)
    mobile: str | None = Field(None, pattern=MOBILE_PATTERN)
    uan: str | None = None
    esic: str | None = None
    pf_status: PfStatus | None = None
    is_active: bool | None = None

    @field_validator("site")
    @classmethod
    def site_ok(cls, v: str | None) -> str | None:
        return None if v is None else _check_site(v)

    @field_validator("emp_code", "pp_number", mode="before")
    @classmethod
    def upper(cls, v):
        return v.strip().upper() if isinstance(v, str) else v


# ---------- Attendance ----------
class MarkAttendance(BaseModel):
    employee_id: str
    date: str | None = None
    status: AttendanceStatus
    check_in: str | None = None
    check_out: str | None = None

    @field_validator("check_in", "check_out")
    @classmethod
    def time_ok(cls, v: str | None) -> str | None:
        return _check_time(v)


class BulkMark(BaseModel):
    date: str | None = None
    status: AttendanceStatus = "P"
    site: str | None = None
    only_unmarked: bool = False


class SelfCheck(BaseModel):
    selfie: str | None = None


# ---------- Regularization ----------
class RegularizationCreate(BaseModel):
    date: str
    type: Literal["missed_punch", "wrong_status", "late_arrival", "early_exit", "on_duty"]
    requested_status: AttendanceStatus
    # Which punch the employee wants corrected: only that time is changed on approval.
    punch: Literal["in", "out", "both"] = "both"
    check_in: str | None = None
    check_out: str | None = None
    reason: str = Field(min_length=5, max_length=500)

    @field_validator("check_in", "check_out")
    @classmethod
    def time_ok(cls, v: str | None) -> str | None:
        return _check_time(v)


class RegularizationReview(BaseModel):
    action: Literal["approve", "reject"]
    remark: str | None = Field(None, max_length=300)


class AdminReview(BaseModel):
    action: Literal["approve", "reject"]

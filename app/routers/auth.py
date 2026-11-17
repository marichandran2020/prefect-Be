import hashlib
import logging
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from pymongo.errors import DuplicateKeyError

from ..config import settings
from ..database import get_db
from ..schemas import AdminLogin, AdminSignup, EmployeeLogin, ForgotPassword, OtpRequest, ResetPassword, TokenOut
from ..security import bearer, create_access_token, decode_token, hash_password, verify_password
from ..services import attendance_public, employee_public, locate, upsert_attendance
from ..utils import now, now_hhmm, save_photo, to_oid, today_str

router = APIRouter(prefix="/api/auth", tags=["auth"])
log = logging.getLogger("auth")


def admin_public(a: dict) -> dict:
    return {
        "id": str(a["_id"]), "name": a["name"], "email": a["email"], "site": a.get("site"),
        "is_super": a.get("is_super", False), "status": a.get("status"),
    }


def _sha(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


# ------------------------------------------------------------------ Admin
@router.post("/admin/login", response_model=TokenOut)
async def admin_login(body: AdminLogin):
    admin = await get_db().admins.find_one({"email": body.email.lower()})
    if not admin or not verify_password(body.password, admin["password_hash"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")
    if admin.get("status") == "pending":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Your admin access request is awaiting approval")
    if admin.get("status") != "active":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Your admin account has been disabled")
    await get_db().admins.update_one({"_id": admin["_id"]}, {"$set": {"last_login": now()}})
    token, exp = create_access_token(str(admin["_id"]), "admin")
    return TokenOut(access_token=token, expires_in=exp, role="admin", user=admin_public(admin))


@router.post("/admin/signup", status_code=201)
async def admin_signup(body: AdminSignup):
    try:
        await get_db().admins.insert_one({
            "name": body.name.strip(),
            "email": body.email.lower(),
            "site": body.site,
            "password_hash": hash_password(body.password),
            "status": "pending",
            "is_super": False,
            "created_at": now(),
        })
    except DuplicateKeyError:
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists")
    return {"message": f"Access request submitted for {body.name}. The super admin will approve it shortly."}


@router.post("/forgot-password")
async def forgot_password(body: ForgotPassword):
    admin = await get_db().admins.find_one({"email": body.email.lower(), "status": "active"})
    resp = {"message": f"If {body.email} is registered, password recovery instructions have been sent."}
    if admin:
        token = secrets.token_urlsafe(32)
        await get_db().password_resets.insert_one({
            "admin_id": admin["_id"], "token_hash": _sha(token), "expires_at": now() + timedelta(minutes=30),
        })
        # No mail server configured: log the link. Wire an SMTP/SES sender here in production.
        link = f"{settings.cors_origins[0]}/reset-password?token={token}"
        log.warning("Password reset link for %s: %s", admin["email"], link)
        if settings.DEMO_MODE:
            resp["demo_reset_link"] = link
    return resp


@router.post("/reset-password")
async def reset_password(body: ResetPassword):
    rec = await get_db().password_resets.find_one({"token_hash": _sha(body.token)})
    if not rec or rec["expires_at"] < now():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Reset link is invalid or has expired")
    await get_db().admins.update_one({"_id": rec["admin_id"]}, {"$set": {"password_hash": hash_password(body.new_password)}})
    await get_db().password_resets.delete_many({"admin_id": rec["admin_id"]})
    return {"message": "Password updated. You can now sign in."}


# ------------------------------------------------------------------ Employee
async def _find_employee(pp_number: str, mobile: str) -> dict:
    emp = await get_db().employees.find_one({"pp_number": pp_number, "is_active": True})
    if not emp or emp.get("mobile") != mobile:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "PP Number and mobile number do not match our records")
    return emp


@router.post("/employee/otp")
async def employee_send_otp(body: OtpRequest):
    emp = await _find_employee(body.pp_number, body.mobile)
    db = get_db()
    recent = await db.otps.find_one({"employee_id": emp["_id"]})
    if recent and (now() - recent["created_at"]).total_seconds() < 30:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Please wait 30 seconds before requesting another OTP")
    code = f"{secrets.randbelow(900000) + 100000}"
    await db.otps.delete_many({"employee_id": emp["_id"]})
    await db.otps.insert_one({
        "employee_id": emp["_id"], "code_hash": _sha(code), "attempts": 0,
        "created_at": now(), "expires_at": now() + timedelta(minutes=settings.OTP_EXPIRE_MINUTES),
    })
    # Plug an SMS gateway (MSG91 / Twilio / etc.) in here for production.
    resp = {"message": f"OTP sent to +91 {body.mobile[:2]}******{body.mobile[-2:]}", "expires_in": settings.OTP_EXPIRE_MINUTES * 60}
    if settings.DEMO_MODE:
        resp["demo_otp"] = code
    return resp


@router.post("/employee/login", response_model=TokenOut)
async def employee_login(body: EmployeeLogin):
    db = get_db()
    emp = await _find_employee(body.pp_number, body.mobile)
    if " ".join(emp["name"].lower().split()) != " ".join(body.name.lower().split()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Employee name does not match the PP Number")

    otp = await db.otps.find_one({"employee_id": emp["_id"]})
    if not otp or otp["expires_at"] < now():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "OTP expired. Please request a new one")
    if otp["attempts"] >= 5:
        await db.otps.delete_one({"_id": otp["_id"]})
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many wrong attempts. Request a new OTP")
    if otp["code_hash"] != _sha(body.otp):
        await db.otps.update_one({"_id": otp["_id"]}, {"$inc": {"attempts": 1}})
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Incorrect OTP")
    await db.otps.delete_one({"_id": otp["_id"]})

    selfie_url = save_photo(body.selfie, f"selfie_{emp['emp_code']}")
    await db.employees.update_one(
        {"_id": emp["_id"]}, {"$set": {"photo_url": selfie_url, "last_login": now(), "last_site": body.site}}
    )
    emp["photo_url"] = selfie_url

    # Login doubles as the day's punch-in (if not already punched in)
    day = today_str()
    rec = await db.attendance.find_one({"employee_id": emp["_id"], "date": day})
    if not rec or rec.get("status") == "A" or not rec.get("check_in"):
        loc = await locate(emp["site"], body.location.latitude, body.location.longitude, body.location.accuracy)
        rec = await upsert_attendance(
            emp, day, "P", check_in=now_hhmm(), source="self", by=emp["name"],
            selfie_in=selfie_url, location_in=loc, keep_times=False,
        )

    token, exp = create_access_token(str(emp["_id"]), "employee")
    user = employee_public(emp) | {"today": attendance_public(rec)}
    return TokenOut(access_token=token, expires_in=exp, role="employee", user=user)


# ------------------------------------------------------------------ Session
@router.get("/me")
async def me(creds: HTTPAuthorizationCredentials | None = Depends(bearer)):
    payload = decode_token(creds)
    if payload["role"] == "admin":
        a = await get_db().admins.find_one({"_id": to_oid(payload["sub"]), "status": "active"})
        if not a:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session no longer valid")
        return {"role": "admin", "user": admin_public(a)}
    e = await get_db().employees.find_one({"_id": to_oid(payload["sub"]), "is_active": True})
    if not e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session no longer valid")
    return {"role": "employee", "user": employee_public(e)}


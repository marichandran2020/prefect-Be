from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings
from .database import get_db
from .utils import to_oid

bearer = HTTPBearer(auto_error=False)
Role = Literal["admin", "employee"]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        return False


def create_access_token(subject: str, role: Role) -> tuple[str, int]:
    expires_in = settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    payload = {
        "sub": subject,
        "role": role,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(seconds=expires_in),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM), expires_in


def _unauthorized(detail: str = "Not authenticated") -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"})


def decode_token(creds: HTTPAuthorizationCredentials | None) -> dict:
    if creds is None or creds.scheme.lower() != "bearer":
        raise _unauthorized()
    try:
        return jwt.decode(creds.credentials, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise _unauthorized("Session expired, please log in again")
    except jwt.InvalidTokenError:
        raise _unauthorized("Invalid token")


async def get_current_admin(creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> dict:
    payload = decode_token(creds)
    if payload.get("role") != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin access required")
    admin = await get_db().admins.find_one({"_id": to_oid(payload["sub"])})
    if not admin or admin.get("status") != "active":
        raise _unauthorized("Admin account is not active")
    return admin


async def get_current_employee(creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> dict:
    payload = decode_token(creds)
    if payload.get("role") != "employee":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Employee access required")
    emp = await get_db().employees.find_one({"_id": to_oid(payload["sub"])})
    if not emp or not emp.get("is_active", True):
        raise _unauthorized("Employee account is not active")
    return emp


async def get_super_admin(admin: Annotated[dict, Depends(get_current_admin)]) -> dict:
    if not admin.get("is_super"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the super admin can perform this action")
    return admin


CurrentAdmin = Annotated[dict, Depends(get_current_admin)]
CurrentEmployee = Annotated[dict, Depends(get_current_employee)]
SuperAdmin = Annotated[dict, Depends(get_super_admin)]

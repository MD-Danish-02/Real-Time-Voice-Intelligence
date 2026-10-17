
from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.security import hash_password
from app.db.session import get_async_db
from app.models.enums import UserRole
from app.models.user import User
from app.schemas.auth import UserOut
from app.security.rbac import require_admin, require_any_role
from app.security.tenant import RequestContext

router = APIRouter(prefix="/users", tags=["users"])


class InviteUser(BaseModel):
    email: EmailStr
    full_name: str
    password: str = Field(min_length=8)
    role: str = UserRole.MANAGER.value


@router.get("", response_model=list[UserOut])
async def list_users(ctx: RequestContext = Depends(require_any_role), db: AsyncSession = Depends(get_async_db)):
    return (await db.execute(select(User).where(User.tenant_id == ctx.tenant_id))).scalars().all()


@router.post("", response_model=UserOut)
async def create_user(payload: InviteUser, ctx: RequestContext = Depends(require_admin),
                      db: AsyncSession = Depends(get_async_db)):
    if payload.role not in {r.value for r in UserRole} or payload.role == UserRole.OWNER.value:
        raise AppError("INVALID_ROLE", "Invalid role.", status_code=400)
    if (await db.execute(select(User).where(User.email == payload.email))).scalar_one_or_none():
        raise AppError("EMAIL_ALREADY_REGISTERED", "Email already registered.", status_code=409)
    user = User(tenant_id=ctx.tenant_id, email=payload.email, full_name=payload.full_name,
                hashed_password=hash_password(payload.password), role=payload.role)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user

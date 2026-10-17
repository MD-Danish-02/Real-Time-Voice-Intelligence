from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_async_db
from app.schemas.analytics import AnalyticsOverview
from app.security.rbac import require_any_role
from app.security.tenant import RequestContext
from app.services import analytics_service

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/overview", response_model=AnalyticsOverview)
async def overview(ctx: RequestContext = Depends(require_any_role), db: AsyncSession = Depends(get_async_db)):
    return await analytics_service.get_overview(db, ctx.tenant_id)

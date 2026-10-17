"""Role-based access control. Roles are hierarchical: owner > admin > manager > agent_viewer."""
from fastapi import Depends, HTTPException, status

from app.models.enums import UserRole
from app.security.tenant import RequestContext, get_request_context

_ROLE_RANK = {
    UserRole.AGENT_VIEWER.value: 0,
    UserRole.MANAGER.value: 1,
    UserRole.ADMIN.value: 2,
    UserRole.OWNER.value: 3,
}


def require_role(minimum_role: str):
    def _dependency(ctx: RequestContext = Depends(get_request_context)) -> RequestContext:
        if _ROLE_RANK.get(ctx.role, -1) < _ROLE_RANK.get(minimum_role, 99):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={
                "error": {"code": "INSUFFICIENT_ROLE",
                          "message": f"This action requires the '{minimum_role}' role or higher."}
            })
        return ctx
    return _dependency


require_owner = require_role(UserRole.OWNER.value)
require_admin = require_role(UserRole.ADMIN.value)
require_manager = require_role(UserRole.MANAGER.value)
require_any_role = require_role(UserRole.AGENT_VIEWER.value)

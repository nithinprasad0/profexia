from fastapi import Depends, HTTPException, status
from app.api.v1.users import get_current_user
from app.domain.models import User


def require_roles(*allowed_roles: str):
    async def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role.name not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )
        return user
    return dependency

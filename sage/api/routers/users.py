"""POST /sage_vaults/{vault_id}/users -- register_user (BH-011)."""

from fastapi import APIRouter, Depends
from starlette.responses import JSONResponse

from sage.api.dependencies import get_user_service, get_vault_id
from sage.models.schemas import RegisterUserRequest, User
from sage.services.user_service import UserService

router = APIRouter(tags=["Access Control"])


@router.post("/users", response_model=User, status_code=201)
async def register_user(
    request: RegisterUserRequest,
    vault_id: str = Depends(get_vault_id),
    user_service: UserService = Depends(get_user_service),
) -> User:
    return await user_service.register_user(request)

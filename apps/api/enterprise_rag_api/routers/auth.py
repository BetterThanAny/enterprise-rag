from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.security import OAuth2PasswordRequestForm

from enterprise_rag_api.dependencies import DatabaseSession, get_settings
from enterprise_rag_core.config import Settings
from enterprise_rag_core.repositories import IdentityRepository
from enterprise_rag_core.schemas import ErrorResponse, TokenResponse
from enterprise_rag_core.services import AuthenticationService

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post(
    "/login",
    response_model=TokenResponse,
    responses={401: {"model": ErrorResponse}},
)
async def login(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    settings: Annotated[Settings, Depends(get_settings)],
    session: DatabaseSession,
) -> TokenResponse:
    service = AuthenticationService(IdentityRepository(session), settings)
    token = await service.login(form.username, form.password)
    return TokenResponse(
        access_token=token,
        expires_in=settings.access_token_expire_minutes * 60,
    )

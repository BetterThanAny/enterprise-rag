from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import SecretStr

from enterprise_rag_core.config import Settings
from enterprise_rag_core.errors import AuthenticationError
from enterprise_rag_core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


def unit_settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://unused@localhost/unused",
        redis_url="redis://localhost:6379/0",
        minio_endpoint="localhost:9000",
        minio_access_key="unused-access",
        minio_secret_key=SecretStr("unused-secret"),
        minio_bucket="unused",
        jwt_secret=SecretStr("unit-only-" + ("x" * 40)),
    )


def test_password_hash_round_trip_and_wrong_password() -> None:
    encoded = hash_password("correct horse battery staple")

    assert encoded != "correct horse battery staple"
    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("wrong password", encoded)


def test_access_token_round_trip() -> None:
    user_id = uuid4()
    settings = unit_settings()

    token = create_access_token(user_id, settings)

    assert decode_access_token(token, settings) == user_id


@pytest.mark.parametrize("token", ["", "not-a-jwt", "a.b.c"])
def test_malformed_access_token_is_rejected(token: str) -> None:
    with pytest.raises(AuthenticationError, match="Invalid or expired access token"):
        decode_access_token(token, unit_settings())


def test_expired_access_token_is_rejected() -> None:
    token = create_access_token(uuid4(), unit_settings(), expires_delta=timedelta(seconds=-1))

    with pytest.raises(AuthenticationError, match="Invalid or expired access token"):
        decode_access_token(token, unit_settings())

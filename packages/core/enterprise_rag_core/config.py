from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore")

    app_name: str = "enterprise-rag-api"
    app_version: str = "0.1.0"
    environment: str = "development"
    log_level: str = "INFO"
    git_sha: str = "unknown"

    database_url: str
    database_pool_size: int = Field(default=20, ge=1, le=100)
    database_max_overflow: int = Field(default=10, ge=0, le=100)
    database_pool_timeout_seconds: float = Field(default=10, gt=0, le=120)
    redis_url: str
    minio_endpoint: str
    minio_access_key: str
    minio_secret_key: SecretStr
    minio_secure: bool = False
    minio_bucket: str = "enterprise-rag-documents"

    max_upload_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    chunk_size_chars: int = Field(default=800, ge=32, le=8000)
    chunk_overlap_chars: int = Field(default=100, ge=0, le=2000)
    embedding_dimensions: Literal[16] = 16
    embedding_model_version: str = "deterministic-sha256-v1"
    index_job_max_attempts: int = Field(default=4, ge=1, le=20)
    index_job_lease_seconds: int = Field(default=30, ge=1, le=3600)
    index_retry_base_seconds: int = Field(default=2, ge=1, le=300)
    index_retry_max_seconds: int = Field(default=60, ge=1, le=3600)
    indexing_fault_pause_stage: str | None = None
    indexing_fault_signal_path: str | None = None

    retrieval_rrf_rank_constant: int = Field(default=60, ge=1, le=1000)
    retrieval_config_version: str = "postgres-fts-pgvector-rrf-v1"
    reranker_provider: Literal["deterministic", "flashrank"] = "deterministic"
    reranker_model_name: str = "ms-marco-TinyBERT-L-2-v2"
    reranker_cache_dir: str = "~/.cache/enterprise-rag/flashrank"
    reranker_max_length: int = Field(default=128, ge=32, le=2048)

    generation_provider: Literal["deterministic", "openai", "deepseek", "ollama"] = (
        "deterministic"
    )
    generation_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    generation_prompt_version: str = "grounded-citations-v1"
    generation_config_version: str = "openai-compatible-chat-v1"
    generation_dataset_version: str | None = None
    generation_dense_evidence_threshold: float = Field(default=0.8, ge=-1, le=1)
    provider_max_attempts: int = Field(default=3, ge=1, le=10)
    provider_retry_base_seconds: float = Field(default=0.25, ge=0, le=30)
    provider_retry_max_seconds: float = Field(default=5.0, ge=0, le=120)
    openai_input_cost_per_million_usd: float = Field(default=0, ge=0)
    openai_output_cost_per_million_usd: float = Field(default=0, ge=0)
    deepseek_input_cost_per_million_usd: float = Field(default=0, ge=0)
    deepseek_output_cost_per_million_usd: float = Field(default=0, ge=0)
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-5-mini"
    openai_api_key: SecretStr | None = None
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-chat"
    deepseek_api_key: SecretStr | None = None
    ollama_base_url: str = "http://host.docker.internal:11434/v1"
    ollama_model: str = "qwen3:8b"

    otel_exporter_otlp_endpoint: str | None = None

    jwt_secret: SecretStr
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "enterprise-rag"
    jwt_audience: str = "enterprise-rag-api"
    access_token_expire_minutes: int = Field(default=30, ge=1, le=1440)

    @field_validator("jwt_secret")
    @classmethod
    def validate_jwt_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("JWT_SECRET must contain at least 32 characters")
        return value

    @field_validator("openai_api_key", "deepseek_api_key", mode="before")
    @classmethod
    def empty_provider_keys_are_unset(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        if not value.startswith("postgresql+psycopg://"):
            raise ValueError("DATABASE_URL must use postgresql+psycopg://")
        return value

    @model_validator(mode="after")
    def validate_indexing_settings(self) -> Settings:
        if self.chunk_overlap_chars >= self.chunk_size_chars:
            raise ValueError("CHUNK_OVERLAP_CHARS must be smaller than CHUNK_SIZE_CHARS")
        if self.index_retry_base_seconds > self.index_retry_max_seconds:
            raise ValueError("INDEX_RETRY_BASE_SECONDS must not exceed INDEX_RETRY_MAX_SECONDS")
        if self.provider_retry_base_seconds > self.provider_retry_max_seconds:
            raise ValueError(
                "PROVIDER_RETRY_BASE_SECONDS must not exceed PROVIDER_RETRY_MAX_SECONDS"
            )
        return self

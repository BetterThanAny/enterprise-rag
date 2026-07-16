from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
import urllib.parse

from smoke_test import main as run_smoke


def resolve_docker() -> str:
    executable = shutil.which("docker")
    if executable is None:
        raise RuntimeError("docker executable is required for the fresh-stack demo")
    return executable


DOCKER = resolve_docker()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Start an isolated Compose stack with ephemeral demo secrets and run E2E smoke"
    )
    parser.add_argument("--project", default=f"enterprise-rag-demo-{secrets.token_hex(4)}")
    parser.add_argument("--postgres-port", type=int, default=25432)
    parser.add_argument("--redis-port", type=int, default=26379)
    parser.add_argument("--minio-port", type=int, default=29000)
    parser.add_argument("--minio-console-port", type=int, default=29001)
    parser.add_argument("--api-port", type=int, default=28000)
    arguments = parser.parse_args()
    password = secrets.token_urlsafe(32)
    encoded_password = urllib.parse.quote(password, safe="")
    environment = {
        **os.environ,
        "POSTGRES_DB": "enterprise_rag",
        "POSTGRES_USER": "enterprise_rag",
        "POSTGRES_PASSWORD": password,
        "POSTGRES_PORT": str(arguments.postgres_port),
        "DATABASE_URL": (
            "postgresql+psycopg://enterprise_rag:"
            f"{encoded_password}@postgres:5432/enterprise_rag"
        ),
        "REDIS_PORT": str(arguments.redis_port),
        "MINIO_PORT": str(arguments.minio_port),
        "MINIO_CONSOLE_PORT": str(arguments.minio_console_port),
        "MINIO_ACCESS_KEY": f"demo-{secrets.token_hex(8)}",
        "MINIO_SECRET_KEY": secrets.token_urlsafe(32),
        "MINIO_BUCKET": "enterprise-rag-documents",
        "JWT_SECRET": secrets.token_urlsafe(48),
        "API_PORT": str(arguments.api_port),
        "GENERATION_PROVIDER": "deterministic",
        "RERANKER_PROVIDER": "deterministic",
    }
    subprocess.run(  # noqa: S603 -- fixed Compose command scoped to the requested project
        [
            DOCKER,
            "compose",
            "-p",
            arguments.project,
            "up",
            "-d",
            "--build",
            "--wait",
        ],
        check=True,
        env=environment,
    )
    os.environ.update(
        environment
        | {
            "DATABASE_URL": (
                "postgresql+psycopg://enterprise_rag:"
                f"{encoded_password}@127.0.0.1:{arguments.postgres_port}/enterprise_rag"
            ),
            "API_URL": f"http://127.0.0.1:{arguments.api_port}",
            "REDIS_URL": f"redis://127.0.0.1:{arguments.redis_port}/0",
            "MINIO_ENDPOINT": f"127.0.0.1:{arguments.minio_port}",
        }
    )
    run_smoke()
    print(
        f"Fresh-stack demo passed for Compose project {arguments.project}. "
        "Containers and isolated volumes were intentionally left intact for inspection."
    )


if __name__ == "__main__":
    main()

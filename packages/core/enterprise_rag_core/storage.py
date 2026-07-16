from __future__ import annotations

import asyncio
from io import BytesIO

from minio import Minio
from minio.error import S3Error

from enterprise_rag_core.config import Settings


class MinioObjectStorage:
    def __init__(self, settings: Settings) -> None:
        self.bucket = settings.minio_bucket
        self.client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key.get_secret_value(),
            secure=settings.minio_secure,
        )

    async def put_if_absent(
        self,
        object_key: str,
        content: bytes,
        *,
        checksum: str,
        content_type: str,
    ) -> bool:
        if await self.exists(object_key):
            return False

        def put() -> None:
            self.client.put_object(
                self.bucket,
                object_key,
                BytesIO(content),
                length=len(content),
                content_type=content_type,
                metadata={"sha256": checksum},
            )

        await asyncio.to_thread(put)
        return True

    async def exists(self, object_key: str) -> bool:
        try:
            await asyncio.to_thread(self.client.stat_object, self.bucket, object_key)
            return True
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject", "NoSuchBucket", "XMinioInvalidObjectName"}:
                return False
            raise

    async def read(self, object_key: str) -> bytes:
        def get() -> bytes:
            response = self.client.get_object(self.bucket, object_key)
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()

        return await asyncio.to_thread(get)

    async def remove(self, object_key: str) -> None:
        await asyncio.to_thread(self.client.remove_object, self.bucket, object_key)

    async def remove_many(self, object_keys: list[str]) -> None:
        for object_key in object_keys:
            await self.remove(object_key)

    async def list_keys(self, prefix: str = "") -> list[str]:
        def collect() -> list[str]:
            return [
                item.object_name
                for item in self.client.list_objects(self.bucket, prefix=prefix, recursive=True)
                if item.object_name is not None
            ]

        return await asyncio.to_thread(collect)

    async def remove_prefix(self, prefix: str) -> int:
        keys = await self.list_keys(prefix)
        await self.remove_many(keys)
        return len(keys)

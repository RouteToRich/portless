"""Durable storage for raw uploaded knowledge files.

The bytes of every file uploaded through ``POST /rag/upload`` are persisted here
so they survive process/container restarts and can be re-ingested or audited.

Backends are pluggable via ``RAG_FILE_STORE_BACKEND``:

  • ``mongodb`` (default) — bytes stored in MongoDB GridFS. No external infra.
  • ``s3``                — Amazon S3 / S3-compatible object store (future).

``get_file_store()`` returns the configured implementation. To add S3 later,
fill in :class:`S3FileStore` and set ``RAG_FILE_STORE_BACKEND=s3`` plus the
``RAG_S3_*`` settings — no caller changes required, because every backend
implements the :class:`FileStore` Protocol.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from .config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Common types
# ---------------------------------------------------------------------------

class StoredFile(BaseModel):
    """Reference to a persisted file, returned by every FileStore backend."""

    file_id: str
    filename: str
    size: int
    backend: str
    content_type: str | None = None
    uploaded_at: str | None = None


@runtime_checkable
class FileStore(Protocol):
    """Backend-agnostic interface for persisting raw uploaded files."""

    backend: str

    async def save(
        self,
        filename: str,
        data: bytes,
        content_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> StoredFile: ...

    async def read(self, file_id: str) -> bytes: ...

    async def delete(self, file_id: str) -> bool: ...

    async def list(self, limit: int = 100) -> list[StoredFile]: ...


# ---------------------------------------------------------------------------
# MongoDB GridFS implementation (default)
# ---------------------------------------------------------------------------

class MongoGridFSFileStore:
    """Persist file bytes in MongoDB via GridFS.

    GridFS chunks large files automatically, so this handles documents above
    the 16 MB BSON limit. Files live in ``<bucket>.files`` / ``<bucket>.chunks``
    in the RAG database.
    """

    backend = "mongodb"

    def __init__(self) -> None:
        self._bucket_name = settings.file_store_gridfs_bucket

    async def _bucket(self) -> Any:
        from motor.motor_asyncio import AsyncIOMotorGridFSBucket
        from shared.llm_gateway.mongo_connection import get_connection

        conn = await get_connection(uri=settings.mongo_uri, db_name=settings.mongo_db_name)
        db = conn.get_database()
        return AsyncIOMotorGridFSBucket(db, bucket_name=self._bucket_name)

    async def save(
        self,
        filename: str,
        data: bytes,
        content_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> StoredFile:
        bucket = await self._bucket()
        meta = {"content_type": content_type, **(metadata or {})}
        file_id = await bucket.upload_from_stream(filename, data, metadata=meta)
        logger.info(
            "FileStore(mongodb): stored %r (%d bytes) as GridFS id %s",
            filename, len(data), file_id,
        )
        return StoredFile(
            file_id=str(file_id),
            filename=filename,
            size=len(data),
            backend=self.backend,
            content_type=content_type,
        )

    async def read(self, file_id: str) -> bytes:
        from bson import ObjectId

        bucket = await self._bucket()
        stream = await bucket.open_download_stream(ObjectId(file_id))
        return await stream.read()

    async def delete(self, file_id: str) -> bool:
        from bson import ObjectId

        bucket = await self._bucket()
        try:
            await bucket.delete(ObjectId(file_id))
            return True
        except Exception as exc:  # noqa: BLE001 — unknown id / already gone
            logger.warning("FileStore(mongodb): delete %s failed: %s", file_id, exc)
            return False

    async def list(self, limit: int = 100) -> list[StoredFile]:
        from shared.llm_gateway.mongo_connection import get_connection

        conn = await get_connection(uri=settings.mongo_uri, db_name=settings.mongo_db_name)
        files_col = conn.get_collection(f"{self._bucket_name}.files")
        cursor = files_col.find({}, sort=[("uploadDate", -1)], limit=limit)
        out: list[StoredFile] = []
        async for doc in cursor:
            meta = doc.get("metadata") or {}
            upload_date = doc.get("uploadDate")
            out.append(
                StoredFile(
                    file_id=str(doc["_id"]),
                    filename=doc.get("filename", ""),
                    size=int(doc.get("length", 0)),
                    backend=self.backend,
                    content_type=meta.get("content_type"),
                    uploaded_at=upload_date.isoformat() if upload_date else None,
                )
            )
        return out


# ---------------------------------------------------------------------------
# S3 / blob-store implementation
# ---------------------------------------------------------------------------

class S3FileStore:
    """Amazon S3 / S3-compatible object store (MinIO, Cloudflare R2, …).

    Enable with ``RAG_FILE_STORE_BACKEND=s3`` and set ``RAG_S3_BUCKET`` (+ region /
    endpoint / credentials as needed). ``file_id`` is the object key (minus any
    configured ``s3_prefix``). boto3 is synchronous, so every call is off-loaded to
    a worker thread via ``asyncio.to_thread`` to avoid blocking the event loop.

    Credentials resolve in the usual boto3 order: explicit ``s3_access_key_id`` /
    ``s3_secret_access_key`` if set, otherwise the ambient AWS chain (IAM role,
    ``~/.aws``, env vars).
    """

    backend = "s3"

    def __init__(self) -> None:
        if not settings.s3_bucket:
            raise ValueError(
                "RAG_S3_BUCKET is required for RAG_FILE_STORE_BACKEND=s3."
            )
        try:
            import boto3  # lazy — optional dependency
        except ImportError as exc:  # pragma: no cover - env dependent
            raise ImportError(
                "boto3 is not installed. Run `pip install boto3` to use "
                "RAG_FILE_STORE_BACKEND=s3 (or use the default 'mongodb')."
            ) from exc

        self._bucket = settings.s3_bucket
        self._prefix = (settings.s3_prefix or "").lstrip("/")

        client_kwargs: dict[str, Any] = {}
        if settings.s3_region:
            client_kwargs["region_name"] = settings.s3_region
        if settings.s3_endpoint_url:
            client_kwargs["endpoint_url"] = settings.s3_endpoint_url
        if settings.s3_access_key_id and settings.s3_secret_access_key:
            client_kwargs["aws_access_key_id"] = settings.s3_access_key_id
            client_kwargs["aws_secret_access_key"] = settings.s3_secret_access_key

        self._client = boto3.client("s3", **client_kwargs)
        logger.info("FileStore(s3): bucket=%s prefix=%r", self._bucket, self._prefix)

    def _key(self, file_id: str) -> str:
        return f"{self._prefix}{file_id}" if self._prefix else file_id

    async def save(
        self,
        filename: str,
        data: bytes,
        content_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> StoredFile:
        file_id = f"{uuid.uuid4().hex}-{filename}"
        extra: dict[str, Any] = {}
        if content_type:
            extra["ContentType"] = content_type
        if metadata:
            extra["Metadata"] = {k: str(v) for k, v in metadata.items()}

        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=self._key(file_id),
            Body=data,
            **extra,
        )
        logger.info("FileStore(s3): stored %r (%d bytes) as key %s", filename, len(data), self._key(file_id))
        return StoredFile(
            file_id=file_id,
            filename=filename,
            size=len(data),
            backend=self.backend,
            content_type=content_type,
            uploaded_at=datetime.now(timezone.utc).isoformat(),
        )

    async def read(self, file_id: str) -> bytes:
        resp = await asyncio.to_thread(
            self._client.get_object, Bucket=self._bucket, Key=self._key(file_id)
        )
        return await asyncio.to_thread(resp["Body"].read)

    async def delete(self, file_id: str) -> bool:
        try:
            await asyncio.to_thread(
                self._client.delete_object, Bucket=self._bucket, Key=self._key(file_id)
            )
            return True
        except Exception as exc:  # noqa: BLE001 — unknown key / permission
            logger.warning("FileStore(s3): delete %s failed: %s", file_id, exc)
            return False

    async def list(self, limit: int = 100) -> list[StoredFile]:
        kwargs: dict[str, Any] = {"Bucket": self._bucket, "MaxKeys": limit}
        if self._prefix:
            kwargs["Prefix"] = self._prefix
        resp = await asyncio.to_thread(self._client.list_objects_v2, **kwargs)
        out: list[StoredFile] = []
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            file_id = key[len(self._prefix):] if self._prefix else key
            last_modified = obj.get("LastModified")
            out.append(
                StoredFile(
                    file_id=file_id,
                    filename=file_id.split("-", 1)[-1],
                    size=int(obj.get("Size", 0)),
                    backend=self.backend,
                    uploaded_at=last_modified.isoformat() if last_modified else None,
                )
            )
        return out


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_BACKENDS = {
    "mongodb": MongoGridFSFileStore,
    "s3": S3FileStore,
}


def get_file_store() -> FileStore:
    """Return the configured file-store backend (default: MongoDB GridFS)."""
    backend = (settings.file_store_backend or "mongodb").lower()
    impl = _BACKENDS.get(backend)
    if impl is None:
        raise ValueError(
            f"Unknown RAG_FILE_STORE_BACKEND {backend!r}. "
            f"Supported: {', '.join(_BACKENDS)}."
        )
    return impl()

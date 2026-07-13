"""Bounded file-backed Artifact Store with database metadata and hash verification."""

from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path
from uuid import UUID, uuid4

from agentcell.errors import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactTooLargeError,
)
from agentcell.events import ArtifactReference
from agentcell.storage.database import Database
from agentcell.storage.repositories import ArtifactRepository
from agentcell.tools.artifacts import ArtifactMetadata

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


class FileArtifactStore:
    """Store bytes below one configured root and verify every load."""

    def __init__(
        self,
        database: Database,
        root: Path,
        *,
        max_artifact_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        if max_artifact_bytes < 1:
            raise ValueError("max_artifact_bytes must be positive")
        self._database = database
        self._root = root.expanduser().resolve()
        self._max_artifact_bytes = max_artifact_bytes

    async def save(
        self,
        content: bytes,
        *,
        media_type: str,
        suggested_name: str,
    ) -> ArtifactReference:
        if len(content) > self._max_artifact_bytes:
            raise ArtifactTooLargeError(
                f"Artifact is {len(content)} bytes; limit is {self._max_artifact_bytes}"
            )
        sha256 = hashlib.sha256(content).hexdigest()
        async with self._database.session() as session:
            existing = await ArtifactRepository(session).find_by_hash(sha256, len(content))
        if existing is not None:
            await self._verify(existing)
            return self._reference(existing)

        artifact_id = uuid4()
        safe_name = _SAFE_NAME_RE.sub("-", Path(suggested_name).name).strip(".-")
        if not safe_name:
            safe_name = "artifact.bin"
        storage_key = f"{artifact_id.hex[:2]}/{artifact_id.hex}.blob"
        metadata = ArtifactMetadata(
            id=artifact_id,
            media_type=media_type,
            size_bytes=len(content),
            sha256=sha256,
            storage_key=storage_key,
            suggested_name=safe_name[:255],
        )
        path = self._path(metadata.storage_key)
        await asyncio.to_thread(self._write_atomic, path, content)
        try:
            async with self._database.transaction() as session:
                await ArtifactRepository(session).create(metadata)
        except BaseException:
            await asyncio.to_thread(path.unlink, missing_ok=True)
            raise
        return self._reference(metadata)

    async def load(self, artifact: UUID | ArtifactReference) -> bytes:
        artifact_id = artifact.artifact_id if isinstance(artifact, ArtifactReference) else artifact
        async with self._database.session() as session:
            metadata = await ArtifactRepository(session).get(artifact_id)
        if metadata is None:
            raise ArtifactNotFoundError(str(artifact_id))
        content = await self._verify(metadata)
        if isinstance(artifact, ArtifactReference) and (
            artifact.sha256 != metadata.sha256
            or artifact.size_bytes != metadata.size_bytes
            or artifact.media_type != metadata.media_type
        ):
            raise ArtifactIntegrityError("Artifact reference metadata does not match storage")
        return content

    async def _verify(self, metadata: ArtifactMetadata) -> bytes:
        path = self._path(metadata.storage_key)
        try:
            content = await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError as error:
            raise ArtifactIntegrityError("Artifact content is missing") from error
        if (
            len(content) != metadata.size_bytes
            or hashlib.sha256(content).hexdigest() != metadata.sha256
        ):
            raise ArtifactIntegrityError("Artifact content failed size or SHA-256 verification")
        return content

    def _path(self, storage_key: str) -> Path:
        path = (self._root / storage_key).resolve()
        try:
            path.relative_to(self._root)
        except ValueError as error:
            raise ArtifactIntegrityError("Artifact storage key escapes its root") from error
        return path

    @staticmethod
    def _write_atomic(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        temporary.write_bytes(content)
        temporary.replace(path)

    @staticmethod
    def _reference(metadata: ArtifactMetadata) -> ArtifactReference:
        return ArtifactReference(
            artifact_id=metadata.id,
            media_type=metadata.media_type,
            size_bytes=metadata.size_bytes,
            sha256=metadata.sha256,
        )

"""Raw-evidence object storage. Every upstream response is stored untouched
BEFORE normalization (audit requirement); the database keeps only refs.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from kyc_tool.authority import authorize_external_io


class ObjectTooLarge(RuntimeError):
    """A bounded read (`get_bounded`) refused an object over its byte cap BEFORE materializing it
    (re-audit `750630c..ca85355` F7: production `.read()` had no cap — a huge stored document
    could be pulled whole into a worker)."""


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        """Store bytes under key; returns an opaque ref (e.g. fs://… or s3://…)."""
        ...

    def get(self, ref: str) -> bytes: ...

    def get_bounded(self, ref: str, *, max_bytes: int | None) -> bytes:
        """Read with a byte cap enforced BEFORE full materialization (size probe / bounded read).
        max_bytes None = uncapped (equivalent to get). Raises ObjectTooLarge over the cap."""
        ...

    def delete(self, ref: str) -> None:
        """Best-effort removal of a staged object whose reference never committed (orphan
        cleanup — the pipeline stages raw bytes OUTSIDE the fenced transaction). Idempotent:
        deleting a missing ref is a no-op."""
        ...

    def iter_refs(self) -> Iterator[tuple[str, datetime]]:
        """Yield (ref, last_modified UTC) for every stored object — the staged-evidence sweeper's
        enumeration surface (crash-window orphans have no DB reference to find them by)."""
        ...

    def verify_access(self) -> None:
        """Raise if the backing store is unreachable/misconfigured (for /readyz)."""
        ...


def _key(value: str) -> str:
    """Reject path aliases before either store interprets a document reference."""
    if (
        type(value) is not str
        or not value
        or value.startswith("/")
        or "\\" in value
        or "\x00" in value
        or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        raise ValueError("invalid object key")
    return value


class FsStore:
    """Filesystem store for dev/test. Refs look like fs://relative/key."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.root = self.root.resolve()

    def _path(self, key: str) -> Path:
        path = self.root / _key(key)
        if not path.resolve().is_relative_to(self.root):
            raise ValueError("object reference escapes configured store")
        return path

    def _ref_path(self, ref: str) -> Path:
        if type(ref) is not str or not ref.startswith("fs://"):
            raise ValueError("not an fs ref")
        return self._path(ref.removeprefix("fs://"))

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return f"fs://{key}"

    def get(self, ref: str) -> bytes:
        return self._ref_path(ref).read_bytes()

    def get_bounded(self, ref: str, *, max_bytes: int | None) -> bytes:
        path = self._ref_path(ref)
        # TRANSITIVE authority (PR 10b slice 1): the store proves the ambient claim + deadline
        # ITSELF — a caller that never heard of the authority still cannot read bytes under a
        # lost claim or spent budget. No ambient budget (direct/ops/unit callers) = no-op.
        authorize_external_io()
        if max_bytes is not None and path.stat().st_size > max_bytes:
            raise ObjectTooLarge(
                f"{ref} is {path.stat().st_size} bytes, over the {max_bytes}-byte cap"
            )
        return path.read_bytes()

    def delete(self, ref: str) -> None:
        self._ref_path(ref).unlink(missing_ok=True)

    def iter_refs(self) -> Iterator[tuple[str, datetime]]:
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and path.resolve().is_relative_to(self.root):
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
                yield f"fs://{path.relative_to(self.root).as_posix()}", mtime

    def verify_access(self) -> None:
        if not self.root.is_dir():
            raise RuntimeError(f"object store root is not a directory: {self.root}")


class S3Store:
    """S3-compatible store for production. Requires the 's3' extra (boto3)."""

    def __init__(self, bucket: str) -> None:
        import boto3  # deferred: optional dependency

        self.bucket = bucket
        self.client = boto3.client("s3")

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        _key(key)
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)
        return f"s3://{self.bucket}/{key}"

    def _ref_key(self, ref: str) -> str:
        if type(ref) is not str or not ref.startswith("s3://"):
            raise ValueError("not an s3 ref")
        bucket, separator, key = ref.removeprefix("s3://").partition("/")
        if not separator or bucket != self.bucket:
            raise ValueError("object reference selects another bucket")
        return _key(key)

    def get(self, ref: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=self._ref_key(ref))["Body"].read()

    def get_bounded(self, ref: str, *, max_bytes: int | None) -> bytes:
        key = self._ref_key(ref)
        authorize_external_io()  # transitive authority — see FsStore.get_bounded
        body = self.client.get_object(Bucket=self.bucket, Key=key)["Body"]
        if max_bytes is None:
            return body.read()
        data = body.read(max_bytes + 1)  # bounded read: never materialize past cap+1
        if len(data) > max_bytes:
            body.close()
            raise ObjectTooLarge(f"{ref} exceeds the {max_bytes}-byte cap")
        return data

    def iter_refs(self) -> Iterator[tuple[str, datetime]]:
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket):
            for obj in page.get("Contents", []):
                yield f"s3://{self.bucket}/{obj['Key']}", obj["LastModified"]

    def delete(self, ref: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._ref_key(ref))  # idempotent in S3

    def verify_access(self) -> None:
        self.client.head_bucket(Bucket=self.bucket)


def make_object_store(kind: str, *, fs_root: Path, s3_bucket: str = "") -> ObjectStore:
    if kind == "fs":
        return FsStore(fs_root)
    if kind == "s3":
        return S3Store(s3_bucket)
    raise ValueError(f"unknown object store kind: {kind}")

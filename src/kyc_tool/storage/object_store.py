"""Raw-evidence object storage. Every upstream response is stored untouched
BEFORE normalization (audit requirement); the database keeps only refs.
"""

from pathlib import Path
from typing import Protocol


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        """Store bytes under key; returns an opaque ref (e.g. fs://… or s3://…)."""
        ...

    def get(self, ref: str) -> bytes: ...

    def verify_access(self) -> None:
        """Raise if the backing store is unreachable/misconfigured (for /readyz)."""
        ...


class FsStore:
    """Filesystem store for dev/test. Refs look like fs://relative/key."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return f"fs://{key}"

    def get(self, ref: str) -> bytes:
        if not ref.startswith("fs://"):
            raise ValueError(f"not an fs ref: {ref}")
        return (self.root / ref.removeprefix("fs://")).read_bytes()

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
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)
        return f"s3://{self.bucket}/{key}"

    def get(self, ref: str) -> bytes:
        if not ref.startswith("s3://"):
            raise ValueError(f"not an s3 ref: {ref}")
        _, _, rest = ref.partition("s3://")
        bucket, _, key = rest.partition("/")
        return self.client.get_object(Bucket=bucket, Key=key)["Body"].read()

    def verify_access(self) -> None:
        self.client.head_bucket(Bucket=self.bucket)


def make_object_store(kind: str, *, fs_root: Path, s3_bucket: str = "") -> ObjectStore:
    if kind == "fs":
        return FsStore(fs_root)
    if kind == "s3":
        return S3Store(s3_bucket)
    raise ValueError(f"unknown object store kind: {kind}")

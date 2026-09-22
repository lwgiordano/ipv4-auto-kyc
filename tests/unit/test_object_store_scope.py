"""Document references cannot escape the store configured for this process."""

from io import BytesIO

import pytest

from kyc_tool.storage.object_store import FsStore, S3Store


@pytest.mark.parametrize("method", ("get", "get_bounded", "delete"))
def test_filesystem_ref_cannot_reach_parent(tmp_path, method):
    outside = tmp_path / "private"
    outside.write_bytes(b"secret")
    store = FsStore(tmp_path / "evidence")
    with pytest.raises(ValueError):
        if method == "get_bounded":
            store.get_bounded("fs://../private", max_bytes=100)
        else:
            getattr(store, method)("fs://../private")
    assert outside.read_bytes() == b"secret"


def test_filesystem_put_cannot_escape_root(tmp_path):
    store = FsStore(tmp_path / "evidence")
    with pytest.raises(ValueError):
        store.put("../private", b"secret")
    assert not (tmp_path / "private").exists()


@pytest.mark.parametrize("method", ("get", "get_bounded", "delete"))
def test_filesystem_ref_cannot_follow_symlink_outside(tmp_path, method):
    outside = tmp_path / "private"
    outside.write_bytes(b"secret")
    store = FsStore(tmp_path / "evidence")
    (store.root / "link").symlink_to(outside)
    with pytest.raises(ValueError):
        if method == "get_bounded":
            store.get_bounded("fs://link", max_bytes=100)
        else:
            getattr(store, method)("fs://link")
    assert outside.read_bytes() == b"secret"


def test_filesystem_normal_nested_ref_still_works(tmp_path):
    store = FsStore(tmp_path / "evidence")
    ref = store.put("documents/one.json", b"data")
    assert store.get(ref) == b"data"
    assert store.get_bounded(ref, max_bytes=4) == b"data"
    store.delete(ref)
    assert not (store.root / "documents" / "one.json").exists()


class _Client:
    def __init__(self):
        self.calls = []

    def get_object(self, **kwargs):
        self.calls.append(("get", kwargs))
        return {"Body": BytesIO(b"data")}

    def delete_object(self, **kwargs):
        self.calls.append(("delete", kwargs))


@pytest.mark.parametrize("method", ("get", "get_bounded", "delete"))
def test_s3_ref_cannot_select_other_bucket(method):
    store = object.__new__(S3Store)
    store.bucket = "approved"
    store.client = _Client()
    with pytest.raises(ValueError):
        if method == "get_bounded":
            store.get_bounded("s3://other/document", max_bytes=100)
        else:
            getattr(store, method)("s3://other/document")
    assert store.client.calls == []


def test_s3_approved_bucket_still_works():
    store = object.__new__(S3Store)
    store.bucket = "approved"
    store.client = _Client()
    assert store.get_bounded("s3://approved/document", max_bytes=4) == b"data"
    assert store.client.calls == [("get", {"Bucket": "approved", "Key": "document"})]

import hashlib
from pathlib import Path

from pptextract.object_store import LocalObjectStore


def test_binary_is_published_by_sha256_with_integrity_check(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path / "objects")
    payload = b"PPTExtract content-addressed object"

    stored = store.put(payload)

    expected_sha256 = hashlib.sha256(payload).hexdigest()
    assert stored.sha256 == expected_sha256
    assert stored.size_bytes == len(payload)
    assert stored.path == tmp_path / "objects" / expected_sha256[:2] / expected_sha256
    assert stored.path.read_bytes() == payload
    assert store.verify(expected_sha256) is True
    assert list(store.staging_root.glob("*.partial")) == []


def test_repeated_write_reuses_immutable_object(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path / "objects")

    first = store.put(b"same bytes")
    second = store.put(b"same bytes")

    assert second == first
    assert list(store.root.rglob(first.sha256)) == [first.path]


def test_writable_probe_is_synced_and_removed(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path / "objects")

    store.check_writable()

    assert list(store.staging_root.iterdir()) == []

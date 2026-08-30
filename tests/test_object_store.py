import hashlib
import multiprocessing
import os
from io import BytesIO
from pathlib import Path

import pytest

from pptextract.object_store import LocalObjectStore, ObjectTooLargeError


def _interrupt_atomic_publish(root: str, payload: bytes, *, after_rename: bool) -> None:
    import pptextract.object_store as object_store_module

    original_replace = object_store_module.os.replace

    def interrupt(source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        if source_path.parent.name == ".staging":
            if after_rename:
                original_replace(source, destination)
            os._exit(92 if after_rename else 91)
        original_replace(source, destination)

    object_store_module.os.replace = interrupt
    LocalObjectStore(Path(root)).put(payload)


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
    assert list(store.root.glob(f"[0-9a-f][0-9a-f]/{first.sha256}")) == [first.path]


def test_writable_probe_is_synced_and_removed(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path / "objects")

    store.check_writable()

    assert list(store.staging_root.iterdir()) == []


def test_oversized_stream_is_not_published(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path / "objects")

    with pytest.raises(ObjectTooLargeError):
        store.put_stream(BytesIO(b"12345"), max_bytes=4)

    assert list(store.staging_root.iterdir()) == []
    assert [
        path for path in store.root.glob("[0-9a-f][0-9a-f]/*") if path.is_file()
    ] == []


@pytest.mark.product_fault
@pytest.mark.parametrize(
    ("after_rename", "exit_code"),
    ((False, 91), (True, 92)),
)
def test_process_interruption_around_atomic_rename_exposes_no_partial_object(
    tmp_path: Path,
    after_rename: bool,
    exit_code: int,
) -> None:
    payload = b"atomic publication survives a process interruption"
    sha256 = hashlib.sha256(payload).hexdigest()
    object_root = tmp_path / ("after" if after_rename else "before")
    process = multiprocessing.get_context("fork").Process(
        target=_interrupt_atomic_publish,
        args=(str(object_root), payload),
        kwargs={"after_rename": after_rename},
    )

    process.start()
    process.join(timeout=10)

    assert process.exitcode == exit_code
    store = LocalObjectStore(object_root)
    destination = store.path_for(sha256)
    if after_rename:
        assert destination.read_bytes() == payload
        assert store.verify(sha256) is True
    else:
        assert not destination.exists()

    retried = store.put(payload)
    assert retried.path.read_bytes() == payload
    assert store.verify(sha256) is True

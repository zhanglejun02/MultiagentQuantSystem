from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from threading import Barrier

import pytest

from quant_system.artifacts import store as store_module
from quant_system.artifacts.envelope import hash_bytes
from quant_system.artifacts.store import LocalArtifactStore
from quant_system.domain.exceptions import ImmutabilityError


def _synchronize_publications(monkeypatch: pytest.MonkeyPatch, writers: int) -> None:
    original_link = os.link
    barrier = Barrier(writers)

    def publish(source, destination):
        barrier.wait(timeout=10)
        original_link(source, destination)

    monkeypatch.setattr(os, "link", publish)


def test_concurrent_identical_json_reuses_uri_and_digest(tmp_path, monkeypatch):
    root = tmp_path / "artifacts"
    writers = 8
    obj = {"name": "\u9ec4\u91d1", "as_of": date(2026, 10, 17), "values": [1, 2, 3]}
    data = json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False, separators=(",", ":")).encode()
    digest = hash_bytes(data)
    final = root / "snapshots" / f"{digest}.json"
    _synchronize_publications(monkeypatch, writers)

    def write(index):
        store = LocalArtifactStore(str(root))
        content = obj if index % 2 else dict(reversed(list(obj.items())))
        return store.put_content_addressed_json("snapshots", content)

    with ThreadPoolExecutor(max_workers=writers) as executor:
        results = list(executor.map(write, range(writers)))

    assert results == [(f"file://{final}", digest)] * writers
    assert final.read_bytes() == data
    assert list(final.parent.iterdir()) == [final]


def test_conflicting_put_bytes_has_exactly_one_winner(tmp_path, monkeypatch):
    root = tmp_path / "artifacts"
    payloads = [b"first" * 16384, b"second" * 16384]
    final = root / "runs" / "result.bin"
    _synchronize_publications(monkeypatch, len(payloads))

    def write(data):
        store = LocalArtifactStore(str(root))
        try:
            return store.put_bytes("runs/result.bin", data)
        except ImmutabilityError:
            return None

    with ThreadPoolExecutor(max_workers=len(payloads)) as executor:
        results = list(executor.map(write, payloads))

    winners = [index for index, result in enumerate(results) if result is not None]
    assert len(winners) == 1
    winner = winners[0]
    assert results[winner] == (f"file://{final}", hash_bytes(payloads[winner]))
    assert final.read_bytes() == payloads[winner]
    assert list(final.parent.iterdir()) == [final]


@pytest.mark.parametrize("writer", ["bytes", "json"])
@pytest.mark.parametrize("failure", ["fsync", "link"])
def test_failed_publication_leaves_no_final_or_temporary_file(tmp_path, monkeypatch, writer, failure):
    store = LocalArtifactStore(str(tmp_path / "artifacts"))

    def fail(*args, **kwargs):
        raise OSError("injected publication failure")

    monkeypatch.setattr(os, failure, fail)
    with pytest.raises(OSError, match="injected publication failure"):
        if writer == "bytes":
            store.put_bytes("runs/result.bin", b"complete payload")
        else:
            store.put_content_addressed_json("runs", {"value": 42})

    assert list((store.root / "runs").iterdir()) == []


@pytest.mark.parametrize("writer", ["bytes", "json"])
@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt])
def test_interrupted_partial_write_is_cleaned_up(tmp_path, monkeypatch, writer, failure):
    store = LocalArtifactStore(str(tmp_path / "artifacts"))
    data = b'{"value":42}' if writer == "json" else b"complete payload"
    filename = f"{hash_bytes(data)}.json" if writer == "json" else "result.bin"
    final = store.root / "runs" / filename
    original_temporary_file = store_module.NamedTemporaryFile

    def temporary_file(*args, **kwargs):
        temporary = original_temporary_file(*args, **kwargs)
        original_write = temporary.write

        def fail_write(payload):
            original_write(payload[: len(payload) // 2])
            temporary.flush()
            assert Path(temporary.name).read_bytes() == data[: len(data) // 2]
            assert not final.exists()
            raise failure("injected partial write failure")

        monkeypatch.setattr(temporary, "write", fail_write)
        return temporary

    monkeypatch.setattr(store_module, "NamedTemporaryFile", temporary_file)
    with pytest.raises(failure, match="injected partial write failure"):
        if writer == "bytes":
            store.put_bytes("runs/result.bin", data)
        else:
            store.put_content_addressed_json("runs", {"value": 42})

    assert list(final.parent.iterdir()) == []


def test_directory_sync_failure_leaves_complete_final_and_no_temporary_file(tmp_path, monkeypatch):
    store = LocalArtifactStore(str(tmp_path / "artifacts"))
    data = b"complete payload"
    final = store.root / "result.bin"
    original_fsync = os.fsync

    def sync(descriptor):
        if final.exists():
            raise OSError("injected directory sync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", sync)
    with pytest.raises(OSError, match="injected directory sync failure"):
        store.put_bytes("result.bin", data)

    assert final.read_bytes() == data
    assert list(store.root.iterdir()) == [final]


def test_final_file_is_published_only_after_complete_data_is_synced(tmp_path, monkeypatch):
    store = LocalArtifactStore(str(tmp_path / "artifacts"))
    data = b"complete payload" * 16384
    final = store.root / "result.bin"
    original_fsync = os.fsync
    original_link = os.link
    synced_inodes = []
    publications = []

    def sync(descriptor):
        original_fsync(descriptor)
        synced_inodes.append(os.fstat(descriptor).st_ino)

    def publish(source, destination):
        source = Path(source)
        assert source.parent == final.parent
        assert not final.exists()
        assert source.read_bytes() == data
        assert source.stat().st_ino in synced_inodes
        original_link(source, destination)
        assert final.read_bytes() == data
        publications.append(Path(destination))

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(os, "link", publish)

    assert store.put_bytes("result.bin", data) == (f"file://{final}", hash_bytes(data))
    assert publications == [final]
    assert final.parent.stat().st_ino in synced_inodes
    assert list(store.root.iterdir()) == [final]


def test_put_bytes_rejects_existing_identical_or_conflicting_content(tmp_path):
    store = LocalArtifactStore(str(tmp_path / "artifacts"))
    uri, digest = store.put_bytes("result.bin", b"original")

    for data in (b"original", b"replacement"):
        with pytest.raises(ImmutabilityError, match="already exists"):
            store.put_bytes("result.bin", data)

    assert store.get_bytes(uri) == b"original"
    assert digest == hash_bytes(b"original")
    assert list(store.root.iterdir()) == [store.root / "result.bin"]


def test_content_addressed_json_reuses_existing_content_and_detects_drift(tmp_path):
    store = LocalArtifactStore(str(tmp_path / "artifacts"))
    obj = {"value": 42}
    uri, digest = store.put_content_addressed_json("snapshots", obj)
    final = store.root / "snapshots" / f"{digest}.json"

    assert store.put_content_addressed_json("snapshots", obj) == (uri, digest)
    final.write_bytes(b"corrupted")
    with pytest.raises(ImmutabilityError, match="drifted"):
        store.put_content_addressed_json("snapshots", obj)

    assert final.read_bytes() == b"corrupted"
    assert list(final.parent.iterdir()) == [final]


@pytest.mark.parametrize("writer", ["bytes", "json"])
@pytest.mark.parametrize("absolute", [False, True])
def test_sibling_root_traversal_is_rejected(tmp_path, writer, absolute):
    store = LocalArtifactStore(str(tmp_path / "artifacts"))
    sibling = tmp_path / "artifacts-sibling"
    namespace = str(sibling) if absolute else "../artifacts-sibling"

    with pytest.raises(ValueError, match="escapes store root"):
        if writer == "bytes":
            store.put_bytes(f"{namespace}/result.bin", b"outside")
        else:
            store.put_content_addressed_json(namespace, {"outside": True})

    assert not sibling.exists()


def test_symlink_to_sibling_root_is_rejected(tmp_path):
    store = LocalArtifactStore(str(tmp_path / "artifacts"))
    sibling = tmp_path / "artifacts-sibling"
    sibling.mkdir()
    (store.root / "outside").symlink_to(sibling, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes store root"):
        store.put_bytes("outside/result.bin", b"outside")

    assert list(sibling.iterdir()) == []

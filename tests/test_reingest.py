"""Tester för should_reingest från rag/ingest.py."""

from __future__ import annotations

from ingest import should_reingest


def test_unchanged_file_skipped() -> None:
    assert should_reingest(stored_mtime=100.0, disk_mtime=100.0, reindex_since=None) is False


def test_modified_file_reingested() -> None:
    assert should_reingest(stored_mtime=100.0, disk_mtime=200.0, reindex_since=None) is True


def test_legacy_row_without_mtime_skipped_by_default() -> None:
    # stored_mtime=0 är sentinel för "indexerad innan mtime-tracking fanns" —
    # ska inte re-indexeras automatiskt (då skulle hela det gamla indexet
    # plöjas om vid första körning).
    assert should_reingest(stored_mtime=0.0, disk_mtime=200.0, reindex_since=None) is False


def test_reindex_since_forces_legacy_reingest() -> None:
    # --reindex-since 150 → alla filer modifierade efter 150 ska re-indexeras,
    # även de utan känd stored_mtime.
    assert should_reingest(stored_mtime=0.0, disk_mtime=200.0, reindex_since=150.0) is True


def test_reindex_since_excludes_files_modified_before_cutoff() -> None:
    assert should_reingest(stored_mtime=0.0, disk_mtime=100.0, reindex_since=150.0) is False


def test_reindex_since_with_tracked_file_modified_after_cutoff() -> None:
    # Filen är redan indexerad (stored=250), men disk-mtime är ny (300)
    # och over cutoff — ska re-indexeras.
    assert should_reingest(stored_mtime=250.0, disk_mtime=300.0, reindex_since=200.0) is True


def test_reindex_since_doesnt_force_unchanged_tracked_file() -> None:
    # stored == disk, ingen ändring sedan indexering — skip även om cutoff är satt.
    assert should_reingest(stored_mtime=100.0, disk_mtime=100.0, reindex_since=50.0) is False

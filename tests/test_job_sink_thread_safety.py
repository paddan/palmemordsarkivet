"""Regressionstest: JobSink.write_log ska inte sammanfläta rader mellan trådar.

En naiv trådtest (många trådar, hoppas på att GIL-timing råkar krocka) visade
sig vara ickedeterministiskt — det passerade även utan låset. Den här varianten
tvingar fram en garanterad interleaving-möjlighet: en fil-wrapper vars write()
delar varje rad i två fysiska os-skrivningar med en sovpaus emellan (GIL
släpps deterministiskt under time.sleep), så två trådar som skriver samtidigt
UTAN lås garanterat hinner klämma in varandras halvor.
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

from operations.worker import JobSink


class _SlowFile:
    """Fil-liknande wrapper som delar varje write() i två delar med en
    sovpaus emellan, för att deterministiskt tvinga fram en interleaving-
    möjlighet mellan trådar som skriver samtidigt."""

    def __init__(self, path: Path) -> None:
        self._f = open(path, "w", encoding="utf-8")  # noqa: SIM115 — hålls öppen över flera anrop

    def write(self, s: str) -> None:
        mid = len(s) // 2
        self._f.write(s[:mid])
        time.sleep(0.01)
        self._f.write(s[mid:])

    def flush(self) -> None:
        self._f.flush()

    def close(self) -> None:
        self._f.close()


def test_write_log_is_thread_safe_against_interleaving(tmp_path) -> None:
    log_path = tmp_path / "job.log"
    slow_file = _SlowFile(log_path)
    try:
        sink = JobSink(conn=None, job_id="x", log_file=slow_file)
        n_threads = 8

        def worker(idx: int) -> None:
            sink.write_log(f"thread-{idx}-" + "A" * 50, level="info")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        slow_file.close()

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == n_threads, (
        f"förväntade {n_threads} rader, fick {len(lines)} — rader har smält ihop"
    )

    pattern = re.compile(r"^\S+ \[info\] thread-\d+-A{50}$")
    corrupted = [line for line in lines if not pattern.match(line)]
    assert not corrupted, f"sammanflätade/trasiga rader: {corrupted[:3]}"

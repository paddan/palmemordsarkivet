"""Tester för den fristående Surya-workern (src/surya_worker.py) med en falsk motor."""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

import surya_worker


def _block(html, label="Text", order=0, polygon=None, confidence=None):
    polygon = polygon or [[10, 20], [110, 20], [110, 40], [10, 40]]
    return SimpleNamespace(
        html=html, label=label, reading_order=order, polygon=polygon, confidence=confidence
    )


class FakeEngine:
    """Ersätter Engine: predictor() returnerar en sida med de angivna blocken."""

    model = "fake-surya 1"

    def __init__(self, blocks=None, failure=None):
        self._blocks = blocks or []
        self._failure = failure
        self.stopped = False

    def predictor(self):
        if self._failure is not None:
            raise self._failure

        def predict(images, full_page=True):
            return [SimpleNamespace(blocks=self._blocks)]

        return predict

    def stop(self):
        self.stopped = True


@pytest.fixture(autouse=True)
def _no_surya_settings(monkeypatch):
    """Projektets .venv kan ha en annan Surya installerad; testerna rör aldrig den."""
    monkeypatch.setattr(surya_worker, "logprobs_requested", lambda: False)


@pytest.fixture
def png(tmp_path: Path) -> Path:
    path = tmp_path / "sida.png"
    Image.new("RGB", (40, 30), "white").save(path)
    return path


def _request(path, **extra):
    request = {
        "protocol": 1,
        "type": "page",
        "pdf_stem": "123 - Förhör",
        "page": 2,
        "image_path": str(path),
    }
    request.update(extra)
    return request


def test_read_page_returns_raw_blocks(png):
    engine = FakeEngine([_block("<h2>PM</h2>", "SectionHeader", 0), _block("<p>Text här</p>", "Text", 1)])
    result = surya_worker.read_page(_request(png), engine)
    assert result["status"] == "read"
    assert result["pdf_stem"] == "123 - Förhör"
    assert result["page"] == 2
    assert result["model"] == "fake-surya 1"
    assert result["blocks"][0]["html"] == "<h2>PM</h2>"
    assert result["blocks"][0]["bbox"] == [10, 20, 110, 40]
    assert result["blocks"][0]["label"] == "SectionHeader"
    assert result["blocks"][1]["reading_order"] == 1


def test_blank_blocks_give_empty(png):
    result = surya_worker.read_page(_request(png), FakeEngine([_block("<p> </p>")]))
    assert result["status"] == "empty"


@pytest.mark.parametrize("html", ["<p>&nbsp;</p>", "<p>&#160;</p>", "<p>\u00a0 </p>"])
def test_entity_only_blocks_give_empty(png, html):
    result = surya_worker.read_page(_request(png), FakeEngine([_block(html)]))
    assert result["status"] == "empty"


def test_decompression_bomb_maps_to_page_too_large(png, monkeypatch):
    def bomb(_path):
        raise Image.DecompressionBombError("för stor")

    monkeypatch.setattr(Image, "open", bomb)
    result = surya_worker.read_page(_request(png), FakeEngine())
    assert result["code"] == "PAGE_TOO_LARGE"


def test_relative_image_path_rejected():
    request = _request("sida.png")
    result = surya_worker.read_page(request, FakeEngine())
    assert result["status"] == "error"
    assert result["code"] == "UNREADABLE_IMAGE"


def test_oversize_raster_rejected(tmp_path):
    path = tmp_path / "stor.png"
    Image.new("L", (5000, 4000), 255).save(path)
    result = surya_worker.read_page(_request(path), FakeEngine())
    assert result["code"] == "PAGE_TOO_LARGE"


def test_too_many_blocks(png):
    blocks = [_block("<p>x</p>", order=i) for i in range(surya_worker.MAX_BLOCKS + 1)]
    result = surya_worker.read_page(_request(png), FakeEngine(blocks))
    assert result["code"] == "OUTPUT_TOO_LARGE"


def test_too_many_html_chars(png):
    html = "<p>" + "a" * surya_worker.MAX_PAGE_CHARS + "</p>"
    result = surya_worker.read_page(_request(png), FakeEngine([_block(html)]))
    assert result["code"] == "OUTPUT_TOO_LARGE"


def test_wrong_protocol(png):
    result = surya_worker.answer(json.dumps(_request(png, protocol=2)), FakeEngine())
    assert result["code"] == "MALFORMED_REQUEST"


def test_not_json_is_malformed():
    assert surya_worker.answer("inte json", FakeEngine())["code"] == "MALFORMED_REQUEST"


def test_import_error_maps_to_needs_surya():
    assert surya_worker.runtime_failure_code(ImportError()) == "NEEDS_SURYA"


def test_other_failure_is_inference_failed(png):
    result = surya_worker.read_page(_request(png), FakeEngine(failure=RuntimeError("fel")))
    assert result["code"] == "INFERENCE_FAILED"


def test_bounded_line_refuses_oversize():
    assert surya_worker.encode({"x": "a" * (surya_worker.MAX_RESULT_BYTES + 1)}) is None


def test_main_exits_on_stdin_close(monkeypatch):
    engines = []

    def make_engine():
        engines.append(FakeEngine())
        return engines[-1]

    monkeypatch.setattr(surya_worker, "Engine", make_engine)
    monkeypatch.setattr(surya_worker, "install_signal_handlers", lambda: None)
    monkeypatch.setattr("sys.argv", ["surya_worker.py"])
    monkeypatch.setattr("sys.stdin", SimpleNamespace(buffer=io.BytesIO(b"")))
    assert surya_worker.main() == 0
    assert engines[0].stopped is True


def test_start_failure_maps_to_surya_start_failed(png, monkeypatch):
    # ett oklassat fel när runtimen startas (ingen prediktor finns än) är ett startfel,
    # inte ett sidfel, och samma kod gäller för följande sidor utan ny start
    starts = []

    class Manager:
        method = "fake"

        def start(self):
            starts.append(1)
            raise RuntimeError("servern svarade inte på hälsokontrollen")

        def stop(self):
            pass

    modules = {
        "surya.inference": SimpleNamespace(get_default_manager=Manager),
        "surya.recognition": SimpleNamespace(RecognitionPredictor=lambda manager: None),
    }
    monkeypatch.setattr(surya_worker.importlib, "import_module", modules.__getitem__)
    engine = surya_worker.Engine()
    first = surya_worker.read_page(_request(png), engine)
    second = surya_worker.read_page(_request(png, page=3), engine)
    assert first["code"] == "SURYA_START_FAILED"
    assert second["code"] == "SURYA_START_FAILED"
    assert second["page"] == 3
    assert len(starts) == 1


def test_start_failure_keeps_cause_classification():
    def wrapped(cause):
        try:
            raise surya_worker.RuntimeStartFailed(str(cause)) from cause
        except surya_worker.RuntimeStartFailed as failure:
            return failure

    assert surya_worker.runtime_failure_code(wrapped(ImportError())) == "NEEDS_SURYA"
    assert surya_worker.runtime_failure_code(wrapped(ValueError())) == "SURYA_START_FAILED"

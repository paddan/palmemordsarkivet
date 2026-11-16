"""Tester för motorn ``llm-vision`` i ``ocr_pages`` (``transcribe`` ersatt, inga nätanrop)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import db as state_db  # noqa: E402
import llm_vision  # noqa: E402
import ocr_pages  # noqa: E402
from llm_vision import VisionError, VisionProfile, VisionResult  # noqa: E402


class _RecordingSink:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def write_log(self, message: str, level: str = "info") -> None:
        self.lines.append((level, message))

    def write_progress(self, update: object) -> None:
        return None

    def write_traceback(self, exc: BaseException) -> None:
        return None


def _context():
    from operations.context import OperationContext

    sink = _RecordingSink()
    return OperationContext(sink=sink, cancel_requested=lambda: False, debug=True), sink


PROFILE = VisionProfile(name="Syn", model="vision-model", base_url="https://openrouter.ai/api/v1",
                        api_key="hemlig-nyckel", external=True)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """En PDF vars sidor ersätts av färdiga bilder; profil och klient ersatta."""
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    errors: list[tuple] = []
    monkeypatch.setattr(ocr_pages, "log_error", lambda *args: errors.append(args))
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.7")
    images: dict[int, Image.Image] = {}
    monkeypatch.setattr(
        ocr_pages, "render_pages", lambda path, dpi: iter(sorted(images.items()))
    )
    loaded: list[str] = []

    def fake_load(name: str) -> VisionProfile:
        loaded.append(name)
        if name != "Syn":
            raise ValueError(f"Okänd LLM-konfiguration {name!r}.")
        return PROFILE

    monkeypatch.setattr(llm_vision, "load_profile", fake_load)
    monkeypatch.setattr(llm_vision, "make_client", lambda profile: object())

    def run(**kwargs):
        ctx, sink = _context()
        params = {
            "inp": pdf, "out_dir": str(tmp_path / "pages"), "engine": "llm-vision",
            "langs": "swe", "dpi": 300, "pages": None, "ocr_dir": str(tmp_path / "ocr"),
            "no_update_pdf": False, "no_detect_redactions": False, "txt_dir": None,
            "profile": "Syn",
        }
        params.update(kwargs)
        rc = ocr_pages.run_ocr_pages(**params, context=ctx)
        conn = state_db.connect()
        state_db.init_schema(conn)
        rows = {row["page_num"]: row for row in state_db.get_pages_for_stem(conn, "doc")}
        conn.close()
        return rc, rows, sink

    return images, run, errors, loaded, tmp_path


def _masked_page() -> Image.Image:
    """Vit sida med ett brett svart block i mitten (pixeldetekteringen hittar det)."""
    image = Image.new("RGB", (1200, 1600), "white")
    ImageDraw.Draw(image).rectangle([200, 760, 1000, 840], fill="black")
    return image


def test_ocr_pages_llm_vision_records_markdown_and_identity(setup, monkeypatch) -> None:
    images, run, _errors, loaded, tmp_path = setup
    images[1] = Image.new("RGB", (400, 400), "white")
    seen: list[tuple] = []

    def fake_transcribe(image, profile, *, client=None):
        seen.append((image.size, profile.name))
        return VisionResult("## Förhör\n\n**M:** Ja.", 100, 20)

    monkeypatch.setattr(llm_vision, "transcribe", fake_transcribe)

    rc, rows, sink = run()

    assert rc == 0
    assert loaded == ["Syn"]
    assert seen == [((400, 400), "Syn")]
    row = rows[1]
    assert row["engine"] == "llm-vision"
    assert row["markdown"] == "## Förhör\n\n**M:** Ja."
    assert row["text"] == "Förhör\n\nM: Ja."
    assert row["engine_identity"] == "llm-vision:Syn:vision-model"
    # inget PDF-textlager för motorn
    assert not (tmp_path / "ocr" / "doc.pdf").exists()
    messages = [m for _lvl, m in sink.lines]
    assert any("[llm-vision] doc: 1 sidor, ↑100 ↓20 token" in m for m in messages)
    assert any("openrouter.ai (extern)" in m for m in messages)
    assert not any("hemlig-nyckel" in m for m in messages)
    assert not any("**M:** Ja." in m for m in messages)


def test_ocr_pages_llm_vision_inserts_masks_in_markdown(setup, monkeypatch) -> None:
    images, run, *_ = setup
    images[1] = _masked_page()
    monkeypatch.setattr(
        llm_vision, "transcribe",
        lambda image, profile, *, client=None: VisionResult("Rad ett\nRad två\nRad tre\nRad fyra",
                                                            None, None),
    )

    rc, rows, _ = run()

    assert rc == 0
    row = rows[1]
    assert "[MASKAD]" in row["markdown"] and "[MASKAD]" in row["text"]
    assert row["markdown"].index("Rad två") < row["markdown"].index("[MASKAD]") \
        < row["markdown"].index("Rad tre")


def test_ocr_pages_llm_vision_fatal_error_exit_code(setup, monkeypatch) -> None:
    images, run, *_ = setup
    images[1] = Image.new("RGB", (400, 400), "white")
    images[2] = Image.new("RGB", (400, 400), "white")
    calls: list[int] = []

    def fake_transcribe(image, profile, *, client=None):
        calls.append(1)
        raise VisionError("HTTP 402: krediterna är slut", fatal=True)

    monkeypatch.setattr(llm_vision, "transcribe", fake_transcribe)

    rc, rows, _ = run()

    assert rc == ocr_pages.EXIT_LLM_FATAL == 4
    assert rows == {}
    assert len(calls) == 1  # resten av filen hoppas


def test_ocr_pages_llm_vision_page_error_skips_page(setup, monkeypatch) -> None:
    images, run, errors, *_ = setup
    images[1] = Image.new("RGB", (400, 400), "white")
    images[2] = Image.new("RGB", (400, 400), "white")
    answers = [VisionError("svaret avklippt (length)"), VisionResult("Sida två", 5, 2)]

    def fake_transcribe(image, profile, *, client=None):
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(llm_vision, "transcribe", fake_transcribe)

    rc, rows, _ = run(pages="1,2", force=True)

    assert rc == 1  # --force: sida 1 gick inte att läsa
    assert set(rows) == {2}
    assert errors and "p1" in errors[0][1]


def test_ocr_pages_llm_vision_requires_known_profile(setup, monkeypatch) -> None:
    images, run, _errors, loaded, _ = setup
    images[1] = Image.new("RGB", (400, 400), "white")
    monkeypatch.setattr(llm_vision, "transcribe",
                        lambda *a, **k: pytest.fail("ingen sida får läsas"))

    rc, rows, sink = run(profile="Okänd")
    assert rc == ocr_pages.EXIT_LLM_FATAL
    assert rows == {}
    assert any("Okänd" in m for _lvl, m in sink.lines)

    rc, rows, _ = run(profile=None)
    assert rc == ocr_pages.EXIT_LLM_FATAL and rows == {}


def test_ocr_pages_llm_vision_model_markers_are_not_doubled(setup, monkeypatch) -> None:
    images, run, *_ = setup
    images[1] = _masked_page()
    monkeypatch.setattr(
        llm_vision, "transcribe",
        lambda image, profile, *, client=None: VisionResult(
            "Rad ett\nRad två bodde på [svärtat] där\nRad tre", None, None),
    )

    rc, rows, sink = run()

    assert rc == 0
    row = rows[1]
    assert "svärtat" not in row["markdown"] and "svärtat" not in row["text"]
    assert row["markdown"].count("[MASKAD]") == 1
    assert "bodde på [MASKAD] där" in row["text"]
    debug = "\n".join(m for lvl, m in sink.lines if lvl == "debug")
    assert "modellens maskeringsmarkörer" in debug

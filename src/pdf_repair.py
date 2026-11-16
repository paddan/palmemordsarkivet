"""Reservväg för PDF:er som PDFium inte kan ladda: en kopia lagad med qpdf.

Några arkivfiler har en stegvis uppdatering där ett sidobjekt som är markerat
fritt i en äldre xref-sektion återanvänds i en senare (t.ex.
``Pol-1986-03-05_E167-00-C_Christina_Wallin.pdf``, sida 2). qpdf och poppler
läser sidan, men PDFium svarar ``Failed to load page``. ``qpdf in.pdf ut.pdf``
skriver om filen med en enda, korrekt xref-tabell, och då laddar PDFium sidan.

``open_pdf`` öppnar originalet och tar en sida ur en lagad kopia bara när
PDFium inte kan ladda den ur originalet (eller inte kan öppna dokumentet alls).
Kopian skapas en gång per öppning i en tillfällig katalog och tas bort när
dokumentet stängs; originalet ändras aldrig. Saknas qpdf, eller misslyckas
lagningen, kastas PDFiums ursprungliga fel vidare.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# Längsta tid qpdf får arbeta med en fil (de största arkivfilerna tar under 1 s).
QPDF_TIMEOUT_S = 120
# qpdf: 0 = klart, 3 = klart med varningar (det vanliga för en lagad fil).
_QPDF_OK = (0, 3)


def repair_copy(src: Path, dst: Path) -> str | None:
    """Skriv en qpdf-lagad kopia av ``src`` till ``dst``; ``None`` vid lyckat, annars orsaken."""
    qpdf = shutil.which("qpdf")
    if qpdf is None:
        return "qpdf saknas (brew install qpdf)"
    try:
        proc = subprocess.run([qpdf, str(src), str(dst)], capture_output=True, text=True,
                              timeout=QPDF_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        return f"qpdf tog längre tid än {QPDF_TIMEOUT_S} s"
    if proc.returncode not in _QPDF_OK or not dst.is_file():
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        return f"qpdf rc={proc.returncode}: {detail[-1] if detail else 'inget meddelande'}"
    return None


class RepairablePdf:
    """Ett PDFium-dokument som läser oladdbara sidor ur en qpdf-lagad kopia."""

    def __init__(self, pdf_path: Path, on_repair: Callable[[str], None] | None = None) -> None:
        import pypdfium2 as pdfium

        self.path = Path(pdf_path)
        self._on_repair = on_repair or (lambda message: None)
        self._tmp: tempfile.TemporaryDirectory[str] | None = None
        self._repaired: Any = None
        self._repair_tried = False
        try:
            self._doc: Any = pdfium.PdfDocument(str(self.path))
        except pdfium.PdfiumError as exc:
            self._doc = None
            if self._repaired_doc(f"dokumentet går inte att öppna ({exc})") is None:
                self.close()
                raise

    def __len__(self) -> int:
        return len(self._doc if self._doc is not None else self._repaired)

    def page(self, page_num: int) -> Any:
        """Sidan ``page_num`` (1-baserad); ur kopian om originalet inte kan ladda den."""
        import pypdfium2 as pdfium

        if self._doc is None:
            return self._repaired[page_num - 1]
        try:
            return self._doc[page_num - 1]
        except pdfium.PdfiumError as exc:
            repaired = self._repaired_doc(f"sida {page_num} går inte att ladda ({exc})")
            if repaired is None:
                raise
            return repaired[page_num - 1]

    def _repaired_doc(self, reason: str) -> Any:
        """Den lagade kopian (skapas vid första behovet), eller ``None`` om lagningen inte gick."""
        import pypdfium2 as pdfium

        if self._repair_tried:
            return self._repaired
        self._repair_tried = True
        self._tmp = tempfile.TemporaryDirectory(prefix="pdf_repair_")
        dst = Path(self._tmp.name) / "lagad.pdf"
        error = repair_copy(self.path, dst)
        if error is None:
            try:
                self._repaired = pdfium.PdfDocument(str(dst))
            except pdfium.PdfiumError as exc:
                error = f"den lagade kopian går inte heller att öppna ({exc})"
        if error is None:
            self._on_repair(f"{self.path.name}: {reason} — läser ur en qpdf-lagad kopia")
        else:
            self._on_repair(f"{self.path.name}: {reason} — kunde inte lagas: {error}")
        return self._repaired

    def close(self) -> None:
        for doc in (self._doc, self._repaired):
            if doc is not None:
                doc.close()
        self._doc = self._repaired = None
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None


@contextmanager
def open_pdf(pdf_path: Path, on_repair: Callable[[str], None] | None = None
             ) -> Iterator[RepairablePdf]:
    """Öppna ``pdf_path`` med reservväg via en qpdf-lagad kopia (se modulens docstring).

    ``on_repair`` får en rad när kopian används eller inte gick att skapa.
    """
    pdf = RepairablePdf(pdf_path, on_repair)
    try:
        yield pdf
    finally:
        pdf.close()

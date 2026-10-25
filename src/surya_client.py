"""Klient för Surya-workern: startar den, skickar en sida per rad och läser svaret.

Workern (``surya_worker.py``) körs med en *annan* Python (``PALME_SURYA_PYTHON``)
och importeras därför aldrig härifrån; protokollets konstanter är dubblerade
med värde och bevakas av ett test. Klienten äger processens livscykel:

- workern startas i en egen session (``start_new_session``) så att hela
  processgruppen, inklusive ``llama-server``, kan avslutas tillsammans;
- varje svarsrad läses med en tidsgräns och en storleksgräns, så en hängd eller
  skenande worker aldrig blockerar pipelinen;
- varje resultat måste bära samma ``pdf_stem`` och ``page`` som begäran, annars
  är protokollet bruten och workern stängs (``PROTOCOL_MISMATCH``).

Felen delas i två: :class:`SuryaUnavailable` betyder att workern inte går att
använda längre (resten av filen hoppas), :class:`SuryaPageError` att just den
här sidan misslyckades och att nästa sida kan prövas. Modulen loggar inget —
anroparen gör det — och lägger aldrig sidtext i felmeddelanden.
"""

from __future__ import annotations

import contextlib
import json
import os
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WORKER_PATH = ROOT / "src" / "surya_worker.py"
DEFAULT_SURYA_PYTHON = Path.home() / ".local/share/palmemordsarkivet/surya-venv/bin/python"
SURYA_PYTHON_ENV = "PALME_SURYA_PYTHON"

# Protokollet (värdena speglar surya_worker och bevakas av tests/test_surya_client.py).
PROTOCOL_VERSION = 1
PAGE_TYPE = "page"
IDENTITY_TYPE = "identity"
IDENTITY_FLAG = "--identity"
STATUS_READ = "read"
STATUS_EMPTY = "empty"
STATUS_ERROR = "error"
STATUS_OK = "ok"
MAX_RESULT_BYTES = 4 * 1024 * 1024
UNAVAILABLE_CODES = frozenset(
    {"NEEDS_SURYA", "NEEDS_LLAMA_CPP", "NEEDS_SURYA_MODEL", "SURYA_START_FAILED"}
)

# Klientens egna felkoder.
WORKER_DIED = "WORKER_DIED"
PROTOCOL_MISMATCH = "PROTOCOL_MISMATCH"

# Exitkod från ``ocr_pages`` när Surya-runtimen saknas eller workern dör om och
# om igen: ett miljöfel, inte ett filfel, så anroparen markerar inget per fil.
EXIT_SURYA_UNAVAILABLE = 3

# Identiteten säger "llama-server unresolved" när binären inte går att hitta —
# då går workern inte att använda, även om --identity svarar ok.
LLAMA_UNRESOLVED = "llama-server unresolved"

DEFAULT_PAGE_TIMEOUT_SECONDS = 300.0
IDENTITY_TIMEOUT_SECONDS = 30.0
CLOSE_WAIT_SECONDS = 10.0
KILL_GRACE_SECONDS = 5.0
# Kortare frist när kontexten lämnas med ett undantag (t.ex. SIGTERM från ett
# avbrutet jobb): jobbets egen SIGKILL kommer efter 5 s, och då måste gruppen
# redan vara borta.
EXIT_KILL_GRACE_SECONDS = 2.0
READ_CHUNK_BYTES = 65536


class SuryaUnavailable(RuntimeError):
    """Workern kan inte användas (saknad runtime, död, hängd eller bruten protokoll)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


class SuryaPageError(RuntimeError):
    """Sidan gick inte att läsa, men workern lever och nästa sida kan prövas."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


@dataclass(frozen=True)
class SuryaPage:
    """En läst sida: status (``read``/``empty``), råa block och modellens identitet."""

    status: str
    blocks: list[dict[str, Any]]
    model: str


def surya_python() -> Path:
    """Tolken för worker-venv:en; ``PALME_SURYA_PYTHON`` går före standardplatsen."""
    override = os.environ.get(SURYA_PYTHON_ENV)
    return Path(override) if override else DEFAULT_SURYA_PYTHON


def _command(python: Path, worker: Path, *extra: str) -> list[str]:
    return [str(python), "-I", str(worker), *extra]


def _signal_group(pgid: int, sig: int) -> bool:
    """Skicka ``sig`` till gruppen; ``False`` när ingen process finns kvar i den."""
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _terminate_group(
    process: subprocess.Popen[bytes],
    wait_seconds: float,
    kill_grace: float = KILL_GRACE_SECONDS,
) -> None:
    """Låt workern avsluta (efter stängd stdin), sedan SIGTERM och SIGKILL mot hela gruppen."""
    pgid = process.pid  # start_new_session gör workern till gruppledare
    if wait_seconds > 0:
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=wait_seconds)
    if _signal_group(pgid, signal.SIGTERM):
        deadline = time.monotonic() + kill_grace
        while time.monotonic() < deadline:
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=0.05)
            if not _signal_group(pgid, 0):
                break
        else:
            _signal_group(pgid, signal.SIGKILL)
    try:
        process.wait(timeout=KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _signal_group(pgid, signal.SIGKILL)
        process.wait()


class SuryaWorker:
    """En körande Surya-worker. Kontexthanterare; startas vid första sidan."""

    def __init__(
        self,
        python: Path | None = None,
        worker: Path | None = None,
        page_timeout: float = DEFAULT_PAGE_TIMEOUT_SECONDS,
    ) -> None:
        self._python = python if python is not None else surya_python()
        self._worker = worker if worker is not None else WORKER_PATH
        self._page_timeout = page_timeout
        self._process: subprocess.Popen[bytes] | None = None
        self._buffer = b""
        self._identity: str | None = None
        self._identity_known = False

    def __enter__(self) -> SuryaWorker:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Graciöst bara vid normal utgång. Ett undantag (SystemExit från SIGTERM,
        # KeyboardInterrupt, fel) betyder att anroparen är på väg bort: väntan på
        # en sida som är halvvägs i modellen hade överlevt jobbets SIGKILL och
        # lämnat workern och llama-server kvar.
        if exc_type is None:
            self.close()
        else:
            self.close(graceful=False, kill_grace=EXIT_KILL_GRACE_SECONDS)

    def _start(self) -> subprocess.Popen[bytes]:
        if self._process is not None and self._process.poll() is None:
            return self._process
        self.close()
        try:
            self._process = subprocess.Popen(
                _command(self._python, self._worker),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=os.environ.copy(),
            )
        except OSError as failure:
            raise SuryaUnavailable(
                WORKER_DIED, f"workern gick inte att starta ({type(failure).__name__})"
            ) from failure
        self._buffer = b""
        return self._process

    def read_page(self, pdf_stem: str, page: int, image_path: Path) -> SuryaPage:
        """Läs en sida. Höjer :class:`SuryaUnavailable` eller :class:`SuryaPageError`."""
        process = self._start()
        request = {
            "protocol": PROTOCOL_VERSION,
            "type": PAGE_TYPE,
            "pdf_stem": pdf_stem,
            "page": page,
            "image_path": str(image_path),
        }
        try:
            assert process.stdin is not None
            process.stdin.write((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
            process.stdin.flush()
        except (OSError, ValueError) as failure:
            self.close(graceful=False)
            raise SuryaUnavailable(WORKER_DIED, "workern tog inte emot begäran") from failure
        line = self._read_line(process)
        return self._interpret(line, pdf_stem, page)

    def _read_line(self, process: subprocess.Popen[bytes]) -> bytes:
        """En svarsrad inom ``page_timeout`` och högst ``MAX_RESULT_BYTES``."""
        assert process.stdout is not None
        fd = process.stdout.fileno()
        deadline = time.monotonic() + self._page_timeout
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_READ)
            while True:
                newline = self._buffer.find(b"\n")
                if newline >= 0:
                    line, self._buffer = self._buffer[:newline], self._buffer[newline + 1 :]
                    return line
                if len(self._buffer) > MAX_RESULT_BYTES:
                    self.close(graceful=False)
                    raise SuryaUnavailable(PROTOCOL_MISMATCH, "svarsraden är längre än gränsen")
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(timeout=remaining):
                    self.close(graceful=False)
                    raise SuryaUnavailable(WORKER_DIED, "tidsgränsen för sidan gick ut")
                try:
                    chunk = os.read(fd, READ_CHUNK_BYTES)
                except OSError:
                    chunk = b""
                if not chunk:
                    self.close(graceful=False)
                    raise SuryaUnavailable(WORKER_DIED, "workern avslutades utan svar")
                self._buffer += chunk

    def _interpret(self, line: bytes, pdf_stem: str, page: int) -> SuryaPage:
        try:
            result = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            result = None
        valid = (
            isinstance(result, dict)
            and result.get("protocol") == PROTOCOL_VERSION
            and result.get("type") == PAGE_TYPE
            and result.get("pdf_stem") == pdf_stem
            and result.get("page") == page
        )
        if not valid:
            self.close(graceful=False)
            raise SuryaUnavailable(PROTOCOL_MISMATCH, "svaret hör inte till begäran")
        assert isinstance(result, dict)
        status = result.get("status")
        if status == STATUS_ERROR:
            code = str(result.get("code") or "UNKNOWN")
            message = str(result.get("message") or "")
            if code in UNAVAILABLE_CODES:
                self.close(graceful=False)
                raise SuryaUnavailable(code, message)
            raise SuryaPageError(code, message)
        blocks = result.get("blocks")
        if status not in (STATUS_READ, STATUS_EMPTY) or not isinstance(blocks, list):
            self.close(graceful=False)
            raise SuryaUnavailable(PROTOCOL_MISMATCH, "svaret har okänd status eller saknar block")
        return SuryaPage(status=status, blocks=blocks, model=str(result.get("model") or ""))

    def identity(self) -> str | None:
        """Workerns identitet (en separat kort process), eller ``None``. Cachas i instansen."""
        if not self._identity_known:
            self._identity = _run_identity(self._python, self._worker)
            self._identity_known = True
        return self._identity

    def close(self, graceful: bool = True, kill_grace: float = KILL_GRACE_SECONDS) -> None:
        """Stäng stdin, vänta, och avsluta sedan hela processgruppen.

        ``graceful=False`` hoppar över väntan (hängd eller trasig worker);
        ``kill_grace`` är fristen mellan SIGTERM och SIGKILL.
        """
        process, self._process = self._process, None
        self._buffer = b""
        if process is None:
            return
        for pipe in (process.stdin,):
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                pass
        try:
            _terminate_group(process, CLOSE_WAIT_SECONDS if graceful else 0.0, kill_grace)
        finally:
            try:
                if process.stdout is not None:
                    process.stdout.close()
            except OSError:
                pass


def _run_identity(python: Path, worker: Path) -> str | None:
    """Kör ``--identity`` i en kort egen process; ``None`` vid fel, tystnad eller tidsgräns.

    Proben körs i en egen session och vid tidsgräns dödas hela processgruppen:
    ett barn (t.ex. ``llama-server --version``) som håller stdout öppen hade
    annars både överlevt och hängt läsningen.
    """
    try:
        process = subprocess.Popen(
            _command(python, worker, IDENTITY_FLAG),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=os.environ.copy(),
            start_new_session=True,
        )
    except OSError:
        return None
    try:
        stdout, _ = process.communicate(timeout=IDENTITY_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _signal_group(process.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.communicate(timeout=KILL_GRACE_SECONDS)
        return None
    if process.returncode != 0:
        return None
    try:
        result = json.loads(stdout.decode("utf-8").strip().splitlines()[-1])
    except (UnicodeDecodeError, ValueError, IndexError):
        return None
    if (
        not isinstance(result, dict)
        or result.get("type") != IDENTITY_TYPE
        or result.get("status") != STATUS_OK
        or not isinstance(result.get("identity"), str)
    ):
        return None
    return str(result["identity"])


def surya_layout_available() -> bool:
    """Tolken finns, workern svarar ``ok`` på ``--identity`` inom 30 s och hittar ``llama-server``."""
    python = surya_python()
    if not python.exists():
        return False
    identity = _run_identity(python, WORKER_PATH)
    return identity is not None and LLAMA_UNRESOLVED not in identity

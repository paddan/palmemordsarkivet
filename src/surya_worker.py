#!/usr/bin/env python3
"""Läs sidor med Surya 2: en begäran in, ett svar ut, en JSON-rad åt gången.

Den här filen körs med *worker-venv:ens* Python (``PALME_SURYA_PYTHON``,
Python 3.12 med ``surya-ocr==0.22.1``), inte projektets ``.venv``. Därför
importerar den inga projektmoduler och är startbar även där Surya saknas —
en saknad runtime blir då felkoden ``NEEDS_SURYA`` i stället för en krasch
före första begäran.

Föräldern (projektets klient) äger livscykeln: den startar processen, skriver
en sida per rad på stdin och läser ett svar per rad på stdout. Workern äger
modellen:

- **Runtime laddas en gång per process.** ``get_default_manager().start()`` och
  ``RecognitionPredictor`` byggs vid första sidan och återanvänds, så att
  ``llama-server`` och vikterna startas en gång per fil och inte per sida.
- **En sida är ett helsidesanrop** (``predictor([bild], full_page=True)``).
  Workern skickar bara *rå HTML* per block (plus etikett, läsordning, bbox och
  konfidens). All tolkning av HTML till text/Markdown sker i föräldern. En
  sida där inget block har text kvar efter att taggar strippats är en *tom*
  läsning (``empty``), inte en lyckad.
- **Inget är obegränsat.** Begäransrad, resultatrad, sidans HTML, antal block
  och rastret har varsin gräns; en överträdelse rapporteras i stället för att
  tyst kapas, eftersom en avkortad läsning inte är belägg för vad sidan säger.
- **Stängd stdin betyder avsluta.** Processen lämnar via normal väg så att
  runtimens egen städning dödar ``llama-server``. SIGTERM görs om till samma
  avslut, eftersom en hårt dödad process inte kör någon städning.

Protokoll (version 1)::

    begäran: {"protocol":1,"type":"page","pdf_stem":..,"page":..,"image_path":..}
    svar:    {"protocol":1,"type":"page","pdf_stem":..,"page":..,
              "status":"read"|"empty"|"error","model":..,
              "blocks":[{"html","label","reading_order","bbox","confidence"}]}
    fel:     status "error" med "code" och "message".

Med ``--identity`` svarar workern med en rad om *sig själv* (runtimeversion,
backend, vikter och ``llama-server``-bygge) utan att starta någon modell.
"""

from __future__ import annotations

import importlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
from html import unescape
from typing import Any

PROTOCOL_VERSION = 1
PAGE_TYPE = "page"
IDENTITY_TYPE = "identity"
IDENTITY_FLAG = "--identity"

# Hur länge llama-server får svara på --version.
LLAMA_SERVER_TIMEOUT_SECONDS = 10

STATUS_READ = "read"
STATUS_EMPTY = "empty"
STATUS_ERROR = "error"
STATUS_OK = "ok"

NEEDS_SURYA = "NEEDS_SURYA"
NEEDS_LLAMA_CPP = "NEEDS_LLAMA_CPP"
NEEDS_SURYA_MODEL = "NEEDS_SURYA_MODEL"
SURYA_START_FAILED = "SURYA_START_FAILED"
MALFORMED_REQUEST = "MALFORMED_REQUEST"
UNREADABLE_IMAGE = "UNREADABLE_IMAGE"
PAGE_TOO_LARGE = "PAGE_TOO_LARGE"
OUTPUT_TOO_LARGE = "OUTPUT_TOO_LARGE"
INFERENCE_FAILED = "INFERENCE_FAILED"

# Hur mycket av en begärans- eller resultatrad som hålls i minnet.
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESULT_BYTES = 4 * 1024 * 1024

# Vad en sidas läsning får innehålla: HTML-tecken (taggar inräknade) och block.
MAX_PAGE_CHARS = 200_000
MAX_BLOCKS = 2_000

# Rastergränsen; en sida över den läses inte.
MAX_PAGE_PIXELS = 16_777_216

NEWLINE = b"\n"

# Nycklar ur begäran som speglas i svaret så att föräldern kan para ihop dem.
ECHO_KEYS = ("pdf_stem", "page")


class LineTooLong(Exception):
    """En rad längre än gränsen för dess läsare."""


class PageTooLarge(Exception):
    """En sida vars raster är större än gränsen för en sidbild."""


class ResultTooLarge(Exception):
    """Ett resultat över gränsen för en resultatrad eller en läsning."""


class ProtocolReader:
    """Rader ur en bytesström; håller högst ``bound`` byte av en rad."""

    def __init__(self, stream: Any, bound: int) -> None:
        self._stream = stream
        self._bound = bound

    def read_line(self) -> str | None:
        """Nästa rad som text, eller ``None`` vid strömmens slut.

        Höjer ``LineTooLong`` över gränsen.
        """
        chunk = self._stream.readline(self._bound + 1)
        if chunk == b"":
            return None
        if len(chunk) > self._bound and not chunk.endswith(NEWLINE):
            # Resten läses och kastas i stället för att nekas: processen i andra
            # änden av röret är blockerad tills det dräneras.
            while True:
                rest = self._stream.readline(self._bound + 1)
                if rest == b"" or rest.endswith(NEWLINE):
                    break
            raise LineTooLong(f"mer än {self._bound} byte på en rad")
        return str(chunk.decode("utf-8", errors="replace"))


class RuntimeStartFailed(Exception):
    """Runtimen gick inte att starta (ingen prediktor finns); orsaken ligger i ``__cause__``.

    Skiljs från ett fel under en sidas inferens: en runtime som aldrig kom
    igång är inte sidans fel, och föräldern ska inte räkna den som det.
    """


class Engine:
    """Inferensruntimen den här processen äger: startas en gång, aldrig två."""

    def __init__(self) -> None:
        self._manager: Any = None
        self._predictor: Any = None
        self._model: str | None = None
        self._failure: Exception | None = None

    def predictor(self) -> Any:
        """Prediktorn; startar runtimen vid första anropet och upprepar samma fel efteråt."""
        if self._predictor is not None:
            return self._predictor
        if self._failure is not None:
            raise self._failure
        try:
            # Importeras här: Surya finns bara i worker-venv:en, aldrig i
            # projektets träd, och modulen måste gå att starta utan den.
            get_default_manager = importlib.import_module("surya.inference").get_default_manager
            recognition_predictor = importlib.import_module("surya.recognition").RecognitionPredictor

            manager = get_default_manager()
            # Startas uttryckligen så att saknad llama-server, misslyckad
            # nedladdning eller osund server blir det här stegets fel.
            manager.start()
            self._manager = manager
            self._model = describe_model(manager)
            self._predictor = recognition_predictor(manager)
        except Exception as failure:
            # Allt som fallerar här fallerar före första sidan; felet sparas och
            # upprepas så att följande sidor får samma kod utan en ny start.
            wrapped = RuntimeStartFailed(str(failure))
            self._failure = wrapped
            raise wrapped from failure
        return self._predictor

    @property
    def model(self) -> str | None:
        """Identiteten på vikterna som läser, som en rad ett fingeravtryck kan bära."""
        return self._model

    def stop(self) -> None:
        """Avslutar det workern startade. Förlåtande: ett fel på väg ut får inte hindra avslutet."""
        try:
            if self._manager is not None:
                self._manager.stop()
        except Exception:
            pass


def describe_model(manager: Any) -> str:
    """Vad workern läser med: version, backend samt vikternas revision och storlek."""
    return " ".join(model_parts(manager.method))


def runtime_identity() -> str:
    """Vad den här tolkens runtime är, utan att starta modell, server eller sida.

    Svaret på ``--identity``. ``llama-server``-bygget läses ur binärens egen
    ``--version``, eftersom Homebrews ``llama.cpp`` flyter: samma vikter
    serverade av ett annat bygge är en annan läsare.
    """
    return " ".join([*model_parts(surya_backend()), f"llama-server {llama_server_build()}"])


def model_parts(backend: str) -> list[str]:
    """Version, backend samt cachade vikters revision och storlek som identitetens ord.

    Revision och storlek ingår eftersom två läsningar bara är jämförbara när de
    kommer från samma vikter.
    """
    settings = surya_settings()
    parts = [
        f"surya-ocr {package_version('surya-ocr')}",
        f"backend {backend}",
        f"model {settings.SURYA_MODEL_CHECKPOINT}",
    ]
    for name in (settings.SURYA_GGUF_MODEL_FILE, settings.SURYA_GGUF_MMPROJ_FILE):
        parts.append(f"{name} {cached_artifact(settings.SURYA_GGUF_REPO, name)}")
    return parts


def surya_backend() -> str:
    """Backenden runtimen skulle läsa med, utfrågad i stället för gissad.

    En manager som inte startats har inte startat något: valet kan läsas utan
    att spawna servern.
    """
    try:
        return str(importlib.import_module("surya.inference").get_default_manager().method)
    except Exception:
        return "unresolved"


def llama_server_build() -> str:
    """Bygget av den ``llama-server`` runtimen skulle starta, eller varför det inte gick att läsa.

    Binären löses som runtimen själv gör: Suryas egen inställning först, sedan ``PATH``.
    """
    try:
        binary = surya_settings().LLAMA_CPP_BINARY
        path = binary if binary and os.path.isfile(binary) else shutil.which(binary or "llama-server")
        if not path:
            return "unresolved"
        # llama.cpp skriver versionen på stderr under en rad om initiering,
        # så svaret väljs på vad det säger och inte var det kom ut.
        outcome = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=LLAMA_SERVER_TIMEOUT_SECONDS,
        )
    except Exception:
        return "unresolved"
    for line in (outcome.stdout + "\n" + outcome.stderr).splitlines():
        if "version:" in line:
            return line.strip()
    return "unresolved"


def surya_settings() -> Any:
    """Suryas egna inställningar, laddade i den tolk som kör workern.

    Dynamisk import: filen måste vara startbar där Surya saknas. Värdena läses
    ur paketet och upprepas inte här.
    """
    return importlib.import_module("surya.settings").settings


def package_version(name: str) -> str:
    """Ett installerat pakets version, eller ``unknown`` när den inte går att fråga efter."""
    try:
        from importlib.metadata import version

        return version(name)
    except Exception:
        return "unknown"


def cached_artifact(repository: str, name: str) -> str:
    """Vilken revision av en fil cachen håller och hur stor den är, utan att hämta något."""
    try:
        hub = importlib.import_module("huggingface_hub")
        path = hub.try_to_load_from_cache(repository, name)
    except Exception:
        return "unresolved"
    if not isinstance(path, str) or not os.path.isfile(path):
        return "uncached"
    revision = os.path.basename(os.path.dirname(path))
    return f"{revision}:{os.path.getsize(path)}"


def read_page(request: dict[str, Any], engine: Engine) -> dict[str, Any]:
    """En sidas läsning, eller felresultatet som säger varför det inte finns någon.

    Bildsökvägen är förälderns egen; här kontrolleras att den är absolut och
    pekar på en fil, eftersom en begäran är ett trådformat och inte ett
    löfte om vad en sökväg är.
    """
    image_path = request.get("image_path")
    if not isinstance(image_path, str) or not image_path or not os.path.isabs(image_path):
        return error(request, UNREADABLE_IMAGE, "sidans bildsökväg är inte absolut")
    if not os.path.isfile(image_path):
        return error(request, UNREADABLE_IMAGE, "sidans bild är inte en fil")
    try:
        image = open_image(image_path)
    except PageTooLarge:
        return error(
            request,
            PAGE_TOO_LARGE,
            f"sidans raster är större än gränsen på {MAX_PAGE_PIXELS} pixlar",
        )
    except ImportError:
        # Tolken kan inte importera runtimens paket: det är inte en sida som
        # inte gick att avkoda utan en runtime som måste installeras.
        return error(request, NEEDS_SURYA, "runtimens paket är inte installerade i den här tolken")
    except Exception:
        return error(request, UNREADABLE_IMAGE, "sidans bild gick inte att avkoda")

    try:
        page = engine.predictor()([image], full_page=True)[0]
    except Exception as failure:
        code = runtime_failure_code(failure)
        if code is not None:
            return error(request, code, "runtimen kunde inte läsa en sida")
        return error(request, INFERENCE_FAILED, "modellen svarade inte för den här sidan")

    try:
        blocks, has_text = collect_blocks(page, engine)
    except ResultTooLarge:
        return error(request, OUTPUT_TOO_LARGE, "sidans läsning är större än en resultatrad rymmer")
    return {
        "protocol": PROTOCOL_VERSION,
        "type": PAGE_TYPE,
        "pdf_stem": request.get("pdf_stem"),
        "page": request.get("page"),
        # En sida utan blocktext är en tom läsning, inte en lyckad. Om pappret
        # är blankt avgör föräldern, som har rastret.
        "status": STATUS_READ if has_text else STATUS_EMPTY,
        "model": engine.model,
        "blocks": blocks,
    }


def collect_blocks(page: Any, engine: Engine) -> tuple[list[dict[str, Any]], bool]:
    """Sidans block som rutor, och om något av dem har text; nekar så fort gränsen passeras.

    Gränserna förbrukas medan blocken samlas, inte mätt efteråt: en sida med
    hundratusen block är redan den allokering gränsen finns för att hindra.
    Ett block utan både HTML och text ingår inte i läsningen.

    Höjer ``ResultTooLarge`` när sidans HTML-tecken eller blockantal passerar gränsen.
    """
    blocks: list[dict[str, Any]] = []
    page_chars = 0
    has_text = False
    for item in page.blocks:
        block = block_result(item, engine)
        if block is None:
            continue
        page_chars += len(block["html"])
        if len(blocks) >= MAX_BLOCKS or page_chars > MAX_PAGE_CHARS:
            raise ResultTooLarge(f"mer än {MAX_BLOCKS} block eller {MAX_PAGE_CHARS} tecken HTML")
        if has_visible_text(block["html"]):
            has_text = True
        blocks.append(block)
    return blocks, has_text


def open_image(path: str) -> Any:
    """En sidas raster, avkodat inom gränsen för en sidbild.

    Den deklarerade storleken läses ur huvudet före avkodning, eftersom den är
    ett tal en liten fil väljer själv.
    """
    from PIL import Image

    try:
        image: Any = Image.open(path)
    except Image.DecompressionBombError as bomb:
        # PIL:s egen skydd mot dekompressionsbomber är samma sak som för stor sida.
        raise PageTooLarge(str(bomb)) from bomb
    width, height = image.size
    if width <= 0 or height <= 0 or width * height > MAX_PAGE_PIXELS:
        raise PageTooLarge(f"{width}x{height}")
    try:
        image.load()
    except Image.DecompressionBombError as bomb:
        raise PageTooLarge(str(bomb)) from bomb
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    return image


def block_result(block: Any, engine: Engine) -> dict[str, Any] | None:
    """Ett av modellens block som en ruta, eller ``None`` när det saknar HTML.

    Rå HTML skickas vidare: tabeller, rubriker och formler tolkas av föräldern.
    Ett block med markup men utan text (en figur) behålls.
    """
    html = block.html if isinstance(block.html, str) else ""
    if not html.strip():
        return None
    xs = [point[0] for point in block.polygon]
    ys = [point[1] for point in block.polygon]
    return {
        "html": html,
        "label": block.label,
        "reading_order": block.reading_order,
        "bbox": [
            int(round(min(xs))),
            int(round(min(ys))),
            int(round(max(xs))),
            int(round(max(ys))),
        ],
        "confidence": measured_confidence(block),
    }


def measured_confidence(block: Any) -> float | None:
    """Blockets konfidens när runtimen mätte en, och ``None`` när den bara satte in ett värde.

    Utan logprobs rapporterar biblioteket ``1.0`` för varje block — en
    ersättning och inte en mätning. Ingen konfidens är bättre än en påhittad.
    """
    if not logprobs_requested():
        return None
    confidence = block.confidence
    if not isinstance(confidence, (int, float)) or not math.isfinite(confidence):
        return None
    if not 0.0 <= confidence <= 1.0:
        return None
    return float(confidence)


def logprobs_requested() -> bool:
    """Om runtimen ombeds om de tokensannolikheter en konfidens kommer ur."""
    try:
        return bool(surya_settings().SURYA_INFERENCE_LOGPROBS)
    except Exception:
        return False


HTML_TAG = re.compile(r"<[^>]*>")


def has_visible_text(html: str) -> bool:
    """Om HTML:en har någon synlig text efter att taggar strippats och entiteter avkodats.

    ``&nbsp;`` och ``&#160;`` blir ett hårt mellanslag (U+00A0), som räknas som blanksteg.
    """
    text = unescape(HTML_TAG.sub("", html)).replace("\u00a0", " ")
    return bool(text.strip())

# Moduler vars fel betyder att vikterna inte gick att hämta: Hubbens klient och
# HTTP-stacken under den. Ett inferensfel kommer från ``openai`` och står
# medvetet inte här: en server som svarade dåligt är sidans fel.
FETCHING_MODULES = frozenset(
    {"huggingface_hub", "huggingface", "requests", "httpx", "httpcore", "urllib3", "socket", "ssl"}
)


def runtime_failure_code(failure: Exception) -> str | None:
    """Vilken del av runtimen ett fel gäller, eller ``None`` när det är sidans eget fel.

    Klasser frågas hellre än text: ett meddelande är prosa som ändras, en typ gör det inte.
    Ett startfel klassas efter sin orsak; en oklassad orsak är ``SURYA_START_FAILED``.
    """
    if isinstance(failure, RuntimeStartFailed):
        cause = failure.__cause__
        code = runtime_failure_code(cause) if isinstance(cause, Exception) else None
        return code or SURYA_START_FAILED
    if isinstance(failure, ImportError):
        return NEEDS_SURYA
    if _is_spawn_failure(failure):
        return NEEDS_LLAMA_CPP if "llama-server binary not found" in str(failure) else SURYA_START_FAILED
    if type(failure).__module__.split(".")[0] in FETCHING_MODULES:
        return NEEDS_SURYA_MODEL
    return None


def _is_spawn_failure(failure: Exception) -> bool:
    """Om felet är Suryas eget serverstartfel.

    Klassen känns igen på namn i stället för import: den här vägen körs just
    när runtimen har fallerat, och en diagnos får inte själv kräva den trasiga runtimen.
    """
    return type(failure).__name__ == "SpawnError"


def error(request: Any, code: str, message: str) -> dict[str, Any]:
    """Ett felresultat som bär identiteten på begäran det svarar på när det finns en."""
    result: dict[str, Any] = {
        "protocol": PROTOCOL_VERSION,
        "type": PAGE_TYPE,
        "status": STATUS_ERROR,
        "code": code,
        "message": message,
    }
    if isinstance(request, dict):
        for key in ECHO_KEYS:
            if key in request:
                result[key] = request[key]
    return result


def answer(line: str, engine: Engine) -> dict[str, Any]:
    """Svaret på en begäransrad, vad raden än visar sig vara."""
    try:
        request = json.loads(line)
    except Exception:
        return error(None, MALFORMED_REQUEST, "begäran är inte JSON")
    if not isinstance(request, dict):
        return error(None, MALFORMED_REQUEST, "begäran är inte ett JSON-objekt")
    if request.get("protocol") != PROTOCOL_VERSION:
        return error(request, MALFORMED_REQUEST, "begäran har inte det här protokollets version")
    if request.get("type") != PAGE_TYPE:
        return error(request, MALFORMED_REQUEST, "begäran är inte en sida")
    return read_page(request, engine)


def write_result(result: dict[str, Any]) -> None:
    """En resultatrad på stdout, eller gränsens eget svar när resultatet inte ryms."""
    line = encode(result)
    if line is None:
        line = encode(error(result, OUTPUT_TOO_LARGE, "läsningen är längre än en resultatrad rymmer"))
    if line is None:
        line = json.dumps(
            {"protocol": PROTOCOL_VERSION, "type": PAGE_TYPE, "status": STATUS_ERROR, "code": OUTPUT_TOO_LARGE},
            separators=(",", ":"),
        )
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def encode(result: dict[str, Any]) -> str | None:
    """Ett resultat som en rad, eller ``None`` när det når gränsen för en resultatrad."""
    writer = BoundedLine(MAX_RESULT_BYTES)
    try:
        json.dump(result, writer, ensure_ascii=False, separators=(",", ":"))
    except ResultTooLarge:
        return None
    return writer.line()


class BoundedLine:
    """En JSON-last som vägrar växa förbi gränsen för en resultatrad.

    Gränsen förbrukas medan lasten *skrivs*, inte mäts när den finns:
    ``json.dumps`` över en enorm läsning bygger hela raden innan någon hunnit
    fråga hur lång den blev. Det som begränsas är de kodade byten.
    """

    def __init__(self, bound: int) -> None:
        self._bound = bound
        self._written = 0
        self._parts: list[str] = []

    def write(self, chunk: str) -> int:
        over = ResultTooLarge(f"mer än {self._bound} byte")
        if self._written + len(chunk) > self._bound:
            raise over
        self._written += len(chunk.encode("utf-8"))
        if self._written > self._bound:
            raise over
        self._parts.append(chunk)
        return len(chunk)

    def line(self) -> str:
        """Lasten som en rad; efterfrågas först när den är känd för att rymmas."""
        return "".join(self._parts)


def install_signal_handlers() -> None:
    """Gör SIGTERM och SIGINT till ett avslut så att runtimen workern startade slutar med den.

    En process som dödas direkt kör inga ``atexit``-hanterare, och Suryas
    backend dödar sin ``llama-server`` från en sådan.
    """

    def stop(_signum: int, _frame: Any) -> None:
        raise SystemExit(0)

    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, stop)


def write_identity() -> None:
    """En identitetsrad på stdout och inget mer: ingen modell, ingen server, ingen sida.

    En runtime som inte kan beskriva sig själv är inte en krasch utan ett svar
    som blir en *frånvarande* identitet hos föräldern.
    """
    try:
        result: dict[str, Any] = {
            "protocol": PROTOCOL_VERSION,
            "type": IDENTITY_TYPE,
            "status": STATUS_OK,
            "identity": runtime_identity(),
        }
    except Exception:
        result = {
            "protocol": PROTOCOL_VERSION,
            "type": IDENTITY_TYPE,
            "status": STATUS_ERROR,
            "code": NEEDS_SURYA,
        }
    write_result(result)


def main() -> int:
    install_signal_handlers()
    if IDENTITY_FLAG in sys.argv[1:]:
        # Startar ingenting: kostar en Python-start och runtimens metadata, inte en modellladdning.
        write_identity()
        return 0
    reader = ProtocolReader(sys.stdin.buffer, MAX_REQUEST_BYTES)
    engine = Engine()
    try:
        while True:
            try:
                line = reader.read_line()
            except LineTooLong:
                write_result(error(None, MALFORMED_REQUEST, "begäransraden är längre än protokollet rymmer"))
                continue
            if line is None:
                # stdin stängd: föräldern som ägde sidorna är borta, och sidorna med den.
                break
            write_result(answer(line, engine))
    finally:
        engine.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

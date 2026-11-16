"""Bildmotorn ``llm-vision``: en LLM-profil läser sidbilden och skriver av den som Markdown.

Används av ``ocr_pages.py --engine llm-vision --profile <namn>``. Profilen löses
med ``config.resolve_runtime_profile`` och måste vara OpenAI-kompatibel (OpenAI,
OpenRouter, Ollama, custom); Claude-profiler stöds inte i första versionen.

Sidbilden skickas som PNG (base64) i ett ``image_url``-innehåll. Svarta
maskeringsfält märks av modellen med ``[MASKAD]`` där de står i texten; egna
varianter (``[svärtat]``, ``[redacted]``, ``███`` …) skrivs om av
``normalize_mask_markers``. Pixeldetekteringen (``insert_mask_markers``) är
reserv och används bara när modellen inte skrev någon markör (``apply_masks``).
Ren text härleds ur Markdownen (``markdown_to_text``) så att text och Markdown
bär samma ``[MASKAD]``-markörer.

Modulen loggar ingenting själv — anroparen loggar med kontext. Nyckeln står
aldrig i ett felmeddelande eller i ``repr``.
"""

from __future__ import annotations

import base64
import io
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from PIL import Image, ImageDraw, ImageFont

import config as _llm_config
from backends import BACKENDS

# Exitkod från ``ocr_pages.py --engine llm-vision`` när LLM-tjänsten inte går att
# använda (profil, nyckel, kredit, kvot): resten av körningen ska avbrytas.
EXIT_LLM_FATAL = 4
# Längsta sidan på bilden som skickas; större sidor skalas ned proportionellt.
MAX_IMAGE_SIDE = 2000
# Försök per sida och väntetid (s) före respektive nytt försök vid 429/5xx/nätfel.
# Fem försök (2+4+8+16 = 30 s) så att en tillfällig hastighetsgräns hos t.ex.
# OpenRouter inte avbryter en stor parallell körning; ett numeriskt
# ``Retry-After`` går före tabellen men aldrig över ``MAX_RETRY_AFTER_SECONDS``.
MAX_ATTEMPTS = 5
BACKOFF_SECONDS = (2, 4, 8, 16)
MAX_RETRY_AFTER_SECONDS = 60.0
# HTTP-koder som betyder att ingen sida kommer att gå bättre: nyckel/kredit/behörighet.
FATAL_STATUS = frozenset({401, 402, 403})
RATE_LIMIT_STATUS = 429
MAX_OUTPUT_TOKENS = 8192
REQUEST_TIMEOUT_SECONDS = 180.0
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
MASK = "[MASKAD]"
CHECK_TEXT = "PALME 1986"

PROMPT = (
    "Du får en bild av en skannad sida ur ett svenskt polisarkiv. Skriv av sidan "
    "ordagrant som Markdown.\n"
    "- Skriv exakt det som står, på originalspråket, med originalets stavning, "
    "förkortningar och skiljetecken. Rätta, översätt eller sammanfatta aldrig.\n"
    "- Bevara radbrytningar och läsordning. Rubriker blir Markdown-rubriker, "
    "tabeller blir Markdown-tabeller och ifyllda blankettfält skrivs som "
    "**Etikett:** värde.\n"
    "- Gissa aldrig. Skriv [oläsligt] för ord eller partier som inte går att läsa.\n"
    "- Svärtade eller maskerade fält skrivs som [MASKAD] på det ställe där de står, "
    "ett [MASKAD] per fält. Försök aldrig återge vad som döljs.\n"
    "- Svara bara med avskriften: inga kommentarer, förklaringar eller kodblock."
)


@dataclass(frozen=True)
class VisionProfile:
    """En löst LLM-profil för bildläsning. ``api_key`` visas aldrig i ``repr``."""

    name: str
    model: str
    base_url: str
    api_key: str = field(repr=False)
    external: bool


@dataclass(frozen=True)
class VisionResult:
    """Modellens avskrift och API:ets rapporterade token (``None`` = okänt)."""

    markdown: str
    input_tokens: int | None
    output_tokens: int | None


class VisionError(Exception):
    """Sidan kunde inte läsas. ``fatal``: ingen följande sida kommer att gå bättre."""

    def __init__(self, message: str, *, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal


def _host(base_url: str) -> str:
    return urlsplit(base_url).hostname or ""


def load_profile(name: str) -> VisionProfile:
    """Lös LLM-profilen ``name``; ``ValueError`` för okänd profil eller Claude-backend."""
    if not name:
        raise ValueError("Motorn llm-vision kräver en LLM-profil (--profile).")
    saved = _llm_config.load_profile(name)
    resolved = _llm_config.resolve_runtime_profile(saved, BACKENDS)
    if resolved["kind"] != "openai":
        raise ValueError(
            f"LLM-profilen {name!r} använder {resolved['backend_name']} "
            f"(tjänsten {resolved['kind'] or 'okänd'}), som motorn llm-vision inte "
            "stöder ännu — välj en OpenAI-kompatibel profil (OpenAI, OpenRouter, "
            "Ollama eller en egen endpoint) med en bildmodell."
        )
    base_url = str(resolved.get("base_url") or "")
    if not base_url:
        raise ValueError(f"LLM-profilen {name!r} saknar endpoint (base_url).")
    model = str(resolved.get("model") or "")
    if not model:
        raise ValueError(f"LLM-profilen {name!r} saknar modell.")
    return VisionProfile(
        name=name,
        model=model,
        base_url=base_url,
        api_key=str(resolved.get("api_key") or ""),
        external=_host(base_url) not in LOCAL_HOSTS,
    )


def vision_profile_names() -> list[str]:
    """LLM-profilerna som motorn llm-vision kan använda, standardprofilen först.

    Samma profilkatalog som Admin (``config.load_all``), avsmalnad till
    OpenAI-kompatibla profiler med endpoint och modell — Claude-profiler och
    profiler med okänd tjänst faller bort. Nycklar läses inte (tom miljö), så
    en profil utan nyckel listas och stoppas först vid bildkontrollen.
    """
    catalog = _llm_config.load_all()
    profiles: dict[str, dict] = catalog["profiles"]
    names = sorted(profiles, key=lambda name: name != catalog["default"])
    usable: list[str] = []
    for name in names:
        try:
            resolved = _llm_config.resolve_runtime_profile(profiles[name], BACKENDS, environ={})
        except ValueError:
            continue
        if resolved["kind"] == "openai" and resolved.get("base_url") and resolved.get("model"):
            usable.append(name)
    return usable


def destination(profile: VisionProfile) -> str:
    """Vart sidbilderna skickas, t.ex. ``openrouter.ai (extern)`` — för loggen."""
    netloc = urlsplit(profile.base_url).netloc.rsplit("@", 1)[-1] or profile.base_url
    return f"{netloc} ({'extern' if profile.external else 'lokal'})"


def make_client(profile: VisionProfile):
    """En synkron OpenAI-klient mot profilens endpoint (inga nätanrop här)."""
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover
        raise VisionError("openai-paketet saknas — kör scripts/install.py", fatal=True) from exc
    return OpenAI(api_key=profile.api_key or "local", base_url=profile.base_url,
                  timeout=REQUEST_TIMEOUT_SECONDS, max_retries=0)


def _png_data_url(image: Image.Image) -> str:
    if max(image.size) > MAX_IMAGE_SIDE:
        image = image.copy()
        image.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE), Image.Resampling.LANCZOS)
    if image.mode not in ("L", "RGB"):
        image = image.convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


_FENCE_RE = re.compile(r"\A\s*```[\w-]*\s*\n(.*?)\n?```\s*\Z", re.DOTALL)


def _strip_fence(content: str) -> str:
    """Ta bort ett kodblock runt hela svaret (vanligt trots instruktionen)."""
    match = _FENCE_RE.match(content)
    return (match.group(1) if match else content).strip()


def _status(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


def _retry_after(exc: BaseException) -> float | None:
    """Ett numeriskt ``Retry-After`` (sekunder) ur felets HTTP-svar, annars ``None``."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get("retry-after")
    except Exception:  # noqa: BLE001 — okänd headertyp: använd tabellen
        return None
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        return None  # saknas, eller ett HTTP-datum: använd tabellen
    if seconds != seconds or seconds < 0:  # NaN eller negativt
        return None
    return min(seconds, MAX_RETRY_AFTER_SECONDS)


def _backoff(exc: BaseException, attempt: int) -> float:
    """Väntetiden före försök ``attempt + 2``: ``Retry-After`` om det finns, annars tabellen."""
    hinted = _retry_after(exc)
    return hinted if hinted is not None else BACKOFF_SECONDS[attempt]


def _request(client, profile: VisionProfile, data_url: str):
    """Ett anrop med återförsök; ``VisionError`` när det inte gick."""
    last: BaseException | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            return client.chat.completions.create(
                model=profile.model,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": PROMPT},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }],
                max_tokens=MAX_OUTPUT_TOKENS,
                temperature=0,
            )
        except Exception as exc:  # noqa: BLE001 — klassas via status_code
            status = _status(exc)
            if status in FATAL_STATUS:
                raise VisionError(
                    f"profilen {profile.name}: HTTP {status} från {_host(profile.base_url)} "
                    "(nyckel, kredit eller behörighet) — "
                    f"{type(exc).__name__}", fatal=True,
                ) from exc
            retryable = status is None or status == RATE_LIMIT_STATUS or status >= 500
            if not retryable:
                raise VisionError(
                    f"profilen {profile.name}: HTTP {status} — {exc}"[:500]
                ) from exc
            last = exc
            if attempt + 1 < MAX_ATTEMPTS:
                time.sleep(_backoff(exc, attempt))
    status = _status(last) if last is not None else None
    if status == RATE_LIMIT_STATUS:
        raise VisionError(
            f"profilen {profile.name}: HTTP 429 (kvot eller hastighetsgräns) efter "
            f"{MAX_ATTEMPTS} försök", fatal=True,
        ) from last
    raise VisionError(
        f"profilen {profile.name}: anropet misslyckades efter {MAX_ATTEMPTS} försök — "
        f"{type(last).__name__}: {last}"[:500]
    ) from last


def transcribe(image: Image.Image, profile: VisionProfile, *, client=None) -> VisionResult:
    """Låt profilens modell skriva av sidbilden som Markdown.

    Ett svar som inte avslutats med ``stop`` (längd, innehållsfilter, fel) är en
    misslyckad sida — aldrig en kort läsning.
    """
    client = client if client is not None else make_client(profile)
    response = _request(client, profile, _png_data_url(image))
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise VisionError(f"profilen {profile.name}: svaret saknar innehåll")
    choice = choices[0]
    finish = getattr(choice, "finish_reason", None)
    if finish != "stop":
        raise VisionError(
            f"profilen {profile.name}: svaret avslutades med {finish!r} i stället för "
            "'stop' (avklippt eller avbrutet) — sidan räknas som oläst"
        )
    content = getattr(getattr(choice, "message", None), "content", None) or ""
    usage = getattr(response, "usage", None)
    return VisionResult(
        markdown=_strip_fence(content),
        input_tokens=getattr(usage, "prompt_tokens", None),
        output_tokens=getattr(usage, "completion_tokens", None),
    )


def _check_image() -> Image.Image:
    """Testbilden: ``PALME 1986`` i svart på vitt."""
    image = Image.new("RGB", (720, 200), "white")
    try:
        font: ImageFont.ImageFont | ImageFont.FreeTypeFont = ImageFont.load_default(size=72)
    except TypeError:  # pragma: no cover — Pillow < 10.1
        font = ImageFont.load_default()
    ImageDraw.Draw(image).text((40, 50), CHECK_TEXT, fill="black", font=font)
    return image


def check_profile(profile: VisionProfile, *, client=None) -> None:
    """Kontrollera att profilens modell kan läsa en bild; annars ``VisionError(fatal=True)``."""
    try:
        result = transcribe(_check_image(), profile, client=client)
    except VisionError as exc:
        if exc.fatal:
            raise
        raise VisionError(
            f"profilen {profile.name} kan inte läsa bilder (bildkontrollen misslyckades: {exc})",
            fatal=True,
        ) from exc
    answer = result.markdown.upper()
    if "PALME" not in answer or "1986" not in answer:
        raise VisionError(
            f"profilen {profile.name} kan inte läsa bilder (modellen {profile.model} "
            f"svarade inte med testbildens text {CHECK_TEXT!r})",
            fatal=True,
        )


def _is_table_row(line: str) -> bool:
    return line.lstrip().startswith("|")


def insert_mask_markers(markdown: str, blocks: list[tuple[int, int]], image_height: int) -> str:
    """Infoga ``[MASKAD]`` som eget stycke vid varje maskeringsblock.

    Samma höjdproportionella regel som ``ocr_pages._merge_redaction_markers`` utan
    radkoordinater, räknat på Markdown-raderna. En markör som skulle hamna inne i
    en tabell flyttas till raden efter tabellen, så att tabellen hålls ihop.
    """
    if not blocks or image_height <= 0:
        return markdown
    lines = markdown.split("\n")
    n = len(lines)
    positions: list[int] = []
    for b0, b1 in blocks:
        idx = max(0, min(n, round((b0 + b1) / 2 / image_height * n)))
        while 0 < idx < n and _is_table_row(lines[idx - 1]) and _is_table_row(lines[idx]):
            idx += 1
        positions.append(idx)
    for idx in sorted(positions, reverse=True):
        lines[idx:idx] = ["", MASK, ""]
    out = "\n".join(lines)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip("\n")


# Modellens egna maskeringsmarkörer: ord inom hakparentes som betyder att texten
# är svärtad, och rader av fyllda block. Ett ensamt blocktecken räknas inte.
_MODEL_MASK_RE = re.compile(
    r"\[\s*(?:svärta\w*|maskera\w*|maskad\w*|överstr\w*|censur\w*|redact\w*"
    r"|blacked(?:\s+out)?|svart\s+fält|svärtad\s+text|maskerad\s+text)"
    r"(?:\s+\w+)?\s*\]"
    r"|[\u2588\u25A0]{2,}",
    re.IGNORECASE,
)


def normalize_mask_markers(markdown: str) -> str:
    """Skriv om modellens egna maskeringsmarkörer till ``[MASKAD]``."""
    return _MODEL_MASK_RE.sub(MASK, markdown)


def apply_masks(markdown: str, blocks: list[tuple[int, int]], image_height: int) -> str:
    """Maskeringsmarkörer för en llm-vision-läsning.

    Modellens markörer (omskrivna till ``[MASKAD]``) står där fältet står i texten
    och går före; pixelblocken infogas bara när modellen inte skrev någon, så att
    markörerna aldrig dubbleras.
    """
    normalized = normalize_mask_markers(markdown)
    if MASK in normalized:
        return normalized
    return insert_mask_markers(normalized, blocks, image_height)


_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+")
_QUOTE_RE = re.compile(r"^\s{0,3}>\s?")


def markdown_to_text(markdown: str) -> str:
    """Ren text ur Markdown: bara syntax utan eget textinnehåll tas bort.

    Rubrikens ``#``, citatets ``>``, fetstil (``**``/``__``) och tabellernas
    lodstreck och avgränsarrader försvinner; tabellrader blir tabbseparerade
    celler (som ren text från ``surya-layout``). Listmarkörer står kvar ordagrant
    — ett punktnummer (``12.``, ``2)``) är arkivets text, och ``- ``/``* ``-rader
    och ``* * *`` lämnas som de är. Radbrytningar och ``[MASKAD]``/``[oläsligt]``
    behålls.
    """
    out: list[str] = []
    for line in markdown.split("\n"):
        if _TABLE_SEPARATOR_RE.match(line) and "-" in line and "|" in line:
            continue
        if _is_table_row(line):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            line = "\t".join(cells)
        else:
            line = _QUOTE_RE.sub("", line)
            line = _HEADING_RE.sub("", line)
        line = line.replace("**", "").replace("__", "")
        out.append(line.rstrip())
    return "\n".join(out)

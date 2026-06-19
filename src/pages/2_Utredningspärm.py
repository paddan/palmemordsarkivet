"""Streamlit-sida: sparade svar och källbokmärken."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import casebook_ui as _casebook_ui  # noqa: E402

_casebook_ui.render_casebook_page(ROOT, _casebook_ui.state_conn())

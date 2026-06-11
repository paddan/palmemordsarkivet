"""Streamlit-sida: utforska kunskapsgrafen som interaktivt ego-nätverk.

Fristående multipage-sida (kräver inga ändringar i webui.py). Söker en entitet
i Neo4j, ritar dess nätverk med pyvis och listar källdokumenten med PDF-knappar.
Degraderar snällt om Neo4j inte är igång.
"""
from __future__ import annotations

import base64
import subprocess
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import citations as _citations  # noqa: E402
from graph import viz  # noqa: E402

st.set_page_config(page_title="Palmemordsarkivet — Graf", layout="wide")
st.title("Kunskapsgraf")
st.caption("Sök en person, plats eller organisation och utforska dess nätverk i materialet.")

password = viz.resolve_password()
if not password:
    st.warning(
        "Neo4j-lösenord saknas. Starta grafen med `./neo4j.sh` "
        "(skapar `neo4j/.password`) och ladda den med `./load_graph.sh`."
    )
    st.stop()

@st.cache_resource(show_spinner=False)
def _driver(pw: str):
    """Singleton Neo4j-driver (stabil över reruns). Vid omstart av Neo4j —
    rensa cachen via menyn ⋮ → Clear cache, eller starta om appen."""
    return viz.connect(pw)


try:
    driver = _driver(password)
except Exception as exc:  # noqa: BLE001
    st.error(
        f"Kan inte nå Neo4j på `bolt://localhost:7687`. Starta den med "
        f"`./neo4j.sh`.\n\n```\n{exc}\n```"
    )
    st.stop()


@st.cache_data(ttl=30, show_spinner=False)
def _search(q: str) -> list[dict]:
    with driver.session() as s:
        return viz.search_entities(s, q, limit=25)


with st.sidebar:
    st.header("Sök")
    query = st.text_input("Namn (del räcker)", placeholder="t.ex. Engström")
    show_docs = st.toggle("Visa dokument som nämner entiteten", value=True)
    max_nodes = st.slider("Max grannar", 10, 150, 80,
                          help="Tak på antal relationsrader som hämtas runt entiteten.")
    graph_height = st.slider("Grafhöjd (px)", 400, 1400, 660, step=20,
                             help="Justera graffönstrets höjd.")

if not query.strip():
    st.info("Skriv ett namn i sidofältet för att rita nätverket.")
    st.stop()

hits = _search(query.strip())
if not hits:
    st.info(f"Inga entiteter matchar “{query}”.")
    st.stop()

labels = [f'{h["namn"]}  ·  {h["label"]}' for h in hits]
idx = st.selectbox("Träffar", range(len(hits)), format_func=lambda i: labels[i])
seed = hits[idx]

# Ackumulera utfällda noder. Byter man sökt frö nollställs utfällningen.
seed_key = (seed["label"], seed["norm"])
if st.session_state.get("graph_seed") != seed_key:
    st.session_state["graph_seed"] = seed_key
    st.session_state["graph_centers"] = [seed]
centers = st.session_state["graph_centers"]

# Hämta och slå ihop ego-nätverken för alla utfällda center.
all_rels: list[dict] = []
all_docs: list[dict] = []
with driver.session() as s:
    for c in centers:
        rels, docs = viz.fetch_ego(s, c["norm"], c["label"], limit=max_nodes)
        all_rels.extend(rels)
        for d in docs:
            all_docs.append({**d, "center_norm": c["norm"]})
all_rels = viz.dedup_rels(all_rels)
if not show_docs:
    all_docs = []

nodes, edges = viz.assemble_graph(centers, all_rels, all_docs)
docs = all_docs

# Sidofält: fäll ut en grannod (lägg till den som nytt center) eller nollställ.
center_ids = {c["norm"] for c in centers}
expandable = sorted(
    {(n["namn"], n["id"], n["type"]) for n in nodes
     if n["type"] != "Dokument" and n["id"] not in center_ids},
    key=lambda t: t[0].lower(),
)
with st.sidebar:
    st.divider()
    st.subheader("Expandera")
    st.caption(f"Utfällda noder: {len(centers)}")
    if expandable:
        pick = st.selectbox(
            "Grannod att fälla ut", range(len(expandable)),
            format_func=lambda i: f"{expandable[i][0]}  ·  {expandable[i][2]}")
        if st.button("Fäll ut noden", use_container_width=True):
            namn, nid, typ = expandable[pick]
            st.session_state["graph_centers"].append(
                {"norm": nid, "namn": namn, "label": typ})
            st.rerun()
    else:
        st.caption("Inga fler grannar att fälla ut.")
    if len(centers) > 1 and st.button("Återställ till sökt nod", use_container_width=True):
        st.session_state["graph_centers"] = [seed]
        st.rerun()

center_names = ", ".join(f"{c['namn']} ({c['label']})" for c in centers)
st.caption(f"**{center_names}** — {len(nodes)} noder, {len(edges)} kanter")

if len(nodes) <= 1 and not edges:
    st.info("Entiteten har inga registrerade relationer eller dokument i grafen ännu.")
else:
    # st.iframe bäddar in pyvis-HTML via data-URL (sandboxad iframe där
    # vis.js-skripten kör). Ersätter avvecklade st.components.v1.html.
    html = viz.build_pyvis_html(nodes, edges, height=f"{graph_height}px")
    data_url = "data:text/html;base64," + base64.b64encode(html.encode("utf-8")).decode("ascii")
    st.iframe(data_url, height=graph_height)

# Källdokument med PDF-knappar (återanvänder citations-uppslaget mot filsystemet).
if docs:
    st.subheader(f"Källdokument ({len(docs)})")
    nr_to_pdf = _citations.build_nr_to_pdf(ROOT)
    for i, d in enumerate(docs):
        nr = (d.get("nr") or "").strip()
        cands = _citations.resolve_nr_all(nr, nr_to_pdf) if nr else []
        with st.container(border=True):
            cols = st.columns([6, 2])
            with cols[0]:
                st.markdown(f"**{d.get('nr', '')}** — {d.get('titel', '') or d['stem']}")
            with cols[1]:
                if cands and st.button("Öppna PDF", key=f"pdf_{i}",
                                       use_container_width=True):
                    try:
                        subprocess.Popen(["open", str(cands[0])])
                    except OSError as e:
                        st.error(f"Kan inte öppna fil: {e}")

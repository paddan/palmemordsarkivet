from search_workbench import hit_excerpt, hit_key, hit_title


def test_hit_key_uses_source_page_and_chunk() -> None:
    hit = {"source": "100 — Skandiaförhör.txt", "page": 28, "chunk_idx": 3}

    assert hit_key(hit) == "100 — Skandiaförhör.txt:28:3"


def test_hit_title_formats_nr_page_and_title() -> None:
    hit = {"nr": "100", "page": 28, "titel": "Skandiaförhör"}

    assert hit_title(hit) == "Nr 100, sida 28 — Skandiaförhör"


def test_hit_excerpt_collapses_whitespace_and_truncates() -> None:
    hit = {
        "text": (
            "  Första raden\n\nandra\t raden med extra mellanrum. "
            "Tredje raden fortsätter längre än maxgränsen."
        )
    }

    assert hit_excerpt(hit, max_chars=55) == (
        "Första raden andra raden med extra mellanrum. Tredje..."
    )

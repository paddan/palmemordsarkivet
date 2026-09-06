"""Källbunden granskning och ren projektion av extraherade grafposter."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from copy import deepcopy


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def validate_suggestion_evidence(
    item: dict, action: str, target: dict, evidence: str, source_text: str,
) -> None:
    """Kräv aktuellt, ordagrant källstöd för ett LLM-förslag."""
    def normalize(value: str) -> str:
        return " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    normalized_evidence = normalize(evidence) if isinstance(evidence, str) else ""
    if not normalized_evidence or normalized_evidence not in normalize(source_text):
        raise ValueError("LLM-förslagets källbelägg finns inte på den aktuella sidan")
    if action == "replace":
        values = ((target.get("namn", ""),) if item["kind"] == "entity"
                  else (target.get("fran", ""), target.get("till", "")))
        if any(normalize(value) not in normalized_evidence for value in values):
            raise ValueError("LLM-förslagets ersättning stöds inte av källbelägget")


def audit_entries(entries: list[dict], decisions: list[dict]) -> list[dict]:
    """Inventera originalen; beslut gäller endast exakt samma sidpayload."""
    from graph.load_neo4j import LABELS, normalize_name

    by_key = {d["item_key"]: d for d in decisions}
    result = []
    for entry in entries:
        payload = entry["payload"]
        source_hash = _hash(payload)
        entities = payload.get("entiteter", [])
        labels: dict[str, set[str]] = {}
        for entity in entities:
            labels.setdefault(normalize_name(entity.get("namn", "")), set()).add(entity.get("typ", ""))
        for kind, values in (("entity", entities), ("relation", payload.get("relationer", []))):
            seen: set[str] = set()
            for index, original in enumerate(values):
                issues = []
                signature = _hash(original)
                if signature in seen:
                    issues.append("Dubblett på sidan")
                seen.add(signature)
                if kind == "entity":
                    name = original.get("namn", "").strip()
                    if not name:
                        issues.append("Tomt namn")
                    if original.get("typ") not in LABELS:
                        issues.append("Okänd entitetstyp")
                    if original.get("typ") == "person" and (len(name) <= 4 or len(name.split()) < 2):
                        issues.append("Kort eller ensamt personnamn; kontrollera initialer")
                    if re.search(r"MASK|\ufffd|[\[\]{}|_]|\d", name, re.IGNORECASE):
                        issues.append("Möjlig maskering eller OCR-skräp")
                else:
                    for field in ("fran", "till"):
                        norm = normalize_name(original.get(field, ""))
                        if not norm or norm not in labels:
                            issues.append(f"Saknad ändpunkt: {field}")
                        elif len(labels[norm]) > 1:
                            issues.append(f"Tvetydig ändpunkt: {field}")
                    if not original.get("typ", "").strip():
                        issues.append("Tom relationstyp")
                    if normalize_name(original.get("fran", "")) == normalize_name(original.get("till", "")):
                        issues.append("Självkant")
                key = _hash([entry["pdf_stem"], entry["page_num"], kind, index])
                decision = by_key.get(key)
                result.append({"item_key": key, "source_hash": source_hash, "kind": kind,
                               "pdf_stem": entry["pdf_stem"], "page_num": entry["page_num"],
                               "original": deepcopy(original), "issues": issues,
                               "decision": deepcopy(decision),
                               "stale": bool(decision and decision["source_hash"] != source_hash)})
    return result


def validate_decision(item: dict, action: str, target: dict, note: str) -> None:
    """Validera ett manuellt beslut innan det sparas."""
    from graph.load_neo4j import LABELS

    if not isinstance(note, str) or not note.strip():
        raise ValueError("En motivering krävs")
    if action not in {"keep", "exclude", "replace", "reset"}:
        raise ValueError("Okänt granskningsbeslut")
    if action != "replace":
        return
    fields = ("namn", "typ") if item["kind"] == "entity" else ("fran", "typ", "till")
    if not isinstance(target, dict) or any(
        not isinstance(target.get(field), str) or not target[field].strip() for field in fields
    ):
        raise ValueError("Alla ersättningsfält måste fyllas i")
    if item["kind"] == "entity" and target["typ"] not in LABELS:
        raise ValueError("Okänd entitetstyp")


def build_reviewed_rows(
    entries: list[dict], decisions: list[dict], name_rules: list[dict] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Applicera giltiga beslut per förekomst utan att ändra originaldata.

    Ändpunkter matchas via sidans entiteter. Tvetydiga etiketter hoppas över eftersom
    relationens format inte anger vilken av entiteterna som avses.
    """
    from graph.load_neo4j import (
        LABELS,
        _doc_meta,
        build_surname_map,
        display_name,
        normalize_name,
    )

    rule_targets = {
        (str(rule.get("typ", "")).casefold(), normalize_name(str(rule.get("source", "")))):
        str(rule.get("target", "")).strip()
        for rule in (name_rules or [])
        if str(rule.get("target", "")).strip()
    }

    def effective_entity(item: dict) -> dict:
        decision = item["decision"] if not item["stale"] else None
        entity: dict = deepcopy(
            decision["target"] if decision and decision["action"] == "replace"
            else item["original"]
        )
        if decision and decision["action"] == "replace":
            return entity
        replacement = rule_targets.get((
            str(entity.get("typ", "")).casefold(), normalize_name(entity.get("namn", "")),
        ))
        if replacement:
            entity["namn"] = replacement
        return entity

    pages: dict[tuple[str, int], list[dict]] = {}
    for item in audit_entries(entries, decisions):
        pages.setdefault((item["pdf_stem"], item["page_num"]), []).append(item)
    # Endast effektiva, inkluderade personnamn får ge dokumentets uppslag.
    effective_entries = []
    for (stem, _page), items in pages.items():
        entities = []
        for item in items:
            if item["kind"] != "entity":
                continue
            d = item["decision"] if not item["stale"] else None
            if d and d["action"] == "exclude":
                continue
            entities.append(effective_entity(item))
        effective_entries.append({"pdf_stem": stem, "payload": {"entiteter": entities}})
    surname_maps = build_surname_map(effective_entries)
    mentions: list[dict] = []
    relations: list[dict] = []
    for (stem, page), items in pages.items():
        original_targets: dict[str, set[tuple[str, str] | None]] = {}
        current_targets: dict[str, set[tuple[str, str]]] = {}
        original_labels: dict[str, set[str]] = {}
        for item in items:
            if item["kind"] != "entity":
                continue
            original = item["original"]
            old_norm = normalize_name(original.get("namn", ""))
            original_labels.setdefault(old_norm, set()).add(original.get("typ", ""))
            d = item["decision"] if not item["stale"] else None
            entity = effective_entity(item)
            name = entity.get("namn", "")
            if not d and entity.get("typ") == "person":
                name = surname_maps.get(stem, {}).get(normalize_name(name), name)
            norm = normalize_name(name)
            label = LABELS.get(entity.get("typ", ""))
            target = (norm, label) if norm and label and not (d and d["action"] == "exclude") else None
            original_targets.setdefault(old_norm, set()).add(target)
            if target is None:
                continue
            current_targets.setdefault(norm, set()).add(target)
            mentions.append({**_doc_meta(stem), "sida": page, "norm": norm,
                             "label": label, "namn": display_name(name)})

        def resolve(
            raw: str, replacement: bool,
            original_targets: dict[str, set[tuple[str, str] | None]] = original_targets,
            original_labels: dict[str, set[str]] = original_labels,
            current_targets: dict[str, set[tuple[str, str]]] = current_targets,
        ) -> tuple[str, str] | None:
            norm = normalize_name(raw)
            # Ett ursprungligt tvetydigt namn eller en uteslutning får aldrig
            # bli en godtycklig nod genom en relationsändring.
            if norm in original_targets:
                targets = original_targets[norm]
                if len(original_labels[norm]) != 1 or len(targets) != 1:
                    return None
                return next(iter(targets))
            current = current_targets.get(norm, set()) if replacement else set()
            return next(iter(current)) if len(current) == 1 else None

        for item in items:
            if item["kind"] != "relation":
                continue
            d = item["decision"] if not item["stale"] else None
            if d and d["action"] == "exclude":
                continue
            replacement = bool(d and d["action"] == "replace")
            rel = d["target"] if d and replacement else item["original"]
            start = resolve(rel.get("fran", ""), replacement)
            end = resolve(rel.get("till", ""), replacement)
            if start is None or end is None:
                continue
            relations.append({"stem": stem, "sida": page, "typ": rel.get("typ", ""),
                              "fran_norm": start[0], "fran_label": start[1],
                              "till_norm": end[0], "till_label": end[1]})
    return mentions, relations


def preview_name_rule(
    entries: list[dict], decisions: list[dict], name_rules: list[dict], candidate: dict,
) -> dict[str, int]:
    """Räkna vilka projekterade rader en ny eller ändrad namnregel påverkar."""
    from graph.load_neo4j import normalize_name

    typ = str(candidate.get("typ", "")).casefold()
    source = normalize_name(str(candidate.get("source", "")))
    replacement_rules = [
        rule for rule in name_rules
        if not (
            str(rule.get("typ", "")).casefold() == typ
            and normalize_name(str(rule.get("source", ""))) == source
        )
    ] + [candidate]
    before_mentions, before_relations = build_reviewed_rows(entries, decisions, name_rules)
    after_mentions, after_relations = build_reviewed_rows(entries, decisions, replacement_rules)
    changed_mentions = [
        after for before, after in zip(before_mentions, after_mentions, strict=False)
        if (before["label"], before["norm"], before["namn"])
        != (after["label"], after["norm"], after["namn"])
    ]
    changed_relations = sum(
        before != after for before, after in zip(before_relations, after_relations, strict=False)
    ) + abs(len(before_relations) - len(after_relations))
    return {
        "entities": len(changed_mentions),
        "pages": len({(row["stem"], row["sida"]) for row in changed_mentions}),
        "relations": changed_relations,
    }

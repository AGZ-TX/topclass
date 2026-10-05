from __future__ import annotations

import copy
import hashlib
import json
import math
import time
from collections import defaultdict
from pathlib import Path

from book_matches import load_readings, load_resolutions, RESOLUTION_FILES
from bibliographic_findings import PATH as FINDINGS_PATH
from catalog import load_catalog
from course_map import read_payload
from education_selection import STOP_WORDS, GENERIC_WORDS, grounded_fields, tokens, match_requirement, passes_filters
from reading_enrichment import FOLDER, child
from universe import build_universe, explicitly_nondegree
from materials_ledger import baseline_material_entries
from course_evidence import load_course_evidence
from course_description_enrichment import load_description_supplement, description_variants_for
from course_code_aliases import load_aliases, aliases_for


ROOT = Path(__file__).resolve().parents[1]


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class CatalogDiscovery:
    def __init__(self, courses, materials, snapshot=None):
        self.nodes = {}
        self.edges = defaultdict(list)
        self.postings = defaultdict(set)
        self.search_fields = {}
        self.snapshot = snapshot or {"format": "topclass-discovery-snapshot-v1", "fingerprint": fingerprint([courses, materials])}
        for record in courses:
            node_id = record.get("discovery_id") or "course:" + record["course_key"]
            payload = dict(record)
            node = {"id": node_id, "kind": "course", "label": str(record.get("title") or record.get("course_title") or node_id),
                    "payload": payload, "fingerprint": fingerprint(payload)}
            self.nodes[node_id] = node
            index_fields = grounded_fields(payload)
            index_fields.extend(('course_code_aliases', alias) for alias in payload.get('course_code_aliases', []))
            if "discovery_academic_areas" in payload:
                index_fields = [(field, text) for field, text in index_fields if field != "academic_areas"]
                index_fields.extend(("discovery_academic_areas", area["label"])
                                    for area in payload["discovery_academic_areas"]
                                    if isinstance(area, dict) and isinstance(area.get("label"), str))
            for word in {word for field, text in index_fields for word in tokens(text)}:
                self.postings[word].add(node_id)
            self.search_fields[node_id] = [(field, tuple(word for word in tokens(text) if word not in STOP_WORDS))
                                          for field, text in index_fields]
        by_key = {node["payload"]["course_key"]: node["id"] for node in self.nodes.values()}
        for index, material in enumerate(materials):
            course_id = by_key.get(material["course_key"])
            if not course_id:
                continue
            node_id = "materials:" + fingerprint([material["course_key"], index, material])[:24]
            self.nodes[node_id] = {"id": node_id, "kind": "materials-status", "label": material.get("status", "unknown"),
                                   "payload": material, "fingerprint": fingerprint(material)}
            self.edges[course_id].append({"src": course_id, "dst": node_id, "relation": "materials-evidence"})

    @classmethod
    def from_records(cls, courses, materials, snapshot=None):
        return cls(courses, materials, snapshot)

    def get(self, node_id):
        if node_id not in self.nodes:
            raise ValueError("Unknown node: " + node_id)
        return self.nodes[node_id]

    def neighbors(self, node_id):
        return self.edges.get(node_id, [])

    def search(self, query, kinds=("course",), limit=20):
        query_words = tuple(word for word in tokens(query) if word not in STOP_WORDS)
        wanted = set(query_words)
        candidates = set()
        for word in wanted:
            for node_id in self.postings.get(word, []):
                if self.nodes[node_id]["kind"] in kinds:
                    candidates.add(node_id)
        scores = {node_id: self.search_score(node_id, query_words, wanted) for node_id in candidates}
        ids = sorted(scores, key=lambda node_id: tuple(-value for value in scores[node_id].values()) + (node_id,))
        return [self.get(node_id) | {"lexical_rank": rank, "discovery_score": scores[node_id]}
                for rank, node_id in enumerate(ids[:limit])]

    def search_score(self, node_id, query_words, wanted):
        source, official, facets = set(), set(), set()
        passage_terms, phrase, title_terms = 0, 0, 0
        for field, words in self.search_fields[node_id]:
            found = wanted.intersection(words)
            if field in {"title", "course_title", "description", "description_variants"}:
                source.update(found)
                passage_terms = max(passage_terms, len(found))
                phrase = max(phrase, int(len(query_words) > 1 and any(
                    words[index:index + len(query_words)] == query_words
                    for index in range(len(words) - len(query_words) + 1))))
                if field in {"title", "course_title"}:
                    title_terms = max(title_terms, len(found))
            elif field in {"academic_areas", "discovery_academic_areas", "expertise_tags"}:
                facets.update(found)
            else:
                official.update(found)
        weighted = sum(math.log1p(len(self.search_fields) / (1 + len(self.postings[word])))
                       for word in source if word not in GENERIC_WORDS)
        return {"source_specificity": weighted, "source_terms": len(source), "source_field_terms": passage_terms, "source_phrase": phrase,
                "title_terms": title_terms, "official_terms": len(official), "facet_terms": len(facets)}

    def is_active(self, node_id):
        return node_id in self.nodes


def load_discovery(root=ROOT, *, include_nondegree=False, baseline_only=False, research_brief=None):
    root = Path(root)
    public_path = root / 'data/public/catalog.json'
    if public_path.exists():
        from distribution import load_public_catalog

        if baseline_only:
            raise ValueError('The public metadata copy does not include the preserved v0.4 archive')
        return load_public_catalog(public_path)
    started = time.monotonic()
    expansion = root / "data" / "expansion"
    watched = {} if baseline_only else {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in
               [child(child(expansion, FOLDER), name) for name in RESOLUTION_FILES if child(child(expansion, FOLDER), name).exists()] +
               [expansion / "course-materials-2026-27" / "manifest.json", child(expansion, FOLDER) / "manifest.json"]}
    findings_path = FINDINGS_PATH
    if findings_path.exists():
        watched[findings_path] = hashlib.sha256(findings_path.read_bytes()).hexdigest()
    readings = baseline_material_entries(root) if baseline_only else load_readings(expansion, all_saved=True)
    evidence_path = root / "data" / "course-evidence" / "verified-courses.json"
    if not baseline_only and evidence_path.exists():
        watched[evidence_path] = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    supplement = {"reading_rows": [], "description_passages": {}} if baseline_only else load_course_evidence(evidence_path)
    description_path = root / "data" / "course-evidence" / "teaching-descriptions.json"
    if not baseline_only and description_path.exists():
        watched[description_path] = hashlib.sha256(description_path.read_bytes()).hexdigest()
    descriptions = {} if baseline_only else load_description_supplement(description_path)
    alias_path = root / 'data/course-evidence/course-code-aliases.json'
    if alias_path.exists():
        watched[alias_path] = hashlib.sha256(alias_path.read_bytes()).hexdigest()
    aliases = load_aliases(alias_path)
    readings = [*readings, *supplement["reading_rows"]]
    if not include_nondegree:
        readings = [row for row in readings if not explicitly_nondegree(row)]
    resolutions = {} if baseline_only else load_resolutions(child(expansion, FOLDER), load_readings(expansion))
    projection = build_universe(readings, resolutions, with_books=True)
    groups = {}
    for university in projection["universities"]:
        for course in university["courses"]:
            record = {"course_key": course["id"], "discovery_id": course["id"], "institution": university["name"],
                      "code": course["code"], "title": course["title"], "school": course["school"],
                      "books": course["books"], "classification_status": "unknown",
                      "source_course_keys": sorted({source["course_key"] for source in course["source_records"]}),
                      "source_records": course["source_records"], "description_variants": []}
            for key in record["source_course_keys"]:
                groups[key] = record
    records = {value["course_key"]: value for value in groups.values()}
    taxonomy_path = expansion / "expertise-taxonomy-v1.json"
    taxonomy_bytes = taxonomy_path.read_bytes() if taxonomy_path.exists() else None
    taxonomy = json.loads(taxonomy_bytes) if taxonomy_bytes else {}
    if taxonomy_bytes is not None:
        watched[taxonomy_path] = hashlib.sha256(taxonomy_bytes).hexdigest()
    for row in readings:
        record = groups.get(row["course_key"])
        if record:
            enrich_classification(record, row, taxonomy)
            for passage in supplement["description_passages"].get(row["course_key"], []):
                record["description_variants"].append({
                    "description": passage["quote"], "course_key": row["course_key"],
                    "inventory": "verified-course-evidence", "source_urls": [passage["source_url"]],
                    "locator": passage["locator"], "source_year": passage["source_year"],
                    "term": passage["source_term"]})
    backlog = {}
    status_by_key = {row["course_key"]: row for row in readings} if research_brief else {}
    research_requirements = ([item["description"] for item in research_brief.get("capabilities", [])] or
                             research_brief.get("specialties", []) + research_brief.get("deliverables", [])) if research_brief else []
    manifests = [] if baseline_only else [{"dataset": "bibliography:" + name, "sha256": hashlib.sha256(child(child(expansion, FOLDER), name).read_bytes()).hexdigest()}
                 for name in RESOLUTION_FILES if child(child(expansion, FOLDER), name).exists()]
    if findings_path in watched:
        manifests.append({'dataset': 'book-finding-evidence', 'sha256': watched[findings_path]})
    if taxonomy_bytes is not None:
        manifests.append({"dataset": "discovery-taxonomy", "sha256": watched[taxonomy_path]})
    if evidence_path in watched:
        manifests.append({"dataset": "verified-course-evidence", "sha256": watched[evidence_path]})
    if description_path in watched:
        manifests.append({"dataset": "teaching-descriptions", "sha256": watched[description_path]})
    if alias_path in watched:
        manifests.append({'dataset': 'course-code-aliases', 'sha256': watched[alias_path]})
    baseline, _, baseline_manifest = load_catalog()
    manifests.append({"dataset": "v0.4", "sha256": baseline_manifest["compressed_sha256"]})
    for original in baseline["courses"]:
        record = groups.get("topclass-v0.4:" + original["course_id"])
        if record:
            enrich(record, original, "v0.4")
            record["description_variants"].extend(description_variants_for(original, descriptions))
            record.update(aliases_for(original, aliases))
        elif research_brief:
            research_lead(backlog, original | {"course_key": "topclass-v0.4:" + original["course_id"], "institution": original.get("school", "")}, status_by_key, research_requirements, research_brief)
    if not baseline_only:
        index_path = root / "data" / "expansion" / "catalog-datasets-2026-27.json"
        index_bytes = index_path.read_bytes()
        index = json.loads(index_bytes)
        watched[index_path] = hashlib.sha256(index_bytes).hexdigest()
        manifests.append({"dataset": "catalog-index", "sha256": hashlib.sha256(index_bytes).hexdigest()})
        for pair in index["dataset_pairs"]:
            folder = root / "data" / "expansion" / pair["inventory"]
            manifest_bytes = (folder / "manifest.json").read_bytes()
            manifest = json.loads(manifest_bytes)
            watched[folder / "manifest.json"] = hashlib.sha256(manifest_bytes).hexdigest()
            manifests.append({"dataset": pair["inventory"], "sha256": hashlib.sha256(manifest_bytes).hexdigest()})
            for entry in manifest["files"]:
                if entry.get("collection", "courses") != "courses":
                    continue
                source_payload = read_payload(folder, entry, "courses")
                for original in source_payload["courses"]:
                    original = original | {"institution": original.get("institution") or source_payload.get("institution") or entry.get("institution", "")}
                    record = groups.get(original["course_key"])
                    if record:
                        enrich(record, original, pair["inventory"])
                        for variant in description_variants_for(original, descriptions):
                            if variant not in record["description_variants"]:
                                record["description_variants"].append(variant)
                    elif research_brief:
                        research_lead(backlog, original, status_by_key, research_requirements, research_brief)
    if not baseline_only:
        material_manifest = root / "data" / "expansion" / "course-materials-2026-27" / "manifest.json"
        manifests.append({"dataset": "materials", "sha256": watched[material_manifest]})
    statuses = []
    for row in readings:
        record = groups.get(row["course_key"])
        if record:
            statuses.append(row | {"course_key": record["course_key"], "source_course_key": row["course_key"]})
    snapshot = {"format": "topclass-discovery-snapshot-v1", "fingerprint": fingerprint(manifests),
                "sources": manifests, "source_rows": len(readings), "usable_course_groups": len(records),
                "course_book_links": projection["summary"]["course_book_links"],
                "degree_scope": "all-saved-scopes" if include_nondegree else "excludes-explicit-nondegree; unknown retained",
                "build_seconds": round(time.monotonic() - started, 3), "storage": "in-memory postings; no full evidence graph required"}
    if not baseline_only:
        current_description_hash = hashlib.sha256(description_path.read_bytes()).hexdigest() if description_path.exists() else None
        if current_description_hash != watched.get(description_path):
            raise ValueError("Teaching-description evidence changed during discovery; retry against a stable saved snapshot")
    for path, observed in watched.items():
        if hashlib.sha256(path.read_bytes()).hexdigest() != observed:
            raise ValueError("Catalog evidence changed during discovery; retry against a stable saved snapshot")
    alias_hash = hashlib.sha256(alias_path.read_bytes()).hexdigest() if alias_path.exists() else None
    if alias_hash != watched.get(alias_path):
        raise ValueError('Course code aliases changed during discovery')
    catalog = CatalogDiscovery(list(records.values()), statuses, snapshot)
    catalog.enrichment_backlog = sorted(backlog.values(), key=lambda item: (item["title"], item["course_id"]))
    catalog.snapshot["build_seconds"] = round(time.monotonic() - started, 3)
    return catalog


def enrich(record, original, inventory):
    description = original.get("description")
    if isinstance(description, str) and description.strip():
        variant = {"description": description, "inventory": inventory,
                   "course_key": original.get("course_key") or original.get("course_id"),
                   "source_urls": original.get("source_urls") or [original.get("source_url", "")]}
        for field in ("source_year", "term", "snapshot"):
            if field in original:
                variant[field] = copy.deepcopy(original[field])
        if variant not in record["description_variants"]:
            record["description_variants"].append(variant)
    for field in ("prerequisites", "academic_subject", "department", "departments", "subject", "official_subject", "level", "career", "program_roles"):
        if original.get(field) and not record.get(field):
            record[field] = original[field]


def enrich_classification(record, original, taxonomy):
    labels = {area["id"]: area["label"] for area in taxonomy.get("academic_areas", [])}
    specialties = {tag["id"]: tag["label"] for tag in taxonomy.get("expertise_domains", [])}
    areas = [copy.deepcopy(area) for area in original.get("academic_areas", [])
             if isinstance(area, dict) and area.get("id") in labels]
    primary = original.get("primary_academic_area")
    if primary in labels and not any(area["id"] == primary for area in areas):
        areas.append({"id": primary, "label": labels[primary], "basis": "saved-classification-candidate"})
    facets = [copy.deepcopy(area) for area in original.get("discovery_academic_areas", areas)
              if isinstance(area, dict) and area.get("id") in labels]
    tags = [copy.deepcopy(tag) for tag in original.get("expertise_tags", [])
            if isinstance(tag, dict) and isinstance(tag.get("id"), str) and isinstance(tag.get("label"), str)]
    specialty = original.get("primary_expertise")
    if specialty in specialties and not any(tag["id"] == specialty for tag in tags):
        tags.append({"id": specialty, "label": specialties[specialty], "basis": "saved-specialty-candidate",
                     "human_reviewed": False})
    if not areas and not tags and not original.get("model_classification") and "discovery_academic_areas" not in original:
        return
    for field, incoming in (("academic_areas", areas), ("discovery_academic_areas", facets), ("expertise_tags", tags)):
        existing = record.setdefault(field, [])
        identifiers = {value["id"] for value in existing}
        for value in incoming:
            if value["id"] not in identifiers:
                existing.append(value)
                identifiers.add(value["id"])
    provenance = {"source_course_key": original.get("course_key") or original.get("course_id"),
                  "primary_academic_area": primary,
                  "primary_expertise": specialty,
                  "classification_status": original.get("classification_status") or "candidate",
                  "academic_areas": areas, "discovery_academic_areas": facets, "expertise_tags": tags}
    for field in ("classification_method", "classification_version", "source_fingerprint", "classification_fingerprint",
                  "model_classification", "review_reasons", "primary_resolution"):
        if field in original:
            provenance[field] = copy.deepcopy(original[field])
    sources = record.setdefault("classification_provenance", [])
    if provenance not in sources:
        sources.append(provenance)
    states = {source["classification_status"] for source in sources}
    record["classification_status"] = ("needs-review" if "needs-review" in states else
                                       "reviewed" if states == {"reviewed"} else "candidate")
    record["review_reasons"] = sorted({reason for source in sources for reason in source.get("review_reasons", [])})
    primaries = {source["primary_academic_area"] for source in sources if source["primary_academic_area"] in labels}
    if len(primaries) > 1:
        record["classification_status"] = "needs-review"
        record["review_reasons"] = sorted(set(record["review_reasons"]) | {"conflicting-group-source-classifications"})
    record["primary_expertise_ids"] = sorted({source["primary_expertise"] for source in sources
                                              if source["primary_expertise"] in specialties})


def research_lead(backlog, original, status_by_key, requirements, brief):
    status = status_by_key.get(original["course_key"])
    if not status or len(backlog) >= 200 or not passes_filters(original, grounded_fields(original), brief):
        return
    matches = [value for value in requirements if match_requirement(value, grounded_fields(original), str(brief.get("role", "")))]
    if not matches:
        return
    key = original["course_key"]
    backlog[key] = {"course_id": "course:" + key, "title": original.get("title", ""), "requirements": matches,
                    "gap": "No documented named book; retained only for later reading enrichment", "usable": False,
                    "materials_statuses": [status.get("status", "unknown")],
                    "source_leads": original.get("source_urls") or [original.get("source_url", "")],
                    "backlog_state": "later enrichment; no selection or coverage credit"}

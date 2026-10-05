import copy
import argparse
import hashlib
import json
import lzma
from pathlib import Path
import re

try:
    from .jev_course_classification import BATCH_VERSION, batch_record, course_state, identity_warning, state_fingerprint
except ImportError:
    from jev_course_classification import BATCH_VERSION, batch_record, course_state, identity_warning, state_fingerprint


OVERLAY_NAME = "course-classification-jev-2026-10-01"
FIELD_REASONS = {"unmapped-area", "conflicting-primary-areas", "description-only-area", "conflicting-source-classifications"}
RULE_FIELDS = ("primary_academic_area", "academic_areas", "primary_candidates", "primary_resolution",
               "primary_selection_basis", "classification_status", "classification_method", "classification_version",
               "review_reasons", "classification_fingerprint")


def overlay_index(payload, taxonomy):
    if payload.get("format") != "topclass-jev-course-fields-v2" or payload.get("question_version") != BATCH_VERSION:
        raise ValueError("Unsupported course-classification overlay")
    taxonomy_hash = state_fingerprint(taxonomy)
    if payload.get("taxonomy_fingerprint") != taxonomy_hash:
        raise ValueError("Course-classification overlay taxonomy is stale")
    records = payload.get("records")
    if not isinstance(records, list):
        raise ValueError("Course-classification overlay requires records")
    indexed = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("course_key"), str) or not record["course_key"]:
            raise ValueError("Course-classification decisions require source course keys")
        if record["course_key"] in indexed:
            raise ValueError("Duplicate course-classification source key")
        if record.get("question_version") != BATCH_VERSION or record.get("taxonomy_fingerprint") != taxonomy_hash:
            raise ValueError("Course-classification record version or taxonomy is stale")
        indexed[record["course_key"]] = record
    return indexed


def refinement_index(payload, taxonomy):
    try:
        from .jev_refine_courses import REFINEMENT_VERSION
    except ImportError:
        from jev_refine_courses import REFINEMENT_VERSION
    taxonomy_hash = state_fingerprint(taxonomy)
    if payload.get("question_version") != REFINEMENT_VERSION or payload.get("taxonomy_fingerprint") != taxonomy_hash:
        raise ValueError("Course-classification refinement version or taxonomy is stale")
    if not isinstance(payload.get("records"), list):
        raise ValueError("Course-classification refinements require records")
    indexed = {}
    for record in payload["records"]:
        key = record.get("course_key") if isinstance(record, dict) else None
        if not isinstance(key, str) or not key or key in indexed:
            raise ValueError("Course-classification refinements require distinct course keys")
        if record.get("question_version") != REFINEMENT_VERSION or record.get("taxonomy_fingerprint") != taxonomy_hash:
            raise ValueError("Course-classification refinement record version or taxonomy is stale")
        indexed[key] = record
    return indexed


def facet_index(payload, taxonomy):
    try:
        from .jev_discovery_facets import DISCOVERY_FACETS_VERSION, RUBRIC_FINGERPRINT
    except ImportError:
        from jev_discovery_facets import DISCOVERY_FACETS_VERSION, RUBRIC_FINGERPRINT
    taxonomy_hash = state_fingerprint(taxonomy)
    if (payload.get("format") != "topclass-jev-discovery-facets-v4" or payload.get("question_version") != DISCOVERY_FACETS_VERSION
            or payload.get("taxonomy_fingerprint") != taxonomy_hash or not isinstance(payload.get("records"), list)):
        raise ValueError("Discovery facet registration version, taxonomy or records are invalid")
    indexed = {}
    for wrapper in payload["records"]:
        key = wrapper.get("course_key") if isinstance(wrapper, dict) else None
        if not isinstance(key, str) or not key or key in indexed:
            raise ValueError("Discovery facets require distinct source course keys")
        if set(wrapper) not in ({"course_key", "readings", "decision"}, {"course_key", "readings", "decision", "provider_receipt"}):
            raise ValueError("Discovery facet registration requires an exact source-bound wrapper schema")
        readings, decision = wrapper.get("readings"), wrapper.get("decision")
        if not isinstance(readings, list) or any(not isinstance(row, dict) or row.get("course_key") != key for row in readings):
            raise ValueError("Discovery facet readings must retain the exact original course identity")
        if (not isinstance(decision, dict) or decision.get("course_key") != key or decision.get("question_version") != DISCOVERY_FACETS_VERSION
                or decision.get("taxonomy_fingerprint") != taxonomy_hash or decision.get("rubric_fingerprint") != RUBRIC_FINGERPRINT):
            raise ValueError("Discovery facet decision source, version, taxonomy or rubric is invalid")
        validate_provider_receipt(wrapper)
        indexed[key] = wrapper
    return indexed


def validate_provider_receipt(wrapper, course=None, taxonomy=None):
    receipt = wrapper.get("provider_receipt")
    if receipt is None:
        return
    required = {"question_version", "rubric_fingerprint", "resolved_model", "request_fingerprint",
                "raw_answers_fingerprint", "provider_call_fingerprint", "assessment_method"}
    if not isinstance(receipt, dict) or set(receipt) != required:
        raise ValueError("Local facet reassessment requires an exact paid-provider receipt schema")
    if (receipt["assessment_method"] != "local-evidence-revalidation-no-provider-call"
            or not isinstance(receipt["question_version"], str) or not receipt["question_version"]
            or receipt["resolved_model"] != wrapper["decision"].get("resolved_model")):
        raise ValueError("Local facet reassessment provider version, method or model is invalid")
    for field in ("rubric_fingerprint", "request_fingerprint", "raw_answers_fingerprint", "provider_call_fingerprint"):
        if not isinstance(receipt[field], str) or not re.fullmatch(r"[0-9a-f]{64}", receipt[field]):
            raise ValueError("Local facet reassessment requires SHA256 receipt fingerprints")
    if receipt["raw_answers_fingerprint"] != state_fingerprint(wrapper["decision"].get("raw_answers")):
        raise ValueError("Local facet reassessment changed the paid provider answers")
    if course is not None:
        try:
            from .jev_discovery_facets import facet_request
        except ImportError:
            from jev_discovery_facets import facet_request
        request = facet_request(course, wrapper["readings"], taxonomy)
        request.pop("model", None)
        if receipt["request_fingerprint"] != state_fingerprint(request):
            raise ValueError("Local facet reassessment changed the paid provider request")


def source_snapshots(root):
    root = Path(root)
    if root.is_symlink():
        raise ValueError("Original source snapshot root cannot use symlinks")
    excluded = {OVERLAY_NAME, "course-materials-2026-27"}
    registry = root / "catalog-datasets-2026-27.json"
    if registry.exists():
        excluded.update(pair["classification_index"] for pair in json.loads(registry.read_text())["dataset_pairs"])
    result = []
    targets = [("expansion", root, path) for path in sorted(root.rglob("*"))
               if path.relative_to(root).parts[0] not in excluded and (path.is_file() or path.is_symlink())]
    baseline_manifest = root.parent / "manifest.json"
    if baseline_manifest.exists():
        baseline = json.loads(baseline_manifest.read_text())
        if isinstance(baseline.get("parts"), list):
            targets.append(("baseline", root.parent, baseline_manifest))
            for part in baseline["parts"]:
                if not re.fullmatch(r"catalog\.xz\.\d{3}", part.get("file", "")):
                    raise ValueError("Unexpected original baseline archive path")
                targets.append(("baseline", root.parent, root.parent / part["file"]))
    for scope, base, path in targets:
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            raise ValueError("Original source snapshots cannot use symlinks")
        raw = path.read_bytes()
        result.append({"scope": scope, "path": path.relative_to(base).as_posix(),
                       "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    return sorted(result, key=lambda entry: (entry["scope"], entry["path"]))


def load_overlay(root, taxonomy=None):
    folder = Path(root) / OVERLAY_NAME
    if not folder.exists():
        return None
    if folder.is_symlink() or (folder / "manifest.json").is_symlink():
        raise ValueError("Course-classification registration cannot use symlinks")
    if taxonomy is None:
        taxonomy = json.loads((Path(root) / "expertise-taxonomy-v1.json").read_text(encoding="utf-8"))
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("public_metadata_only") is not True or manifest.get("taxonomy_fingerprint") != state_fingerprint(taxonomy):
        raise ValueError("Course-classification manifest must bind public metadata and the current taxonomy")
    payloads = {}
    names = {"course_classifications": "classifications.json.xz", "course_refinements": "refinements.json.xz",
             "course_discovery_facets": "facets.json.xz"}
    for entry in manifest.get("files", []):
        collection = entry.get("collection")
        if collection not in names or entry.get("file") != names[collection] or collection in payloads:
            raise ValueError("Course-classification manifest has an unsupported or duplicate collection")
        path = folder / entry["file"]
        if path.is_symlink():
            raise ValueError("Course-classification data cannot use symlinks")
        compressed = path.read_bytes()
        if hashlib.sha256(compressed).hexdigest() != entry.get("compressed_sha256") or len(compressed) != entry.get("compressed_bytes"):
            raise ValueError("Course-classification compressed file integrity mismatch")
        payload = json.loads(lzma.decompress(compressed))
        if not isinstance(payload.get("records"), list) or len(payload["records"]) != entry.get("records"):
            raise ValueError("Course-classification registered record count mismatch")
        payloads[collection] = payload
    if "course_classifications" not in payloads:
        raise ValueError("Course-classification registration requires first-pass decisions")
    indexed = overlay_index(payloads["course_classifications"], taxonomy)
    if "course_refinements" in payloads:
        for key, refinement in refinement_index(payloads["course_refinements"], taxonomy).items():
            original = indexed.get(key) or {"course_key": key, "question_version": BATCH_VERSION,
                "state_fingerprint": refinement.get("state_fingerprint"), "taxonomy_fingerprint": state_fingerprint(taxonomy),
                "status": "not-completed"}
            indexed[key] = original | {"refinement": refinement}
    if "course_discovery_facets" in payloads:
        snapshots = manifest.get("upstream_source_snapshots")
        if not isinstance(snapshots, list) or not snapshots or snapshots != source_snapshots(root):
            raise ValueError("Discovery facet original source snapshot set or fingerprints changed")
        for key, facet in facet_index(payloads["course_discovery_facets"], taxonomy).items():
            if key not in indexed:
                raise ValueError("Discovery facets require an existing first-pass course identity")
            indexed[key] = indexed[key] | {"facets": facet}
    return indexed


def write_snapshot(first_pass, taxonomy, out, *, refinements=None, facets=None, source_root=None):
    out = Path(out)
    if any(path.is_symlink() for path in (out.absolute(), *out.absolute().parents)):
        raise ValueError("Course-classification snapshot output cannot use symlinks")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise FileExistsError("Use a new or empty course-classification output directory")
    overlay_index(first_pass, taxonomy)
    payloads = [("classifications.json.xz", "course_classifications", first_pass)]
    if refinements is not None:
        refinement_index(refinements, taxonomy)
        payloads.append(("refinements.json.xz", "course_refinements", refinements))
    snapshots = None
    if facets is not None:
        facet_index(facets, taxonomy)
        if source_root is None:
            raise ValueError("Discovery facets require original source snapshot bindings")
        snapshots = source_snapshots(source_root)
        if not snapshots:
            raise ValueError("Discovery facets require a nonempty original source snapshot set")
        payloads.append(("facets.json.xz", "course_discovery_facets", facets))
    generated = []
    for name, collection, payload in payloads:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        compressed = lzma.compress(encoded)
        entry = {"file": name, "collection": collection, "records": len(payload["records"]),
                 "compressed_sha256": hashlib.sha256(compressed).hexdigest(), "compressed_bytes": len(compressed),
                 "question_version": payload["question_version"],
                 "models": sorted({record.get("decision", record)["resolved_model"] for record in payload["records"]
                                   if record.get("decision", record).get("resolved_model")})}
        generated.append((entry, compressed))
    manifest = {"format": "topclass-jev-course-classification-manifest-v1", "public_metadata_only": True,
                "taxonomy_version": taxonomy.get("version"), "taxonomy_fingerprint": state_fingerprint(taxonomy),
                "human_reviewed": False, "scope": "Source metadata discovery candidates, not verified course identities, learning outcomes or book adoption.",
                "first_pass_summary": first_pass.get("summary", {}),
                "files": [entry for entry, _ in generated]}
    if refinements is not None:
        manifest["refinement_summary"] = refinements.get("summary", {})
    if snapshots is not None:
        manifest["upstream_source_snapshots"] = snapshots
        manifest["discovery_facet_summary"] = facets.get("summary", {})
    out.mkdir(parents=True, exist_ok=True)
    for entry, compressed in generated:
        with (out / entry["file"]).open("xb") as destination:
            destination.write(compressed)
    with (out / "manifest.json").open("x", encoding="utf-8") as destination:
        json.dump(manifest, destination, ensure_ascii=False, indent=2)
        destination.write("\n")
    return manifest


def binding_course(course, taxonomy=None):
    evidence = course.get("classification_evidence", {})
    context = evidence.get("institution_subject_context", {}) if isinstance(evidence, dict) else {}
    if (isinstance(context, dict) and context.get("matched_field") == "course_code"
            and course.get("code") and course.get("code") == course.get("course_code")):
        result = copy.copy(course)
        result["classification_evidence"] = evidence | {"institution_subject_context": context | {"matched_field": "code"}}
        return result
    if (not context and taxonomy and taxonomy.get("institution_subject_contexts")
            and course.get("offering_status") != "bookstore-listed/registrar-unverified"):
        try:
            from .classify_courses import institution_subject_context, normalize
        except ImportError:
            from classify_courses import institution_subject_context, normalize
        lookup = {normalize(alias): {normalize(subject["prefix"]): subject | {"source_url": entry["source_url"]}
                                    for subject in entry["subjects"]}
                  for entry in taxonomy["institution_subject_contexts"] for alias in entry["institution_aliases"]}
        derived = institution_subject_context(course, {"context_lookup": lookup})
        if derived:
            result = copy.copy(course)
            result["classification_evidence"] = evidence | {"institution_subject_context": derived}
            return result
    return course


def validated_refinement(record, course, taxonomy):
    course = binding_course(course, taxonomy)
    try:
        from .jev_refine_courses import FIELD_BOUNDARIES, REFINEMENT_VERSION, revalidate_refinement
    except ImportError:
        from jev_refine_courses import FIELD_BOUNDARIES, REFINEMENT_VERSION, revalidate_refinement
    bindings = {"course_key": course["course_key"], "question_version": REFINEMENT_VERSION,
                "state_fingerprint": state_fingerprint(course_state(course)),
                "taxonomy_fingerprint": state_fingerprint(taxonomy), "rubric_fingerprint": state_fingerprint(FIELD_BOUNDARIES)}
    if any(record.get(field) != value for field, value in bindings.items()):
        raise ValueError("Course refinement does not match current metadata, taxonomy or rubric")
    checked = revalidate_refinement(record, course, taxonomy)
    for field in ("primary_academic_area", "classification_status", "primary_specialty", "specialty_status"):
        if record.get(field) != checked.get(field):
            raise ValueError("Course refinement contains forged derived fields")
    return checked


def strong_rule_primary(rules, taxonomy):
    identifiers = {area["id"] for area in taxonomy["academic_areas"]}
    primary = rules.get("primary_academic_area")
    if primary not in identifiers or rules.get("primary_resolution") != "resolved" or rules.get("primary_candidates") != [primary]:
        return None
    if set(rules.get("review_reasons", [])) & {"conflicting-primary-areas", "conflicting-source-classifications", "bookstore-identity-unverified"}:
        return None
    for area in rules.get("academic_areas", []):
        strength = area.get("evidence_strength", [])
        if area.get("id") == primary and strength and strength[0] >= 3 and area.get("matched_evidence"):
            return primary
    return None


def discovery_areas(course, rules, provenance, taxonomy):
    if identity_warning(course):
        return []
    labels = {area["id"]: area["label"] for area in taxonomy["academic_areas"]}
    facets = {}
    for area in rules.get("academic_areas", []):
        strength = area.get("evidence_strength", [])
        if area.get("id") in labels and strength and strength[0] >= 3 and area.get("matched_evidence"):
            facets[area["id"]] = {"id": area["id"], "label": labels[area["id"]],
                                  "basis": "source-grounded-rules-candidate", "human_reviewed": False,
                                  "source_evidence_fields": sorted({item["field"] for item in area["matched_evidence"] if item.get("field")})}
    primary = provenance.get("primary_academic_area")
    if provenance.get("classification_status") == "candidate" and primary in labels:
        facets[primary] = {"id": primary, "label": labels[primary], "basis": "jev-source-metadata-candidate",
                          "human_reviewed": False, "confidence": provenance["answer"]["confidence"],
                          "model": provenance.get("resolved_model"), "source_fingerprint": provenance["state_fingerprint"]}
        matching = next((area for area in rules.get("academic_areas", []) if area.get("id") == primary), None)
        if matching and matching.get("matched_evidence"):
            facets[primary]["source_evidence_fields"] = sorted({item["field"] for item in matching["matched_evidence"] if item.get("field")})
    if provenance.get("specialty_status") == "candidate" and provenance.get("primary_specialty"):
        domain = next(domain for domain in taxonomy["expertise_domains"] if domain["id"] == provenance["primary_specialty"])
        for identifier in domain["academic_area_ids"]:
            if identifier not in facets:
                facets[identifier] = {"id": identifier, "label": labels[identifier],
                    "basis": "taxonomy-parent-of-jev-specialty-candidate", "specialty_id": domain["id"],
                    "human_reviewed": False, "confidence": provenance["specialty_answer"]["confidence"],
                    "model": provenance.get("resolved_model"), "source_fingerprint": provenance["state_fingerprint"]}
    return [facets[key] for key in sorted(facets)]


def prioritize_specific_facets(areas):
    specific_fields = {"title", "course_title", "description", "description_variants", "subject_description"}
    affiliation_fields = {"code", "course_code", "subject_code", "official_department_labels", "official_school_labels",
                          "catalog_department", "academic_subject", "departments", "subject_areas", "school", "college",
                          "institution_subject_context.department", "institutional_subject_context.department"}
    specific = {area["id"] for area in areas if any(anchor.get("kind") == "course" and anchor.get("field") in specific_fields
                for anchor in area.get("anchor_candidates", [])) or set(area.get("source_evidence_fields", [])) & specific_fields}
    if not specific:
        return areas
    result = []
    for area in areas:
        fields = set(area.get("source_evidence_fields", []))
        fields.update(anchor.get("field", "") for anchor in area.get("anchor_candidates", []) if anchor.get("kind") == "course")
        if area["id"] in specific or not fields or not fields <= affiliation_fields:
            result.append(area)
    return result


def load_discovery_supplements(root, taxonomy):
    root = Path(root)
    result = {"public": [], "bibliographic": None, "source_context": {}}
    try:
        from .public_subject_evidence import load_evidence
        from .bibliographic_subjects import load_supplement, FOLDER as BIBLIOGRAPHIC_FOLDER
        from .source_subject_context import load_source_subject_context, FOLDER as SOURCE_CONTEXT_FOLDER
    except ImportError:
        from public_subject_evidence import load_evidence
        from bibliographic_subjects import load_supplement, FOLDER as BIBLIOGRAPHIC_FOLDER
        from source_subject_context import load_source_subject_context, FOLDER as SOURCE_CONTEXT_FOLDER
    if (root / "public-subject-evidence-2026-10-01").is_dir():
        result["public"] = load_evidence(taxonomy, root=root.parent.parent)
    if (root / BIBLIOGRAPHIC_FOLDER).is_dir():
        result["bibliographic"] = load_supplement(taxonomy, root=root.parent.parent)
    if (root / SOURCE_CONTEXT_FOLDER).is_dir():
        result["source_context"] = load_source_subject_context(root, taxonomy)
    return result


def apply_discovery_supplements(course, record, taxonomy, supplements, *, source_urls=None):
    key = course["course_key"]
    bibliography = supplements.get("bibliographic")
    bibliography_match = bibliography is not None and any(binding["course_key"] == key for binding in bibliography["bindings"])
    context = supplements.get("source_context", {}).get(key)
    public_match = any(row["source_course_key"] == key for row in supplements.get("public", []))
    if not (bibliography_match or context or public_match):
        return course
    wrapper = (record or {}).get("facets")
    if (bibliography_match or context) and not isinstance(wrapper, dict):
        raise ValueError(f"Discovery supplements require original source-bound readings for {key}")
    readings = wrapper["readings"] if isinstance(wrapper, dict) else []
    result = course
    try:
        from .public_subject_evidence import apply_subject_evidence
        from .bibliographic_subjects import apply_bibliographic_subjects
        from .source_subject_context import source_subject_facets
    except ImportError:
        from public_subject_evidence import apply_subject_evidence
        from bibliographic_subjects import apply_bibliographic_subjects
        from source_subject_context import source_subject_facets
    if public_match:
        result = apply_subject_evidence(result, taxonomy, supplements["public"], source_urls=source_urls)
    if bibliography_match:
        result = apply_bibliographic_subjects(result, taxonomy, readings, bibliography)
    if context:
        result = copy.deepcopy(result)
        additions = source_subject_facets(binding_course(result, taxonomy), readings, taxonomy, context)
        result["source_subject_context_evidence"] = {"facets": additions, "human_reviewed": False,
            "current_adoption_verified": False, "task_fit_verified": False}
        result["discovery_academic_areas"] = result.get("discovery_academic_areas", []) + additions
    result["discovery_academic_areas"] = prioritize_specific_facets(result.get("discovery_academic_areas", []))
    return result


def apply_record(course, record, taxonomy, *, allow_rule_fallback=True):
    result = copy.deepcopy(course)
    bound_course = binding_course(course, taxonomy)
    rules = copy.deepcopy(course.get("rule_classification") or {field: course[field] for field in RULE_FIELDS if field in course})
    result["rule_classification"] = rules
    result["original_primary_academic_area"] = rules.get("primary_academic_area", "")
    preserved = set(rules.get("review_reasons", [])) - FIELD_REASONS
    primary = "other-academic-subject"
    method = "jev-source-metadata-candidate"
    if record is None:
        preserved.add("jev-classification-not-completed")
        provenance = {"course_key": course["course_key"], "question_version": BATCH_VERSION,
                      "state_fingerprint": state_fingerprint(course_state(bound_course)),
                      "taxonomy_fingerprint": state_fingerprint(taxonomy), "status": "not-completed", "human_reviewed": False}
    else:
        bindings = {"course_key": course["course_key"], "question_version": BATCH_VERSION,
                    "state_fingerprint": state_fingerprint(course_state(bound_course)),
                    "taxonomy_fingerprint": state_fingerprint(taxonomy)}
        if any(record.get(field) != value for field, value in bindings.items()):
            raise ValueError("Course-classification decision does not match current source metadata or taxonomy")
        if "answer" in record:
            checked = batch_record(bound_course, taxonomy, record["answer"], record.get("resolved_model"))
            for field in ("primary_academic_area", "classification_status"):
                if record.get(field) != checked[field]:
                    raise ValueError("Course-classification decision has forged derived fields")
            primary = checked["primary_academic_area"]
            provenance = copy.deepcopy(checked)
            if checked["classification_status"] != "candidate":
                preserved.add("jev-subject-needs-review")
                preserved.update(checked.get("review_reasons", []))
            elif rules.get("primary_academic_area") not in (None, "", "other-academic-subject", primary):
                preserved.add("jev-rule-field-disagreement")
        elif record.get("status") in ("invalid-answer", "not-completed"):
            provenance = copy.deepcopy({field: value for field, value in record.items() if field != "refinement"})
            preserved.add("jev-invalid-answer" if record["status"] == "invalid-answer" else "jev-classification-not-completed")
        else:
            raise ValueError("Course-classification record requires an answer or explicit invalid status")
        if "refinement" in record:
            refined = validated_refinement(record["refinement"], course, taxonomy)
            first = provenance
            provenance = copy.deepcopy(refined)
            provenance["first_pass"] = first
            primary = refined["primary_academic_area"]
            preserved -= {"jev-subject-needs-review", "low-model-confidence", "insufficient-subject-evidence",
                          "choice-distribution-conflict", "jev-invalid-answer", "jev-classification-not-completed",
                          "jev-rule-field-disagreement"}
            if refined["classification_status"] != "candidate":
                preserved.add("jev-subject-needs-review")
                preserved.update(refined.get("review_reasons", []))
            elif rules.get("primary_academic_area") not in (None, "", "other-academic-subject", primary):
                preserved.add("jev-rule-field-disagreement")
    if primary == "other-academic-subject" and allow_rule_fallback:
        fallback = strong_rule_primary(rules, taxonomy)
        if fallback:
            primary = fallback
            method = "source-grounded-rules-fallback-after-jev-review"
            provenance["discovery_source"] = "source-grounded-rules-fallback"
    if identity_warning(course):
        preserved.add("bookstore-identity-unverified")
    provenance["identity_warning"] = identity_warning(course)
    provenance["human_reviewed"] = False
    result["model_classification"] = provenance
    result["discovery_academic_areas"] = discovery_areas(course, rules, provenance, taxonomy)
    if record is not None and "facets" in record:
        try:
            from .jev_discovery_facets import revalidate_facets, grounded_facets, GROUNDING_VERSION
        except ImportError:
            from jev_discovery_facets import revalidate_facets, grounded_facets, GROUNDING_VERSION
        wrapper = record["facets"]
        readings = wrapper.get("readings")
        if (wrapper.get("course_key") != course["course_key"] or not isinstance(readings, list)
                or any(not isinstance(row, dict) or row.get("course_key") != course["course_key"] for row in readings)):
            raise ValueError("Discovery facet reading identity does not match the current source course")
        try:
            checked = revalidate_facets(wrapper["decision"], bound_course, readings, taxonomy)
        except ValueError as exc:
            raise ValueError(f"Discovery facet revalidation failed for {course['course_key']}: {exc}") from exc
        validate_provider_receipt(wrapper, bound_course, taxonomy)
        if "provider_receipt" in wrapper:
            checked = checked | {"provider_receipt": copy.deepcopy(wrapper["provider_receipt"])}
        result["facet_classification"] = copy.deepcopy(checked)
        facets = {area["id"]: area for area in result["discovery_academic_areas"]}
        facets.update({area["id"]: copy.deepcopy(area) | {"model": checked["resolved_model"],
                       "source_fingerprint": checked["source_fingerprint"]} for area in checked["accepted_facets"]})
        literal = grounded_facets(bound_course, readings, taxonomy)
        result["literal_facet_classification"] = {"grounding_version": GROUNDING_VERSION,
            "source_fingerprint": checked["source_fingerprint"], "reading_fingerprint": checked["reading_fingerprint"],
            "taxonomy_fingerprint": checked["taxonomy_fingerprint"], "human_reviewed": False, "facets": copy.deepcopy(literal)}
        for area in literal:
            if area["id"] not in facets:
                facets[area["id"]] = area
            else:
                combined = facets[area["id"]]
                combined["anchor_candidates"] = combined.get("anchor_candidates", []) + area.get("anchor_candidates", [])
                combined["grounding_version"] = area["grounding_version"]
                combined["grounding_basis"] = area["basis"]
                combined["matched_phrases"] = area.get("matched_phrases", [])
        result["discovery_academic_areas"] = prioritize_specific_facets([facets[key] for key in sorted(facets)])
    result["primary_academic_area"] = primary
    result["classification_status"] = "needs-review" if preserved or primary == "other-academic-subject" else "candidate"
    result["review_reasons"] = sorted(preserved)
    result["classification_method"] = method
    result["classification_version"] = provenance.get("question_version", BATCH_VERSION)
    result["primary_resolution"] = "unresolved" if primary == "other-academic-subject" else "model-candidate" if method == "jev-source-metadata-candidate" else "source-grounded-model-review"
    result["primary_candidates"] = [primary]
    labels = {area["id"]: area["label"] for area in taxonomy["academic_areas"]}
    if method == "jev-source-metadata-candidate":
        result["academic_areas"] = [{"id": primary, "label": labels.get(primary, "Other academic subject"),
                                     "basis": method, "human_reviewed": False}]
    else:
        result["academic_areas"] = copy.deepcopy(rules.get("academic_areas", []))
    if "primary_specialty" in provenance:
        result["original_expertise_tags"] = copy.deepcopy(course.get("original_expertise_tags", course.get("expertise_tags", [])))
        result["primary_expertise"] = provenance["primary_specialty"]
        result["expertise_tags"] = copy.deepcopy(result["original_expertise_tags"])
        specialty = provenance["primary_specialty"]
        if specialty:
            domain = next(domain for domain in taxonomy["expertise_domains"] if domain["id"] == specialty)
            result["expertise_tags"] = [tag for tag in result["expertise_tags"] if tag.get("id") != specialty]
            result["expertise_tags"].append({"id": specialty, "label": domain["label"],
                "academic_area_ids": domain["academic_area_ids"], "basis": "jev-source-metadata-candidate",
                "confidence": provenance["specialty_answer"]["confidence"], "human_reviewed": False,
                "source_fingerprint": provenance["state_fingerprint"], "model": provenance["resolved_model"]})
    result["classification_fingerprint"] = state_fingerprint({"rules": rules, "model": provenance, "primary": primary,
                                                             "facets": result.get("facet_classification")})
    return result


def main():
    parser = argparse.ArgumentParser(description="Register source-bound Jev discovery candidates as a fresh compressed snapshot")
    parser.add_argument("--first-pass", type=Path, required=True)
    parser.add_argument("--refinements", type=Path)
    parser.add_argument("--facets", type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--taxonomy", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    taxonomy = json.loads(args.taxonomy.read_text(encoding="utf-8"))
    first = json.loads(args.first_pass.read_text(encoding="utf-8"))
    refined = json.loads(args.refinements.read_text(encoding="utf-8")) if args.refinements else None
    facets = json.loads(args.facets.read_text(encoding="utf-8")) if args.facets else None
    manifest = write_snapshot(first, taxonomy, args.out, refinements=refined, facets=facets, source_root=args.source_root)
    print(json.dumps({"files": len(manifest["files"]), "records": [entry["records"] for entry in manifest["files"]]}))


if __name__ == "__main__":
    main()

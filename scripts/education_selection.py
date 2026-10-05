from __future__ import annotations

import math
import re
from collections import OrderedDict

from catalog_identity import resolve_institution
from materials_ledger import resource_identity, is_learning_book
from expert_brief import ACTION_VERB, normalize_brief
from subject_strength import rerank_candidates
from universe import explicitly_nondegree
from bibliography_display import display_identity


STOP_WORDS = {"a", "an", "the", "and", "or", "to", "of", "for", "with", "agent", "expert", "in", "on", "by"}
GENERIC_WORDS = {"plan", "method", "selection", "introduction", "advanced", "basic", "system", "research",
                 "design", "engineer", "engineering", "expertise", "need", "help", "work", "develop", "create",
                 "build", "apply", "adviser", "advisor", "specialist", "guide", "user", "use", "support"}
GENERIC_WORDS.update({"analysis", "evaluation", "assessment", "compare", "interpretation", "explain",
                      "investigate", "reconstruct", "assistant"})
WORD_FORMS = {
    "robot": "robot", "robots": "robot", "robotic": "robot", "robotics": "robot",
    "control": "control", "controls": "control", "controlled": "control",
    "sensor": "sensor", "sensors": "sensor", "calibrate": "calibration", "calibrating": "calibration",
    "teacher": "teach", "teachers": "teach", "teaching": "teach", "teach": "teach",
    "learning": "learn", "learn": "learn", "methods": "method", "systems": "system",
    "patients": "patient", "treatments": "treatment", "diagnostics": "diagnosis",
    "histories": "history", "historical": "history", "history": "history",
    "statutes": "statute", "statutory": "statute", "laws": "law",
    "calibrates": "calibration", "calibrate": "calibration", "calibrations": "calibration",
    "calibrated": "calibration", "choose": "selection", "choosing": "selection",
    "select": "selection", "selecting": "selection", "selected": "selection",
    "prototypes": "prototype", "designing": "design", "designs": "design",
    "mechanisms": "mechanism", "measurements": "measurement", "archives": "archive",
    "archival": "archive", "studies": "study", "epidemiological": "epidemiology",
    "statistical": "statistics", "statistic": "statistics", "communications": "communication",
    "engineers": "engineer", "historians": "history", "medievalist": "medieval",
    "curricula": "curriculum", "curriculums": "curriculum", "educational": "education",
    "outbreaks": "outbreak", "programs": "program", "statements": "statement",
    "decisions": "decision", "tests": "test", "loads": "load", "structures": "structure",
    "interpretations": "interpretation", "sources": "source", "transactions": "transaction",
    "landscapes": "landscape", "archaeological": "archaeology", "archeological": "archaeology",
    "archeology": "archaeology", "motors": "motor", "languages": "language",
    "analyze": "analysis", "analyse": "analysis", "analyzing": "analysis", "analyses": "analysis",
    "evaluate": "evaluation", "evaluating": "evaluation", "assess": "assessment",
    "assessing": "assessment", "interpret": "interpretation", "interpreting": "interpretation",
}
GROUNDED_FIELDS = ("title", "code", "course_title", "course_code", "description", "subject", "official_subject",
                   "department", "departments", "official_department", "school", "official_school",
                   "academic_subject", "catalog_department", "subject_code", "subject_areas", "college",
                   "academic_areas", "expertise_tags")
REVIEW_FIELDS_EXCLUDED = {"academic_areas", "expertise_tags", "code", "course_code", "subject_code"}
TEACHING_FIELDS = {"description", "description_variants"}


def tokens(value):
    return [WORD_FORMS.get(word, word) for word in re.findall(r"[\w]+", value.casefold())]


def meaningful_tokens(value):
    return set(tokens(value)) - STOP_WORDS


def subject_tokens(value):
    subject = re.sub(r"^" + ACTION_VERB + r"\s+", "", value.strip(), flags=re.I)
    return meaningful_tokens(subject) - GENERIC_WORDS


def string_values(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for entry in value for text in string_values(entry)]
    if isinstance(value, dict):
        return [value[key] for key in ("label", "displayName", "name") if isinstance(value.get(key), str)]
    return []


def grounded_fields(payload):
    fields = [(field, text) for field in GROUNDED_FIELDS
              for text in string_values(payload.get("discovery_academic_areas", payload.get(field))
                                        if field == "academic_areas" else payload.get(field)) if text.strip()]
    fields.extend(("description_variants", variant["description"])
                  for variant in payload.get("description_variants", []) if isinstance(variant, dict)
                  and isinstance(variant.get("description"), str) and variant["description"].strip())
    return fields


def source_quote_lineage(payload, evidence):
    field, quote = evidence.get("field"), evidence.get("text")
    if field in {"academic_areas", "expertise_tags"} or not isinstance(quote,str) or not quote:
        return []
    if field in {"description", "description_variants"}:
        matches = [variant for variant in payload.get("description_variants", [])
                   if isinstance(variant,dict) and isinstance(variant.get("description"),str)
                   and quote in variant["description"]]
        if matches:
            return [{key:variant[key] for key in ("course_key", "source_course_key", "inventory", "source_url", "source_urls", "source_year", "term", "snapshot", "locator", "source_version", "fetched_on", "source_html_sha256", "source_document_sha256", "description_sha256", "identity_evidence", "source_verification_status", "version_status", "identity_binding", "evidence_kind", "code_equivalence")
                     if key in variant} | {"source_course_key":variant.get("source_course_key") or variant.get("course_key"),
                                           "scope":"matching-description-variant"} for variant in matches]
        if field == "description_variants":
            return []
    return [{key:payload[key] for key in ("course_key", "source_course_keys", "inventory", "source_url", "source_urls")
             if key in payload} | {"scope":"course-group-source-links-not-passage-specific"}]


def source_term_window(text, wanted):
    occurrences = [(index, word) for index, word in enumerate(tokens(text)) if word in wanted]
    counts, left, best = {}, 0, math.inf
    for right, (position, word) in enumerate(occurrences):
        counts[word] = counts.get(word, 0) + 1
        while len(counts) == len(wanted):
            best = min(best, position - occurrences[left][0])
            earliest = occurrences[left][1]
            counts[earliest] -= 1
            if not counts[earliest]:
                del counts[earliest]
            left += 1
    return best


def match_requirement(requirement, fields, context="", search_terms=None, *, allow_single_term=False):
    alternatives = [requirement] + list(search_terms or [])
    best = None
    context_terms = meaningful_tokens(context) - GENERIC_WORDS
    role_noise = {"engineer", "expertise", "need", "help", "work", "develop", "create", "build", "apply", "adviser", "advisor", "specialist", "guide", "user", "use", "support"}
    task_terms = meaningful_tokens(requirement) - role_noise
    for query in alternatives:
        wanted = meaningful_tokens(query) - role_noise
        subjects = subject_tokens(query)
        if not wanted or not (wanted - GENERIC_WORDS):
            continue
        matches = []
        label_terms = set()
        for field, text in fields:
            if field in {"academic_areas", "expertise_tags"}:
                label_terms |= wanted & meaningful_tokens(text)
                continue
            passages = [text] if field not in {"description", "description_variants"} else re.split(r"(?<=[.!?;])\s+|\n+", text)
            for passage in passages:
                found = wanted & meaningful_tokens(passage)
                if not found:
                    continue
                if not allow_single_term and len(wanted) > 1 and len(found) < 2:
                    continue
                if allow_single_term and found == {"physical"} and len(wanted) > 1:
                    continue
                if "selection" in meaningful_tokens(query):
                    positions = tokens(passage)
                    relationship = [index for index, word in enumerate(positions) if word in {"selection", "select", "selecting", "choose", "choosing"}]
                    anchors = [index for index, word in enumerate(positions) if word in wanted - {"selection", "select", "selecting", "choose", "choosing"}]
                    if not relationship or not anchors or min(abs(left - right) for left in relationship for right in anchors) > 3:
                        continue
                if "calibration" in wanted and not (wanted - GENERIC_WORDS - {"calibration"}) and context_terms and not (context_terms & meaningful_tokens(passage)):
                    continue
                if not (found - GENERIC_WORDS):
                    continue
                subject_fraction = len(subjects & meaningful_tokens(passage)) / len(subjects) if subjects else 0
                action = tokens(query)[0] if tokens(query) else ""
                action_found = action in found and bool(re.match(r"^" + ACTION_VERB + r"\s+", query, re.I))
                if not allow_single_term and subjects and (subject_fraction < .5 or subject_fraction == .5 and not action_found):
                    continue
                subject_found = subjects & meaningful_tokens(passage)
                if len(subject_found) > 1 and subject_fraction < 1 and source_term_window(passage, subject_found) > 8:
                    continue
                if field not in {"description", "description_variants"} and len(found) > 1 and source_term_window(passage, found) > 5:
                    continue
                strength = len(found) + (8 if field in {"title", "course_title"} else 0) + (5 if contains_phrase(passage, query) else 0)
                task_found = task_terms & meaningful_tokens(passage)
                task_score = len(task_found) / len(task_terms) if task_terms else 0
                matches.append((task_score, len(found) / len(wanted), strength, field, passage, task_found))
        if not matches:
            continue
        score, lexical_score, strength, field, passage, matched = max(matches, key=lambda value: (value[3] in TEACHING_FIELDS, value[0], value[1], value[2], value[3], value[4]))
        result = {"matched_terms": sorted(matched), "missing_terms": sorted(task_terms - matched),
                  "coverage_fraction": score, "lexical_fit_fraction": lexical_score, "candidate_label_terms": sorted(label_terms),
                  "fit_basis": "same-passage source terms; candidate fit, not verified educational coverage",
                  "evidence": [{"field": field, "text": passage, "matched_terms": sorted(matched), "basis": "source-field"}],
                  "source_strength": strength, "review_state": "candidate", "query_used": query,
                  "work_review_needed": bool(re.match(r"^" + ACTION_VERB + r"\s+", requirement, re.I)),
                  "relevance_method": "context-gated source passage"}
        if best is None or (score, lexical_score, strength) > (best["coverage_fraction"], best["lexical_fit_fraction"], best["source_strength"]):
            best = result
    if best is not None:
        context_terms -= {"expert", "expertise", "agent", "adviser", "advisor", "specialist"}
        source_context = set().union(*(meaningful_tokens(text) for field, text in fields
                                     if field not in {"academic_areas", "expertise_tags", "code", "course_code", "subject_code"}))
        best["context_review_needed"] = bool(context_terms and not context_terms.issubset(source_context))
        best["support_level"] = "supporting" if best["coverage_fraction"] == 0 else "context-review-needed" if best["context_review_needed"] else "direct" if best["coverage_fraction"] == 1 else "partial"
    return best


def validate_brief(brief, per_specialty, semantic_candidates):
    if not isinstance(brief, dict):
        raise ValueError("Provide an education brief object")
    if isinstance(per_specialty, bool) or not isinstance(per_specialty, int) or not 1 <= per_specialty <= 20:
        raise ValueError("per_specialty must be an integer from 1 to 20")
    for field in ("specialties", "deliverables", "institutions", "exclude", "constraints", "academic_areas", "levels"):
        values = brief.get(field, [])
        if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError(field + " must be a list of nonempty strings")
    if not brief.get("specialties") and not brief.get("capabilities"):
        raise ValueError("Provide specialties or capabilities")
    for field in ("require_reviewed_classifications", "require_material_evidence", "require_course_review", "include_nondegree"):
        if field in brief and not isinstance(brief[field], bool):
            raise ValueError(field + " must be a boolean")
    if semantic_candidates is not None:
        if not isinstance(semantic_candidates, dict):
            raise ValueError("semantic_candidates must map requirements to candidate lists")
        for requirement, candidates in semantic_candidates.items():
            if not isinstance(requirement, str) or not isinstance(candidates, list):
                raise ValueError("Invalid semantic candidate list")
            for node in candidates:
                if not isinstance(node, dict) or not isinstance(node.get("id"), str):
                    raise ValueError("Semantic candidate requires a node id")
                score = node.get("semantic_score")
                if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not -1 <= score <= 1:
                    raise ValueError("Semantic candidate score must be finite and between -1 and 1")


def expanded_query(requirement):
    wanted = meaningful_tokens(requirement)
    alternatives = {word for word, normalized in WORD_FORMS.items() if normalized in wanted}
    return " ".join(sorted(wanted | alternatives))


def contains_phrase(text, phrase):
    haystack = tokens(text)
    needle = tokens(phrase)
    if not needle:
        return False
    return any(haystack[index:index + len(needle)] == needle for index in range(len(haystack) - len(needle) + 1))


def classification_reliability(payload):
    return "reviewed" if payload.get("classification_status") == "reviewed" else str(payload.get("classification_status") or "unknown")


def active_course(graph, node_id):
    if hasattr(graph, "is_active"):
        return bool(graph.is_active(node_id))
    return not graph.db.execute("SELECT 1 FROM catalog_courses WHERE node_id=? AND active=0", (node_id,)).fetchone()


def passes_filters(payload, fields, brief):
    institution = resolve_institution(str(payload.get("institution") or payload.get("university") or ""))
    if brief.get("institutions"):
        requested = [resolve_institution(name) for name in brief["institutions"]]
        if not any(institution["institution_id"] == choice["institution_id"] and
                   (not choice["school_id"] or institution["school_id"] == choice["school_id"])
                   for choice in requested):
            return False
    if any(contains_phrase(text, phrase) for phrase in brief.get("exclude", []) for field, text in fields):
        return False
    if brief.get("academic_areas"):
        labels = payload.get("discovery_academic_areas", payload.get("academic_areas", []))
        areas = {str(value).casefold() for label in labels if isinstance(label, dict)
                 for value in (label.get("id", ""), label.get("label", ""))}
        if not areas & {value.casefold() for value in brief["academic_areas"]}:
            return False
    if brief.get("levels"):
        levels = {text.casefold() for field in ("level", "career", "course_level", "academic_level")
                  for text in string_values(payload.get(field))}
        if not levels & {value.casefold() for value in brief["levels"]}:
            return False
    return not brief.get("require_reviewed_classifications") or classification_reliability(payload) == "reviewed"


def materials_evidence(graph, course_id):
    if callable(getattr(graph, 'materials_evidence', None)):
        return graph.materials_evidence(course_id)
    return [graph.get(edge["dst"])["payload"] for edge in graph.neighbors(course_id)
            if edge["src"] == course_id and edge["relation"] == "materials-evidence"]


def has_named_material(statuses):
    return any(is_learning_book(material) and resource_identity(material)
               for status in statuses for material in status.get("materials", []) if isinstance(material, dict))


def candidate_fields(graph, node):
    fields = grounded_fields(node["payload"])
    fields.extend(("book_title", material["title"]) for status in materials_evidence(graph, node["id"])
                  for material in status.get("materials", []) if isinstance(material, dict)
                      and is_learning_book(material) and isinstance(material.get("title"), str) and material["title"].strip())
    return fields


def program_roles(payload):
    roles = payload.get("program_requirements", payload.get("program_roles", []))
    if isinstance(roles, dict):
        roles = [roles]
    if not isinstance(roles, list):
        roles = []
    if payload.get("program") and payload.get("program_requirement_group") and payload.get("degree_role"):
        roles = roles + [{key: payload.get(key) for key in ("program", "program_requirement_group", "degree_role",
                                                          "source_url", "source_urls")}]
    return [role for role in roles if isinstance(role, dict) and
            (role.get("program_id") or role.get("program") or role.get("program_name")) and
            (role.get("requirement_group") or role.get("program_requirement_group")) and
            (role.get("role") or role.get("degree_role")) and
            (role.get("source_url") or role.get("source_urls"))]


def retrieve_candidates(graph, requirements, semantic_candidates):
    candidates = {}
    similarities = {}
    for kind, requirement in requirements:
        for node in graph.search(expanded_query(requirement), limit=1000):
            candidates[node["id"]] = node
        for node in (semantic_candidates or {}).get(requirement, []):
            canonical = graph.get(node["id"])
            if canonical["kind"] != "course":
                raise ValueError("Semantic education candidates must be courses")
            if not active_course(graph, canonical["id"]):
                continue
            candidates[canonical["id"]] = canonical
            similarities[canonical["id"], requirement] = node["semantic_score"]
    return candidates, similarities


def rank_candidates(graph, candidates, requirements, brief):
    ranked = {}
    for node_id, node in candidates.items():
        if node.get("kind") != "course" or not active_course(graph, node_id):
            continue
        fields = candidate_fields(graph, node)
        if not passes_filters(node["payload"], fields, brief):
            continue
        if not brief.get("include_nondegree") and explicitly_nondegree(node["payload"]):
            continue
        context = str(brief.get("role") or brief.get("original_description") or "")
        terms = {item["description"]: item.get("search_terms", []) for item in brief.get("capabilities", [])}
        matches = {(kind, requirement): match_requirement(requirement, fields, context, terms.get(requirement))
                   for kind, requirement in requirements}
        for review in brief.get("course_reviews", []):
            if review["course_id"] != node_id:
                continue
            key = next((key for key in requirements if key[1] == review["requirement"]), None)
            if key and review["decision"] == "reject":
                matches.pop(key, None)
            elif key:
                matches[key] = {"matched_terms": [], "missing_terms": [], "coverage_fraction": 1.0,
                                "lexical_fit_fraction": None, "candidate_label_terms": [],
                                "fit_basis": "reviewed task rationale with locally validated source quote; not verified outcomes",
                                "evidence": [{"field": anchor["field"], "text": anchor["quote"], "basis": "source-field"}
                                             for anchor in [review] + review.get("supporting_evidence", [])],
                                "source_strength": 10, "review_state": "host-reviewed",
                                "support_level": "supporting" if review["decision"] == "supplemental" else "direct",
                                "review_rationale": review["rationale"], "review_decision": review["decision"], "relevance_method": "source-anchored review"}
        matches = {requirement: match for requirement, match in matches.items() if match and
                   (not brief.get("require_course_review") or match.get("review_state") == "host-reviewed")}
        for match in matches.values():
            for evidence in match.get("evidence", []):
                evidence["source_lineage"] = ([{"source_course_key": status.get("source_course_key"),
                                                "sources": status.get("sources", []), "scope": "linked-book-title-not-book-content"}
                                               for status in materials_evidence(graph, node_id)
                                              if any(isinstance(material, dict) and is_learning_book(material)
                                                      and evidence["text"] in str(material.get("title", ""))
                                                      for material in status.get("materials", []))]
                                              if evidence["field"] == "book_title" else source_quote_lineage(node["payload"], evidence))
        if not matches:
            continue
        statuses = materials_evidence(graph, node_id)
        named_material = has_named_material(statuses)
        if not named_material:
            continue
        ranked[node_id] = {"node": node, "matches": matches, "materials": statuses,
                           "named_material": named_material,
                           "deliverable_fit": sum(match["coverage_fraction"] for (kind, requirement), match in matches.items()
                                                  if kind == "deliverable")}
    return ranked


def selected_courses(ranked, requirements, similarities, per_specialty, brief, subject_strength=None):
    chosen = OrderedDict()
    priorities = {item["description"]: item["priority"] for item in brief.get("capabilities", [])}
    tiers = {}
    reasons = {}
    for kind, requirement in requirements:
        eligible = [item for item in ranked.values() if (kind, requirement) in item["matches"]]

        def order(item):
            match = item["matches"][kind, requirement]
            return (match.get("review_state") != "host-reviewed", match.get("review_decision") == "supplemental", -match["coverage_fraction"], -item["deliverable_fit"], -match["source_strength"], bool(match.get("context_review_needed")),
                    classification_reliability(item["node"]["payload"]) != "reviewed",
                    -similarities.get((item["node"]["id"], requirement), -1),
                    not item["named_material"], item["node"]["id"])

        ordered = rerank_candidates(sorted(eligible, key=order), requirement, lambda item: order(item)[:-1],
                                    index=subject_strength)
        for index, item in enumerate(ordered[:per_specialty]):
            node_id = item["node"]["id"]
            match = item["matches"][kind, requirement]
            direct = (match["coverage_fraction"] == 1 and not match.get("context_review_needed")
                      and not re.match(r"^" + ACTION_VERB + r"\s+", requirement, re.I))
            tier = "must-have" if index == 0 and direct and priorities.get(requirement, "essential") == "essential" else "supplemental"
            for review in brief.get("course_reviews", []):
                if review["course_id"] == node_id and review["requirement"] == requirement and review["decision"] != "reject":
                    tier = "must-have" if index == 0 and review["decision"] == "essential" and priorities.get(requirement, "essential") == "essential" else "supplemental"
            chosen[node_id] = item
            if node_id not in tiers or tier == "must-have":
                tiers[node_id] = tier
            reasons.setdefault(node_id, []).append({"requirement": requirement, "priority": priorities.get(requirement, "essential"),
                                                    "selection_kind": "primary" if tier == "must-have" else "alternative" if priorities.get(requirement, "essential") == "essential" else "supplemental-interest",
                                                    "rationale": match.get("review_rationale") or ("Source vocabulary matches; fit to the intended work needs local review." if match.get("context_review_needed") else "Documented course metadata supports this candidate match, not acquired knowledge."),
                                                    "evidence": match["evidence"], "outcomes_verified": False})
    courses = []
    for node_id, item in chosen.items():
        payload = item["node"]["payload"]
        matches = []
        for (kind, requirement), match in item["matches"].items():
            matches.append({"kind": kind, "requirement": requirement, kind: requirement,
                            **{key: value for key, value in match.items() if key != "source_strength"},
                            "semantic_similarity": similarities.get((node_id, requirement)),
                            "ranking_basis": "grounded task fit, deliverable fit, source evidence, review, optional similarity and materials"})
        courses.append({"course": item["node"], "selection_role": tiers[node_id], "matches": matches,
                        "selection_rationale": reasons[node_id], "selection_review_state": "candidate",
                        "classification_reliability": classification_reliability(payload),
                        "materials_evidence": item["materials"],
                        "prerequisites": payload.get("prerequisites") or "Not mapped",
                        "program_core_or_elective": program_roles(payload) or "Not mapped to a documented program"})
    return courses


def requirement_coverage(courses, requirements, gaps):
    coverage = []
    for kind, requirement in requirements:
        links = [{"course_id": course["course"]["id"], "coverage_fraction": match["coverage_fraction"],
                  "lexical_fit_fraction": match.get("lexical_fit_fraction"),
                  "review_decision": match.get("review_decision"), "support_level": match.get("support_level"),
                  "context_review_needed": bool(match.get("context_review_needed")),
                  "work_review_needed": bool(match.get("work_review_needed")),
                  "missing_terms": match["missing_terms"]} for course in courses for match in course["matches"]
                 if match["kind"] == kind and match["requirement"] == requirement]
        supported = any(link["coverage_fraction"] == 1 and link.get("review_decision") != "supplemental"
                        and link.get("support_level") != "supporting" and not link.get("context_review_needed")
                        and not link.get("work_review_needed") for link in links)
        status = "candidate-match" if supported else "partial-candidate-match" if links else "uncovered"
        coverage.append({"kind": kind, "requirement": requirement, "status": status, "courses": links,
                         "fit_basis": "source passage or anchored review; not verified educational coverage",
                         "outcomes_verified": False})
        if not supported:
            context_pending = bool(links and all(link.get("context_review_needed") for link in links))
            gaps.append({"kind": kind, "requirement": requirement, kind: requirement,
                         "gap_kind": "task-context-review" if context_pending else "partial-task-fit" if links else "missing-task-evidence",
                         "next_action": "Review original course metadata with the local agent" if links else "Expand the reviewed capability queries and research documented course readings",
                         "gap": "Only supporting or partial grounded matches; direct task coverage needs review" if links else "No evidenced candidate found in this snapshot/filter"})
    return coverage


def acquisition_plan(courses, gaps):
    acquisitions = {}
    unresolved = []
    for course in courses:
        node_id = course["course"]["id"]
        if course["prerequisites"] == "Not mapped":
            gaps.append({"course_id": node_id, "gap": "Prerequisites not mapped"})
        if isinstance(course["program_core_or_elective"], str):
            gaps.append({"course_id": node_id, "gap": "Program core/elective role not mapped"})
        for status in course["materials_evidence"]:
            if status.get("gaps") or status.get("status") not in {"found", "has-saved-reference-records"}:
                gaps.append({"course_id": node_id, "status": status.get("status"), "gaps": status.get("gaps", [])})
            for index, material in enumerate(status.get("materials", [])):
                if isinstance(material, dict) and not is_learning_book(material):
                    continue
                candidate = resource_identity(material) if isinstance(material, dict) else None
                if not candidate:
                    unresolved.append({"course_id": node_id, "evidence": material})
                    continue
                exact_edition = bool(material.get("bibliography_verified") is True and candidate["edition"] and candidate["authors"])
                key = (candidate["key"], candidate["title"].casefold(), candidate["authors"].casefold(), candidate["edition"].casefold())
                if not exact_edition:
                    key += (node_id, status.get("source_course_key", ""), index)
                reading_role = str(material.get("assignment_role") or material.get("assigned_vs_suggested_wording") or "not_stated").casefold()
                material_priority = ("supplemental" if any(word in reading_role for word in ("optional", "recommended", "suggested", "supplemental", "supplementary")) else
                                     course["selection_role"] if any(word in reading_role for word in ("required", "assigned", "primary")) and "not" not in reading_role else "review-needed")
                acquisition = acquisitions.setdefault(key, {
                    "identity_candidate": candidate, "acquisition_status": "not supplied", "processing_status": "not processed",
                    "display_identity": display_identity(material, candidate),
                    "verify_before_deduplicating": not exact_edition,
                    "identity_status": "verified edition" if exact_edition else "source-listed book",
                    "topclass_priority": material_priority, "priority_basis": "course priority and documented reading role; unknown adoption needs review", "assignments": []})
                priorities = {"supplemental": 0, "review-needed": 1, "must-have": 2}
                if priorities[material_priority] > priorities[acquisition["topclass_priority"]]:
                    acquisition["topclass_priority"] = material_priority
                acquisition["assignments"].append({"course_id": node_id, "source_course_key": status.get("source_course_key"),
                                                   "evidence": material, "sources": status.get("sources", []),
                                                   "selection_priority": course["selection_role"]})
    return list(acquisitions.values()), unresolved


def validate_course_reviews(graph, brief, requirements):
    reviews = brief.get("course_reviews", [])
    if not isinstance(reviews, list) or len(reviews) > 300:
        raise ValueError("Provide at most 300 source-anchored course reviews")
    known_requirements = {value for kind, value in requirements}
    for review in reviews:
        if not isinstance(review, dict) or review.get("decision") not in {"essential", "supplemental", "reject"}:
            raise ValueError("Invalid course review decision")
        if review.get("requirement") not in known_requirements:
            raise ValueError("Course review must reference an existing requirement")
        node = graph.get(review.get("course_id"))
        if node["kind"] != "course":
            raise ValueError("Course review requires a course id")
        if any(not isinstance(review.get(field), str) or not review[field].strip() for field in ("field", "quote", "rationale")):
            raise ValueError("Course review requires source field, quote and rationale")
        supporting = review.get("supporting_evidence", [])
        if not isinstance(supporting, list) or len(supporting) > 20:
            raise ValueError("Course review supporting evidence must contain at most 20 anchors")
        for anchor in supporting:
            if not isinstance(anchor, dict) or set(anchor) != {"field", "quote"} or any(
                    not isinstance(anchor.get(key), str) or not anchor[key].strip() for key in ("field", "quote")):
                raise ValueError("Course review supporting evidence requires source field and quote")
        for anchor in [review] + supporting:
            sources = [text for field, text in candidate_fields(graph, node) if field == anchor["field"] and field not in REVIEW_FIELDS_EXCLUDED]
            if not any(anchor["quote"] in text for text in sources):
                raise ValueError("Course review quote does not occur in the named source field")
    return reviews


def education_plan(graph, brief, per_specialty=3, semantic_candidates=None, subject_strength=None):
    brief = normalize_brief(brief)
    validate_brief(brief, per_specialty, semantic_candidates)
    requirements = ([("capability", item["description"]) for item in brief["capabilities"]] +
                    [("deliverable", value) for value in dict.fromkeys(brief.get("deliverables", []))
                     if value not in {item["description"] for item in brief["capabilities"]}] if brief.get("capabilities") else
                    [(kind, value) for kind, field in (("specialty", "specialties"), ("deliverable", "deliverables"))
                     for value in dict.fromkeys(brief.get(field, []))])
    validated = validate_course_reviews(graph, brief, requirements)
    reviews = list({(review["course_id"], review["requirement"]): review for review in validated}.values())
    brief = brief | {"course_reviews": reviews, "review_resolution": "last locally validated decision for each course and requirement"}
    candidates, similarities = retrieve_candidates(graph, requirements, semantic_candidates)
    for capability in brief.get("capabilities", []):
        for term in capability.get("search_terms", []):
            for node in graph.search(expanded_query(term), limit=1000):
                candidates[node["id"]] = node
    for review in reviews:
        candidates[review["course_id"]] = graph.get(review["course_id"])
    ranked = rank_candidates(graph, candidates, requirements, brief)
    courses = selected_courses(ranked, requirements, similarities, per_specialty, brief, subject_strength)
    gaps = []
    coverage = requirement_coverage(courses, requirements, gaps)
    acquisitions, unresolved = acquisition_plan(courses, gaps)
    research_gaps = []
    for node_id, node in candidates.items():
        fields = grounded_fields(node["payload"])
        if not active_course(graph, node_id) or not passes_filters(node["payload"], fields, brief):
            continue
        if not brief.get("include_nondegree") and explicitly_nondegree(node["payload"]):
            continue
        if has_named_material(materials_evidence(graph, node_id)):
            continue
        terms = {item["description"]: item.get("search_terms", []) for item in brief.get("capabilities", [])}
        matches = [value for kind, value in requirements if match_requirement(value, fields, str(brief.get("role", "")), terms.get(value))]
        if matches:
            research_gaps.append({"course_id": node_id, "title": node["label"], "requirements": matches,
                                  "gap": "No documented named book; unusable for this education plan", "usable": False,
                                  "materials_statuses": [status.get("status") for status in materials_evidence(graph, node_id)],
                                  "source_leads": node["payload"].get("source_urls") or [node["payload"].get("source_url", "")],
                                  "backlog_state": "later enrichment; no coverage credit"})
    research_gaps.extend(getattr(graph, "enrichment_backlog", []))
    for known in brief.get("existing_knowledge", []):
        gaps.append({"constraint": known, "gap": "Existing knowledge is user-stated; course-level mastery and prerequisite exemptions are not verified"})
    constraints = list(brief.get("constraints", []))
    for constraint in constraints:
        gaps.append({"constraint": constraint, "gap": "Free-form constraint not automatically checked"})
    return {"format": "topclass-expert-education-v2", "brief": brief,
            "status": "source-backed candidate education; review task fit and bibliography before supply",
            "selection_limits": "Only documented named-book courses are usable. Same-passage task vocabulary or locally anchored reviews support candidate fit, not learning guarantees. "
                                "Discovery is bounded to 1000 candidates per query. Free-form constraints remain unchecked.",
            "not_claimed": ["best university/course", "complete reading lists", "professional competence", "books have been read"],
            "catalog_coverage": "unknown; no complete-university coverage claim", "snapshot": getattr(graph, "snapshot", None),
            "unchecked_constraints": constraints, "requirement_coverage": coverage, "courses": courses,
            "must_have_courses": [course for course in courses if course["selection_role"] == "must-have"],
            "supplemental_courses": [course for course in courses if course["selection_role"] == "supplemental"],
            "materials_to_supply": acquisitions, "unidentified_materials": unresolved,
            "unusable_course_research_gaps": research_gaps, "enrichment_backlog": research_gaps, "gaps": gaps,
            "handoff": {"next_step": "Supply chosen original books and confirm their editions", "supplied": False, "processed": False}}

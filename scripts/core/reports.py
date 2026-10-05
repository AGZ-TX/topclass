from __future__ import annotations

import re

from materials_ledger import is_learning_book


def clean(value):
    return str(value or "Not stated").replace("|", "\\|").replace("\n", " ")


def parser_warnings(identity):
    warnings = []
    if re.search(r"\]\s*=|\[[A-Z]{2,}|\$[a-z]|\b(?:author|editor)\s+\d", identity.get("authors", "")):
        warnings.append("Author field contains citation or library parser syntax; verify the original citation.")
    if "/ ISBN" in identity.get("title", ""):
        warnings.append("Title includes structured citation metadata; use the preserved citation to confirm the book title.")
    return warnings


def render_report(plan):
    brief = plan["brief"]
    lines = ["# Expert education and books", "", clean(brief.get("original_description") or brief.get("role") or "; ".join(brief.get("specialties", []))), "",
             "Only courses with documented named books are selected. Priorities below are Topclass choices for this work. University reading and degree roles remain separate.", "",
             "All materials are **not supplied** and **not processed**. Course descriptions support selection; no book teachings have been inferred.", ""]
    if brief.get("interpretation"):
        lines += ["Brief: " + clean(brief["interpretation"].get("method")) + "; " + clean(brief["interpretation"].get("review_state")) + ".", ""]
    for field, label in (("institutions", "University filter"), ("exclude", "Excluded course topics"),
                         ("existing_knowledge", "User-stated existing knowledge"), ("assumptions", "Assumptions to review")):
        if brief.get(field):
            lines += [label + ": " + clean("; ".join(brief[field])) + ".", ""]
    optional = [item["description"] for item in brief.get("capabilities", []) if item["priority"] == "supplemental"]
    if optional:
        lines += ["Optional interests: " + clean("; ".join(optional)) + ".", ""]
    for key, label in (("must_have_courses", "Must-have courses"), ("supplemental_courses", "Supplemental courses and alternatives")):
        lines += ["## " + label, ""]
        rows = plan.get(key, [])
        if not rows:
            lines += ["No supported selection in this snapshot. Review the capability gaps below.", ""]
        for item in rows:
            course = item["course"]
            payload = course["payload"]
            lines += ["### " + clean(payload.get("institution") or payload.get("school")) + " — " + clean(payload.get("code")) + " " + clean(course["label"]), "",
                      "Course ID: `" + course["id"] + "`. Selection: " + item["selection_role"] + "; " + item.get("selection_review_state", "candidate") + ".", ""]
            for reason in item.get("selection_rationale", []):
                lines += ["Needed for: **" + clean(reason["requirement"]) + "** (" + clean(reason.get("selection_kind", "candidate")) + "). " + clean(reason["rationale"]), ""]
                for evidence in reason["evidence"]:
                    lines += ["> " + clean(evidence["text"]), "", "Evidence field: " + evidence["field"] + ".", ""]
            lines += ["Prerequisites: " + clean(item.get("prerequisites")) + ". University program role: " + clean(item.get("program_core_or_elective")) + ".", ""]
            book_views = [book for book in payload.get("books", [])
                          if is_learning_book({"title": book["title"], "resource_type": "book"})]
            if book_views:
                lines += ["Documented book candidates (variants below are evidence, not confirmed separate purchases):", ""]
                for book in book_views:
                    details = [("Authors", book.get("authors", [])), ("Assigned edition", book.get("assigned_editions", [])),
                               ("Assigned ISBN", book.get("assigned_isbns", [])),
                               ("Publisher-matched ISBN", book.get("publisher_matched_isbns", [])),
                               ("Library candidate ISBN", book.get("library_candidate_isbns", []))]
                    extras = [label + ": " + clean("; ".join(values)) for label, values in details if values]
                    lines += ["- **" + clean(book["title"]) + "**" + (" — " + "; ".join(extras) if extras else "")]
                lines += [""]
    lines += ["## Books for your selection", "", "Useful book titles are enough to review this list. Authors, editions and ISBNs are included when available. Repeated citations retain their source evidence.", ""]
    for index, material in enumerate(plan.get("materials_to_supply", []), 1):
        identity = material.get("display_identity", material["identity_candidate"])
        lines += ["### " + str(index) + ". " + clean(identity["title"]), "",
                  "Topclass priority: **" + material.get("topclass_priority", "supplemental") + "**. " + material.get("identity_status", "Bibliography candidate") + ".", ""]
        extras = [label + ": " + clean(identity[key]) for key, label in
                  (("authors", "Author"), ("edition", "Edition"), ("isbn", "Source ISBN")) if identity.get(key)]
        if extras:
            lines += [". ".join(extras) + ".", ""]
        for finding in identity.get("finding_aids", []):
            details = [clean(finding["title"])] if finding.get("title") != identity["title"] else []
            if finding.get("isbn"):
                details.append("ISBN: " + finding["isbn"])
            if finding.get("edition"):
                details.append("Edition: " + clean(finding["edition"]))
            details.append(clean(finding["edition_status"]))
            lines += ["; ".join(details) + ". [Find this book](" + finding["source_url"] + ").", ""]
        for assignment in material["assignments"]:
            evidence = assignment["evidence"]
            role = evidence.get("assignment_role") or evidence.get("assigned_vs_suggested_wording") or "not_stated"
            lines += ["Course: `" + assignment["course_id"] + "`. Reading role: " + clean(role) + ".", "",
                      "Original citation: " + clean(evidence.get("exact_source_title_or_citation") or evidence.get("source_title_or_citation") or evidence.get("citation") or evidence.get("title") or identity["title"]) + ".", ""]
            urls = {str(evidence.get("source_url") or evidence.get("course_source_url") or "")}
            urls.update(source.get("source_url", "") for source in assignment.get("sources", []) if isinstance(source, dict))
            for url in sorted(url for url in urls if url.startswith(("https://", "http://"))):
                lines += ["Source: [saved course/reading evidence](" + url + ").", ""]
            for field in ("source_year", "source_date", "catalog_year", "assigned_portions", "association_status", "isbn_validation", "bibliographic_status"):
                if evidence.get(field):
                    lines += [field.replace("_", " ").capitalize() + ": " + clean(evidence[field]) + ".", ""]
        for warning in parser_warnings(material["identity_candidate"]):
            lines += ["- " + warning]
        lines += [""]
    if not plan.get("materials_to_supply"):
        lines += ["No books are listed for the current selection.", ""]
    lines += ["## Capability gaps and unchecked conditions", ""]
    for item in plan.get("requirement_coverage", []):
        lines += ["- **" + clean(item["requirement"]) + "**: " + item["status"] + "; course learning outcomes are not verified."]
    for gap in plan.get("gaps", []):
        lines += ["- " + clean(gap.get("requirement") or gap.get("constraint") or gap.get("course_id") or "Evidence") + ": " +
                  clean(gap.get("gap") or "; ".join(gap.get("gaps", [])) or gap.get("status"))]
    lines += ["", "## Later reading-enrichment backlog", ""]
    backlog = plan.get("enrichment_backlog", plan.get("unusable_course_research_gaps", []))
    for item in backlog:
        lines += ["- `" + item["course_id"] + "` " + clean(item.get("title")) + ": " + clean(item.get("gap")) + ". Relevant to " + clean("; ".join(item.get("requirements", []))) + ". No selection or coverage credit."]
    if not backlog:
        lines += ["No bookless candidates are included in the usable index. Missing capabilities remain open for later evidence enrichment."]
    lines += ["", "## Review your list", "", "Review how these courses and books fit your agent's purpose. Add or remove courses and change priorities in the education HTML. Obtaining and processing books are later steps; no books have been learned.", ""]
    snapshot = plan.get("snapshot")
    if snapshot:
        lines += ["Saved snapshot fingerprint: `" + str(snapshot["fingerprint"]) + "`. Usable course groups: " + str(snapshot.get("usable_course_groups", "unknown")) + ".", ""]
    return "\n".join(lines)

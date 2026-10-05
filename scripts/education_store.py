from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from catalog_identity import normalized_name, resolve_institution


SCHEMA_VERSION = 1
MATERIAL_STATUSES = {"ambiguous", "explicit-no-textbook-stated", "found", "has-saved-reference-records",
                     "not-researched", "searched-no-book-evidence", "searched-no-public-material",
                     "source-access-limited"}
CATALOG_KINDS = {"course", "institution", "school", "area", "specialty", "materials-status", "assignment", "resource-candidate"}
CATALOG_RELATIONS = {"listed-by", "listed-in-school", "part-of", "area-candidate", "specialty-candidate",
                     "materials-evidence", "assigns-or-mentions", "identity-candidate"}


def update_json_array(digest, records, *, ensure_ascii=False, separators=(",", ":")):
    digest.update(b"[")
    delimiter = (separators[0] if separators else ", ").encode()
    first = True
    for record in records:
        if not first:
            digest.update(delimiter)
        first = False
        packed = json.dumps(record, ensure_ascii=ensure_ascii, sort_keys=True, separators=separators)
        digest.update(packed.encode())
    digest.update(b"]")


def curriculum_fingerprint(courses, materials):
    digest = hashlib.sha256()
    digest.update(b'{"courses":')
    update_json_array(digest, courses)
    digest.update(b',"materials":')
    update_json_array(digest, materials)
    digest.update(b"}")
    return digest.hexdigest()


def json_array_fingerprint(records):
    digest = hashlib.sha256()
    update_json_array(digest, records, ensure_ascii=True, separators=None)
    return digest.hexdigest()


def search_text(kind, label, payload):
    if kind != "course":
        return label + " " + json.dumps(payload, ensure_ascii=False, sort_keys=True)
    fields = {key: payload.get(key) for key in (
        "title", "code", "course_title", "course_code", "course_code_aliases", "description", "subject", "official_subject",
        "department", "departments", "official_department", "school", "official_school",
        "academic_subject", "catalog_department", "subject_code", "subject_areas", "college")}
    fields["description_variants"] = [item.get("description", "") for item in payload.get("description_variants", [])
                                      if isinstance(item, dict)]
    for name in ("academic_areas", "expertise_tags"):
        labels = payload.get("discovery_academic_areas", payload.get(name, [])) if name == "academic_areas" else payload.get(name, [])
        fields[name] = [{key: item[key] for key in ("id", "label", "matched_terms") if key in item}
                        for item in labels if isinstance(item, dict)]
    return label + " " + json.dumps(fields, ensure_ascii=False, sort_keys=True)


@contextmanager
def transaction(db):
    name = "change_" + uuid4().hex
    db.execute("SAVEPOINT " + name)
    try:
        yield
        db.execute("RELEASE SAVEPOINT " + name)
    except BaseException:
        db.execute("ROLLBACK TO SAVEPOINT " + name)
        db.execute("RELEASE SAVEPOINT " + name)
        raise


def project_course(graph, course, node_id):
    source = str(course.get("institution") or course.get("university") or "unresolved")
    resolved = resolve_institution(source)
    iid = resolved["institution_id"]
    institution_node = "institution:" + iid
    graph.node(institution_node, "institution", resolved["institution_name"],
               {"institution_id": iid, "name": resolved["institution_name"], "in_scope": resolved["in_scope"]})
    graph.db.execute("INSERT INTO institutions VALUES(?,?,?,?) ON CONFLICT(id) DO NOTHING",
                     (iid, resolved["institution_name"], int(resolved["in_scope"]), institution_node))
    graph.db.execute("INSERT OR IGNORE INTO institution_aliases VALUES(?,?)", (normalized_name(source), iid))
    school = resolved["school_id"]
    if school:
        school_node = "school:" + school
        graph.node(school_node, "school", resolved["school_name"],
                   {"school_id": school, "institution_id": iid, "name": resolved["school_name"]})
        graph.db.execute("INSERT OR IGNORE INTO schools VALUES(?,?,?,?)", (school, iid, resolved["school_name"], school_node))
        graph.edge(school_node, "part-of", institution_node, "source-stated", {"source_institution": source})
        graph.edge(node_id, "listed-in-school", school_node, "source-stated", {"source_institution": source})
    packed = json.dumps(course, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    graph.db.execute("INSERT INTO catalog_courses VALUES(?,?,?,?,?,?,?,?,?,1) ON CONFLICT(course_key) DO UPDATE SET "
                     "institution_id=excluded.institution_id,school_id=excluded.school_id,source_institution=excluded.source_institution,"
                     "code=excluded.code,title=excluded.title,payload=excluded.payload,classification_status=excluded.classification_status,active=1",
                     (course["course_key"], node_id, iid, school, source, course.get("code") or course.get("course_code"),
                      str(course.get("title") or course.get("course_title") or course["course_key"]), packed,
                      course.get("classification_status") or "unmapped"))
    if graph._catalog_scope:
        graph.db.execute("INSERT INTO catalog_course_sources VALUES(?,?,?) ON CONFLICT(scope,node_id) DO UPDATE SET payload=excluded.payload",
                         (graph._catalog_scope, node_id, packed))
    return institution_node


def initialize(graph):
    version = graph.db.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise ValueError("Database schema is newer than this application")
    legacy = version == 0 and graph.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='nodes'").fetchone()
    if legacy:
        invalid_nodes = graph.db.execute("SELECT 1 FROM nodes WHERE id IS NULL OR id='' OR kind IS NULL OR kind='' OR label IS NULL OR fingerprint IS NULL OR NOT json_valid(payload) LIMIT 1").fetchone()
        invalid_edges = graph.db.execute("SELECT 1 FROM edges WHERE src IS NULL OR dst IS NULL OR relation IS NULL OR relation='' OR status IS NULL OR status NOT IN ('source-stated','candidate','inferred','reviewed') OR NOT json_valid(evidence) OR NOT EXISTS(SELECT 1 FROM nodes WHERE id=src) OR NOT EXISTS(SELECT 1 FROM nodes WHERE id=dst) LIMIT 1").fetchone()
        if invalid_nodes or invalid_edges:
            raise ValueError("Legacy graph contains invalid records; migration refused")
    schema = Path(__file__).with_name("education_schema.sql").read_text()
    graph.db.executescript("BEGIN;\n" + schema)
    with transaction(graph.db):
        if version == 0:
            graph.db.execute("INSERT OR IGNORE INTO search_lookup SELECT s.id,min(s.rowid) FROM search s JOIN nodes n ON n.id=s.id GROUP BY s.id")
            graph.db.execute("DELETE FROM search WHERE rowid NOT IN (SELECT search_rowid FROM search_lookup)")
            for row in graph.db.execute("SELECT id,label,payload FROM nodes WHERE id NOT IN (SELECT node_id FROM search_lookup)").fetchall():
                result = graph.db.execute("INSERT INTO search VALUES(?,?)", (row["id"], row["label"] + " " + row["payload"]))
                graph.db.execute("INSERT INTO search_lookup VALUES(?,?)", (row["id"], result.lastrowid))
            if legacy:
                migrate_legacy_catalog(graph)
            graph.db.execute("PRAGMA user_version=" + str(SCHEMA_VERSION))


def migrate_legacy_catalog(graph):
    rows = graph.db.execute("SELECT id,payload FROM nodes WHERE kind='course'").fetchall()
    if not rows:
        return
    fingerprint = json_array_fingerprint(tuple(row) for row in rows)
    import_id = "legacy:" + fingerprint
    graph.db.execute("INSERT INTO catalog_imports(id,scope,fingerprint,course_count,materials_count) VALUES(?,?,?,?,?)",
                     (import_id, "legacy", fingerprint, len(rows), 0))
    graph.db.execute("INSERT INTO catalog_scopes VALUES(?,?)", ("legacy", import_id))
    graph._catalog_scope = "legacy"
    try:
        for row in rows:
            course = json.loads(row["payload"])
            course.setdefault("course_key", row["id"].removeprefix("course:"))
            iid = project_course(graph, course, row["id"])
            label = graph.get(row["id"])["label"]
            graph.db.execute("UPDATE search SET text=? WHERE rowid=(SELECT search_rowid FROM search_lookup WHERE node_id=?)",
                             (search_text("course", label, course), row["id"]))
            old_edges = graph.db.execute("SELECT * FROM edges WHERE src=? AND relation='listed-by'", (row["id"],)).fetchall()
            if old_edges:
                for edge in old_edges:
                    graph.edge(row["id"], "listed-by", iid, edge["status"], json.loads(edge["evidence"]))
                    if edge["dst"] != iid:
                        graph.db.execute("DELETE FROM edges WHERE id=?", (edge["id"],))
            else:
                graph.edge(row["id"], "listed-by", iid, "source-stated", {"migration": "legacy catalog identity"})
        graph.db.execute("UPDATE nodes SET kind='institution-alias' WHERE kind='institution' AND id NOT IN (SELECT node_id FROM institutions)")
        graph.db.executemany("INSERT OR IGNORE INTO catalog_nodes VALUES('legacy',?)",
                            ((row[0],) for row in graph.db.execute("SELECT id FROM nodes WHERE kind IN (" + ",".join("?" for _ in CATALOG_KINDS) + ")", tuple(CATALOG_KINDS)).fetchall()))
        graph.db.executemany("INSERT OR IGNORE INTO catalog_edges VALUES('legacy',?)",
                            ((row[0],) for row in graph.db.execute("SELECT id FROM edges WHERE status!='reviewed' AND relation IN (" + ",".join("?" for _ in CATALOG_RELATIONS) + ")", tuple(CATALOG_RELATIONS)).fetchall()))
    finally:
        graph._catalog_scope = None


def begin_import(graph, courses, materials, scope):
    if not isinstance(scope, str) or not scope.strip():
        raise ValueError("Catalog scope must be a nonempty string")
    for collection in (courses, materials):
        keys = [item.get("course_key") for item in collection]
        if any(not isinstance(key, str) or not key for key in keys) or len(keys) != len(set(keys)):
            raise ValueError("Catalog course keys must be unique nonempty strings")
    for item in materials:
        if item.get("status") not in MATERIAL_STATUSES:
            raise ValueError("Invalid material evidence status: " + str(item.get("status")))
    fingerprint = curriculum_fingerprint(courses, materials)
    current = graph.db.execute("SELECT i.fingerprint FROM catalog_scopes s JOIN catalog_imports i ON i.id=s.import_id WHERE s.scope=?", (scope,)).fetchone()
    if current and current[0] == fingerprint:
        return None
    stale = {row[0] for row in graph.db.execute("SELECT node_id FROM catalog_nodes WHERE scope=?", (scope,))}
    edges = [row[0] for row in graph.db.execute("SELECT edge_id FROM catalog_edges WHERE scope=?", (scope,))]
    graph.db.execute("DELETE FROM catalog_edges WHERE scope=?", (scope,))
    graph.db.execute("DELETE FROM catalog_nodes WHERE scope=?", (scope,))
    graph.db.execute("DELETE FROM catalog_course_sources WHERE scope=?", (scope,))
    for eid in edges:
        graph.db.execute("DELETE FROM edges WHERE id=? AND status!='reviewed' AND NOT EXISTS(SELECT 1 FROM catalog_edges WHERE edge_id=?)", (eid, eid))
    import_id = scope + ":" + fingerprint
    graph.db.execute("INSERT OR IGNORE INTO catalog_imports(id,scope,fingerprint,course_count,materials_count) VALUES(?,?,?,?,?)",
                     (import_id, scope, fingerprint, len(courses), len(materials)))
    graph.db.execute("INSERT INTO catalog_scopes VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET import_id=excluded.import_id", (scope, import_id))
    return {"stale": stale, "fingerprint": fingerprint, "import_id": import_id}


def carry_review(graph, course):
    if course.get("classification_status") == "reviewed":
        return course
    row = graph.db.execute("SELECT payload FROM nodes WHERE id=?", ("course:" + course["course_key"],)).fetchone()
    if not row:
        return course
    old = json.loads(row[0])
    saved = old.get("classification_review")
    if not saved:
        if old.get("classification_review_history"):
            return dict(course) | {"classification_review_history": old["classification_review_history"]}
        return course
    from classification_review import apply_review
    from classify_courses import TAXONOMY_PATH
    try:
        taxonomy = json.loads(TAXONOMY_PATH.read_text())
        review = {key: value for key, value in saved.items() if key != "candidate_classification"}
        return apply_review(course, review, taxonomy)
    except ValueError:
        result = dict(course)
        result["classification_status"] = "needs-review"
        result["classification_review_state"] = "stale"
        result["classification_review_history"] = old.get("classification_review_history", []) + [saved]
        return result


def save_review(graph, course, node_id):
    graph.db.execute("UPDATE catalog_reviews SET status='stale' WHERE node_id=?", (node_id,))
    review = course.get("classification_review")
    if course.get("classification_status") != "reviewed" or not review:
        return
    payload = json.dumps(review, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    graph.db.execute("INSERT INTO catalog_reviews VALUES(?,?,?,?,?,'current') ON CONFLICT(fingerprint) DO UPDATE SET status='current'",
                     (course["review_fingerprint"], node_id, review["source_fingerprint"], review["classification_fingerprint"], payload))


def finish_import(graph, state):
    for nid in state["stale"]:
        if graph.db.execute("SELECT 1 FROM catalog_nodes WHERE node_id=?", (nid,)).fetchone():
            if graph._catalog_scope and graph.db.execute("SELECT 1 FROM catalog_nodes WHERE scope=? AND node_id=?",
                                                         (graph._catalog_scope, nid)).fetchone():
                continue
            row = graph.db.execute("SELECT payload FROM catalog_course_sources WHERE node_id=? ORDER BY scope LIMIT 1", (nid,)).fetchone()
            if row:
                course = carry_review(graph, json.loads(row[0]))
                previous_scope = graph._catalog_scope
                graph._catalog_scope = None
                try:
                    graph.node(nid, "course", str(course.get("title") or course.get("course_title") or course["course_key"]), course)
                    project_course(graph, course, nid)
                    save_review(graph, course, nid)
                finally:
                    graph._catalog_scope = previous_scope
            continue
        if graph.db.execute("SELECT 1 FROM edges WHERE src=? OR dst=? UNION ALL SELECT 1 FROM catalog_reviews WHERE node_id=? LIMIT 1", (nid, nid, nid)).fetchone():
            graph.db.execute("UPDATE catalog_courses SET active=0 WHERE node_id=?", (nid,))
            continue
        if graph.db.execute("SELECT 1 FROM institutions WHERE node_id=? UNION ALL SELECT 1 FROM schools WHERE node_id=?", (nid, nid)).fetchone():
            continue
        graph.db.execute("DELETE FROM catalog_courses WHERE node_id=?", (nid,))
        graph.db.execute("DELETE FROM nodes WHERE id=?", (nid,))


def checked_backup(graph, destination):
    destination = Path(destination)
    if graph.db.in_transaction:
        raise ValueError("Commit pending writes before backup")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb"):
        pass
    copy = sqlite3.connect(destination)
    try:
        graph.db.backup(copy)
        integrity = copy.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok" or copy.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Backup database integrity check failed")
    finally:
        copy.close()
    verify = sqlite3.connect(f"file:{destination.resolve()}?mode=ro", uri=True)
    try:
        if verify.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Reopened backup failed integrity check")
    finally:
        verify.close()
    return {"saved": str(destination), "integrity": "ok", "schema_version": SCHEMA_VERSION}

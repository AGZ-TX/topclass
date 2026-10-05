#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
from collections import Counter
from pathlib import Path

from course_map import load_course_map, read_payload
from course_description_enrichment import load_description_supplement, description_variants_for
from course_code_aliases import load_aliases, aliases_for
from materials_ledger import (baseline_material_entries, resource_identity,
                              material_title, material_assignment_role, CLASSIFICATION_FIELDS)
from catalog import load_catalog
from education_selection import education_plan
from education_store import (initialize, project_course, begin_import, finish_import,
                             transaction, checked_backup, search_text, carry_review, save_review)

ROOT = Path(__file__).resolve().parents[2]


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def identity(kind, value):
    return kind + ":" + hashlib.sha256(encode(value).encode()).hexdigest()


def words(text):
    return set(re.findall(r"[\w]+", text.casefold())) - {
        "a", "an", "the", "and", "or", "to", "of", "for", "with", "agent", "expert", "in"}


class Graph:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self._catalog_scope = None
        try:
            initialize(self)
            self.db.commit()
        except BaseException:
            self.db.rollback()
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def backup(self, destination):
        return checked_backup(self, destination)

    def node(self, node_id, kind, label, payload):
        packed = encode(payload)
        fingerprint = hashlib.sha256(encode([kind,label,payload]).encode()).hexdigest()
        old = self.db.execute("SELECT fingerprint,kind,label,payload FROM nodes WHERE id=?", (node_id,)).fetchone()
        if old and old["kind"] != kind:
            raise ValueError("Node kind cannot change for an existing identity")
        if old and old["label"] == label and (old["payload"] == packed or json.loads(old["payload"]) == payload):
            if self._catalog_scope:
                self.db.execute("INSERT OR IGNORE INTO catalog_nodes VALUES(?,?)", (self._catalog_scope, node_id))
            return node_id
        self.db.execute("DELETE FROM vectors WHERE node_id=?", (node_id,))
        self.db.execute("INSERT INTO nodes VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                        "kind=excluded.kind,label=excluded.label,payload=excluded.payload,"
                        "fingerprint=excluded.fingerprint", (node_id, kind, label, packed, fingerprint))
        if old:
            self.db.execute("DELETE FROM search WHERE rowid=(SELECT search_rowid FROM search_lookup WHERE node_id=?)", (node_id,))
            self.db.execute("DELETE FROM search_lookup WHERE node_id=?", (node_id,))
        result = self.db.execute("INSERT INTO search VALUES (?,?)", (node_id, search_text(kind,label,payload)))
        self.db.execute("INSERT INTO search_lookup VALUES(?,?)", (node_id, result.lastrowid))
        if self._catalog_scope:
            self.db.execute("INSERT OR IGNORE INTO catalog_nodes VALUES(?,?)", (self._catalog_scope, node_id))
        return node_id

    def edge(self, src, relation, dst, status, evidence):
        if status not in {"source-stated", "candidate", "inferred", "reviewed"}:
            raise ValueError("Invalid edge evidence status")
        eid = identity("edge", [src, relation, dst, status, evidence])
        self.db.execute("INSERT OR IGNORE INTO edges VALUES (?,?,?,?,?,?)",
                        (eid, src, relation, dst, status, encode(evidence)))
        if self._catalog_scope and status != "reviewed":
            self.db.execute("INSERT OR IGNORE INTO catalog_edges VALUES(?,?)", (self._catalog_scope, eid))
        return eid

    def get(self, node_id):
        row = self.db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        if not row:
            raise ValueError("Unknown node: " + node_id)
        return dict(row) | {"payload": json.loads(row["payload"])}

    def neighbors(self, node_id):
        return [dict(r) | {"evidence": json.loads(r["evidence"])} for r in self.db.execute(
            "SELECT * FROM edges WHERE src=? OR dst=? ORDER BY relation,id", (node_id, node_id))]

    def stats(self):
        return {"nodes": dict(self.db.execute("SELECT kind,count(*) FROM nodes GROUP BY kind")),
                "edges": dict(self.db.execute("SELECT relation,count(*) FROM edges GROUP BY relation")),
                "vector_spaces": dict(self.db.execute("SELECT space,count(*) FROM vectors GROUP BY space")),
                "imports": dict(self.db.execute("SELECT key,value FROM meta"))}

    def search(self, query, kinds=("course",), limit=20):
        terms = sorted(words(query))
        if not terms:
            return []
        expression = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
        placeholders = ",".join("?" for _ in kinds)
        rows = self.db.execute(
            "SELECT n.id,bm25(search) AS rank FROM search JOIN nodes n ON n.id=search.id "
            f"WHERE search MATCH ? AND n.kind IN ({placeholders}) "
            "AND NOT EXISTS(SELECT 1 FROM catalog_courses c WHERE c.node_id=n.id AND c.active=0) "
            "ORDER BY rank,n.id LIMIT ?",
            [expression, *kinds, limit]).fetchall()
        return [self.get(r["id"]) | {"lexical_rank": r["rank"]} for r in rows]

    def put_vector(self, node_id, space, vector):
        if not isinstance(vector, list) or not vector or any(
                isinstance(x, bool) or not isinstance(x, (float, int)) or not math.isfinite(x)
                for x in vector):
            raise ValueError("Invalid embedding vector")
        norm = math.sqrt(sum(x*x for x in vector))
        if norm == 0:
            raise ValueError("Zero embedding vector")
        node = self.get(node_id)
        existing = self.db.execute("SELECT vector FROM vectors WHERE space=? LIMIT 1", (space,)).fetchone()
        if existing and len(json.loads(existing[0])) != len(vector):
            raise ValueError("Embedding dimensions differ inside a space")
        self.db.execute("INSERT OR REPLACE INTO vectors VALUES (?,?,?,?)",
                        (node_id, space, node["fingerprint"], encode([x/norm for x in vector])))

    def semantic(self, vector, space, kinds=("course",), limit=20, source_ids=None):
        if not vector or any(not math.isfinite(x) for x in vector):
            raise ValueError("Invalid query embedding")
        norm = math.sqrt(sum(x*x for x in vector))
        if not norm:
            raise ValueError("Zero query embedding")
        query = [x/norm for x in vector]
        import heapq
        best = []
        placeholders = ",".join("?" for _ in kinds)
        scope_clause = ""
        scope_values = []
        if source_ids is not None:
            if not source_ids:
                return []
            scope_values = list(source_ids)
            scope_clause = " AND json_extract(n.payload,'$.source_id') IN (" + ",".join("?" for _ in scope_values) + ")"
        for row in self.db.execute(
            "SELECT v.node_id,v.vector FROM vectors v JOIN nodes n ON n.id=v.node_id "
            f"WHERE space=? AND n.kind IN ({placeholders}) AND v.fingerprint=n.fingerprint "
            "AND NOT EXISTS(SELECT 1 FROM catalog_courses c WHERE c.node_id=n.id AND c.active=0)" + scope_clause,
            [space, *kinds, *scope_values]):
            data = json.loads(row["vector"])
            if len(data) != len(query):
                raise ValueError("Query embedding space/dimension mismatch")
            score = sum(a*b for a,b in zip(query,data))
            item = (score, row["node_id"])
            if len(best) < limit:
                heapq.heappush(best,item)
            elif item > best[0]:
                heapq.heapreplace(best,item)
        return [self.get(nid) | {"semantic_score": score} for score,nid in sorted(best,reverse=True)]


def import_curriculum(graph, courses, materials, scope):
    with transaction(graph.db):
        state = begin_import(graph, courses, materials, scope)
        if state is None:
            return
        graph._catalog_scope = scope
        try:
            _write_curriculum(graph, courses, materials, scope)
            finish_import(graph, state)
            graph.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                             ("curriculum:"+scope,encode({"courses":len(courses),"materials_rows":len(materials),
                                                       "fingerprint":state["fingerprint"],"import_id":state["import_id"]})))
        finally:
            graph._catalog_scope = None


def _write_curriculum(graph, courses, materials, scope):
        for course in courses:
            course = carry_review(graph, course)
            key = course["course_key"]
            cid = "course:" + key
            payload={k:v for k,v in course.items() if k!="course_materials"}
            graph.node(cid, "course", str(course.get("title") or course.get("course_title") or key), payload)
            save_review(graph, course, cid)
            sid = project_course(graph, payload, cid)
            graph.edge(cid,"listed-by",sid,"source-stated",{
                "source_urls": course.get("source_urls",[]), "source_records": course.get("source_records",[]),
                "source_institution":course.get("institution") or course.get("university")})
            for field, kind, relation in (("academic_areas","area","area-candidate"),
                                           ("expertise_tags","specialty","specialty-candidate")):
                supported = field == "academic_areas" and "discovery_academic_areas" in course
                labels = course["discovery_academic_areas"] if supported else course.get(field, [])
                for tag in labels:
                    tid = kind + ":" + tag["id"]
                    graph.node(tid,kind,tag.get("label",tag["id"]),{"id":tag["id"],"label":tag.get("label")})
                    reviewed = tag.get("human_reviewed") is True if supported else course.get("classification_status") == "reviewed"
                    graph.edge(cid,relation,tid,"reviewed" if reviewed else "candidate",{
                        "classification_evidence":course.get("classification_evidence",{}),
                        "match":tag,"status":course.get("classification_status"),
                        "source_fingerprint":course.get("source_fingerprint"),
                        "classification_fingerprint":course.get("classification_fingerprint"),
                        "review_fingerprint":course.get("review_fingerprint")})
        for entry in materials:
            cid = "course:" + entry["course_key"]
            if not graph.db.execute("SELECT 1 FROM nodes WHERE id=?",(cid,)).fetchone():
                graph.node(cid,"course",entry.get("title") or entry.get("code") or entry["course_key"],{
                    "course_key":entry["course_key"],"institution":entry.get("institution"),
                    "code":entry.get("code"),"title":entry.get("title"),"identity_only":True})
                sid = project_course(graph, graph.get(cid)["payload"], cid)
                graph.edge(cid,"listed-by",sid,"source-stated",{"scope":scope,"identity_only":True})
            else:
                graph.db.execute("INSERT OR IGNORE INTO catalog_nodes VALUES(?,?)", (scope, cid))
            lid = identity("materials-status",[scope,entry["course_key"]])
            graph.node(lid,"materials-status",entry["status"],entry)
            graph.edge(cid,"materials-evidence",lid,"source-stated",{"scope":scope})
            for i,material in enumerate(entry.get("materials",[])):
                if not isinstance(material,dict):
                    continue
                mid = identity("assignment",[entry["course_key"],i,material])
                graph.node(mid,"assignment",material_title(material) or "Unidentified material",material)
                graph.edge(cid,"assigns-or-mentions",mid,"source-stated",{
                    "assignment_role":material_assignment_role(material),
                    "source_url":material.get("source_url"),"scope":scope})
                candidate = resource_identity(material)
                if candidate:
                    rid = identity("resource-candidate",candidate["key"])
                    graph.node(rid,"resource-candidate",candidate["key"],{
                        "candidate_key":candidate["key"],"identity_basis":candidate["identity_basis"],
                        "certainty":candidate["certainty"],"bibliography_verified":False})
                    graph.edge(mid,"identity-candidate",rid,"candidate",candidate)


def build(graph, root=ROOT, baseline_only=False, reviews=None, _replace=False):
    if not _replace and graph.db.execute("SELECT 1 FROM nodes LIMIT 1").fetchone():
        raise ValueError("Build curriculum in a fresh database; do not overlay changed snapshots")
    description_path = Path(root) / "data/course-evidence/teaching-descriptions.json"
    description_hash = hashlib.sha256(description_path.read_bytes()).hexdigest() if not baseline_only and description_path.exists() else None
    descriptions = {} if baseline_only else load_description_supplement(description_path)
    alias_path = Path(root) / 'data/course-evidence/course-code-aliases.json'
    alias_hash = hashlib.sha256(alias_path.read_bytes()).hexdigest() if alias_path.exists() else None
    aliases = load_aliases(alias_path)
    data,_,manifest = load_catalog()
    baseline = []
    entries = baseline_material_entries(root)
    by_id = {r["baseline_course_id"]:r for r in entries}
    for original in data["courses"]:
        e = by_id[original["course_id"]]
        baseline.append(original | {"course_key":e["course_key"],"institution":e["institution"],
                                    "code":e["code"],"title":e["title"]} |
                        {field:e[field] for field in (*CLASSIFICATION_FIELDS, "primary_academic_area", "expertise_tags")
                         if field in e})
    baseline = [row | {"description_variants": [*row.get("description_variants", []), *variants]}
                if (variants := description_variants_for(row, descriptions)) else row for row in baseline]
    baseline = [row | aliases_for(row, aliases) for row in baseline]
    with transaction(graph.db):
        if _replace:
            for old_scope in ("legacy", "2026-27-expansion" if baseline_only else None):
                if old_scope and graph.db.execute("SELECT 1 FROM catalog_scopes WHERE scope=?", (old_scope,)).fetchone():
                    import_curriculum(graph, [], [], old_scope)
        if not baseline_only:
            expansion = root / "data" / "expansion"
            index = json.loads((expansion/"catalog-datasets-2026-27.json").read_text())
            for pair in index["dataset_pairs"]:
                for name in (pair["inventory"],pair["classification_index"]):
                    folder = expansion/name
                    m = json.loads((folder/"manifest.json").read_text())
                    for file in m["files"]:
                        if file.get("collection","courses") == "courses" or name == pair["classification_index"]:
                            if not (folder/file["file"]).is_file():
                                raise ValueError("Missing dataset: " + str(folder/file["file"]))
            folder = expansion/"course-materials-2026-27"
            m = json.loads((folder/"manifest.json").read_text())
            ledger = read_payload(folder,m["files"][0],"entries")["entries"]
            rows = load_course_map(expansion, reviews=reviews, include_materials=False)
            rows = [row | {"description_variants": [*row.get("description_variants", []), *variants]}
                    if (variants := description_variants_for(row, descriptions)) else row for row in rows]
            expected = {r["course_key"] for r in rows} | {r["course_key"] for r in baseline}
            keys = [r["course_key"] for r in ledger]
            if len(set(keys)) != len(keys) or set(keys) != expected:
                raise ValueError("Materials ledger does not exactly cover course identities")
            import_curriculum(graph,rows,ledger,"2026-27-expansion")
        import_curriculum(graph,baseline,entries,"v0.4")
        graph.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                         ("baseline_hash",manifest["compressed_sha256"]))
        current_alias_hash = hashlib.sha256(alias_path.read_bytes()).hexdigest() if alias_path.exists() else None
        if current_alias_hash != alias_hash:
            raise ValueError('Course code aliases changed during database build')
        graph.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('course_code_alias_hash', alias_hash or 'absent'))
        if not baseline_only:
            current_hash = hashlib.sha256(description_path.read_bytes()).hexdigest() if description_path.exists() else None
            if current_hash != description_hash:
                raise ValueError("Teaching-description supplement changed during database build")
    return graph.stats()


def refresh(graph, root=ROOT, baseline_only=False, reviews=None):
    return build(graph, root, baseline_only, reviews=reviews, _replace=True)


def main():
    parser = argparse.ArgumentParser(description='Evidence graph and explainable education plans. No provider calls on import.')
    parser.add_argument("--db",type=Path,required=True)
    sub = parser.add_subparsers(dest="command",required=True)
    for command in ("build", "refresh"):
        p = sub.add_parser(command)
        p.add_argument("--baseline-only",action="store_true")
        p.add_argument("--reviews",type=Path)
    p = sub.add_parser("backup"); p.add_argument("--out",type=Path,required=True)
    sub.add_parser("stats")
    p = sub.add_parser("search"); p.add_argument("query")
    p = sub.add_parser("neighbors"); p.add_argument("id")
    p = sub.add_parser("plan"); p.add_argument("--brief",type=Path,required=True)
    p.add_argument("--per-specialty",type=int,default=3); p.add_argument("--out",type=Path,required=True)
    args = parser.parse_args()
    reviews = None
    if args.command in {"build", "refresh"} and args.reviews:
        from classification_review import read_records
        reviews = read_records(args.reviews)
    args.db.parent.mkdir(parents=True,exist_ok=True)
    g = Graph(args.db)
    try:
        if args.command=="build": result=build(g,baseline_only=args.baseline_only,reviews=reviews)
        elif args.command=="refresh": result=refresh(g,baseline_only=args.baseline_only,reviews=reviews)
        elif args.command=="backup": result=g.backup(args.out)
        elif args.command=="stats": result=g.stats()
        elif args.command=="search": result=g.search(args.query)
        elif args.command=="neighbors": result=g.neighbors(args.id)
        else:
            if not 1 <= args.per_specialty <= 20: raise ValueError("per-specialty must be 1..20")
            result=education_plan(g,json.loads(args.brief.read_text()),args.per_specialty)
            args.out.parent.mkdir(parents=True,exist_ok=True)
            args.out.write_text(json.dumps(result,indent=2,ensure_ascii=False)+"\n")
            result={"saved":str(args.out),"courses":len(result["courses"]),"gaps":len(result["gaps"])}
        print(json.dumps(result,indent=2,ensure_ascii=False))
    finally:
        g.close()


if __name__=="__main__":
    main()

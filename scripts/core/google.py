#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import math
import re
from pathlib import Path

from core.graph import Graph, encode, education_plan, identity

MODEL = "gemini-embedding-2"
FORMAT_VERSION = "topclass-gemini-retrieval-v2"
UNIT_BYTES = 4000


class GeminiEmbeddings:
    def __init__(self, graph, dimensions=768, max_requests=None, daily_cap=None, transport=None, runtime=None):
        if dimensions not in {768, 1536, 3072} or (max_requests is not None and max_requests < 1) or (daily_cap is not None and daily_cap < 1):
            raise ValueError("Invalid dimensions or explicit request budgets")
        if runtime is not None and transport is not None:
            raise ValueError("Choose runtime or a test transport")
        self.graph, self.dimensions = graph, dimensions
        self.max_requests, self.daily_cap = max_requests, daily_cap
        self.transport, self.runtime = transport, runtime
        self.attempts = 0
        self.space = f"google:{MODEL}:{dimensions}:{FORMAT_VERSION}"
        graph.db.execute("CREATE TABLE IF NOT EXISTS provider_usage (day TEXT PRIMARY KEY, attempts INTEGER NOT NULL)")
        graph.db.commit()

    def _reserve(self):
        if self.max_requests is not None and self.attempts >= self.max_requests:
            raise ValueError("Local per-run request cap reached; indexing is resumable")
        if self.runtime is None and self.daily_cap is not None:
            day = dt.datetime.now(dt.timezone.utc).date().isoformat()
            with self.graph.db:
                self.graph.db.execute("INSERT OR IGNORE INTO provider_usage VALUES (?,0)", (day,))
                updated = self.graph.db.execute(
                    "UPDATE provider_usage SET attempts=attempts+1 WHERE day=? AND attempts<?", (day, self.daily_cap))
                if updated.rowcount != 1:
                    raise ValueError("Local UTC-day request cap reached; this is not the provider's quota/reset")
        self.attempts += 1

    def embed(self, parts):
        if not parts:
            raise ValueError("Empty embedding content")
        if self.runtime is None and self.transport is None:
            raise ValueError("Live embedding requires a configured ProviderRuntime and user-owned Google profile")
        self._reserve()
        payload = {"model": "models/" + MODEL, "content": {"parts": parts}, "outputDimensionality": self.dimensions}
        if self.runtime is not None:
            estimate = sum(len(part.get("text", "").encode()) + (4096 if "inlineData" in part else 0) for part in parts)
            response = self.runtime.request("google", MODEL, "embedContent", payload, estimated_tokens=estimate)
        else:
            response = self.transport(payload)
        return self.checked_vector(response.get("embedding", {}) if isinstance(response, dict) else {})

    def checked_vector(self, embedding):
        vector = embedding.get("values") if isinstance(embedding, dict) else None
        if not isinstance(vector, list) or len(vector) != self.dimensions or any(
                isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in vector):
            raise ValueError("Provider returned an invalid embedding")
        if not sum(value * value for value in vector):
            raise ValueError("Provider returned a zero embedding")
        return vector

    def embed_many(self, contents):
        if not isinstance(contents, list) or not 1 <= len(contents) <= 16 or any(not parts for parts in contents):
            raise ValueError("An embedding batch needs 1 to 16 nonempty contents")
        if self.runtime is None and self.transport is None:
            raise ValueError("Live embedding requires a configured ProviderRuntime and user-owned Google profile")
        self._reserve()
        payload = {"requests": [{"model": "models/" + MODEL, "content": {"parts": parts},
                                 "outputDimensionality": self.dimensions} for parts in contents]}
        estimate = sum(len(part.get("text", "").encode()) + (4096 if "inlineData" in part else 0)
                       for parts in contents for part in parts)
        response = (self.runtime.request("google", MODEL, "batchEmbedContents", payload, estimated_tokens=estimate)
                    if self.runtime is not None else self.transport(payload))
        embeddings = response.get("embeddings") if isinstance(response, dict) else None
        if not isinstance(embeddings, list) or len(embeddings) != len(contents):
            raise ValueError("Provider returned an incomplete embedding batch")
        return [self.checked_vector(embedding) for embedding in embeddings]

    def query(self, text):
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > 6000:
            raise ValueError("Query must be nonempty and within the conservative input budget")
        return self.embed([{"text": "task: search result | query: " + text}])

    def usage(self):
        if self.runtime is not None:
            return {"embedding_calls_in_run": self.attempts, "provider_remaining": "unknown", **self.runtime.usage()}
        day = dt.datetime.now(dt.timezone.utc).date().isoformat()
        row = self.graph.db.execute("SELECT attempts FROM provider_usage WHERE day=?", (day,)).fetchone()
        used = row[0] if row else 0
        return {"local_day_UTC": day, "attempts_in_run": self.attempts, "local_day_attempts": used,
                "local_day_remaining": max(0, self.daily_cap - used) if self.daily_cap is not None else None, "provider_remaining": "unknown",
                "provider_reset": "unknown; test-transport local budget resets at UTC midnight"}


def bounded_text(text, size=UNIT_BYTES):
    if len(text.encode()) > size:
        raise ValueError("Content exceeds one retrieval unit; index complete units instead")
    return text


def node_text(node):
    payload = node["payload"]
    if node["kind"] in {"region", "retrieval-unit"}:
        return payload.get("text", "")
    if node["kind"] == "course":
        return encode({key: payload.get(key) for key in (
            "institution", "code", "description", "description_variants", "expertise_tags")})
    if node["kind"] == "knowledge":
        return encode({key: payload.get(key) for key in (
            "content", "kind", "topics", "conditions", "exceptions", "warnings", "uncertainty")})
    return encode(payload)


def representation(node):
    text = bounded_text(node_text(node))
    if node['kind'] == 'retrieval-unit' and node['payload'].get('context_version') == 1:
        before, after = node['payload'].get('context_before', ''), node['payload'].get('context_after', '')
        if len(before.encode()) > 1024 or len(after.encode()) > 1024:
            raise ValueError('Retrieval-unit context exceeds its bound')
        text = before + text + after
    image = node["payload"].get("image_path") if node["kind"] in {"region", "retrieval-unit"} else None
    if image:
        raw = Path(image).read_bytes()
        if len(raw) > 8_000_000:
            raise ValueError("Rendered page exceeds inline image budget; explicit resizing adapter needed")
        return ([{"text": text}] if text.strip() else []) + [
            {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(raw).decode()}}]
    title = node["label"]
    if len(title.encode()) > 2000:
        raise ValueError("Source title exceeds the representation budget")
    return [{"text": f"title: {title} | text: {text}"}]


def text_spans(text):
    start = 0
    while start < len(text):
        end = start
        used = 0
        while end < len(text):
            size = len(text[end].encode())
            if used + size > UNIT_BYTES:
                break
            used += size
            end += 1
        boundaries = [match.end() + start for match in re.finditer(r'\n|(?<=[.!?])\s+', text[start:end])]
        boundary = next((value for value in reversed(boundaries) if value >= start + (end - start) // 2), None)
        if end < len(text) and boundary is not None:
            end = boundary
        yield start, end
        start = end


def retrieval_nodes(graph, node):
    text = node_text(node)
    if len(text.encode()) <= UNIT_BYTES:
        return [node]
    payload = node["payload"]
    image = payload.get("image_path") if node["kind"] == "region" else None
    image_hash = hashlib.sha256(Path(image).read_bytes()).hexdigest() if image else None
    content_hash = hashlib.sha256(encode({"label": node["label"], "text": text,
        "source_version": payload.get("source_version"), "image_hash": image_hash}).encode()).hexdigest()
    units = []
    with graph.db:
        for start, end in text_spans(text):
            unit_id = identity("retrieval-unit", [node["id"], content_hash, start, end, FORMAT_VERSION, "context-v1"])
            value = {"parent_id": node["id"], "parent_kind": node["kind"], "parent_content_hash": content_hash,
                     "source_id": payload.get("source_id"), "source_version": payload.get("source_version"),
                     "offset_start": start, "offset_end": end, "text": text[start:end],
                     "image_path": image, "representation_version": FORMAT_VERSION,
                     "context_version": 1, "context_before": text[max(0, start - 256):start],
                     "context_after": text[end:end + 256]}
            graph.node(unit_id, "retrieval-unit", node["label"], value)
            graph.edge(unit_id, "retrieval-unit-of", node["id"], "source-stated", {"offset_start": start, "offset_end": end})
            units.append(graph.get(unit_id))
    return units


def coverage_table(graph):
    graph.db.execute("CREATE TABLE IF NOT EXISTS embedding_coverage (parent_id TEXT, space TEXT, "
                     "fingerprint TEXT NOT NULL, unit_ids TEXT NOT NULL, status TEXT NOT NULL, "
                     "PRIMARY KEY(parent_id,space))")
    graph.db.commit()


def checked_index_source(graph, node, sources):
    if node["kind"] not in {"region", "knowledge"}:
        return
    from core.sources import checked_region, checked_source
    source_id = node["payload"].get("source_id")
    try:
        if source_id not in sources:
            sources[source_id] = checked_source(graph, source_id)
        source = sources[source_id]
        region_id = node["id"] if node["kind"] == "region" else node["payload"]["region_id"]
        region = checked_region(graph, region_id, source)
        if node["payload"].get("source_version") != region["payload"]["source_version"]:
            raise ValueError("Indexed source version is stale")
    except (ValueError, KeyError, OSError):
        with graph.db:
            graph.db.execute("DELETE FROM vectors WHERE node_id IN (SELECT id FROM nodes "
                             "WHERE json_extract(payload,'$.source_id')=?)", (source_id,))
            graph.db.execute("UPDATE embedding_coverage SET status='invalid-source' WHERE parent_id IN "
                             "(SELECT id FROM nodes WHERE json_extract(payload,'$.source_id')=?)", (source_id,))
        raise


def index(graph, provider, kinds=("course", "assignment", "region", "knowledge"), source_ids=None):
    if not kinds or any(kind == "retrieval-unit" for kind in kinds):
        raise ValueError("Index original node kinds; retrieval units are derived automatically")
    coverage_table(graph)
    placeholders = ",".join("?" for _ in kinds)
    source_clause = ""
    source_values = []
    if source_ids is not None:
        source_values = list(source_ids)
        source_clause = (" AND json_extract(n.payload,'$.source_id') IN (" +
                         ",".join("?" for _ in source_values) + ")") if source_values else " AND 0"
    ids = [row[0] for row in graph.db.execute(
        f"SELECT n.id FROM nodes n WHERE kind IN ({placeholders}) "
        "AND NOT EXISTS(SELECT 1 FROM catalog_courses c WHERE c.node_id=n.id AND c.active=0)" +
        source_clause + " ORDER BY n.id", [*kinds, *source_values])]
    indexed = skipped = complete = incomplete = 0
    gaps = []
    deferred = None
    checked_sources = {}
    for node_id in ids:
        node = graph.get(node_id)
        if node["kind"] == "knowledge" and node["payload"].get("review_state") != "reviewed":
            skipped += 1
            continue
        checked_index_source(graph, node, checked_sources)
        units = retrieval_nodes(graph, node)
        unit_ids = [unit["id"] for unit in units]
        old = graph.db.execute("SELECT fingerprint,unit_ids FROM embedding_coverage WHERE parent_id=? AND space=?",
                               (node_id, provider.space)).fetchone()
        with graph.db:
            if old and (old[0] != node["fingerprint"] or json.loads(old[1]) != unit_ids):
                for stale_id in set(json.loads(old[1])) - set(unit_ids):
                    graph.db.execute("DELETE FROM vectors WHERE node_id=? AND space=?", (stale_id, provider.space))
                graph.db.execute("DELETE FROM vectors WHERE node_id=? AND space=?", (node_id, provider.space))
            graph.db.execute("INSERT OR REPLACE INTO embedding_coverage VALUES(?,?,?,?,?)",
                             (node_id, provider.space, node["fingerprint"], encode(unit_ids), "partial"))
        vectors = []
        for unit in units:
            current = graph.db.execute("SELECT fingerprint,vector FROM vectors WHERE node_id=? AND space=?",
                                       (unit["id"], provider.space)).fetchone()
            if current and current[0] == unit["fingerprint"]:
                skipped += 1
                vectors.append(json.loads(current[1]))
                continue
            if deferred is not None or (provider.max_requests is not None and provider.attempts >= provider.max_requests):
                break
            try:
                vector = provider.embed(representation(unit))
            except Exception as exc:
                from core.provider import ProviderDeferred
                if not isinstance(exc, ProviderDeferred):
                    raise
                deferred = {"reason": exc.reason, "next_attempt_at": exc.next_attempt_at}
                break
            with graph.db:
                graph.put_vector(unit["id"], provider.space, vector)
            vectors.append(vector)
            indexed += 1
        if len(vectors) == len(units):
            with graph.db:
                if len(units) > 1:
                    aggregate = [sum(values) / len(vectors) for values in zip(*vectors)]
                    if sum(value * value for value in aggregate):
                        graph.put_vector(node_id, provider.space, aggregate)
                graph.db.execute("UPDATE embedding_coverage SET status='complete' WHERE parent_id=? AND space=?",
                                 (node_id, provider.space))
            complete += 1
        else:
            incomplete += 1
            gaps.append({"node_id": node_id, "completed_units": len(vectors), "total_units": len(units)})
    return {"indexed": indexed, "skipped": skipped, "eligible_nodes": len(ids),
            "complete_nodes": complete, "incomplete_nodes": incomplete, "coverage_gaps": gaps,
            "deferred": deferred, "space": provider.space, "usage": provider.usage()}


def main():
    parser = argparse.ArgumentParser(description="Gemini Embedding 2 with complete source units and coordinated provider budgets.")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--dimensions", type=int, default=768)
    parser.add_argument("--max-requests", type=int)
    parser.add_argument("--daily-cap", type=int, help="Optional lower project request limit; the provider profile supplies the default")
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--runtime-db", type=Path, default=Path("derived_private/providers.db"))
    parser.add_argument("--allow-model-calls", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("index")
    command.add_argument("--kinds", nargs="+", default=["region", "knowledge"])
    command = sub.add_parser("search")
    command.add_argument("query")
    command.add_argument("--kinds", nargs="+", default=["knowledge", "region", "retrieval-unit"])
    command = sub.add_parser("plan")
    command.add_argument("--brief", type=Path, required=True)
    command.add_argument("--out", type=Path, required=True)
    sub.add_parser("usage")
    args = parser.parse_args()
    if args.daily_cap is not None and args.daily_cap < 1:
        parser.error("--daily-cap must be positive")
    runtime = None
    if args.profile:
        from core.provider import ProviderRuntime
        profile = json.loads(args.profile.read_text())
        profile["enabled"] = args.allow_model_calls
        from core.pipeline import google_profile
        profile = google_profile(profile)
        runtime = ProviderRuntime(args.runtime_db, profile)
    if args.command != "usage" and (runtime is None or not args.allow_model_calls):
        parser.error("Live operations require --profile and --allow-model-calls")
    graph = Graph(args.db)
    try:
        provider = GeminiEmbeddings(graph, args.dimensions, args.max_requests, None, runtime=runtime)
        if args.command == "usage":
            result = provider.usage()
        elif args.command == "index":
            result = index(graph, provider, tuple(args.kinds))
        elif args.command == "search":
            result = graph.semantic(provider.query(args.query), provider.space, tuple(args.kinds))
        else:
            brief = json.loads(args.brief.read_text())
            requirements = list(dict.fromkeys(brief.get("specialties", []) + brief.get("deliverables", [])))
            if provider.max_requests is not None and len(requirements) > provider.max_requests:
                raise ValueError("Increase max-requests to cover requested capability queries")
            candidates = {requirement: graph.semantic(provider.query(requirement), provider.space, ("course",), 100)
                          for requirement in requirements}
            result = education_plan(graph, brief, semantic_candidates=candidates)
            result["embedding_space"] = provider.space
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
            result = {"saved": str(args.out), "usage": provider.usage()}
        print(json.dumps(result, indent=2, ensure_ascii=False))
    finally:
        graph.close()
        if runtime is not None:
            runtime.close()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
from __future__ import annotations

import json
import math

from core.graph import encode
from core.google import (GeminiEmbeddings, checked_index_source, coverage_table,
                              representation, retrieval_nodes)
from core.provider import ProviderDeferred
from core.sources import regions


def index_originals(graph, provider, source_ids, batch_size=8, progress=None):
    if not isinstance(source_ids, list) or not source_ids or len(set(source_ids)) != len(source_ids):
        raise ValueError("Provide distinct registered source IDs")
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 16:
        raise ValueError("Batch size must be between 1 and 16")
    coverage_table(graph)
    parents, pending, checked, skipped = [], [], {}, 0
    for source_id in source_ids:
        from core.markup import attach_visuals
        attach_visuals(graph, source_id)
        source_regions = regions(graph, source_id)
        if not source_regions:
            raise ValueError("Source has no registered regions")
        for parent in source_regions:
            checked_index_source(graph, parent, checked)
            units = retrieval_nodes(graph, parent)
            unit_ids = [unit["id"] for unit in units]
            old = graph.db.execute("SELECT fingerprint,unit_ids FROM embedding_coverage WHERE parent_id=? AND space=?",
                                   (parent["id"], provider.space)).fetchone()
            with graph.db:
                if old and (old[0] != parent["fingerprint"] or json.loads(old[1]) != unit_ids):
                    for stale in set(json.loads(old[1])) - set(unit_ids):
                        graph.db.execute("DELETE FROM vectors WHERE node_id=? AND space=?", (stale, provider.space))
                    graph.db.execute("DELETE FROM vectors WHERE node_id=? AND space=?", (parent["id"], provider.space))
                graph.db.execute("INSERT OR REPLACE INTO embedding_coverage VALUES(?,?,?,?,?)",
                                 (parent["id"], provider.space, parent["fingerprint"], encode(unit_ids), "partial"))
            parents.append((parent, units))
            for unit in units:
                current = graph.db.execute("SELECT fingerprint FROM vectors WHERE node_id=? AND space=?",
                                           (unit["id"], provider.space)).fetchone()
                if current and current[0] == unit["fingerprint"]:
                    skipped += 1
                else:
                    pending.append(unit)
    total_units = skipped + len(pending)
    if progress:
        progress({"completed_units": skipped, "total_units": total_units})
    indexed, deferred, offset = 0, None, 0
    try:
        while offset < len(pending) and (provider.max_requests is None or provider.attempts < provider.max_requests):
            batch, contents, size = [], [], 0
            for unit in pending[offset:offset + batch_size]:
                parts = representation(unit)
                part_size = len(encode(parts).encode())
                if batch and size + part_size > 16_000_000:
                    break
                batch.append(unit)
                contents.append(parts)
                size += part_size
            vectors = provider.embed_many(contents)
            with graph.db:
                for unit, vector in zip(batch, vectors):
                    graph.put_vector(unit["id"], provider.space, vector)
            indexed += len(batch)
            offset += len(batch)
            if progress:
                progress({"completed_units": skipped + indexed, "total_units": total_units})
    except ProviderDeferred as exc:
        deferred = {"reason": exc.reason, "next_attempt_at": exc.next_attempt_at}
    finally:
        complete, gaps = 0, []
        with graph.db:
            for parent, units in parents:
                vectors = []
                for unit in units:
                    row = graph.db.execute("SELECT fingerprint,vector FROM vectors WHERE node_id=? AND space=?",
                                           (unit["id"], provider.space)).fetchone()
                    if row and row[0] == unit["fingerprint"]:
                        vectors.append(json.loads(row[1]))
                if len(vectors) == len(units):
                    if len(units) > 1:
                        aggregate = [sum(values) / len(vectors) for values in zip(*vectors)]
                        if sum(value * value for value in aggregate):
                            graph.put_vector(parent["id"], provider.space, aggregate)
                    graph.db.execute("UPDATE embedding_coverage SET status='complete' WHERE parent_id=? AND space=?",
                                     (parent["id"], provider.space))
                    complete += 1
                else:
                    gaps.append({"node_id": parent["id"], "completed_units": len(vectors), "total_units": len(units)})
    images = [parent for parent, _ in parents if parent['payload'].get('image_path')]
    visual_gaps, expected_figures = [], 0
    for source_id in source_ids:
        source = graph.get(source_id)
        payload = source['payload']
        actual_figures = sum(parent['payload'].get('figure_number') is not None for parent, _ in parents if parent['payload']['source_id'] == source_id)
        expected_figures += payload.get('expected_figures', len(payload.get('retained_visuals', [])))
        visual_gaps.extend(payload.get('visual_gaps', []))
        if payload.get('retained_visuals') and not actual_figures:
            visual_gaps.append({'source_id': source_id, 'reason': 'Retained external figures are not registered for embedding; import the original HTML asset bundle'})
    image_complete = sum(not any(gap['node_id'] == image['id'] for gap in gaps) for image in images)
    if image_complete < len(images):
        visual_gaps.append({'kind': 'visual-embedding', 'reason': 'Some retained images do not have complete embeddings', 'missing': len(images) - image_complete})
    visual_coverage = {'image_regions': len(images), 'complete_image_regions': image_complete,
                       'expected_html_figures': expected_figures, 'gaps': visual_gaps,
                       'quality': 'representation coverage only; inspect original-resolution visuals when answering'}
    return {"indexed": indexed, "skipped": skipped, "eligible_nodes": len(parents), "complete_nodes": complete,
            "incomplete_nodes": len(gaps), "coverage_gaps": gaps, "deferred": deferred,
            "space": provider.space, "usage": provider.usage(), "reader_calls": 0, "visual_coverage": visual_coverage}


def semantic_links(graph, source_ids, space, neighbors=3):
    try:
        import numpy as np
    except ImportError as exc:
        raise ValueError("Semantic links require requirements-education.txt") from exc

    if not isinstance(source_ids, list) or not source_ids or not isinstance(space, str) or not space:
        raise ValueError("Provide source IDs and their explicit embedding space")
    if isinstance(neighbors, bool) or not isinstance(neighbors, int) or not 1 <= neighbors <= 10:
        raise ValueError("Choose between 1 and 10 semantic neighbors")
    checked, nodes, vectors, parents = {}, [], [], []
    for source_id in source_ids:
        for region in regions(graph, source_id):
            checked_index_source(graph, region, checked)
            coverage = graph.db.execute("SELECT status,fingerprint FROM embedding_coverage WHERE parent_id=? AND space=?",
                                        (region["id"], space)).fetchone()
            if coverage and coverage[0] == "complete" and coverage[1] == region["fingerprint"]:
                for unit_id in json.loads(graph.db.execute('SELECT unit_ids FROM embedding_coverage WHERE parent_id=? AND space=?', (region['id'], space)).fetchone()[0]):
                    unit = graph.get(unit_id)
                    vector = graph.db.execute('SELECT fingerprint,vector FROM vectors WHERE node_id=? AND space=?', (unit_id, space)).fetchone()
                    if vector and vector['fingerprint'] == unit['fingerprint']:
                        nodes.append(unit)
                        parents.append(region)
                        vectors.append(json.loads(vector['vector']))
    if not nodes:
        return {"indexed_regions": 0, "links": 0, "basis": "embedding similarity; no extracted claims"}
    matrix = np.asarray(vectors, dtype=np.float64)
    if matrix.ndim != 2 or not np.isfinite(matrix).all():
        raise ValueError("Invalid graph vectors")
    norms = np.linalg.norm(matrix, axis=1)
    if np.any(norms == 0):
        raise ValueError("Invalid zero graph vector")
    matrix /= norms[:, None]
    links, proposals = 0, {}
    with graph.db:
        placeholders = ",".join("?" for _ in source_ids)
        graph.db.execute("DELETE FROM edges WHERE relation='semantic-neighbor' AND json_extract(evidence,'$.space')=? "
                         "AND (src IN (SELECT id FROM nodes WHERE json_extract(payload,'$.source_id') IN (" + placeholders + ")) "
                         "OR dst IN (SELECT id FROM nodes WHERE json_extract(payload,'$.source_id') IN (" + placeholders + ")))",
                         [space, *source_ids, *source_ids])
        for start in range(0, len(nodes), 128):
            scores = matrix[start:start + 128] @ matrix.T
            for local, values in enumerate(scores):
                index = start + local
                values[index] = -math.inf
                for target in range(len(nodes)):
                    if parents[target]["id"] == parents[index]["id"]:
                        values[target] = -math.inf
                candidates = sorted(range(len(nodes)), key=lambda target: (-float(values[target]), nodes[target]["id"]))
                selected = set()
                for target in candidates:
                    if values[target] <= 0 or len(selected) >= neighbors:
                        break
                    left, right = parents[index], parents[target]
                    if right['id'] in selected:
                        continue
                    selected.add(right['id'])
                    pair = (left['id'], right['id'])
                    score = float(values[target])
                    if pair not in proposals or score > proposals[pair][0]:
                        proposals[pair] = (score, left, right, nodes[index], nodes[target])
        counts = {}
        for (left_id, right_id), (score, left, right, left_unit, right_unit) in sorted(proposals.items(), key=lambda item: (-item[1][0], item[0])):
            if counts.get(left_id, 0) >= neighbors:
                continue
            counts[left_id] = counts.get(left_id, 0) + 1
            graph.edge(left_id, 'semantic-neighbor', right_id, 'candidate', {
                'space': space, 'cosine_similarity': score,
                'source_versions': [left['payload']['source_version'], right['payload']['source_version']],
                'node_fingerprints': [left['fingerprint'], right['fingerprint']],
                'matched_units': [left_unit['id'], right_unit['id']],
                'unit_fingerprints': [left_unit['fingerprint'], right_unit['fingerprint']],
                'basis': 'Strongest exact-unit embedding similarity; inspect original context; no factual claims'})
            links += 1
    return {"indexed_regions": len({parent["id"] for parent in parents}), "links": links, "basis": "embedding similarity; no extracted claims"}

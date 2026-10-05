import json
import math

from core.sources import checked_region, checked_source, regions, source_validation


@source_validation
def review(graph, source_ids=None, validate=True):
    source_ids = source_ids if source_ids is not None else [row[0] for row in graph.db.execute("SELECT id FROM nodes WHERE kind='source'")]
    has_coverage = graph.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='embedding_coverage'").fetchone()
    sources = []
    for sid in source_ids:
        source = graph.get(sid)
        if source['kind'] != 'source':
            raise ValueError('Review only registered sources belonging to this agent')
        gaps = list(source['payload'].get('visual_gaps', []))
        if validate:
            try:
                checked_source(graph, sid)
            except (ValueError, OSError, KeyError) as error:
                gaps.append({'kind': 'source-integrity', 'reason': str(error)})
        rows = regions(graph, sid)
        complete, images, complete_images, figures = 0, 0, 0, 0
        for region in rows:
            payload = region['payload']
            image = bool(payload.get('image_path'))
            images += image
            figures += payload.get('figure_number') is not None
            valid = True
            if validate:
                try:
                    checked_region(graph, region['id'])
                except (ValueError, OSError, KeyError) as error:
                    valid = False
                    gaps.append({'kind': 'region-integrity', 'region_id': region['id'], 'reason': str(error)})
            coverage = list(graph.db.execute('SELECT * FROM embedding_coverage WHERE parent_id=?', (region['id'],))) if has_coverage else []
            covered = False
            for item in coverage:
                if item['status'] != 'complete' or item['fingerprint'] != region['fingerprint']:
                    continue
                units = json.loads(item['unit_ids'])
                units_valid, pieces = bool(units), []
                for unit_id in units:
                    try:
                        unit = graph.get(unit_id)
                    except ValueError:
                        units_valid = False
                        break
                    vector = graph.db.execute('SELECT fingerprint,vector FROM vectors WHERE node_id=? AND space=?', (unit_id, item['space'])).fetchone()
                    values = json.loads(vector['vector']) if vector else []
                    if not vector or vector['fingerprint'] != unit['fingerprint'] or not values or (item['space'].startswith('google:gemini-embedding-2:') and len(values) != int(item['space'].split(':')[2])) or any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in values) or not sum(value * value for value in values):
                        units_valid = False
                    pieces.append(unit['payload'].get('text', ''))
                if units_valid and ''.join(pieces) == payload['text']:
                    covered = True
            complete += valid and covered
            complete_images += valid and covered and image
        expected_figures = source['payload'].get('expected_figures', len(source['payload'].get('retained_visuals', [])))
        if expected_figures > figures:
            gaps.append({'kind': 'visual-registration', 'reason': 'Some expected figures have no registered searchable region', 'missing': expected_figures - figures})
        if len(rows) > complete:
            gaps.append({'kind': 'embedding-coverage', 'reason': 'Some source regions do not have complete valid embeddings', 'missing': len(rows) - complete})
        if images > complete_images:
            gaps.append({'kind': 'visual-embedding', 'reason': 'Some retained images do not have complete valid embeddings', 'missing': images - complete_images})
        trees = graph.db.execute("SELECT COUNT(*) FROM nodes WHERE kind='document-tree' AND json_extract(payload,'$.source_id')=?", (sid,)).fetchone()[0]
        sources.append({'source_id': sid, 'title': source['label'], 'regions': len(rows), 'complete_regions': complete,
                        'image_regions': images, 'complete_image_regions': complete_images, 'expected_figures': expected_figures,
                        'registered_figures': figures, 'native_navigation_trees': trees, 'gaps': gaps})
    return {'sources': sources, 'source_integrity_checked': validate,
            'retrieval_quality_verified': False,
            'basis': 'Source and representation health; answer accuracy requires anchored retrieval evaluation',
            'graph_basis': 'Candidate similarity navigation, not extracted reasoning or factual claims'}

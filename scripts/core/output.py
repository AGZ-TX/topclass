import json


def size(value):
    return len(json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8'))


def bounded(value, budget=24000):
    if isinstance(budget, bool) or not isinstance(budget, int) or not 2000 <= budget <= 200000:
        raise ValueError('Output budget must be between 2000 and 200000 UTF-8 bytes')
    value['output'] = {'byte_budget': budget, 'serialized_bytes': 0, 'trimmed': False,
                       'basis': 'Complete indented JSON bytes; model token counts depend on the host tokenizer'}
    for _ in range(4):
        value['output']['serialized_bytes'] = size(value)
    if size(value) <= budget:
        return value
    value['output']['trimmed'] = True
    if 'navigation' in value:
        value['navigation'] = [{'source_id': item['source_id'], 'expand': 'Use --source for navigation'} for item in value['navigation']]
    for row in value.get('results', []):
        for key in ('original_path', 'origin', 'image_hash', 'rights', 'processing', 'retrieved_id', 'source_hash', 'page_width', 'page_height'):
            row.pop(key, None)
        row['related_regions'] = [{'region_id': item['region_id'], 'source_id': item['source_id']} for item in row.get('related_regions', [])]
    gaps = value.get('gaps', [])
    if len(gaps) > 5:
        value['omitted_gaps'] = len(gaps) - 5
        value['gaps'] = gaps[:5]
    while size(value) > budget:
        rows = value.get('results', [])
        longest = max(rows, key=lambda row: len(row.get('text', '')), default=None)
        if longest and len(longest.get('text', '')) > 200:
            text = longest['text']
            longest['text'] = text[:max(200, len(text) // 2)]
            excerpt = longest.get('excerpt')
            if excerpt:
                excerpt['offset_end'] = excerpt['offset_start'] + len(longest['text'])
                excerpt['truncated'] = True
                if 'section' in value:
                    value['continue_region_id'] = longest['region_id']
                    value['continue_offset'] = excerpt['offset_end']
            continue
        if rows:
            removed = rows.pop()
            value['omitted_results'] = value.get('omitted_results', 0) + 1
            value['continue_region_id'] = removed['region_id']
            if 'section' in value:
                value['continue_offset'] = removed.get('excerpt', {}).get('offset_start', 0)
            continue
        value = {key: value[key] for key in ('agent_id', 'source_id', 'continue_region_id', 'omitted_results', 'semantic_search', 'output') if key in value}
        value['message'] = 'Evidence exceeds the output budget. Narrow the query or open the continuation region.'
        break
    if 'section' in value:
        unfinished = next((row for row in value.get('results', []) if row.get('excerpt', {}).get('offset_end', 0) < row.get('excerpt', {}).get('total_characters', 0)), None)
        if unfinished:
            value['continue_region_id'] = unfinished['region_id']
            value['continue_offset'] = unfinished['excerpt']['offset_end']
    value['returned_text_characters'] = sum(len(row.get('text', '')) for row in value.get('results', []))
    for _ in range(4):
        value['output']['serialized_bytes'] = size(value)
    if size(value) > budget:
        raise ValueError('Output cannot fit its byte budget')
    return value

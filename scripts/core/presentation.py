from __future__ import annotations

import json
from pathlib import Path
import re


TEMPLATE = Path(__file__).resolve().parents[2] / 'docs/templates/asterium.html'


def check_presentation(html, expected):
    normalized = html
    values = {}
    for marker, placeholder in [('const DATA=', '__PLANNER_DATA__'), ('const PROFILE=', '__AGENT_PROFILE__')]:
        if normalized.count(marker) != 1:
            raise ValueError('The approved template needs exactly one ' + marker + ' JSON payload')
        start = normalized.index(marker) + len(marker)
        raw = normalized[start:]
        value, length = json.JSONDecoder().raw_decode(raw)
        if any(char in raw[:length] for char in '<>&\u2028\u2029'):
            raise ValueError('Escape HTML delimiters and Unicode line separators in embedded JSON')
        values[marker] = value
        normalized = normalized[:start] + placeholder + raw[length:]
    if values['const DATA='] != expected:
        raise ValueError('HTML course evidence or choices differ from the validated AI proposal')
    if normalized != TEMPLATE.read_text(encoding='utf-8'):
        raise ValueError('HTML changed outside the allowed data and agent profile fields; restore the approved template')
    profile = values['const PROFILE=']
    if not isinstance(profile, dict) or set(profile) != {'title', 'description', 'shortfall'}:
        raise ValueError('Agent profile allows only title, description and shortfall')
    if any(not isinstance(value, str) for value in profile.values()):
        raise ValueError('Agent profile values must be text')
    title, description, shortfall = (profile[key].strip() for key in ('title', 'description', 'shortfall'))
    if not 2 <= len(title.split()) <= 3 or len(title) > 80:
        raise ValueError('Role title must contain 2–3 words and at most 80 characters')
    if not description or len(description) > 300 or len(re.findall(r'[.!?]+(?:\s|$)', description)) > 2:
        raise ValueError('Role description must contain 1–2 short sentences of at most 300 characters')
    if len(shortfall) > 300:
        raise ValueError('Explain a recommendation shortfall in at most 300 characters')
    count = len(expected['recommendation_courses'])
    if count == 10 and shortfall:
        raise ValueError('Leave the catalog shortfall empty when recommending 10 courses')
    if count != 10 and not (0 < count < 10 and shortfall):
        raise ValueError('Recommend 10 courses, or explain why fewer relevant book-backed courses are available')
    return {'template_matches': True, 'recommended_courses': count, 'profile_valid': True}

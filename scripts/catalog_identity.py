from __future__ import annotations

import hashlib
import re


INSTITUTIONS = {
    "berkeley": ("University of California, Berkeley", ("Berkeley", "UC Berkeley", "University of California Berkeley")),
    "brown": ("Brown University", ("Brown",)),
    "carnegie-mellon": ("Carnegie Mellon University", ("Carnegie Mellon", "CMU")),
    "columbia": ("Columbia University", ("Columbia",)),
    "cornell": ("Cornell University", ("Cornell",)),
    "dartmouth": ("Dartmouth College", ("Dartmouth",)),
    "harvard": ("Harvard University", ("Harvard",)),
    "mit": ("Massachusetts Institute of Technology", ("MIT",)),
    "penn": ("University of Pennsylvania", ("Penn", "UPenn")),
    "princeton": ("Princeton University", ("Princeton",)),
    "stanford": ("Stanford University", ("Stanford",)),
    "yale": ("Yale University", ("Yale",)),
}


def normalized_name(value):
    return re.sub(r"[^\w]+", " ", str(value).casefold()).strip()


ALIASES = {normalized_name(alias): (key, canonical)
           for key, (canonical, aliases) in INSTITUTIONS.items()
           for alias in (key, canonical, *aliases)}


def resolve_institution(name):
    source_name = str(name or "unresolved").strip() or "unresolved"
    normalized = normalized_name(source_name)
    if normalized in {"weill cornell medicine", "weill cornell medical college"}:
        return {"institution_id": "cornell", "institution_name": "Cornell University",
                "school_id": "cornell:weill-medicine", "school_name": "Weill Cornell Medicine",
                "source_name": source_name, "in_scope": True}
    if normalized in ALIASES:
        key, canonical = ALIASES[normalized]
        return {"institution_id": key, "institution_name": canonical,
                "school_id": None, "school_name": None,
                "source_name": source_name, "in_scope": True}
    key = "external:" + hashlib.sha256(normalized.encode()).hexdigest()[:24]
    return {"institution_id": key, "institution_name": source_name,
            "school_id": None, "school_name": None,
            "source_name": source_name, "in_scope": False}

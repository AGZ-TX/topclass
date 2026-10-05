CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT NOT NULL,
    payload TEXT NOT NULL CHECK(json_valid(payload)), fingerprint TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS node_kind ON nodes(kind);
CREATE TABLE IF NOT EXISTS edges (
    id TEXT PRIMARY KEY,
    src TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    relation TEXT NOT NULL,
    dst TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK(status IN ('source-stated','candidate','inferred','reviewed')),
    evidence TEXT NOT NULL CHECK(json_valid(evidence))
);
CREATE INDEX IF NOT EXISTS edge_src ON edges(src,relation);
CREATE INDEX IF NOT EXISTS edge_dst ON edges(dst,relation);
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(id UNINDEXED,text);
CREATE TABLE IF NOT EXISTS search_lookup (
    node_id TEXT PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
    search_rowid INTEGER NOT NULL UNIQUE
);
CREATE TRIGGER IF NOT EXISTS node_search_cleanup BEFORE DELETE ON nodes BEGIN
    DELETE FROM search WHERE rowid=(SELECT search_rowid FROM search_lookup WHERE node_id=OLD.id);
END;
CREATE TABLE IF NOT EXISTS vectors (
    node_id TEXT REFERENCES nodes(id) ON DELETE CASCADE,
    space TEXT NOT NULL, fingerprint TEXT NOT NULL, vector TEXT NOT NULL,
    PRIMARY KEY(node_id,space)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS institutions (
    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
    in_scope INTEGER NOT NULL CHECK(in_scope IN (0,1)),
    node_id TEXT NOT NULL UNIQUE REFERENCES nodes(id)
);
CREATE TABLE IF NOT EXISTS institution_aliases (
    alias TEXT PRIMARY KEY,
    institution_id TEXT NOT NULL REFERENCES institutions(id)
);
CREATE TABLE IF NOT EXISTS schools (
    id TEXT PRIMARY KEY,
    institution_id TEXT NOT NULL REFERENCES institutions(id),
    name TEXT NOT NULL,
    node_id TEXT NOT NULL UNIQUE REFERENCES nodes(id),
    UNIQUE(institution_id,name), UNIQUE(id,institution_id)
);
CREATE INDEX IF NOT EXISTS school_institution ON schools(institution_id);
CREATE TABLE IF NOT EXISTS catalog_courses (
    course_key TEXT PRIMARY KEY,
    node_id TEXT NOT NULL UNIQUE REFERENCES nodes(id) ON DELETE CASCADE,
    institution_id TEXT NOT NULL REFERENCES institutions(id),
    school_id TEXT,
    source_institution TEXT NOT NULL,
    code TEXT, title TEXT NOT NULL,
    payload TEXT NOT NULL CHECK(json_valid(payload) AND json_type(payload)='object'),
    classification_status TEXT NOT NULL CHECK(classification_status IN ('candidate','needs-review','reviewed','unmapped')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    FOREIGN KEY(school_id,institution_id) REFERENCES schools(id,institution_id)
);
CREATE INDEX IF NOT EXISTS course_institution ON catalog_courses(institution_id,school_id);
CREATE INDEX IF NOT EXISTS course_code ON catalog_courses(institution_id,code);
CREATE TABLE IF NOT EXISTS catalog_reviews (
    fingerprint TEXT PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES nodes(id),
    source_fingerprint TEXT NOT NULL,
    classification_fingerprint TEXT NOT NULL,
    payload TEXT NOT NULL CHECK(json_valid(payload)),
    status TEXT NOT NULL CHECK(status IN ('current','stale'))
);
CREATE INDEX IF NOT EXISTS review_course ON catalog_reviews(node_id,status);
CREATE TABLE IF NOT EXISTS catalog_imports (
    id TEXT PRIMARY KEY, scope TEXT NOT NULL, fingerprint TEXT NOT NULL,
    course_count INTEGER NOT NULL CHECK(course_count>=0),
    materials_count INTEGER NOT NULL CHECK(materials_count>=0),
    imported_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(scope,fingerprint)
);
CREATE TABLE IF NOT EXISTS catalog_scopes (
    scope TEXT PRIMARY KEY,
    import_id TEXT NOT NULL REFERENCES catalog_imports(id)
);
CREATE TABLE IF NOT EXISTS catalog_nodes (
    scope TEXT NOT NULL REFERENCES catalog_scopes(scope) ON DELETE CASCADE,
    node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    PRIMARY KEY(scope,node_id)
);
CREATE INDEX IF NOT EXISTS catalog_node_scope ON catalog_nodes(node_id,scope);
CREATE TABLE IF NOT EXISTS catalog_course_sources (
    scope TEXT NOT NULL REFERENCES catalog_scopes(scope) ON DELETE CASCADE,
    node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    payload TEXT NOT NULL CHECK(json_valid(payload) AND json_type(payload)='object'),
    PRIMARY KEY(scope,node_id)
);
CREATE INDEX IF NOT EXISTS course_source_node ON catalog_course_sources(node_id,scope);
CREATE TABLE IF NOT EXISTS catalog_edges (
    scope TEXT NOT NULL REFERENCES catalog_scopes(scope) ON DELETE CASCADE,
    edge_id TEXT NOT NULL REFERENCES edges(id) ON DELETE CASCADE,
    PRIMARY KEY(scope,edge_id)
);
CREATE INDEX IF NOT EXISTS catalog_edge_scope ON catalog_edges(edge_id,scope);
CREATE TRIGGER IF NOT EXISTS validate_node_insert BEFORE INSERT ON nodes BEGIN
    SELECT CASE WHEN NEW.id IS NULL OR NEW.id='' OR NEW.kind IS NULL OR NEW.kind='' OR NEW.label IS NULL
        OR NEW.fingerprint IS NULL OR NOT json_valid(NEW.payload)
        THEN RAISE(ABORT,'Invalid graph node') END;
END;
CREATE TRIGGER IF NOT EXISTS validate_node_update BEFORE UPDATE ON nodes BEGIN
    SELECT CASE WHEN NEW.id IS NULL OR NEW.id='' OR NEW.kind IS NULL OR NEW.kind='' OR NEW.label IS NULL
        OR NEW.fingerprint IS NULL OR NOT json_valid(NEW.payload)
        THEN RAISE(ABORT,'Invalid graph node') END;
END;
CREATE TRIGGER IF NOT EXISTS legacy_node_cleanup BEFORE DELETE ON nodes BEGIN
    DELETE FROM edges WHERE src=OLD.id OR dst=OLD.id;
    DELETE FROM vectors WHERE node_id=OLD.id;
END;
CREATE TRIGGER IF NOT EXISTS validate_edge_insert BEFORE INSERT ON edges BEGIN
    SELECT CASE WHEN NEW.src IS NULL OR NEW.dst IS NULL OR NEW.relation IS NULL OR NEW.relation=''
        OR NEW.status IS NULL OR NEW.status NOT IN ('source-stated','candidate','inferred','reviewed')
        OR NOT json_valid(NEW.evidence)
        OR NOT EXISTS(SELECT 1 FROM nodes WHERE id=NEW.src) OR NOT EXISTS(SELECT 1 FROM nodes WHERE id=NEW.dst)
        THEN RAISE(ABORT,'Invalid graph edge') END;
END;
CREATE TRIGGER IF NOT EXISTS validate_edge_update BEFORE UPDATE ON edges BEGIN
    SELECT CASE WHEN NEW.src IS NULL OR NEW.dst IS NULL OR NEW.relation IS NULL OR NEW.relation=''
        OR NEW.status IS NULL OR NEW.status NOT IN ('source-stated','candidate','inferred','reviewed')
        OR NOT json_valid(NEW.evidence)
        OR NOT EXISTS(SELECT 1 FROM nodes WHERE id=NEW.src) OR NOT EXISTS(SELECT 1 FROM nodes WHERE id=NEW.dst)
        THEN RAISE(ABORT,'Invalid graph edge') END;
END;

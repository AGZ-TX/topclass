#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from importlib import metadata
from pathlib import Path

from core.graph import Graph, identity
from education_store import transaction
from core.sources import regions


SDK_VERSION = "0.2.10"
FORMAT_VERSION = "topclass-pageindex-tree-v1"
NATIVE_FORMAT_VERSION = "topclass-native-page-tree-v1"
ROOT = Path(__file__).resolve().parents[2]


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def checked_source(graph, source_id):
    source = graph.get(source_id)
    payload = source["payload"]
    if source["kind"] != "source" or payload.get("kind") != ".pdf":
        raise ValueError("PageIndex navigation requires a registered PDF source")
    original = Path(payload.get("original_path", ""))
    if not original.is_file() or original.suffix.casefold() != ".pdf":
        raise ValueError("Retained original PDF is unavailable")
    content_hash = payload.get("content_hash")
    if not isinstance(content_hash, str) or file_hash(original) != content_hash:
        raise ValueError("Retained original PDF hash changed; the source is stale")
    pages = {}
    for region in regions(graph, source_id):
        record = region["payload"]
        page = record.get("physical_page")
        if region["kind"] != "region" or record.get("source_id") != source_id or record.get("source_version") != content_hash:
            raise ValueError("PDF region source version is stale or invalid")
        if isinstance(page, bool) or not isinstance(page, int) or page < 1 or page in pages:
            raise ValueError("PDF regions need unique physical page numbers")
        pages[page] = region
    if not pages or set(pages) != set(range(1, len(pages) + 1)):
        raise ValueError("PDF physical page anchors are incomplete")
    return source, pages


def required_string(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(name + " must be a nonempty string")
    return value.strip()


def checked_model(value):
    model = required_string(value, "index_model")
    if "cloud" in model.casefold().split("/"):
        raise ValueError("Use an explicit non-cloud index model; PageIndex Cloud is not supported")
    return model


def validated_nodes(nodes, page_count, seen=None, depth=0):
    if not isinstance(nodes, list) or depth > 100:
        raise ValueError("Tree nodes must be a nested list within the supported depth")
    seen = set() if seen is None else seen
    result = []
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError("Each tree node must be an object")
        node_id = required_string(node.get("node_id"), "node_id")
        if node_id in seen or len(seen) >= 100000:
            raise ValueError("Tree node IDs must be unique within a bounded document tree")
        seen.add(node_id)
        title = required_string(node.get("title"), "title")
        page = node.get("page_index")
        if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= page_count:
            raise ValueError("page_index must be an integer physical page within the source")
        clean = {"node_id": node_id, "title": title, "page_index": page, "end_unknown": True}
        for name in ("summary", "prefix_summary"):
            if name in node:
                if not isinstance(node[name], str):
                    raise ValueError(name + " must be a generated text string")
                clean[name] = node[name]
        clean["nodes"] = validated_nodes(node.get("nodes", []), page_count, seen, depth + 1)
        result.append(clean)
    return result


def import_tree(graph, source_id, envelope):
    source, pages = checked_source(graph, source_id)
    if not isinstance(envelope, dict) or envelope.get("format") != FORMAT_VERSION:
        raise ValueError("Expected a Topclass PageIndex tree artifact")
    if envelope.get("source_id") != source_id or envelope.get("content_hash") != source["payload"]["content_hash"]:
        raise ValueError("Tree artifact source identity or content hash is stale")
    if envelope.get("sdk_version") != SDK_VERSION:
        raise ValueError("Tree artifact requires PageIndex SDK " + SDK_VERSION)
    model = checked_model(envelope.get("index_model"))
    doc_id = required_string(envelope.get("doc_id"), "doc_id")
    result = envelope.get("result")
    if not isinstance(result, dict) or result.get("status") != "completed" or result.get("doc_id") != doc_id:
        raise ValueError("Tree artifact must contain a completed, matching PageIndex document")
    nodes = validated_nodes(result.get("result"), len(pages))
    if not nodes:
        raise ValueError("Completed document tree must not be empty")
    payload = {"format": FORMAT_VERSION, "source_id": source_id,
               "content_hash": source["payload"]["content_hash"], "sdk_version": SDK_VERSION,
               "index_model": model, "doc_id": doc_id, "nodes": nodes,
               "state": "candidate-document-navigation", "summary_state": "generated-unreviewed",
               "anchor_basis": "physical start page only", "end_unknown": True,
               "untrusted_evidence": True, "understanding_complete": False}
    tree_id = identity("document-tree", payload)
    with transaction(graph.db):
        graph.node(tree_id, "document-tree", source["label"] + ": PageIndex navigation", payload)
        graph.edge(tree_id, "document-tree-for", source_id, "candidate", {
            "source_version": payload["content_hash"], "summary_state": "generated-unreviewed"})
    return tree_id


def tree_source(graph, source_id):
    from core.sources import checked_source as original_source

    source = original_source(graph, source_id)
    if source['payload'].get('kind') == '.pdf':
        return checked_source(graph, source_id)
    rows = regions(graph, source_id)
    if not rows:
        raise ValueError('Source has no navigation regions')
    return source, {row['payload']['region_index'] + 1: row for row in rows}


def document_tree(graph, tree_id):
    tree = graph.get(tree_id)
    payload = tree["payload"]
    if tree["kind"] != "document-tree" or payload.get("format") not in {FORMAT_VERSION, NATIVE_FORMAT_VERSION}:
        raise ValueError("Expected a PageIndex document-tree node")
    source, pages = tree_source(graph, payload["source_id"])
    if source["payload"]["content_hash"] != payload["content_hash"]:
        raise ValueError("Document tree content hash is stale")
    validated_nodes(payload["nodes"], len(pages))
    return payload


def native_outline(graph, source_id):
    import fitz

    source, pages = tree_source(graph, source_id)
    physical = source['payload'].get('kind') == '.pdf'
    if physical:
        with fitz.open(source['payload']['original_path']) as document:
            outline = document.get_toc()
    else:
        outline = []
        for heading in source['payload'].get('native_headings', []):
            region = next((row for row in pages.values() if row['payload'].get('offset_start', -1) <= heading['offset'] < row['payload'].get('offset_end', -1)), None)
            if region and heading['title']:
                outline.append((heading['level'], heading['title'], region['payload']['region_index'] + 1))
    roots, stack, rejected = [], [], []
    for position, (level, title, page) in enumerate(outline):
        if not isinstance(level, int) or level < 1 or not isinstance(title, str) or not title.strip() or page not in pages:
            rejected.append(position)
            continue
        node = {"node_id": "outline-" + str(position), "title": title.strip(), "page_index": page,
                "end_unknown": True, "nodes": []}
        while stack and stack[-1][0] >= level:
            stack.pop()
        (stack[-1][1]["nodes"] if stack else roots).append(node)
        stack.append((level, node))
    basis = "embedded PDF outline" if physical else "original HTML headings"
    if not roots:
        basis = "physical pages; no usable embedded outline" if physical else "original source regions; no native headings"
        roots = [{"node_id": "page-" + str(page), "title": ("Physical page " if physical else "Source region ") + str(page),
                  "page_index": page, "end_unknown": True, "nodes": []} for page in sorted(pages)]
    payload = {"format": NATIVE_FORMAT_VERSION, "source_id": source_id,
               "content_hash": source["payload"]["content_hash"], "nodes": validated_nodes(roots, len(pages)),
               "state": "candidate-document-navigation", "summary_state": "not-generated",
               "artifact_origin": basis, "sdk_used": False, "model_used": False,
               "anchor_basis": "physical start page" if physical else "source region ordinal", "physical_pages": physical, "end_unknown": True,
               "rejected_outline_entries": rejected, "untrusted_evidence": True, "understanding_complete": False}
    tree_id = identity("document-tree", payload)
    with transaction(graph.db):
        graph.node(tree_id, "document-tree", source["label"] + ": original page index", payload)
        graph.edge(tree_id, "document-tree-for", source_id, "source-stated", {
            "source_version": payload["content_hash"], "basis": basis})

        def connect(nodes, parent):
            for node in nodes:
                section_id = identity("source-section", [tree_id, node["node_id"]])
                graph.node(section_id, "source-section", node["title"], {
                    "source_id": source_id, "source_version": payload["content_hash"],
                    "tree_id": tree_id, "node_id": node["node_id"], "physical_page" if physical else "region_ordinal": node["page_index"],
                    "basis": basis, "end_unknown": True})
                graph.edge(section_id, "section-of", parent, "source-stated", {"basis": basis})
                graph.edge(section_id, "starts-at", pages[node["page_index"]]["id"], "source-stated", {"basis": basis})
                connect(node["nodes"], section_id)

        connect(payload["nodes"], tree_id)
        ordered = [pages[page] for page in sorted(pages)]
        for left, right in zip(ordered, ordered[1:]):
            graph.edge(left["id"], "next-page" if physical else "next-region", right["id"], "source-stated", {"source_version": payload["content_hash"]})
    return tree_id


def find_node(nodes, node_id):
    for node in nodes:
        if node["node_id"] == node_id:
            return node
        found = find_node(node["nodes"], node_id)
        if found:
            return found
    return None


def read_node(graph, tree_id, node_id):
    payload = document_tree(graph, tree_id)
    node = find_node(payload["nodes"], required_string(node_id, "node_id"))
    if not node:
        raise ValueError("Unknown document tree node: " + node_id)
    region = next(region for region in regions(graph, payload["source_id"])
                  if region["payload"].get("physical_page") == node["page_index"])
    return {"tree_id": tree_id, "node_id": node["node_id"], "source_id": payload["source_id"],
            "source_version": payload["content_hash"], "start_physical_page": node["page_index"],
            "end_unknown": True, "region": region, "untrusted_evidence": True,
            "reading_scope": "Original start page only; inspect its image and determine any further reading from the source",
            "processing_state_changed": False}


def load_local_client():
    try:
        installed = metadata.version("pageindex")
    except metadata.PackageNotFoundError as exc:
        raise ValueError("Install scripts/requirements-pageindex.txt to enable optional local indexing") from exc
    if installed != SDK_VERSION:
        raise ValueError("PageIndex SDK " + SDK_VERSION + " is required; install scripts/requirements-pageindex.txt")
    try:
        from pageindex import PageIndexLocalClient
    except ImportError as exc:
        raise ValueError("Install scripts/requirements-pageindex.txt and its optional indexing dependencies") from exc
    return PageIndexLocalClient


def private_path(path):
    path = Path(path).resolve()
    if path in {Path(path.anchor), Path.home().resolve(), ROOT}:
        raise ValueError("Choose a dedicated private file or storage directory")
    if path.is_relative_to(ROOT) and path.relative_to(ROOT).parts[0] not in {"derived_private", "private_sources"}:
        raise ValueError("Store source-derived artifacts in a private directory, not repository code, data or docs")
    return path


def index_source(graph, source_id, storage_path, index_model, output_path, allow_model_calls=False, client_factory=None):
    if allow_model_calls is not True:
        raise ValueError("Indexing can call a model provider; explicitly use --allow-model-calls")
    model = checked_model(index_model)
    source, pages = checked_source(graph, source_id)
    storage = private_path(storage_path)
    output = private_path(output_path)
    original = Path(source["payload"]["original_path"]).resolve()
    databases = {Path(row[2]).resolve() for row in graph.db.execute("PRAGMA database_list") if row[2]}
    if output == original or output in databases or output == storage:
        raise ValueError("Artifact output must not equal the source PDF, database, or index directory")
    if output.exists():
        raise FileExistsError("Artifact output already exists; choose a new private file")
    if storage.exists() and not storage.is_dir():
        raise ValueError("Index storage must be a private directory")
    factory = client_factory or load_local_client()
    output.parent.mkdir(parents=True, exist_ok=True)
    storage.mkdir(parents=True, exist_ok=True)
    artifact = {"format": FORMAT_VERSION, "source_id": source_id,
                "content_hash": source["payload"]["content_hash"], "sdk_version": SDK_VERSION,
                "index_model": model, "doc_id": None, "result": None}
    with output.open("x", encoding="utf-8") as stream:
        try:
            client = factory(storage_path=str(storage), index_model=model)
            submission = client.submit_document(str(original))
            artifact["doc_id"] = required_string(submission.get("doc_id"), "submitted doc_id")
            artifact["result"] = client.get_tree(artifact["doc_id"], node_summary=True, include_text=False)
        except Exception as exc:
            artifact["index_operation_error"] = type(exc).__name__
            json.dump(artifact, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
            raise ValueError("Local indexing failed; recovery artifact retained at " + str(output)) from None
        json.dump(artifact, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        tree_id = import_tree(graph, source_id, artifact)
    except ValueError as exc:
        raise ValueError("Index artifact retained at " + str(output) + "; tree import failed: " + str(exc)) from exc
    return {"tree_id": tree_id, "artifact_path": str(output), "summary_state": "generated-unreviewed"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Optional local PageIndex navigation. Generated summaries are not reviewed knowledge.")
    parser.add_argument("--db", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    index = sub.add_parser("index")
    index.add_argument("--source", required=True)
    index.add_argument("--storage", type=Path, required=True)
    index.add_argument("--index-model", required=True)
    index.add_argument("--allow-model-calls", action="store_true")
    index.add_argument("--out", type=Path, required=True)
    imported = sub.add_parser("import-tree")
    imported.add_argument("--source", required=True)
    imported.add_argument("--input", type=Path, required=True)
    native = sub.add_parser("native-outline")
    native.add_argument("--source", required=True)
    tree = sub.add_parser("tree")
    tree.add_argument("--id", required=True)
    read = sub.add_parser("read")
    read.add_argument("--id", required=True)
    read.add_argument("--node", required=True)
    args = parser.parse_args(argv)
    graph = Graph(args.db)
    try:
        if args.command == "index":
            result = index_source(graph, args.source, args.storage, args.index_model, args.out, args.allow_model_calls)
        elif args.command == "import-tree":
            result = {"tree_id": import_tree(graph, args.source, json.loads(args.input.read_text(encoding="utf-8")))}
        elif args.command == "native-outline":
            result = {"tree_id": native_outline(graph, args.source)}
        elif args.command == "tree":
            result = document_tree(graph, args.id)
        else:
            result = read_node(graph, args.id, args.node)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        graph.close()


if __name__ == "__main__":
    main()

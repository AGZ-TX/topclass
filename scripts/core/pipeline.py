#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import contextmanager
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from core.search import replace_json
from core.system import lock_file, worker_options
from bibliographic_findings import valid_isbn
from core.graph import identity
from core.google import GeminiEmbeddings
from core.pages import document_tree, file_hash, native_outline
from core.provider import ProviderDeferred, ProviderFailure, ProviderRuntime
from core.sources import register
from core.index import index_originals, semantic_links


VERSION = "topclass-original-index-v1"


def google_profile(profile):
    profile = dict(profile)
    for name in ('rpm', 'tpm', 'rpd', 'daily_token_cap', 'daily_cost_cap'):
        profile.pop(name, None)
    return profile | {'provider_managed': True, 'limits_basis': 'Google enforces quota; no Topclass request, token, rate or spending caps'}


def configure_google(home, key_file=None, profile=None, fallback=False):
    from core.app import child, locked

    key = Path(key_file).read_text().strip() if key_file else os.environ.get("GEMINI_API_KEY") or getpass.getpass(
        "Google backup API key: " if fallback else "Google API key: ").strip()
    if not key or any(character.isspace() for character in key):
        raise ValueError("Provide a Google API key as plain text")
    if fallback:
        from core.keys import add_fallback
        return add_fallback(home, key)
    binding = hashlib.sha256(key.encode()).hexdigest()
    default = {"provider": "google", "project_id": "user-google-key-" + binding[:16], "account_mode": "paid",
               "enabled": True, "provider_managed": True,
               "cost_rates": {"gemini-embedding-2": {"text_per_million": 0.20, "image_per_million": 0.45}},
               "pricing_date": "2026-10-02", "max_attempts": 12, "max_elapsed_seconds": 3600,
               "max_inline_wait_seconds": 2,
               "limits_basis": "Google enforces quota; usage estimates do not impose spending limits."}
    profile = dict(profile or default)
    with locked(home) as home:
        config_path = child(home, ".google.json")
        fallback_bindings = []
        if config_path.is_file():
            previous = json.loads(config_path.read_text())
            fallback_bindings = [token for token in previous.get('fallback_bindings', []) if token != binding]
            if previous["credential_binding"] == binding:
                profile = previous["profile"]
        profile = google_profile(profile)
        runtime = ProviderRuntime(child(home, ".google-providers.db"), profile)
        runtime.close()
        path = child(home, ".google-api-key")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            path.chmod(0o600)
            stream.write(key)
        replace_json(config_path, {"format": VERSION, "credential_binding": binding, "profile": profile,
                                  "fallback_bindings": fallback_bindings,
                                  "embedding_model": "gemini-embedding-2", "reader_model": None})
    return {"configured": True, "embedding_model": "gemini-embedding-2", "reader_model": None,
            "next": "Confirm your education list. Available books will be indexed privately for that agent."}


@contextmanager
def google_provider(home, graph, max_requests=None):
    from core.app import child, locked
    with locked(home) as home:
        config = json.loads(child(home, '.google.json').read_text())
        updated = google_profile(config['profile'])
        if updated != config['profile']:
            config['profile'] = updated
            replace_json(child(home, '.google.json'), config)
    key = child(home, ".google-api-key").read_text().strip()
    if hashlib.sha256(key.encode()).hexdigest() != config["credential_binding"]:
        raise ValueError("Google credential and configuration do not match")
    previous = os.environ.get("GEMINI_API_KEY")
    os.environ["GEMINI_API_KEY"] = key
    runtime = None
    try:
        from core.keys import fallback_keys, FallbackRuntime
        primary = ProviderRuntime(child(home, ".google-providers.db"), config["profile"], credential=key)
        runtime = primary
        backups = fallback_keys(home, config)
        if backups:
            runtime = FallbackRuntime([primary])
            for token, backup in backups:
                runtime.runtimes.append(ProviderRuntime(child(home, '.google-providers.db'), config['profile'],
                                                       credential=backup, credential_scope=token))
            for member in runtime.runtimes:
                member.quota_failover = True
        yield GeminiEmbeddings(graph, dimensions=3072, max_requests=max_requests,
                               daily_cap=None, runtime=runtime)
    finally:
        if runtime is not None:
            runtime.close()
        if previous is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = previous


def queue_material(home, agent_id, path, title, edition="Not stated", origin="", expected_hash=None, isbn=None):
    from core.app import agent_at, child, locked, memory

    path = Path(path).absolute()
    with locked(home) as home:
        folder, agent = agent_at(home, agent_id)
        graph = memory(folder, agent)
        graph.close()
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()):
            raise ValueError("Index only a retained file belonging to this agent")
        digest = file_hash(path)
        if expected_hash and digest != expected_hash:
            raise ValueError("Material changed after its file-check receipt")
        queue_path = child(folder, "index-queue.json")
        queue = json.loads(queue_path.read_text()) if queue_path.exists() else {"format": VERSION, "agent_id": agent_id, "jobs": {}}
        if queue.get("agent_id") != agent_id:
            raise ValueError("Index queue belongs to another agent")
        job_id = identity("original-index-job", [digest, title, edition, origin, VERSION])
        if job_id not in queue["jobs"]:
            unsupported = path.suffix.casefold() not in {".pdf", ".txt", ".md", ".html", ".htm"}
            excluded = bool(re.search(r"\b(?:solns|solutions\s+manual|instructor(?:'s)?\s+manual|answer\s+key)\b", title, re.I))
            isbn_only_title = bool(isbn) and all(valid_isbn(token) for token in re.split(r"[,;\s]+", title.strip()))
            queue["jobs"][job_id] = {"id": job_id, "path": str(path), "sha256": digest, "title": title,
                "edition": edition, "origin": origin, "isbn": isbn, "state": "unsupported" if unsupported else "excluded" if excluded or isbn_only_title else "pending",
                "reason": "This format needs an original-source adapter" if unsupported else "Reported title identifies supplemental answers, not the requested book" if excluded else "Search record has only ISBNs, not a useful book title; file identity needs verification" if isbn_only_title else "",
                "source_id": None, "next_attempt_at": 0, "reader_calls": 0}
        replace_json(queue_path, queue)
    return queue["jobs"][job_id]


def enqueue_search(home, agent_id, selection, outcome):
    from core.app import agent_at

    folder, agent = agent_at(home, agent_id)
    if not Path(selection).resolve().is_relative_to(folder.resolve()):
        raise ValueError("Search selection belongs to another agent")
    for record in outcome.get("files", []):
        if not Path(record["path"]).resolve().is_relative_to(Path(selection).resolve()):
            raise ValueError("Search file leaves the confirmed selection")
        queue_material(home, agent_id, record["path"], record.get("title") or "Retrieved book",
                       "Retrieved ISBN " + record["isbn"] + "; edition not independently verified",
                       record.get("url", ""), record["sha256"], record["isbn"])
    return launch_worker(home, agent_id) if outcome.get("files") else None


def pipeline_status(home, agent_id):
    from core.app import agent_at, child

    folder, _ = agent_at(home, agent_id)
    path = child(folder, "index-queue.json")
    if not path.is_file():
        return None
    queue = json.loads(path.read_text())
    if queue.get("agent_id") != agent_id:
        raise ValueError("Index queue belongs to another agent")
    counts = {}
    for job in queue["jobs"].values():
        counts[job["state"]] = counts.get(job["state"], 0) + 1
    return {"agent_id": agent_id, "states": counts, "queue": str(path), "reader_model": None,
            "embedding_model": "gemini-embedding-2", "google_configured": child(home, ".google.json").is_file(),
            "jobs": [{key: job.get(key) for key in ("id", "title", "state", "source_id", "reason", "next_attempt_at", "coverage")} for job in queue["jobs"].values()]}


def progress_message(progress):
    states = progress['states']
    if any(states.get(state) for state in ('pending', 'running', 'deferred')):
        if not progress['google_configured']:
            return 'Set up your Google key once. Saved books will be indexed automatically; /add is only for extra material.'
        return 'Available material is being indexed for this agent. Use /recall for completed passages; remaining book gaps stay listed.'
    if states.get('complete'):
        return 'Indexed original material is ready for /recall. Review any remaining book or indexing gaps.'
    return 'No material has completed indexing. Review the listed file, format, or provider issues.'


def launch_worker(home, agent_id):
    from core.app import agent_at, child

    folder, _ = agent_at(home, agent_id)
    if not child(home, ".google.json").is_file():
        return {"status": "setup-required", "next": "Set up your Google API key once to index available material."}
    queue = pipeline_status(home, agent_id)
    if not queue or not any(state in queue["states"] for state in ("pending", "deferred", "running")):
        return {"status": "not-needed"}
    log = child(folder, "index-worker.log")
    with log.open("a") as output:
        log.chmod(0o600)
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve().parents[1] / "source_pipeline.py"), "--home", str(Path(home).absolute()),
                                    "--agent", agent_id], stdin=subprocess.DEVNULL, stdout=output,
                                   stderr=subprocess.STDOUT, **worker_options())
    return {"status": "indexing", "pid": process.pid, "log": str(log)}


def run_worker(home, agent_id, loop=True, max_seconds=None):
    from core.app import agent_at, child, memory

    folder, agent = agent_at(home, agent_id)
    queue_path = child(folder, "index-queue.json")
    if not queue_path.is_file():
        return {"status": "not-needed", "next": "No material is queued for this agent."}
    if not child(home, ".google.json").is_file():
        return {"status": "setup-required", "next": "Set up your Google API key once to index the saved material."}
    with child(folder, ".index.lock").open("a") as lock:
        Path(lock.name).chmod(0o600)
        try:
            lock_file(lock, blocking=False)
        except BlockingIOError:
            return {"status": "busy"}
        started = time.time()
        while True:
            queue = json.loads(queue_path.read_text())
            if queue.get("agent_id") != agent_id:
                raise ValueError("Index queue belongs to another agent")
            eligible = [job for job in queue["jobs"].values() if job["state"] in {"pending", "running", "deferred"}]
            due = [job for job in eligible if job["next_attempt_at"] <= time.time() or job.get("reason", "").startswith("Configured ")]
            if not eligible or (max_seconds is not None and time.time() - started >= max_seconds):
                break
            for job in due:
                graph = memory(folder, agent)
                try:
                    job.update(state="running", reason="")
                    update_job(home, queue_path, job)
                    path = Path(job["path"])
                    if not path.resolve().is_relative_to(folder.resolve()) or path.is_symlink() or file_hash(path) != job["sha256"]:
                        raise ValueError("Queued material changed or leaves this agent")
                    sid = register(graph, path, child(folder, "sources"), job["title"], job["edition"], job["origin"])
                    job["source_id"] = sid
                    if path.suffix.casefold() in {".pdf", ".txt", ".md", ".html", ".htm"}:
                        tid = native_outline(graph, sid)
                        replace_json(child(child(folder, "pageindex"), sid.split(":")[1] + ".json"), {
                            "tree_id": tid, **document_tree(graph, tid)})
                    with google_provider(home, graph) as provider:
                        result = index_originals(graph, provider, [sid])
                        job["coverage"] = {key: result[key] for key in ("complete_nodes", "incomplete_nodes", "eligible_nodes", "space", "visual_coverage")}
                        job["usage"] = result["usage"]
                        if result["indexed"] or not result["incomplete_nodes"]:
                            scope = [row[0] for row in graph.db.execute("SELECT id FROM nodes WHERE kind='source'")]
                            job["semantic_graph"] = semantic_links(graph, scope, provider.space)
                        if result["incomplete_nodes"]:
                            deferred = result.get("deferred") or {}
                            job.update(state="deferred", next_attempt_at=deferred.get("next_attempt_at", time.time()),
                                       reason=deferred.get("reason", "Completed units saved; continuing remaining material"))
                        else:
                            job.update(state="complete", next_attempt_at=0, reason="Retained original material indexed; no reading model used" + ("; some source visuals are unavailable" if result["visual_coverage"]["gaps"] else ""))
                except ProviderDeferred as exc:
                    job.update(state="deferred", next_attempt_at=exc.next_attempt_at, reason=exc.reason)
                except (ValueError, OSError, KeyError, RuntimeError, ProviderFailure) as exc:
                    job.update(state="failed", reason=str(exc))
                finally:
                    graph.close()
                    queue = update_job(home, queue_path, job)
            if not loop:
                break
            if not due:
                time.sleep(min(5, max(0.1, min(job["next_attempt_at"] for job in eligible) - time.time())))
        return pipeline_status(home, agent_id)


def update_job(home, queue_path, job):
    from core.app import locked

    with locked(home):
        queue = json.loads(queue_path.read_text())
        if job["id"] not in queue["jobs"]:
            raise ValueError("Index job disappeared from this agent")
        queue["jobs"][job["id"]] = job
        replace_json(queue_path, queue)
    return queue


def main():
    parser = argparse.ArgumentParser(description="Resume private source indexing without a reading model.")
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--agent", required=True)
    args = parser.parse_args()
    print(json.dumps(run_worker(args.home, args.agent), indent=2))


if __name__ == "__main__":
    main()

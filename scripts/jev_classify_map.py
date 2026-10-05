import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
from pathlib import Path
import time

try:
    from .jev_course_classification import BATCH_VERSION, batch_record, batch_request, course_state, identity_warning, state_fingerprint
except ImportError:
    from jev_course_classification import BATCH_VERSION, batch_record, batch_request, course_state, identity_warning, state_fingerprint


class LifetimeBudgetReached(ValueError):
    pass


def budgeted_runtime(path, profile, **options):
    from processing_jobs import connect
    from provider_runtime import ProviderRuntime

    class LifetimeRuntime(ProviderRuntime):
        def _reserve(self, call_id, provider, model, operation, tokens, job_id, output_tokens=0):
            attempt_id, cached = super()._reserve(call_id,provider,model,operation,tokens,job_id,output_tokens)
            if cached is not None:
                return attempt_id,cached
            with connect(self.path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                spent = connection.execute("SELECT COALESCE(SUM(MAX(COALESCE(estimated_cost,0),COALESCE(usage_cost_estimate,0))),0) FROM provider_attempts WHERE scope=?", (self.scope,)).fetchone()[0]
                if spent > self.profile["daily_cost_cap"]:
                    connection.execute("UPDATE provider_attempts SET status='budget-rejected',estimated_input_tokens=0,estimated_cost=0 WHERE id=?", (attempt_id,))
                    connection.execute("UPDATE provider_calls SET state='deferred',next_attempt_at=?,reason='Lifetime course-classification budget reached' WHERE id=?", (time.time()+86400,call_id))
                    connection.commit()
                    raise LifetimeBudgetReached("Lifetime course-classification budget reached")
                connection.commit()
            return attempt_id,cached

    return LifetimeRuntime(path,profile,**options)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def batches(courses, taxonomy, size=20, request_builder=batch_request):
    batch = []
    for course in courses:
        candidate = batch + [course]
        request = request_builder(candidate, taxonomy)
        state_bytes = len(canonical(request["state"]).encode())
        longest = max(len(canonical(question).encode()) for question in request["questions"].values())
        total = len(canonical(request).encode())
        if batch and (len(candidate) > size or total > 60000 or state_bytes + longest > 30000):
            yield batch
            batch = [course]
        else:
            batch = candidate
    if batch:
        yield batch


def classify_batch(runtime, courses, taxonomy, model):
    from provider_runtime import ProviderDeferred
    request = batch_request(courses, taxonomy)
    payload = {"model": model, **request}
    state_bytes = len(canonical(request["state"]).encode())
    longest = max(len(canonical(question).encode()) for question in request["questions"].values())
    if len(canonical(request).encode()) > 60000 or state_bytes + longest > 30000:
        raise ValueError("A course exceeds the conservative request context allowance")
    while True:
        try:
            response = runtime.request("typesafe", model, "systemone", payload,
                                       estimated_tokens=max(1, math.ceil(len(canonical(payload).encode()) / 3)),
                                       cache_version=BATCH_VERSION)
            break
        except ProviderDeferred as error:
            delay = error.next_attempt_at - time.time()
            if "daily" in str(error).lower() or "cost" in str(error).lower() or delay > 60:
                raise
            time.sleep(max(.1, delay))
    if not isinstance(response.get("model"), str) or not isinstance(response.get("answers"), dict) or set(response["answers"]) != set(request["questions"]):
        raise ValueError("Jev response does not match this batch")
    records = []
    for index, course in enumerate(courses):
        try:
            records.append(batch_record(course, taxonomy, response["answers"][f"course_{index}"], response["model"]))
        except ValueError as error:
            records.append({"course_key": course["course_key"], "state_fingerprint": state_fingerprint(course_state(course)),
                            "taxonomy_fingerprint": state_fingerprint(taxonomy), "question_version": BATCH_VERSION,
                            "raw_answer": response["answers"][f"course_{index}"], "resolved_model": response["model"],
                            "status": "invalid-answer", "error_type": type(error).__name__})
    return records


def run(input_path, taxonomy_path, database_path, profile, workers=4, model="jev-1.13.0", batch_limit=None,
        version=BATCH_VERSION, request_builder=batch_request, batch_classifier=None, revalidate=None):
    from processing_jobs import connect
    from provider_runtime import ProviderDeferred
    taxonomy = json.loads(Path(taxonomy_path).read_text())
    if not isinstance(workers, int) or isinstance(workers, bool) or not 1 <= workers <= 16:
        raise ValueError("Use one to sixteen coordinated workers")
    source = json.loads(Path(input_path).read_text())
    courses = source["courses"] if isinstance(source, dict) else source
    if len({course["course_key"] for course in courses}) != len(courses):
        raise ValueError("Full-map inputs require unique source course identities")
    runtime = budgeted_runtime(str(database_path), profile)
    taxonomy_hash = state_fingerprint(taxonomy)
    grouped = {}
    for course in courses:
        grouped.setdefault(state_fingerprint(course_state(course)), []).append(course)
    with connect(database_path) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS jev_course_results (course_key TEXT NOT NULL,input_hash TEXT NOT NULL,taxonomy_hash TEXT NOT NULL,model TEXT NOT NULL,version TEXT NOT NULL,result TEXT NOT NULL,PRIMARY KEY(course_key,input_hash,taxonomy_hash,model,version))")
        saved = {(row["course_key"],row["input_hash"]): json.loads(row["result"]) for row in connection.execute(
            "SELECT course_key,input_hash,result FROM jev_course_results WHERE taxonomy_hash=? AND model=? AND version=?", (taxonomy_hash,model,version))}
        by_state = {}
        for (_, fingerprint), record in saved.items():
            by_state.setdefault(fingerprint, record)
        for fingerprint, members in grouped.items():
            if fingerprint not in by_state:
                continue
            cached = by_state[fingerprint]
            for course in members:
                if (course["course_key"],fingerprint) in saved:
                    continue
                result = cached | {"course_key":course["course_key"], "identity_warning":identity_warning(course)}
                connection.execute("INSERT OR REPLACE INTO jev_course_results VALUES (?,?,?,?,?,?)",
                                   (course["course_key"],fingerprint,taxonomy_hash,model,version,canonical(result)))
                saved[(course["course_key"],fingerprint)] = result
    pending = [members[0] for fingerprint,members in grouped.items()
               if any((course["course_key"],fingerprint) not in saved for course in members)]
    queue = list(batches(pending, taxonomy, request_builder=request_builder))
    if batch_limit is not None:
        queue = queue[:batch_limit]
    completed = 0
    deferred = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        iterator = iter(queue)
        stopped = False
        while True:
            while len(futures) < workers and not stopped:
                batch = next(iterator, None)
                if batch is None:
                    stopped = True
                    break
                with connect(database_path) as connection:
                    spent = connection.execute("SELECT COALESCE(SUM(MAX(COALESCE(estimated_cost,0),COALESCE(usage_cost_estimate,0))),0) FROM provider_attempts").fetchone()[0]
                if spent >= profile["daily_cost_cap"] - .01:
                    stopped = True
                    break
                futures[pool.submit(batch_classifier or classify_batch, runtime, batch, taxonomy, model)] = batch
            if not futures:
                break
            future = next(as_completed(futures))
            batch = futures.pop(future)
            try:
                records = future.result()
            except ProviderDeferred as error:
                deferred.extend(course["course_key"] for course in batch)
                if "daily" in str(error).lower() or "cost" in str(error).lower():
                    stopped = True
                continue
            except LifetimeBudgetReached:
                stopped = True
                deferred.extend(course["course_key"] for course in batch)
                continue
            except Exception as error:
                deferred.extend(course["course_key"] for course in batch)
                print(canonical({"batch_failure":type(error).__name__,"affected":len(batch)}), flush=True)
                continue
            with connect(database_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                for record in records:
                    members = grouped[record["state_fingerprint"]]
                    for course in members:
                        result = record | {"course_key":course["course_key"], "identity_warning":identity_warning(course)}
                        connection.execute("INSERT OR REPLACE INTO jev_course_results VALUES (?,?,?,?,?,?)",
                                           (course["course_key"],record["state_fingerprint"],taxonomy_hash,model,version,canonical(result)))
                        completed += 1
                connection.commit()
            if completed % 200 < sum(len(grouped[record["state_fingerprint"]]) for record in records):
                print(canonical({"saved_this_run":completed,"source_identities":len(courses),"unique_metadata":len(grouped),"usage":runtime.usage()}), flush=True)
    with connect(database_path) as connection:
        rows = connection.execute("SELECT result FROM jev_course_results WHERE taxonomy_hash=? AND model=? AND version=?", (taxonomy_hash,model,version)).fetchall()
    results = [json.loads(row[0]) for row in rows]
    valid_keys = {course["course_key"]:state_fingerprint(course_state(course)) for course in courses}
    results = [record for record in results if valid_keys.get(record["course_key"]) == record["state_fingerprint"]]
    current_courses = {course["course_key"]:course for course in courses}
    checked = []
    for record in results:
        course = current_courses[record["course_key"]]
        if revalidate is not None:
            record = revalidate(record, course, taxonomy)
        elif "answer" in record or "raw_answer" in record:
            try:
                record = batch_record(course,taxonomy,record.get("answer",record.get("raw_answer")),record["resolved_model"])
            except ValueError:
                record = record | {"status":"invalid-answer", "classification_status":"needs-review",
                                   "primary_academic_area":"other-academic-subject"}
                if "answer" in record:
                    record["raw_answer"] = record["answer"]
                record.pop("answer",None)
        checked.append(record | {"identity_warning":identity_warning(course)})
    results = checked
    summary = {"source_identities":len(courses),"unique_metadata":len(grouped),"saved_results":len(results),
               "valid_answers":sum("answer" in record for record in results),
               "invalid_answers":sum("answer" not in record for record in results),
               "confident_candidates":sum(record.get("classification_status") == "candidate" for record in results),
               "remaining":len(courses)-len(results),"deferred_keys":len(deferred),"usage":runtime.usage()}
    summary["not_validly_classified"] = len(courses) - summary["valid_answers"]
    print(canonical(summary), flush=True)
    return {"format":"topclass-jev-course-fields-v2","question_version":version,
            "taxonomy_fingerprint":taxonomy_hash,"records":results,"summary":summary}


def main():
    parser = argparse.ArgumentParser(description="Classify every supplied course identity with resumable, budget-gated Jev batches")
    parser.add_argument("--input", required=True)
    parser.add_argument("--taxonomy", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--batch-limit", type=int)
    parser.add_argument("--refine", action="store_true")
    parser.add_argument("--allow-model-calls", action="store_true")
    args = parser.parse_args()
    if not args.allow_model_calls:
        parser.error("Explicit provider-processing authorization is required")
    target = Path(args.out)
    if target.exists():
        raise FileExistsError("Use a fresh output path")
    profile = json.loads(Path(args.profile).read_text())
    options = {}
    if args.refine:
        try:
            from .jev_refine_courses import REFINEMENT_VERSION, classify_refinement_batch, refinement_request, revalidate_refinement
        except ImportError:
            from jev_refine_courses import REFINEMENT_VERSION, classify_refinement_batch, refinement_request, revalidate_refinement
        options = {"version":REFINEMENT_VERSION, "request_builder":refinement_request,
                   "batch_classifier":classify_refinement_batch, "revalidate":revalidate_refinement}
    result = run(args.input,args.taxonomy,args.database,profile,args.workers,batch_limit=args.batch_limit,**options)
    if args.refine:
        result["format"] = "topclass-jev-course-refinement-v3"
    with target.open("x") as output:
        json.dump(result,output,ensure_ascii=False,separators=(",", ":"))


if __name__ == "__main__":
    main()

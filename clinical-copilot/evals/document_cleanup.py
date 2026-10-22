"""Per-case document cleanup for evals/cases.py and evals/unit_tests.py.

Every eval case/test that uploads a document to one of this module's
allowlisted fixture patients registers the exact filenames it created
(source_doc_id and/or extracted_doc_id -- compute_document_ids()'s own
return values, never a raw patient-wide query) via
register_for_cleanup(). The caller flushes in its own `finally` block
(evals/run_evals.py does this once per case; evals/unit_tests.py's own
citepreview test does it directly around itself), so the documents it
created are gone again regardless of whether the case/test passed,
failed, or raised.

Originally TEST-0007 (PID7_UPLOAD_EVAL) only -- found live 2026-09-25:
without per-case cleanup, dozens of cases uploading to that shared
fixture patient within one run left it with ~30 accumulated documents by
the time a later case asked "what did the most recently uploaded
document show", which has no reliable answer once that many are on
file. Generalized 2026-09-27 to also cover TEST-0001 (PID1_ALICE):
evals/unit_tests.py's test_uploaded_document_citations_preview_resolves_
on_both_paths uploads a real Source Document + AI-Extracted pair to
PID1 every run (a per-run nonce, deliberately -- see that test's own
docstring for why fixed/deduped content doesn't work there: PID1's
AI-Extracted category is exactly what graph_cases.py's
check_pid1_has_no_uploaded_documents precondition checks, so anything
left behind there, even a single permanently-deduped document, would
fail every later run of uploaded_documents_referenced_with_none_on_
file_is_honest).

evals/prune_upload_fixture.py's own pre-run sweep is kept as a safety
net for whatever this misses on TEST-0007 (a case added later without
registering its uploads, or a hard interpreter crash before any
`finally` runs) -- this module does not replace it. TEST-0001 has no
equivalent sweep (it's a shared general-purpose fixture, not a
dedicated one this suite owns outright), so its own cleanup must not
skip the `finally`.

Deletion is scoped STRICTLY to (a) filenames the calling code explicitly
registered, never "every document on the patient", and (b) a hardcoded
allowlist of pubpids (_ALLOWED_PUBPIDS) -- resolving or deleting against
any other pubpid raises outright, so a typo'd or attacker-controlled
pubpid can never make this module touch an arbitrary patient. Reuses
evals/prune_upload_fixture.py's own DbConfig/_run_mysql/PruneError
(same connection config, same soft-delete mechanism) rather than a
second, independently-written DB access path.
"""

from __future__ import annotations

import os
import sys
import threading

from evals.prune_upload_fixture import DbConfig, PruneError, _run_mysql

# TEST-0007 (PID7_UPLOAD_EVAL, evals/fixtures.py) and TEST-0001
# (PID1_ALICE) only -- see the module docstring for why each is here.
# Deliberately NOT sourced from a config file or env var: a hardcoded
# allowlist is the entire safety property this module offers over a
# bare "delete documents for this patient" helper.
_ALLOWED_PUBPIDS = frozenset({"TEST-0001", "TEST-0007"})

# pubpid -> filenames registered since the last flush for that pubpid.
# Thread-local: evals/run_evals.py may run cases on several threads, and a
# case's `finally` flush must only delete what THAT case uploaded -- a
# process-wide registry would let one thread's flush delete documents a
# concurrent case is still using. Each case runs start to finish on one
# thread, so this is exactly per-case scoping.
_state = threading.local()


def _pending() -> dict[str, list[str]]:
    pending = getattr(_state, "pending_by_pubpid", None)
    if pending is None:
        pending = _state.pending_by_pubpid = {}
    return pending


def _cleanup_db_config() -> DbConfig:
    return DbConfig(
        host=os.environ.get("PRUNE_DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("PRUNE_DB_PORT", "8320")),
        user=os.environ.get("PRUNE_DB_USER", "root"),
        password=os.environ.get("PRUNE_DB_PASSWORD", "root"),
        database=os.environ.get("PRUNE_DB_NAME", "openemr"),
        client_binary=os.environ.get("PRUNE_MYSQL_CLIENT", "mysql"),
    )


def _resolve_pid(cfg: DbConfig, pubpid: str, run_mysql=_run_mysql) -> int:
    """Resolves the internal numeric pid for an allowlisted pubpid --
    refuses anything not in _ALLOWED_PUBPIDS outright, before ever
    issuing a query, so this can't be tricked into resolving (let alone
    deleting against) an arbitrary patient."""
    if pubpid not in _ALLOWED_PUBPIDS:
        raise PruneError(
            f"pubpid={pubpid!r} is not in this module's cleanup allowlist ({sorted(_ALLOWED_PUBPIDS)}) -- "
            "refusing to resolve or touch it."
        )
    out = run_mysql(cfg, f"SELECT pid FROM patient_data WHERE pubpid = '{pubpid}';")
    rows = [line for line in out.strip().splitlines() if line]
    if len(rows) != 1:
        raise PruneError(f"Expected exactly one patient_data row for pubpid={pubpid!r}, found {len(rows)}.")
    return int(rows[0])


def register_for_cleanup(pubpid: str, *filenames: str | None) -> None:
    """Called by an upload helper right after a real upload succeeds (or
    is attempted -- a failed extraction's None doc_id is silently
    ignored, nothing to clean up for that). `pubpid` must be one of
    _ALLOWED_PUBPIDS; validated again at flush time via _resolve_pid, not
    just here, so registering under a bogus pubpid can't be used to bypass
    the allowlist -- it just means that pubpid's cleanup will fail loudly
    (logged, not raised) when flushed."""
    names = [name for name in filenames if name]
    if not names:
        return
    _pending().setdefault(pubpid, []).extend(names)


def _register_for_cleanup(*filenames: str | None) -> None:
    """Back-compat entry point for evals/cases.py's existing call sites,
    all of which upload to TEST-0007 (PID7_UPLOAD_EVAL). Equivalent to
    register_for_cleanup("TEST-0007", *filenames)."""
    register_for_cleanup("TEST-0007", *filenames)


def delete_documents_for_pubpid(pubpid: str, filenames: list[str]) -> int:
    """Soft-deletes exactly the `documents` rows whose `name` matches one
    of the given filenames, scoped to the given (allowlisted-only) pubpid
    -- see _resolve_pid. Returns the number of rows actually deleted.
    Filenames are always this suite's own compute_document_ids() hex-hash
    output, never raw external input, but still passed as literal SQL
    string values, not string-interpolated identifiers."""
    if not filenames:
        return 0
    cfg = _cleanup_db_config()
    try:
        pid = _resolve_pid(cfg, pubpid)
    except PruneError as exc:
        print(f"[document_cleanup] could not resolve pubpid={pubpid!r}, skipping cleanup: {exc}", file=sys.stderr)
        return 0
    quoted = ", ".join("'" + name.replace("'", "''") + "'" for name in filenames)
    out = _run_mysql(
        cfg,
        f"SELECT id FROM documents WHERE foreign_id = {pid} AND name IN ({quoted}) AND deleted = 0;",
    )
    ids = [int(line) for line in out.strip().splitlines() if line.strip()]
    if not ids:
        return 0
    ids_csv = ",".join(str(i) for i in ids)
    _run_mysql(cfg, f"UPDATE documents SET deleted = 1 WHERE id IN ({ids_csv});")
    return len(ids)


def flush_cleanup() -> None:
    """Deletes whatever has been registered across every pubpid since the
    last flush, then clears the registry -- regardless of whether the
    calling case/test passed, failed, or raised. A pubpid that registered
    nothing makes this a no-op for that pubpid."""
    pending = dict(_pending())
    _pending().clear()
    for pubpid, filenames in pending.items():
        try:
            deleted = delete_documents_for_pubpid(pubpid, filenames)
            if deleted != len(filenames):
                print(
                    f"[document_cleanup] pubpid={pubpid!r}: registered {len(filenames)} filename(s) but only "
                    f"found/deleted {deleted} -- the rest were already gone or never actually stored (e.g. a "
                    "case/test that failed before its own upload completed).",
                    file=sys.stderr,
                )
        except Exception as exc:  # noqa: BLE001 -- cleanup must never crash the suite; prune_upload_fixture.py is the safety net for TEST-0007
            print(f"[document_cleanup] cleanup failed for pubpid={pubpid!r}: {exc!r}", file=sys.stderr)


def _flush_cleanup() -> None:
    """Back-compat entry point for evals/run_evals.py's existing `finally`
    call, once per case. Equivalent to flush_cleanup()."""
    flush_cleanup()

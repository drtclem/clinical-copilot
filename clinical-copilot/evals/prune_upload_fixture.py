"""Prunes accumulated uploaded documents for the dedicated upload-eval
fixture patient (TEST-0007 / PID7_UPLOAD_EVAL, evals/fixtures.py) so months
of eval_golden_set runs don't leave an unbounded number of Source Document /
AI-Extracted Pending Review documents on that one patient -- evals/
fixtures.py's own comment already names bounded accumulation on exactly this
one patient (never PID1/PID2) as the reason PID7 exists at all.

No REST API route exists for deleting a document. Confirmed by reading
src/RestControllers/DocumentRestController.php directly: it registers only
GET (list), POST (create), and GET (download) for /api/patient/{pid}/
document[/{did}] -- no DELETE. This is the same shape as the CSRF-key
read-back gap clinical-copilot/README.md's "Known gap" section already
documents for a different route, just for delete instead of read. This
script therefore goes around the REST layer entirely and issues a
soft-delete directly against MySQL (`UPDATE documents SET deleted = 1
...`) -- the same mechanism OpenEMR's own
library/classes/Document.class.php::process_deleted() uses internally, not
a hard DELETE and not a new invented mechanism (documents.deleted is a
real, existing column -- sql/database.sql).

Never tested against a live server as of this writing -- see
fix/tomorrow-prep's report for exactly what needs sign-off before this runs
for real anywhere (evals/seed_fixtures.py's own docstring establishes the
same "written against the schema, not yet live-verified" caveat pattern for
a new maintenance script).

Effectiveness, checked directly against the two real read paths this
prune is meant to bound -- confirmed by reading the PHP, not assumed:
  - `get_uploaded_documents` (the tool app/tools.py's chart_data_worker
    exposes, backed by app/ingestion/storage.py's list_extracted_documents
    -> the Standard API's GET /patient/:pid/document, which calls
    src/Services/DocumentService.php's getAllAtPath()) DOES correctly
    exclude a soft-deleted document -- that method's own SQL has
    `AND doc.deleted = 0` (DocumentService.php line 107). This prune is
    effective for that path: a document this script marks deleted stops
    appearing there on the very next call, no caching involved.
  - The click-to-source PREVIEW path
    (app/ingestion/storage.py's get_source_document_bytes(), which
    searches FHIR `DocumentReference?patient=...` then fetches the
    matching `Binary`) does NOT exclude a soft-deleted document. Checked
    two ways: (1) src/Services/DocumentService.php's generic search()
    method -- the one src/Services/FHIR/DocumentReference/
    FhirPatientDocumentReferenceService.php's searchForOpenEMRRecords()
    actually calls for this route -- builds its WHERE clause entirely
    from the caller's own search params (patient=<id> here) with no
    hardcoded `deleted = 0` anywhere; a soft-deleted row is still
    returned, just later given FHIR status "entered-in-error" instead of
    "current" (parseOpenEMRRecord(), same file, lines 216-224). (2) even
    if that status distinction were useful, get_source_document_bytes()'s
    own Python code never reads `resource.get("status")` at all -- it
    matches purely on category text + attachment title. **Net effect: a
    document this script prunes would still be fetchable through the
    preview endpoint specifically**, even though it correctly disappears
    from get_uploaded_documents. Not fixed here -- out of scope for a
    prune script, flagged for whoever owns that endpoint next. Doesn't
    undermine this script's actual purpose (bounding what
    get_uploaded_documents accumulates for eval-run correctness/speed),
    but is a real, separate gap, not a hypothetical one.

Safety, hard-scoped to exactly one fixture patient, never guessable from a
raw pid alone:
  - The internal numeric pid is resolved by joining on `pubpid =
    'TEST-0007'` (the exact PatientSpec in evals/fixtures.py) -- never
    accepted as a raw --pid argument, so a misconfigured environment
    variable cannot point this at a real or different patient.
  - Only rows in `documents` linked (via `categories_to_documents`) to the
    two categories app/ingestion/storage.py ever writes to -- Source
    Document (id 35) and AI-Extracted Pending Review (id 36) -- are ever
    touched, and only after confirming those ids still map to those exact
    names on this stack (aborts loudly instead of silently pruning the
    wrong category if they don't).
  - Only rows with `deleted = 0` are selected (idempotent -- a second run
    finds nothing left to prune).
  - The UPDATE is scoped to `WHERE id IN (<the exact ids just SELECTed and
    printed>)` -- it never re-evaluates the original WHERE clause, so
    nothing can match more rows between the SELECT and the UPDATE than were
    actually shown to the operator first.
  - The target patient, the categories scanned, and the document count are
    always printed before any write, dry-run or not.
  - The DB host is checked before anything else runs (see _validate_host):
    unconditionally refuses the known production host (localhost,
    W2_ARCHITECTURE.md 4.0), and refuses any non-loopback host that isn't
    explicitly declared via --staging-host/PRUNE_STAGING_DB_HOST -- this
    script never guesses what "staging" is.
    **KNOWN GAP, found live 2026-09-25, not yet fixed:** the staging
    droplet's own CI runner is a shell executor colocated with the mysql
    container (EVAL_GATE.md), so it reaches MySQL via 127.0.0.1:8320 --
    a real, in-use staging value, not a placeholder. The loopback
    exemption above was designed around "127.0.0.1 = someone's personal
    dev sandbox", which is simply wrong in that specific case -- on that
    runner, --staging-host/PRUNE_STAGING_DB_HOST is set but never
    actually checked, because 127.0.0.1 short-circuits past it. The
    production-host refusal is unaffected and still holds. See
    .gitlab-ci.yml's own comment on the prune_upload_fixture job for the
    same note; not patched here, since the fix needs a real decision
    (e.g. an explicit --environment flag instead of inferring safety
    from the host string), not a quick special-case.

**Operational requirement, not (yet) enforced by this script: the DB user
this connects as should be a dedicated account with exactly SELECT on
patient_data and UPDATE on documents -- never root, never the full
OpenEMR database password.** --db-user/--db-password default to root/root
purely for local-dev convenience (matching docker/development-easy's own
MYSQL_ROOT_PASSWORD); running with --execute as root against a non-
loopback host prints a loud warning (not a hard refusal -- creating that
scoped user on staging is real setup work, tracked as a to-do, not
something this script can create for itself).

Dry-run is the default. Live pruning requires --execute.

Usage:
    # local dev, your own machine: list what would be pruned, no changes
    # made (the safe default)
    python -m evals.prune_upload_fixture \\
        --db-host 127.0.0.1 --db-port 8320 --db-user root --db-password root

    # staging, run FROM the staging droplet itself (its CI runner reaches
    # mysql via the same 127.0.0.1:8320 local dev also defaults to --
    # see the KNOWN GAP note above, --staging-host is set here for
    # documentation/intent but is not actually enforced for this
    # specific host value today):
    python -m evals.prune_upload_fixture \\
        --db-host 127.0.0.1 --db-port 8320 --staging-host 127.0.0.1 \\
        --mysql-client mariadb \\
        --db-user <scoped-user> --db-password <scoped-password>
    # add --execute only once a dry-run's output has been reviewed

    # staging, from a genuinely different/remote machine (--staging-host
    # is actually enforced in this case, since the host isn't loopback):
    python -m evals.prune_upload_fixture \\
        --db-host <staging-ip> --staging-host <staging-ip> \\
        --mysql-client mariadb \\
        --db-user <scoped-user> --db-password <scoped-password> \\
        --execute

Connection args default to this LOCAL docker/development-easy stack's
values (docker/development-easy/docker-compose.yml's mysql service) --
override every --db-* flag (or the matching PRUNE_DB_* env var) for
staging; there is no default that could accidentally reach it, and
production is refused unconditionally regardless of any flag.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass

FIXTURE_PUBPID = "TEST-0007"
SOURCE_DOCUMENT_CATEGORY_ID = 35
SOURCE_DOCUMENT_CATEGORY_NAME = "Source Document"
AI_EXTRACTED_CATEGORY_ID = 36
AI_EXTRACTED_CATEGORY_NAME = "AI-Extracted Pending Review"
_PRUNABLE_CATEGORY_IDS = (SOURCE_DOCUMENT_CATEGORY_ID, AI_EXTRACTED_CATEGORY_ID)

# W2_ARCHITECTURE.md 4.0: the real, known production host. Refused
# unconditionally below -- no flag, env var, or misconfigured
# --staging-host can make this script run against it.
PRODUCTION_DB_HOST = "localhost"

# Local dev is inherently out of scope for the production/staging
# distinction (the user's own sandboxed docker stack, never shared
# infrastructure) -- exempted from the --staging-host requirement so local
# usage stays frictionless; still checked against PRODUCTION_DB_HOST first,
# trivially never equal to any of these.
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


@dataclass(frozen=True)
class DbConfig:
    host: str
    port: int
    user: str
    password: str
    database: str
    client_binary: str = "mysql"


class PruneError(Exception):
    """A safety precondition failed. Never caught silently -- main() lets
    this abort the whole run with a nonzero exit code rather than
    proceeding on an unverified assumption."""


def _validate_host(host: str, staging_host: str | None) -> None:
    """Checked before anything else in main() -- resolving the fixture pid
    or running any query already means a live connection was made, so this
    must run first, not after. Two independent rules, both must pass:

    1. The known production host is refused unconditionally -- not
       overridable by --staging-host, since a --staging-host typo that
       happens to equal PRODUCTION_DB_HOST must never silently authorize
       a production run.
    2. Any other non-loopback host must be explicitly declared via
       --staging-host (or PRUNE_STAGING_DB_HOST) and match --db-host
       exactly -- this script never infers "this is staging" from a
       default or a guess."""
    if host == PRODUCTION_DB_HOST:
        raise PruneError(
            f"--db-host {host!r} is the known production host ({PRODUCTION_DB_HOST}, "
            f"W2_ARCHITECTURE.md 4.0). Refusing unconditionally -- this script must never run "
            f"against production, regardless of --staging-host."
        )
    if host in _LOOPBACK_HOSTS:
        return
    if not staging_host:
        raise PruneError(
            f"--db-host {host!r} is not localhost, and no --staging-host (or "
            f"PRUNE_STAGING_DB_HOST) was given to confirm it's the real staging host. Refusing "
            f"to guess -- pass --staging-host explicitly to run against anything other than "
            f"local dev."
        )
    if host != staging_host:
        raise PruneError(
            f"--db-host {host!r} does not match the declared --staging-host {staging_host!r}. "
            f"Refusing to run against an undeclared host."
        )


def _run_mysql(cfg: DbConfig, sql: str) -> str:
    """Runs one statement via a MySQL-protocol CLI client (subprocess, not
    a new Python DB-driver dependency -- this is a one-off ops script, not
    app code the rest of clinical-copilot depends on, and requirements.txt
    has never had a MySQL client library; the app itself only ever talks
    to OpenEMR through the REST/FHIR API, by design). `-N -B` -> tab-
    separated, no column-header row, easiest to parse without a real
    driver.

    cfg.client_binary, not a hardcoded "mysql": found live 2026-09-25,
    first real run against staging -- docker/development-easy's own mysql
    image (mariadb:11.8.8) ships no `mysql` binary at all, only
    `mariadb` (MariaDB renamed the client starting with this release
    line, no compat symlink in this particular image). Configurable via
    --mysql-client/PRUNE_MYSQL_CLIENT rather than hardcoding "mariadb"
    instead, since a different MySQL/MariaDB image or a genuinely
    different `mysql`-named client elsewhere shouldn't require another
    code change -- this script had never been run against a real server
    before that first staging run, exactly why dry-run-first mattered."""
    cmd = [
        cfg.client_binary,
        f"--host={cfg.host}",
        f"--port={cfg.port}",
        f"--user={cfg.user}",
        f"--password={cfg.password}",
        "-N",
        "-B",
        cfg.database,
        "-e",
        sql,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise PruneError(f"{cfg.client_binary!r} exited {result.returncode}: {result.stderr.strip()}")
    return result.stdout


def resolve_fixture_pid(cfg: DbConfig, run_mysql=_run_mysql) -> int:
    """Internal numeric pid for pubpid='TEST-0007', resolved fresh every
    run -- never hardcoded, since evals/fixtures.py's own docstring says
    fixture ids/uuids are stack-specific (local dev vs. staging assign
    their own values)."""
    out = run_mysql(cfg, f"SELECT pid FROM patient_data WHERE pubpid = '{FIXTURE_PUBPID}';")
    rows = [line for line in out.strip().splitlines() if line]
    if len(rows) != 1:
        raise PruneError(
            f"Expected exactly one patient_data row for pubpid={FIXTURE_PUBPID!r}, found "
            f"{len(rows)}. Refusing to guess -- run evals/seed_fixtures.py first if this "
            f"patient doesn't exist yet on this stack."
        )
    return int(rows[0])


def find_prunable_documents(cfg: DbConfig, pid: int, run_mysql=_run_mysql) -> list[tuple[int, str, str]]:
    """Returns (document_id, category_name, document_name) for every
    non-deleted document on this exact pid, in either of the two
    categories this pipeline ever writes to. Verifies id<->name agreement
    for both categories BEFORE running the real query -- if OpenEMR's
    category ids were ever renumbered on a given stack (storage.py's own
    note calls these "already created" fixed ids, not guaranteed identical
    across every install), this aborts loudly instead of silently pruning
    the wrong category."""
    expected_names = {
        SOURCE_DOCUMENT_CATEGORY_ID: SOURCE_DOCUMENT_CATEGORY_NAME,
        AI_EXTRACTED_CATEGORY_ID: AI_EXTRACTED_CATEGORY_NAME,
    }
    ids_csv = ", ".join(str(i) for i in _PRUNABLE_CATEGORY_IDS)
    out = run_mysql(cfg, f"SELECT id, name FROM categories WHERE id IN ({ids_csv});")
    found_names: dict[int, str] = {}
    for line in out.strip().splitlines():
        if not line:
            continue
        cid_s, name = line.split("\t", 1)
        found_names[int(cid_s)] = name
    for cid, expected_name in expected_names.items():
        actual_name = found_names.get(cid)
        if actual_name != expected_name:
            raise PruneError(
                f"categories.id={cid} is named {actual_name!r} on this stack, expected "
                f"{expected_name!r} (app/ingestion/storage.py's hardcoded category ids). "
                f"Refusing to prune against a category mapping that doesn't match what the "
                f"upload pipeline actually writes to."
            )

    out = run_mysql(
        cfg,
        f"""
        SELECT d.id, c.name, d.name
        FROM documents d
        JOIN categories_to_documents ctd ON ctd.document_id = d.id
        JOIN categories c ON c.id = ctd.category_id
        WHERE d.foreign_id = {pid}
          AND d.deleted = 0
          AND ctd.category_id IN ({ids_csv});
        """,
    )
    rows: list[tuple[int, str, str]] = []
    for line in out.strip().splitlines():
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        doc_id_s, category_name, doc_name = parts
        rows.append((int(doc_id_s), category_name, doc_name))
    return rows


def prune(cfg: DbConfig, document_ids: list[int], run_mysql=_run_mysql) -> None:
    """Soft-deletes exactly the given, already-SELECTed document ids --
    never re-evaluates the WHERE clause that found them, so nothing can
    match more rows between the SELECT and this UPDATE than were actually
    printed to the operator."""
    if not document_ids:
        return
    ids_csv = ",".join(str(i) for i in document_ids)
    run_mysql(cfg, f"UPDATE documents SET deleted = 1 WHERE id IN ({ids_csv});")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-host", default=os.environ.get("PRUNE_DB_HOST", "127.0.0.1"))
    parser.add_argument("--db-port", type=int, default=int(os.environ.get("PRUNE_DB_PORT", "8320")))
    parser.add_argument("--db-user", default=os.environ.get("PRUNE_DB_USER", "root"))
    parser.add_argument("--db-password", default=os.environ.get("PRUNE_DB_PASSWORD", "root"))
    parser.add_argument("--db-name", default=os.environ.get("PRUNE_DB_NAME", "openemr"))
    parser.add_argument(
        "--mysql-client",
        default=os.environ.get("PRUNE_MYSQL_CLIENT", "mysql"),
        help="The MySQL-protocol CLI binary to invoke -- \"mysql\" by default, but "
        "docker/development-easy's own mariadb:11.8.8 image ships only \"mariadb\" (no "
        "compat symlink), found live against staging. Pass --mysql-client mariadb (or set "
        "PRUNE_MYSQL_CLIENT=mariadb) for that image.",
    )
    parser.add_argument(
        "--staging-host",
        default=os.environ.get("PRUNE_STAGING_DB_HOST"),
        help="Required for any --db-host that isn't localhost -- must match --db-host exactly, "
        "a deliberate, separate confirmation that this really is the staging host.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually soft-delete the listed documents. Without this flag, only lists them "
        "(dry run, the default).",
    )
    args = parser.parse_args()

    cfg = DbConfig(
        host=args.db_host,
        port=args.db_port,
        user=args.db_user,
        password=args.db_password,
        database=args.db_name,
        client_binary=args.mysql_client,
    )

    try:
        _validate_host(cfg.host, args.staging_host)
        pid = resolve_fixture_pid(cfg)
        rows = find_prunable_documents(cfg, pid)
    except PruneError as exc:
        print(f"[abort] {exc}", file=sys.stderr)
        return 1

    print(f"Target: pubpid={FIXTURE_PUBPID}  pid={pid}  host={cfg.host}")
    print(f"Categories scanned: {SOURCE_DOCUMENT_CATEGORY_NAME!r}, {AI_EXTRACTED_CATEGORY_NAME!r}")
    print(f"Documents found: {len(rows)}")
    if not rows:
        print("Nothing to prune -- 0 non-deleted documents in either category.")
        return 0

    for doc_id, category_name, doc_name in rows:
        print(f"  id={doc_id}  category={category_name!r}  name={doc_name!r}")

    if not args.execute:
        print(f"\n[dry-run] Would soft-delete {len(rows)} document(s) above. Re-run with --execute to actually prune.")
        return 0

    if cfg.host not in _LOOPBACK_HOSTS and cfg.user == "root":
        print(
            "[warning] Connected as 'root' against a non-loopback host. This should be a "
            "dedicated user scoped to SELECT on patient_data and UPDATE on documents, not root "
            "-- see this script's module docstring. Proceeding, since creating that user is "
            "separate setup work this script can't do for itself, but this should be fixed "
            "before relying on this for staging.",
            file=sys.stderr,
        )

    document_ids = [doc_id for doc_id, _, _ in rows]
    prune(cfg, document_ids)
    print(f"\n[pruned] Soft-deleted {len(document_ids)} document(s) (documents.deleted = 1).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

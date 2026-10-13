"""
ChemAIRS SAScore API client — the commercial replacement for RDKit's local
SAscore, per ~/Desktop/APIInterfaceDocumentationEN.pdf, section "1. SAScore API".

Flow (documented, async task model):
  1. POST {base}/open/v2/sascore-json-search   -> returns a task id (string)
  2. GET  {base}/open/v2/sascore-results/{taskId}/{pageSize}/{pageIndex}
         -> poll until every row has a terminal result

*** UNRESOLVED, PLEASE CONFIRM BEFORE TRUSTING FILTER RESULTS ***
The documentation never states the numeric range or direction of the
"SAScore" field it returns (RDKit's own SAscore is 1=easy .. 10=hard; this
vendor's scale/direction is NOT specified anywhere in the 37-page doc, and
every worked example in the doc uses a placeholder value of "0"). This
module does NOT assume it matches RDKit's scale. It passes the raw number
through under `sa_score` with `scale="unknown (confirm with ChemAIRS)"` so
it is visibly different from the RDKit path until a human confirms it
against a real account. The SA stage's pass/fail threshold comparison
(`sa_limit`) will silently do the wrong thing if this is wrong -- check the
first few real results by eye before trusting a filtered-out molecule list.

Completion detection is similarly inferred, not documented: the doc defines
an explicit status enum for the *Retrosynthesis* task-info endpoint but not
for SAScore results. A row is treated as "done" once `isError` is no longer
null AND (isError is true OR saScore is a non-empty value). Confirm this
too once real calls can be made.

No live network call has been made against this API (no working key was
available while writing this module) -- the request/response shapes below
are built strictly from the documented examples. Run
`pytest -m real tests/integration/test_chemairs.py` once CHEMAIRS_API_KEY
and CHEMAIRS_BASE_URL are set to validate it for real; it is skipped
otherwise, exactly like the BioSolveIT integration tests.
"""
from __future__ import annotations

import dataclasses
import json
import time
import urllib.error
import urllib.request

from ..config import settings

CREATE_PATH = "/open/v2/sascore-json-search"
RESULTS_PATH = "/open/v2/sascore-results/{task_id}/{page_size}/{page_index}"


class ChemAIRSError(RuntimeError):
    """The API reachable but refused the request, or returned something we can't parse."""


@dataclasses.dataclass
class SAScoreResult:
    smiles: str
    row_number: int
    is_error: bool
    sa_score: float | None
    feasibility_score: str | None
    remark: str | None
    raw: dict            # the full searchResponseDtos entry, for anything not mapped above


def build_create_payload(smiles_list: list[str], *, use_eln_data: bool = False,
                         mode: int | None = None, material_source: int | None = None,
                         callback_url: str | None = None) -> dict:
    """Pure function: the JSON body for `POST .../sascore-json-search`. No I/O."""
    if not smiles_list:
        raise ValueError("smiles_list must not be empty")
    body = {
        "useELNData": use_eln_data,
        "mode": mode if mode in (2, 4) else settings.chemairs_mode,
        "materialSource": material_source if material_source in (0, 1) else settings.chemairs_material_source,
        "smiles": list(smiles_list),
    }
    if callback_url:
        body["callbackUrl"] = callback_url
    return body


def parse_results_page(payload: dict) -> tuple[int, str, list[SAScoreResult]]:
    """Pure function: parse one page of `GET .../sascore-results/...`. No I/O.

    Returns (total_count, progress_string, [SAScoreResult, ...]).
    """
    rows = payload.get("searchResponseDtos") or []
    out = []
    for i, row in enumerate(rows):
        raw_score = row.get("saScore")
        try:
            sa_score = float(raw_score) if raw_score not in (None, "") else None
        except (TypeError, ValueError):
            sa_score = None
        out.append(SAScoreResult(
            smiles=row.get("smiles") or "",
            row_number=i + 1,
            is_error=bool(row.get("isError")),
            sa_score=sa_score,
            feasibility_score=row.get("feasibilityScore"),
            remark=row.get("remark"),
            raw=row,
        ))
    return int(payload.get("totalCount") or 0), str(payload.get("progress") or ""), out


def _row_is_terminal(row: dict) -> bool:
    if row.get("isError"):
        return True
    sa = row.get("saScore")
    return sa not in (None, "")


def _request(method: str, url: str, body: dict | None, timeout: float) -> dict:
    if not settings.chemairs_api_key:
        raise ChemAIRSError("CHEMAIRS_API_KEY is not set")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(  # noqa: S310 - fixed scheme is the configured base URL
        url, data=data, method=method,
        headers={"X-Api-Key": settings.chemairs_api_key, "Content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise ChemAIRSError(f"HTTP {exc.code} from ChemAIRS: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ChemAIRSError(f"Could not reach ChemAIRS at {settings.chemairs_base_url}: {exc.reason}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ChemAIRSError(f"ChemAIRS response was not valid JSON: {raw[:200]!r}") from exc


def create_task(smiles_list: list[str], timeout: float = 30.0) -> str:
    if not settings.chemairs_configured:
        raise ChemAIRSError("ChemAIRS is not configured (CHEMAIRS_BASE_URL / CHEMAIRS_API_KEY unset)")
    body = build_create_payload(smiles_list)
    result = _request("POST", settings.chemairs_base_url + CREATE_PATH, body, timeout)
    # Documented response is a bare JSON string (the task id); be lenient about a wrapped form too.
    task_id = result if isinstance(result, str) else (result.get("taskId") or result.get("id"))
    if not task_id:
        raise ChemAIRSError(f"ChemAIRS did not return a task id: {result!r}")
    return task_id


def fetch_results(task_id: str, page_size: int, page_index: int = 1, timeout: float = 30.0) -> dict:
    url = settings.chemairs_base_url + RESULTS_PATH.format(task_id=task_id, page_size=page_size, page_index=page_index)
    return _request("GET", url, None, timeout)


def score_batch(smiles_list: list[str]) -> dict[int, SAScoreResult]:
    """Create one task for the whole batch, poll until every row is terminal, return
    {row_number (1-based, matches input order): SAScoreResult}.

    Raises ChemAIRSError on a batch-level problem (unreachable, bad key, malformed
    response). A molecule-level problem (isError=true for that row) is NOT an
    exception -- it comes back as a terminal SAScoreResult with is_error=True, for
    the caller to turn into a per-molecule failure (same convention as every other
    engine in this codebase).
    """
    if not smiles_list:
        return {}
    task_id = create_task(smiles_list)
    deadline = time.monotonic() + settings.chemairs_poll_timeout_seconds
    last_progress = ""
    while True:
        payload = fetch_results(task_id, page_size=len(smiles_list), page_index=1)
        total, progress, rows = parse_results_page(payload)
        last_progress = progress
        by_row = {r.row_number: r for r in rows}
        if rows and all(_row_is_terminal(r.raw) for r in rows):
            return by_row
        if time.monotonic() > deadline:
            raise ChemAIRSError(
                f"ChemAIRS SAScore task {task_id} did not finish within "
                f"{settings.chemairs_poll_timeout_seconds:.0f}s (last progress: {last_progress!r})"
            )
        time.sleep(settings.chemairs_poll_interval_seconds)

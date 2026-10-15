"""
BioSolveIT HPSee adapter: REST client, status probe and result parsing.

HPSee is BioSolveIT's compute server (API + workers + database, deployed with
Docker). This module talks to its REST API (OpenAPI document served by the
server at /swagger/v1/swagger.json; built against HPSee 2.4.0 "Electra"):

    POST /api/v1/IAM/Authenticate                    -> JWT (valid 8 h)
    GET  /api/v1/Licenses                            -> per-workflow licence state
    GET  /api/v1/ChemicalSpaces                      -> uploaded, prepared .space files
    POST /api/v1/ChemicalSpaceDockings/Create        -> C-S-D project for one space
    POST /api/v1/ChemicalSpaceDockings/Anchoring     -> dock + score the space's anchor synthons
    POST /api/v1/ChemicalSpaceDockings/Extension     -> extend up to 500 selected synthons
    POST /api/v1/Dockings/StandardInput              -> FlexX docking of an enumerated SDF library
    POST /api/v1/Scorings/StandardInput              -> HYDE scoring of docked poses
    GET  /api/v1/Workflows/{id}[/Errors]             -> status, per-task progress, errors
    POST /api/v1/Workflows/{id}/Errors/Retry         -> requeue failed, retryable jobs
    DELETE /api/v1/Workflows/{id}                    -> cancel (queued jobs only; running jobs finish)
    GET  /api/v1/MoleculeLibraries/{id}/Entries      -> result entries (id + SDF record)

VERIFIED against a licensed HPSee 2.4.0 (real runs, 2026-10-15; the records are
kept as tests/fixtures/hpsee_*.json):
  * authentication, licences, empty listings (HTTP 204), multipart submission;
  * docking: workflow = tasks Docking + Annotation; progress is reported per task
    and moves 0 -> 100 when a task finishes (it is not a fine-grained counter);
    10 poses per input molecule, titled `<input name>_NN`; score tag
    BIOSOLVEIT.DOCKING_SCORE; server-side sorting `Properties.<tag>:number asc`;
  * the Annotation task adds BIOSOLVEIT.USMILES and BIOSOLVEIT.NUMBER_OF_LINK_ATOMS
    (open synthon attachment points; 0 = complete molecule) to every entry;
  * scoring: the same HYDE tags as the hydescorer CLI, EXCEPT the per-atom scores
    (BIOSOLVEIT.HYDE_ATOM_SCORES) - so the "cumulative HYDE score" of the company's
    extractor cannot be computed for HPSee-scored poses; the vendor's estimated
    affinity range is used instead (and labelled as such).

NOT verified (no SeeSAR-exported definition files or .space file were available):
  * Chemical Space Docking anchoring/extension output. It is parsed with the same
    verified tags; link atoms fall back to the U/Np/Pu/Am pseudo-elements of
    BioSolveIT .space files if NUMBER_OF_LINK_ATOMS were ever missing.

The SeeSAR definition files (.flexx docking definition, .hydescorer scoring
definition) that Chemical Space Docking requires are passed through unchanged;
their format is undocumented and this module never parses or generates them.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import functools
import json
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Iterator

from ...config import settings
from ...logging_config import get_logger
from ...tools import proc
from .. import sdf as sdflib
from . import hyde as hydelib

log = get_logger("hpsee")

API = "/api/v1"
TAG_DOCKING_SCORE = "BIOSOLVEIT.DOCKING_SCORE"
TAG_LINK_ATOMS = "BIOSOLVEIT.NUMBER_OF_LINK_ATOMS"
TAG_USMILES = "BIOSOLVEIT.USMILES"
# BioSolveIT .space synthons mark open attachment points with these pseudo-elements.
LINKER_ELEMENTS = frozenset({"U", "Np", "Pu", "Am"})
MAX_EXTENSION_SELECTION = 500  # documented limit of /ChemicalSpaceDockings/Extension

# ProcessingStatus (documented enum)
UNKNOWN, PENDING, RUNNING, RUNNING_WITH_ERRORS, SUCCESSFUL, SUCCESSFUL_NO_RESULT, \
    FINISHED_WITH_ERRORS, FAILED, SKIPPED = range(9)
STATUS_TEXT = {
    UNKNOWN: "Unknown", PENDING: "Pending", RUNNING: "Running", RUNNING_WITH_ERRORS: "Running, some jobs failed",
    SUCCESSFUL: "Successful", SUCCESSFUL_NO_RESULT: "Successful, no results", FINISHED_WITH_ERRORS:
    "Finished with partial errors", FAILED: "Failed", SKIPPED: "Skipped",
}
TERMINAL = frozenset({SUCCESSFUL, SUCCESSFUL_NO_RESULT, FINISHED_WITH_ERRORS, FAILED, SKIPPED})
HAS_RESULTS = frozenset({SUCCESSFUL, FINISHED_WITH_ERRORS})

# ErrorCategory (documented enum)
ERROR_CATEGORY = {0: "No error", 2: "Unknown", 3: "Process crashed", 4: "Process was shut down",
                  5: "License error", 6: "Invalid or empty input", 7: "Internal command error",
                  8: "HPSee internal error"}
NON_RETRYABLE_CATEGORIES = frozenset({5, 6})

# Workflow names as GET /Licenses reports them.
WF_DOCKING = "Docking"
WF_SCORING = "Scoring"
WF_ANCHORING = "ChemicalSpaceDockingAnchoring"
WF_EXTENSION = "ChemicalSpaceDockingExtension"


class HPSeeError(proc.ToolError):
    """An HPSee call failed. `category` says why, in terms a user can act on."""

    def __init__(self, message: str, *, category: str = "rejected", status: int | None = None):
        super().__init__(message)
        self.category = category   # not_configured | unreachable | auth | license | rejected | not_found | protocol | workflow
        self.http_status = status


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------
def _multipart(fields: list[tuple[str, object]]) -> tuple[bytes, str]:
    """Encode multipart/form-data. A value is a str, or (filename, bytes) for a file part."""
    boundary = f"----mdp{uuid.uuid4().hex}"
    out = bytearray()
    for name, value in fields:
        out += f"--{boundary}\r\n".encode()
        if isinstance(value, tuple):
            filename, data = value
            safe = filename.replace('"', "")
            out += f'Content-Disposition: form-data; name="{name}"; filename="{safe}"\r\n'.encode()
            out += b"Content-Type: application/octet-stream\r\n\r\n"
            out += data
        else:
            out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            out += str(value).encode()
        out += b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def _message(body: bytes) -> str:
    """HPSee error bodies are a JSON string, ProblemDetails, or plain text."""
    text = body.decode("utf-8", errors="replace").strip()
    try:
        data = json.loads(text)
    except ValueError:
        return text[:1000]
    if isinstance(data, str):
        return data.strip()[:1000]
    if isinstance(data, dict):
        parts = [str(data.get(k)) for k in ("title", "detail") if data.get(k)]
        if isinstance(data.get("errors"), dict):
            parts += [f"{k}: {'; '.join(map(str, v))}" for k, v in data["errors"].items()]
        return " - ".join(parts)[:1000] or text[:1000]
    return text[:1000]


_TOKENS: dict[tuple[str, str], tuple[str, float]] = {}
_TOKEN_LOCK = threading.Lock()


class HPSeeClient:
    def __init__(self, base_url: str | None = None, username: str | None = None, password: str | None = None,
                 *, timeout: float | None = None, verify_tls: bool | None = None) -> None:
        self.base = (base_url or settings.hpsee_url or "").rstrip("/")
        self.username = username or settings.hpsee_username
        self._password = password or settings.hpsee_password
        if not (self.base and self.username and self._password):
            raise HPSeeError(
                "HPSee is not configured. Set HPSEE_URL, HPSEE_USERNAME and HPSEE_PASSWORD (see docs/HPSEE.md).",
                category="not_configured")
        self.timeout = timeout or settings.hpsee_request_timeout_seconds
        verify = settings.hpsee_verify_tls if verify_tls is None else verify_tls
        self._ssl = None if verify else ssl._create_unverified_context()  # noqa: S323 - explicit opt-in

    # -- low level -----------------------------------------------------------
    def _send(self, method: str, path: str, *, query: dict | None = None, body: bytes | None = None,
              content_type: str | None = None, auth: bool = True, timeout: float | None = None) -> tuple[int, bytes]:
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        headers = {"Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        if auth:
            headers["Authorization"] = f"Bearer {self._token()}"
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout, context=self._ssl) as resp:  # noqa: S310
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read() or b""
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise HPSeeError(f"HPSee at {self.base} is not reachable: {reason}", category="unreachable") from exc

    def _call(self, method: str, path: str, **kw) -> tuple[int, bytes]:
        status, data = self._send(method, path, **kw)
        if status == 401 and kw.get("auth", True):
            self._forget_token()  # expired or revoked: one fresh login, then give up
            status, data = self._send(method, path, **kw)
        if status in (200, 201, 202, 204):
            return status, data
        msg = _message(data) or f"HTTP {status}"
        if status == 401:
            raise HPSeeError(f"HPSee rejected the credentials: {msg}", category="auth", status=status)
        if status == 403:
            raise HPSeeError(f"The HPSee user '{self.username}' is not allowed to do this: {msg}",
                             category="auth", status=status)
        if status == 404:
            raise HPSeeError(f"Not found on HPSee: {msg}", category="not_found", status=status)
        if "licen" in msg.lower():
            raise HPSeeError(f"HPSee licence problem: {msg}", category="license", status=status)
        if status >= 500:
            raise HPSeeError(f"HPSee server error (HTTP {status}): {msg}", category="unreachable", status=status)
        raise HPSeeError(f"HPSee refused the request: {msg}", category="rejected", status=status)

    def _json(self, method: str, path: str, **kw):
        status, data = self._call(method, path, **kw)
        if status == 204 or not data.strip():
            return None
        try:
            return json.loads(data)
        except ValueError as exc:
            raise HPSeeError(f"HPSee returned something that is not JSON from {path}", category="protocol") from exc

    # -- authentication --------------------------------------------------------
    def _key(self) -> tuple[str, str]:
        return (self.base, self.username)

    def _forget_token(self) -> None:
        with _TOKEN_LOCK:
            _TOKENS.pop(self._key(), None)

    def _token(self) -> str:
        with _TOKEN_LOCK:
            cached = _TOKENS.get(self._key())
            if cached and cached[1] - time.time() > 300:
                return cached[0]
        body = json.dumps({"userName": self.username, "password": self._password}).encode()
        status, data = self._send("POST", f"{API}/IAM/Authenticate", body=body,
                                  content_type="application/json", auth=False)
        if status != 200:
            msg = _message(data) or f"HTTP {status}"
            raise HPSeeError(f"HPSee login failed for user '{self.username}': {msg}", category="auth", status=status)
        info = json.loads(data)
        exp = time.time() + 7.5 * 3600  # documented lifetime is 8 h
        try:
            exp = dt.datetime.fromisoformat(info["expiration"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError, AttributeError):
            pass
        with _TOKEN_LOCK:
            _TOKENS[self._key()] = (info["token"], exp)
        return info["token"]

    def login(self) -> None:
        self._forget_token()
        self._token()

    # -- system ------------------------------------------------------------------
    def version(self) -> dict:
        return self._json("GET", f"{API}/System/Version", auth=False) or {}

    def licenses(self) -> dict[str, dict]:
        rows = self._json("GET", f"{API}/Licenses") or []
        out = {}
        for r in rows:
            exp = (r.get("earliestExpirationDate") or "")[:10]
            # HPSee reports "0001-01-01" (DateTime.MinValue) when no key exists at all.
            out[r["workflow"]] = {"licensed": bool(r.get("isLicensed")),
                                  "expires": None if not exp or exp.startswith("0001") else exp}
        return out

    def entry_properties(self) -> list[dict]:
        return self._json("GET", f"{API}/Annotations/EntryProperties") or []

    # -- paging ------------------------------------------------------------------
    def _pages(self, path: str, *, page_size: int = 200, sorting: str = "", filters: str = "",
               limit: int | None = None) -> Iterator[dict]:
        page, seen = 1, 0
        while True:
            data = self._json("GET", path, query={"PageNumber": page, "PageSize": page_size,
                                                  "Sorting": sorting or None, "Filters": filters or None},
                              timeout=max(self.timeout, 300.0))
            if not data:
                return
            for item in data.get("items", []):
                yield item
                seen += 1
                if limit is not None and seen >= limit:
                    return
            if not data.get("pageInfo", {}).get("hasNextPage"):
                return
            page += 1

    # -- chemical spaces ------------------------------------------------------------
    def chemical_spaces(self) -> list[dict]:
        return list(self._pages(f"{API}/ChemicalSpaces", page_size=200))

    def chemical_space(self, space_id: str) -> dict:
        return self._json("GET", f"{API}/ChemicalSpaces/{space_id}")

    # -- chemical space docking ---------------------------------------------------
    def create_csd(self, space_id: str, description: str) -> dict:
        body = json.dumps({"chemicalSpaceId": space_id, "description": description}).encode()
        return self._json("POST", f"{API}/ChemicalSpaceDockings/Create", body=body, content_type="application/json")

    def recent_csds(self, limit: int = 400) -> list[dict]:
        return list(self._pages(f"{API}/ChemicalSpaceDockings", page_size=200, sorting="CreatedAt:date desc",
                                limit=limit))

    def csd(self, csd_id: str) -> dict:
        return self._json("GET", f"{API}/ChemicalSpaceDockings/{csd_id}")

    def start_anchoring(self, csd_id: str, docking_definition: Path, scoring_definition: Path, *,
                        description: str, filters: str = "") -> dict:
        fields: list[tuple[str, object]] = [
            ("ChemicalSpaceDockingId", csd_id),
            ("DockingDefinition", (docking_definition.name, docking_definition.read_bytes())),
            ("ScoringDefinition", (scoring_definition.name, scoring_definition.read_bytes())),
            ("Description", description),
        ]
        if filters:
            fields.append(("Filters", filters))
        body, ctype = _multipart(fields)
        return self._json("POST", f"{API}/ChemicalSpaceDockings/Anchoring", body=body, content_type=ctype,
                          timeout=max(self.timeout, 600.0))

    def start_extension(self, csd_id: str, entry_ids: list[int], docking_definition: Path,
                        scoring_definition: Path, *, description: str, filters: str = "") -> dict:
        if not 1 <= len(entry_ids) <= MAX_EXTENSION_SELECTION:
            raise HPSeeError(f"Select between 1 and {MAX_EXTENSION_SELECTION} synthons to extend.", category="rejected")
        fields: list[tuple[str, object]] = [("ChemicalSpaceDockingId", csd_id)]
        fields += [("SelectedMolecules", str(int(e))) for e in entry_ids]
        fields += [
            ("DockingDefinition", (docking_definition.name, docking_definition.read_bytes())),
            ("ScoringDefinition", (scoring_definition.name, scoring_definition.read_bytes())),
            ("Description", description),
        ]
        if filters:
            fields.append(("Filters", filters))
        body, ctype = _multipart(fields)
        return self._json("POST", f"{API}/ChemicalSpaceDockings/Extension", body=body, content_type=ctype,
                          timeout=max(self.timeout, 600.0))

    # -- enumerated-library docking ---------------------------------------------
    def start_docking(self, protein_pdb: Path, reference_ligand_sdf: Path, library_sdf: Path, *,
                      description: str) -> dict:
        body, ctype = _multipart([
            ("Protein", (protein_pdb.name, protein_pdb.read_bytes())),
            ("ReferenceLigand", (reference_ligand_sdf.name, reference_ligand_sdf.read_bytes())),
            ("Library", (library_sdf.name, library_sdf.read_bytes())),
            ("Description", description),
        ])
        return self._json("POST", f"{API}/Dockings/StandardInput", body=body, content_type=ctype,
                          timeout=max(self.timeout, 600.0))

    def start_scoring(self, protein_pdb: Path, reference_ligand_sdf: Path, poses_sdf: Path, *,
                      description: str) -> dict:
        body, ctype = _multipart([
            ("Protein", (protein_pdb.name, protein_pdb.read_bytes())),
            ("ReferenceLigand", (reference_ligand_sdf.name, reference_ligand_sdf.read_bytes())),
            ("Library", (poses_sdf.name, poses_sdf.read_bytes())),
            ("Description", description),
        ])
        return self._json("POST", f"{API}/Scorings/StandardInput", body=body, content_type=ctype,
                          timeout=max(self.timeout, 600.0))

    # -- workflows ------------------------------------------------------------------
    def workflow(self, workflow_id: str) -> dict:
        return self._json("GET", f"{API}/Workflows/{workflow_id}")

    def recent_workflows(self, limit: int = 200) -> list[dict]:
        return list(self._pages(f"{API}/Workflows", page_size=min(limit, 200), sorting="CreatedAt:date desc",
                                limit=limit))

    def workflow_errors(self, workflow_id: str, limit: int = 50) -> list[dict]:
        data = self._json("GET", f"{API}/Workflows/{workflow_id}/Errors",
                          query={"PageNumber": 1, "PageSize": limit})
        if not data:
            return []
        return (data.get("errors") or {}).get("items", [])

    def retry_workflow(self, workflow_id: str) -> None:
        self._call("POST", f"{API}/Workflows/{workflow_id}/Errors/Retry")

    def cancel_workflow(self, workflow_id: str) -> None:
        self._call("DELETE", f"{API}/Workflows/{workflow_id}", query={"deleteResults": "false"})

    # -- result libraries -----------------------------------------------------------
    def library(self, library_id: str) -> dict:
        return self._json("GET", f"{API}/MoleculeLibraries/{library_id}")

    def library_entries(self, library_id: str, *, limit: int, sorting: str = "") -> Iterator[dict]:
        return self._pages(f"{API}/MoleculeLibraries/{library_id}/Entries", page_size=min(limit, 1000),
                           sorting=sorting, limit=limit)


# ---------------------------------------------------------------------------
# Status probe (what /api/tools and the UI show)
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class HPSeeStatus:
    configured: bool
    reachable: bool = False
    authenticated: bool = False
    version: str | None = None
    licenses: dict = dataclasses.field(default_factory=dict)
    reason: str | None = None

    def licensed(self, *workflows: str) -> bool:
        return all(self.licenses.get(w, {}).get("licensed") for w in workflows)

    @property
    def docking_ready(self) -> bool:
        return self.authenticated and self.licensed(WF_DOCKING)

    @property
    def scoring_ready(self) -> bool:
        return self.authenticated and self.licensed(WF_SCORING)

    @property
    def csd_ready(self) -> bool:
        return self.authenticated and self.licensed(WF_ANCHORING, WF_EXTENSION)

    def public(self) -> dict:
        return {
            "key": "hpsee", "name": "BioSolveIT HPSee", "configured": self.configured, "reachable": self.reachable,
            "authenticated": self.authenticated, "version": self.version, "licenses": self.licenses,
            "library_docking_available": self.docking_ready, "scoring_available": self.scoring_ready,
            "chemical_space_docking_available": self.csd_ready,
            "available": self.docking_ready or self.csd_ready, "reason": self.reason,
        }


def _probe() -> HPSeeStatus:
    if not settings.hpsee_configured:
        return HPSeeStatus(False, reason="Not configured: set HPSEE_URL, HPSEE_USERNAME and HPSEE_PASSWORD.")
    st = HPSeeStatus(True)
    try:
        client = HPSeeClient(timeout=10.0)
        v = client.version()
        st.reachable = True
        st.version = v.get("version")
        client.login()
        st.authenticated = True
        st.licenses = client.licenses()
    except HPSeeError as exc:
        st.reason = str(exc)
        return st
    missing = [w for w in (WF_DOCKING, WF_SCORING, WF_ANCHORING, WF_EXTENSION) if not st.licensed(w)]
    if missing:
        st.reason = ("HPSee is connected, but these workflows are not licensed on it: " + ", ".join(missing)
                     + ". Upload a licence in the HPSee admin dashboard (Licenses page).")
    return st


@functools.lru_cache(maxsize=1)
def _cached(stamp: int) -> HPSeeStatus:
    return _probe()


def status(refresh: bool = False) -> HPSeeStatus:
    """Connection/licence state, cached for 60 s (each probe is three HTTP calls)."""
    if refresh:
        _cached.cache_clear()
    return _cached(int(time.time() // 60))


def require_status(*workflows: str) -> HPSeeStatus:
    st = status()
    if not st.configured:
        raise HPSeeError(st.reason or "HPSee is not configured.", category="not_configured")
    if not st.authenticated:
        raise HPSeeError(st.reason or "HPSee is not reachable.", category="unreachable")
    missing = [w for w in workflows if not st.licensed(w)]
    if missing:
        raise HPSeeError("HPSee has no valid licence for: " + ", ".join(missing)
                         + ". Upload a licence in the HPSee admin dashboard.", category="license")
    return st


# ---------------------------------------------------------------------------
# Result parsing
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class Entry:
    entry_id: int
    name: str
    smiles: str | None           # None when RDKit cannot read the record
    n_linkers: int               # open synthon attachment points (0 = complete molecule)
    docking_score: float | None
    hyde: hydelib.HydeScore | None
    record: str                  # the SDF record exactly as HPSee returned it
    note: str | None = None

    @property
    def complete(self) -> bool:
        return self.smiles is not None and self.n_linkers == 0

    def sort_key(self):
        """Best first: HYDE estimated affinity (lower bound, nM) when scored, then the docking score."""
        h = self.hyde.affinity_lower_nm if self.hyde else None
        return (h is None, h if h is not None else 0.0, self.docking_score is None,
                self.docking_score if self.docking_score is not None else 0.0, self.entry_id)


def parse_entry(entry_id: int, record_text: str) -> Entry:
    from rdkit import Chem

    text = record_text if record_text.rstrip().endswith("$$$$") else record_text.rstrip("\n") + "\n$$$$\n"
    try:
        recs = sdflib.parse_sdf(text)
    except sdflib.SdfError as exc:
        return Entry(entry_id, f"entry {entry_id}", None, 0, None, None, text, note=f"unreadable record: {exc}")
    if not recs:
        return Entry(entry_id, f"entry {entry_id}", None, 0, None, None, text, note="empty record")
    rec = recs[0]
    try:
        score = float(rec.props[TAG_DOCKING_SCORE])
    except (KeyError, ValueError):
        score = None
    hyde = None
    if any(k.startswith("BIOSOLVEIT.HYDE") for k in rec.props):
        parsed = hydelib.parse_hyde_output(rec.text)
        hyde = next(iter(parsed.values()), None)
    smiles, n_link, note = None, None, None
    try:
        n_link = int(float(rec.props[TAG_LINK_ATOMS]))   # HPSee's own annotation (verified)
    except (KeyError, ValueError):
        pass
    mol = Chem.MolFromMolBlock(rec.molblock, sanitize=False, removeHs=False)
    if mol is None:
        note = "RDKit could not read the structure"
    else:
        if n_link is None:
            n_link = sum(1 for a in mol.GetAtoms() if a.GetSymbol() in LINKER_ELEMENTS or a.GetAtomicNum() == 0)
        try:
            Chem.SanitizeMol(mol)
            smiles = Chem.MolToSmiles(Chem.RemoveHs(mol))
        except Exception as exc:  # noqa: BLE001 - one odd record must not sink a result set
            note = f"structure could not be sanitised: {exc.__class__.__name__}"
    if smiles is None and rec.props.get(TAG_USMILES):
        smiles, note = rec.props[TAG_USMILES].strip(), (note or "") + " (SMILES taken from HPSee's annotation)"
    n_link = n_link or 0
    return Entry(entry_id, rec.name or f"entry {entry_id}", smiles, n_link, score, hyde, rec.text, note)


def result_sorting(properties: list) -> str:
    """Server-side sort for a result library, using only properties the library says it has.

    Verified syntax: `Properties.BIOSOLVEIT.DOCKING_SCORE:number asc` (most negative = best first).
    """
    names = {(p.get("name") if isinstance(p, dict) else str(p)) for p in properties or []}
    if TAG_DOCKING_SCORE in names:
        return f"Properties.{TAG_DOCKING_SCORE}:number asc"
    return ""


def summarise_tasks(info: dict) -> list[dict]:
    """Per-task state exactly as HPSee reports it (type, status, progress %). No synthesis."""
    return [{"type": t.get("type"), "status": t.get("status"),
             "status_text": t.get("statusDescription") or STATUS_TEXT.get(t.get("status"), "?"),
             "progress": t.get("progress"), "result_size": (t.get("result") or {}).get("librarySize")}
            for t in info.get("taskInfos") or []]


def describe_errors(errors: list[dict]) -> tuple[str, bool]:
    """(human summary, retryable) from /Workflows/{id}/Errors items."""
    if not errors:
        return "", True
    cats = sorted({e.get("category", 2) for e in errors})
    text = "; ".join(sorted({(e.get("categoryDescription") or ERROR_CATEGORY.get(e.get("category"), "error"))
                             + (f": {e['messages'].strip()[:300]}" if e.get("messages") else "") for e in errors}))
    return f"{len(errors)} failed HPSee job(s) - {text}"[:1500], not any(c in NON_RETRYABLE_CATEGORIES for c in cats)

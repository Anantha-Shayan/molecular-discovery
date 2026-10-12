from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from . import models, schemas, storage
from .config import settings
from .database import check_connection, get_session, init_db
from .campaign.runner import runner
from .logging_config import configure_logging, get_logger
from .targets import service as target_service

configure_logging()
log = get_logger("api")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()  # no-op on PostgreSQL: Alembic owns the schema there
    log.info(
        "starting",
        extra={"status": f"env={settings.app_env} db={settings.safe_database_url}"},
    )
    # Campaigns execute in this process (one at a time). Anything left
    # running by a previous crash/restart is put back in the queue and
    # resumed from its saved batches; nothing finished is repeated.
    try:
        runner.start()
    except Exception as exc:  # noqa: BLE001 - never fatal: /ready reports DB problems
        log.warning("campaign runner failed to start: %s", exc.__class__.__name__)
    yield
    runner.stop()


_docs = settings.enable_api_docs
app = FastAPI(
    title="Molecular Discovery Platform — Integration MVP",
    lifespan=lifespan,
    docs_url="/docs" if _docs else None,
    redoc_url=None,
    openapi_url="/openapi.json" if _docs else None,
)

# The UI is served by this app (same origin), so CORS is only needed when a
# separate origin must call the API. With CORS_ORIGINS unset, no CORS
# headers are sent at all.
if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

# Slack allowed on top of the file limit for multipart framing.
_UPLOAD_OVERHEAD_BYTES = 1024 * 1024


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    # Reject oversized uploads from the Content-Length header before the
    # body is read at all.
    if request.method == "POST" and request.url.path == "/api/targets/upload":
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > settings.max_upload_bytes + _UPLOAD_OVERHEAD_BYTES:
            return JSONResponse(
                status_code=413,
                content={"detail": {
                    "message": f"File exceeds the {settings.max_upload_mb} MB upload limit.",
                    "checks": [],
                }},
            )
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    return response


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception):
    """Last-resort handler: log the traceback server-side, never send it out.

    The response carries only a request ID that can be matched to the log
    line. Internal exception text (which for database errors can include
    SQL, parameters and paths) is shown only when APP_ENV=development.
    """
    request_id = uuid.uuid4().hex[:12]
    log.error(
        "unhandled error on %s %s", request.method, request.url.path,
        exc_info=exc, extra={"request_id": request_id},
    )
    body = {"detail": "Internal server error", "request_id": request_id}
    if settings.expose_error_detail:
        body["error"] = f"{exc.__class__.__name__}: {exc}"
    return JSONResponse(status_code=500, content=body)


# ---------------------------------------------------------------------------
# Operational endpoints
# ---------------------------------------------------------------------------
@app.get("/health", include_in_schema=False)
def health():
    """Liveness: the process is up and serving. Touches nothing external."""
    return {"status": "ok"}


@app.get("/ready", include_in_schema=False)
def ready():
    """Readiness: the dependencies a request actually needs are available.

    Returns 503 until PostgreSQL answers and its schema is at the newest
    migration this build ships, so an orchestrator won't route traffic to
    an instance that would only return errors.
    """
    checks: dict[str, str] = {}

    try:
        check_connection()
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        log.warning("readiness: database unavailable (%s)", exc.__class__.__name__)
        checks["database"] = "unavailable"

    if checks["database"] == "ok" and not settings.is_sqlite:
        try:
            from . import migrations

            checks["schema"] = "current" if migrations.schema_is_current() else "outdated"
        except Exception as exc:  # noqa: BLE001
            log.warning("readiness: schema check failed (%s)", exc.__class__.__name__)
            checks["schema"] = "unknown"

    checks["data_dir"] = "writable" if storage.is_writable() else "not writable"
    checks["demo_fixture"] = (
        "present" if target_service.demo_fixture_path().exists() else "missing"
    )

    good = {"ok", "current", "writable", "present"}
    is_ready = all(value in good for value in checks.values())
    return JSONResponse(
        status_code=200 if is_ready else 503,
        content={"status": "ready" if is_ready else "not ready", "checks": checks},
    )



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _job_or_404(session, job_id: str) -> models.Job:
    job = session.get(models.Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _target_or_404(session, target_id: str) -> models.Target:
    target = session.get(models.Target, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Target not found")
    return target


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _target_response(target: models.Target) -> schemas.TargetResponse:
    return schemas.TargetResponse(
        target_id=target.id,
        name=target.name,
        source=target.source,
        pdb_id=target.pdb_id,
        original_filename=target.original_filename,
        structure_format=target.structure_format,
        checksum=target.checksum,
        file_size_bytes=target.file_size_bytes,
        title=target.title,
        experiment_method=target.experiment_method,
        resolution_a=target.resolution_a,
        chains=[schemas.ChainOut(**chain) for chain in (target.chains or [])],
        selected_chain=target.selected_chain,
        residue_count=target.residue_count,
        atom_count=target.atom_count,
        ligands=target.ligands or [],
        validation_status=target.validation_status,
        validation_checks=[
            schemas.ValidationCheckOut(**check) for check in (target.validation_checks or [])
        ],
        is_demo=bool(target.is_demo),
        created_at=_iso(target.created_at),
    )



# ---------------------------------------------------------------------------
# Target intake
# ---------------------------------------------------------------------------
async def _read_limited(file: UploadFile) -> bytes:
    """Read an upload in chunks, aborting as soon as it exceeds the limit."""
    limit = settings.max_upload_bytes
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            raise HTTPException(
                status_code=413,
                detail={
                    "message": f"File exceeds the {settings.max_upload_mb} MB upload limit.",
                    "checks": [],
                },
            )
        chunks.append(chunk)
    return b"".join(chunks)


@app.post("/api/targets/upload", response_model=schemas.TargetResponse, status_code=201)
async def upload_target(file: UploadFile = File(...), name: str | None = None):
    raw = await _read_limited(file)
    with get_session() as session:
        try:
            target = target_service.create_target(
                session, raw=raw, filename=file.filename, source="upload", name=name
            )
        except target_service.TargetIntakeError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"message": exc.message, "checks": exc.checks},
            ) from exc
        return _target_response(target)


@app.post("/api/targets/pdb-id", response_model=schemas.TargetResponse, status_code=201)
def create_target_from_pdb_id(req: schemas.PdbIdRequest):
    with get_session() as session:
        try:
            raw = target_service.fetch_pdb_by_id(req.pdb_id)
            target = target_service.create_target(
                session,
                raw=raw,
                filename=f"{req.pdb_id.strip().upper()}.pdb",
                source="pdb_id",
                name=req.name,
                pdb_id=req.pdb_id.strip().upper(),
            )
        except target_service.TargetIntakeError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"message": exc.message, "checks": exc.checks},
            ) from exc
        return _target_response(target)


@app.post("/api/targets/demo", response_model=schemas.TargetResponse, status_code=201)
def create_demo_target():
    """The bundled demo fixture. Deterministic and works offline."""
    with get_session() as session:
        try:
            target = target_service.create_demo_target(session)
        except target_service.TargetIntakeError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"message": exc.message, "checks": exc.checks},
            ) from exc
        return _target_response(target)


@app.get("/api/targets/{target_id}", response_model=schemas.TargetResponse)
def get_target(target_id: str):
    with get_session() as session:
        return _target_response(_target_or_404(session, target_id))


@app.get("/api/targets/{target_id}/structure")
def get_target_structure(target_id: str):
    """Raw structure text — powers the 3D viewer and 'View source'."""
    with get_session() as session:
        target = _target_or_404(session, target_id)
        try:
            text = target_service.read_structure_text(target)
        except target_service.TargetIntakeError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
        return Response(content=text, media_type="chemical/x-pdb")


from .api_jobs import router as jobs_router  # noqa: E402

app.include_router(jobs_router)


# ---------------------------------------------------------------------------
# Frontend (served by the API so the UI and API share an origin)
# ---------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def index():
    return RedirectResponse(url="/app/discoveries.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    path = settings.frontend_dir / "favicon.ico"
    if path.exists():
        return FileResponse(path)
    return Response(status_code=204)


if settings.frontend_dir.is_dir():
    app.mount("/app", StaticFiles(directory=settings.frontend_dir, html=True), name="frontend")

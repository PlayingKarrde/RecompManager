"""Inputs vault API. Inputs are the user's dumps and never leave without a token."""

from __future__ import annotations

import os
import queue
import tarfile
import threading
import uuid
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from recompmanager import events, storage
from recompmanager.api.games import get_game_or_404
from recompmanager.api.jobs import JobSummary, job_summary
from recompmanager.auth import Principal, require_admin, require_token
from recompmanager.db import get_session
from recompmanager.errors import ApiError, not_found
from recompmanager.inputs import (
    HASH,
    IMPORT,
    InputError,
    check_source,
    evaluate,
    input_path,
    recipes_of,
    source_roots,
)
from recompmanager.models import (
    Input,
    InputFile,
    InputKind,
    InputState,
    InputStorage,
    Upload,
)
from recompmanager.services import Services, get_services

router = APIRouter(tags=["inputs"])


# ---------------------------------------------------------------- schemas


class InputFit(BaseModel):
    recomp_id: str
    slot: str = Field(description="Which of the recipe's inputs this fills")
    status: Literal["verified", "unverified", "mismatch"]
    reason: str


class InputOut(BaseModel):
    id: int
    game_id: str
    name: str
    kind: InputKind
    storage: InputStorage = Field(description="vault: copied in; reference: read in place")
    state: InputState
    error: str | None
    size: int
    file_count: int
    sha1: str | None
    md5: str | None
    crc32: str | None
    sha256: str | None
    facts: dict = Field(description="What was learned: PS4 param.sfo, Switch program ID, ...")
    source: str
    hashed_at: datetime | None
    created_at: datetime
    fits: list[InputFit] = Field(description="How well this input suits each recomp of the game")


class ImportRequest(BaseModel):
    path: str = Field(description="Absolute path under /import or /roms")
    mode: Literal["copy", "move", "reference"] = Field(
        default="copy",
        description="reference reads the dump in place (no copy); move needs a writable share",
    )
    member: str | None = Field(default=None, description="Entry inside a .zip/.7z to take")
    extract: bool = Field(default=False, description="Unpack the whole archive into a folder")


class UploadCreate(BaseModel):
    filename: str = Field(min_length=1, max_length=300)
    size: int = Field(ge=0)


class UploadOut(BaseModel):
    id: str
    game_id: str
    filename: str
    size: int
    received: int


class ManifestFile(BaseModel):
    path: str
    size: int
    sha256: str | None


class Manifest(BaseModel):
    input_id: int
    name: str
    total_size: int
    files: list[ManifestFile]


class BrowseEntry(BaseModel):
    name: str
    path: str
    is_dir: bool
    size: int | None


class BrowseOut(BaseModel):
    roots: list[str]
    path: str | None
    parent: str | None
    entries: list[BrowseEntry]


# ---------------------------------------------------------------- helpers


def input_out(session: Session, svc: Services, inp: Input) -> InputOut:
    fits = []
    for recomp_id, recipe in recipes_of(svc, inp.game):
        v = evaluate(session, svc.env, inp, inp.game, recipe)
        if v is not None:
            fits.append(
                InputFit(recomp_id=recomp_id, slot=v.slot, status=v.status, reason=v.reason)
            )
    return InputOut(
        id=inp.id,
        game_id=inp.game_id,
        name=inp.name,
        kind=inp.kind,
        storage=inp.storage,
        state=inp.state,
        error=inp.error,
        size=inp.size,
        file_count=inp.file_count,
        sha1=inp.sha1,
        md5=inp.md5,
        crc32=inp.crc32,
        sha256=inp.sha256,
        facts=inp.facts or {},
        source=inp.source,
        hashed_at=inp.hashed_at,
        created_at=inp.created_at,
        fits=fits,
    )


def _input(session: Session, input_id: int) -> Input:
    inp = session.get(Input, input_id)
    if inp is None:
        raise not_found("Input")
    return inp


def _ready_path(svc: Services, inp: Input) -> Path:
    if inp.state != InputState.ready:
        raise ApiError(409, "not_ready", f"This input is {inp.state}")
    p = input_path(svc.env, inp)
    if not p.exists():
        raise ApiError(410, "missing", "The input's file or folder is gone. Rescan or remove it.")
    return p


# ---------------------------------------------------------------- listing and importing


@router.get(
    "/games/{game_id}/inputs", response_model=list[InputOut], summary="List a game's inputs"
)
def list_inputs(
    game_id: str,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_token),
) -> list[InputOut]:
    game = get_game_or_404(session, game_id)
    return [input_out(session, svc, i) for i in sorted(game.inputs, key=lambda i: i.id)]


@router.post(
    "/games/{game_id}/inputs/import",
    response_model=JobSummary,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Import a dump from /import or /roms",
)
def import_input(
    game_id: str,
    body: ImportRequest,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_admin),
) -> JobSummary:
    get_game_or_404(session, game_id)
    try:
        src = check_source(svc.env, body.path)
    except InputError as e:
        raise ApiError(422, "invalid_path", str(e)) from e
    if (body.member or body.extract) and src.suffix.lower() not in (".zip", ".7z"):
        raise ApiError(422, "not_an_archive", "member/extract need a .zip or .7z file")
    params = {"source": str(src), "mode": body.mode, "extract": body.extract}
    if body.member:
        params["member"] = body.member
    return job_summary(svc.jobs.enqueue(session, IMPORT, game_id=game_id, params=params))


@router.get(
    "/system/browse",
    response_model=BrowseOut,
    summary="Browse /import and /roms to pick something to import",
    tags=["system"],
)
def browse(
    path: str | None = None,
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_admin),
) -> BrowseOut:
    roots = source_roots(svc.env)
    if not path:
        return BrowseOut(
            roots=[str(p) for p in roots.values()],
            path=None,
            parent=None,
            entries=[
                BrowseEntry(name=n, path=str(p), is_dir=True, size=None) for n, p in roots.items()
            ],
        )
    try:
        p = check_source(svc.env, path)
    except InputError as e:
        raise ApiError(422, "invalid_path", str(e)) from e
    if not p.is_dir():
        raise ApiError(422, "not_a_folder", "That's a file, not a folder")
    entries = []
    with os.scandir(p) as it:
        for e in sorted(it, key=lambda e: (not e.is_dir(), e.name.lower())):
            if e.name.startswith("."):
                continue
            is_dir = e.is_dir()
            entries.append(
                BrowseEntry(
                    name=e.name,
                    path=str(Path(e.path)),
                    is_dir=is_dir,
                    size=None if is_dir else e.stat().st_size,
                )
            )
    is_root = any(p == r.resolve() for r in roots.values())
    return BrowseOut(
        roots=[str(r) for r in roots.values()],
        path=str(p),
        parent=None if is_root else str(p.parent),
        entries=entries,
    )


# ---------------------------------------------------------------- resumable upload


@router.post(
    "/games/{game_id}/uploads",
    response_model=UploadOut,
    status_code=status.HTTP_201_CREATED,
    summary="Start a resumable upload",
    description="Then PUT chunks to /uploads/{id}?offset=N (raw bytes), and POST "
    "/uploads/{id}/complete. GET /uploads/{id} says how much arrived, to resume. "
    "For large dumps, prefer importing from /import.",
)
def create_upload(
    game_id: str,
    body: UploadCreate,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_admin),
) -> UploadOut:
    get_game_or_404(session, game_id)
    name = PurePosixPath(body.filename.replace("\\", "/")).name
    if not name or name in (".", ".."):
        raise ApiError(422, "invalid_name", "Give a file name")
    free = storage.free_bytes(svc.env.data_dir)
    if body.size + 1024**3 > free:
        raise ApiError(507, "no_space", f"Not enough disk space ({free / 1024**3:.1f} GiB free)")
    up = Upload(id=uuid.uuid4().hex, game_id=game_id, filename=name, size=body.size, received=0)
    session.add(up)
    session.commit()
    _upload_path(svc, up.id).parent.mkdir(parents=True, exist_ok=True)
    _upload_path(svc, up.id).touch()
    return UploadOut(id=up.id, game_id=game_id, filename=name, size=up.size, received=0)


def _upload_path(svc: Services, upload_id: str) -> Path:
    return storage.resolve(svc.env.data_dir, f"inputs/.uploads/{upload_id}.part")


def _upload(session: Session, upload_id: str) -> Upload:
    up = session.get(Upload, upload_id)
    if up is None:
        raise not_found("Upload")
    return up


@router.get("/uploads/{upload_id}", response_model=UploadOut, summary="Upload progress")
def get_upload(
    upload_id: str,
    session: Session = Depends(get_session),
    _p: Principal = Depends(require_admin),
) -> UploadOut:
    up = _upload(session, upload_id)
    return UploadOut(
        id=up.id, game_id=up.game_id, filename=up.filename, size=up.size, received=up.received
    )


@router.put(
    "/uploads/{upload_id}",
    response_model=UploadOut,
    summary="Send a chunk",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
        }
    },
)
async def put_upload_chunk(
    upload_id: str,
    request: Request,
    offset: int = Query(ge=0, description="Must equal the bytes received so far"),
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_admin),
) -> UploadOut:
    up = _upload(session, upload_id)
    if offset != up.received:
        raise ApiError(
            409, "wrong_offset", f"Expected offset {up.received}", {"received": up.received}
        )
    path = _upload_path(svc, upload_id)
    n = up.received
    with path.open("r+b") as f:
        f.seek(n)
        async for chunk in request.stream():
            if n + len(chunk) > up.size:
                f.truncate(up.received)
                raise ApiError(413, "too_large", "More bytes than the upload's declared size")
            f.write(chunk)
            n += len(chunk)
    up.received = n
    session.commit()
    return UploadOut(
        id=up.id, game_id=up.game_id, filename=up.filename, size=up.size, received=up.received
    )


@router.post(
    "/uploads/{upload_id}/complete",
    response_model=JobSummary,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Finish an upload; the input is hashed in a job",
)
def complete_upload(
    upload_id: str,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_admin),
) -> JobSummary:
    up = _upload(session, upload_id)
    if up.received != up.size:
        raise ApiError(409, "incomplete", f"Only {up.received} of {up.size} bytes arrived")
    inp = Input(
        game_id=up.game_id,
        name=up.filename,
        kind=InputKind.file,
        storage=InputStorage.vault,
        path="",
        state=InputState.pending,
        source="upload",
        facts={},
    )
    session.add(inp)
    session.flush()
    rel = f"{storage.input_dir_rel(up.game_id, inp.id)}/{storage.safe_component(up.filename)}"
    storage.move(_upload_path(svc, upload_id), storage.resolve(svc.env.data_dir, rel))
    inp.path = rel
    session.delete(up)
    session.commit()
    svc.events.publish(events.GAME_UPDATED, {"id": inp.game_id})
    return job_summary(
        svc.jobs.enqueue(session, HASH, game_id=inp.game_id, params={"input_id": inp.id})
    )


@router.delete("/uploads/{upload_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Abandon")
def delete_upload(
    upload_id: str,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_admin),
) -> None:
    up = _upload(session, upload_id)
    _upload_path(svc, upload_id).unlink(missing_ok=True)
    session.delete(up)
    session.commit()


# ---------------------------------------------------------------- one input


@router.get("/inputs/{input_id}", response_model=InputOut, summary="Get an input")
def get_input(
    input_id: int,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_token),
) -> InputOut:
    return input_out(session, svc, _input(session, input_id))


@router.post(
    "/inputs/{input_id}/rehash",
    response_model=JobSummary,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Check an input again (re-hashes only what changed unless force)",
)
def rehash_input(
    input_id: int,
    force: bool = False,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_admin),
) -> JobSummary:
    inp = _input(session, input_id)
    return job_summary(
        svc.jobs.enqueue(
            session, HASH, game_id=inp.game_id, params={"input_id": inp.id, "force": force}
        )
    )


@router.delete(
    "/inputs/{input_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove an input",
    description="Deletes the vault copy. A referenced dump (under /roms or /import) is never "
    "touched; only the server's record of it goes.",
)
def delete_input(
    input_id: int,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_admin),
) -> None:
    inp = _input(session, input_id)
    if inp.state == InputState.pending:
        raise ApiError(409, "busy", "This input is still being imported or hashed")
    game_id = inp.game_id
    if inp.storage == InputStorage.vault:
        folder = storage.resolve(svc.env.data_dir, storage.input_dir_rel(game_id, inp.id))
        storage.remove_tree(folder)
        storage.remove_empty_dirs(folder.parent, storage.resolve(svc.env.data_dir, "inputs"))
    session.delete(inp)
    session.commit()
    svc.events.publish(events.GAME_UPDATED, {"id": game_id})


# ---------------------------------------------------------------- downloading


def _tar_stream(root: Path, files: list[str]) -> Iterator[bytes]:
    """Stream an uncompressed tar of ``files`` (relative to ``root``)."""
    q: queue.Queue[bytes | BaseException | None] = queue.Queue(maxsize=8)
    stop = threading.Event()

    class _Writer:
        def write(self, b: bytes) -> int:
            while not stop.is_set():
                try:
                    q.put(bytes(b), timeout=1)
                    return len(b)
                except queue.Full:
                    continue
            raise BrokenPipeError

    def produce() -> None:
        try:
            with tarfile.open(fileobj=_Writer(), mode="w|", format=tarfile.PAX_FORMAT) as t:
                for rel in files:
                    t.add(root / rel, arcname=rel, recursive=False)
            q.put(None)
        except BaseException as e:  # passed to the consumer
            if not stop.is_set():
                q.put(e)

    threading.Thread(target=produce, daemon=True).start()
    try:
        while True:
            item = q.get()
            if item is None:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        stop.set()


@router.get(
    "/inputs/{input_id}/download",
    summary="Download an input",
    description="A file input comes as-is (Range requests supported). A folder comes as an "
    "uncompressed tar stream; to resume or verify a large folder, use the manifest and "
    "fetch files one by one.",
    response_class=FileResponse,
    responses={
        200: {
            "content": {"application/octet-stream": {}, "application/x-tar": {}},
            "description": "The file, or a tar of the folder",
        },
        206: {"content": {"application/octet-stream": {}}, "description": "Part of the file"},
    },
)
def download_input(
    input_id: int,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_token),
):
    inp = _input(session, input_id)
    p = _ready_path(svc, inp)
    if inp.kind == InputKind.file:
        return FileResponse(
            p,
            filename=inp.name,
            media_type="application/octet-stream",
            headers={"ETag": f'"{inp.sha256}"'} if inp.sha256 else None,
        )
    files = [
        f.path
        for f in session.scalars(
            select(InputFile).where(InputFile.input_id == input_id).order_by(InputFile.path)
        )
    ]
    return StreamingResponse(
        _tar_stream(p, files),
        media_type="application/x-tar",
        headers={"Content-Disposition": f'attachment; filename="{inp.name}.tar"'},
    )


@router.get(
    "/inputs/{input_id}/manifest",
    response_model=Manifest,
    summary="Files of a folder input with sizes and sha256",
)
def input_manifest(
    input_id: int,
    session: Session = Depends(get_session),
    _p: Principal = Depends(require_token),
) -> Manifest:
    inp = _input(session, input_id)
    if inp.kind != InputKind.folder:
        raise ApiError(409, "not_a_folder", "Only folder inputs have a manifest")
    files = session.scalars(
        select(InputFile).where(InputFile.input_id == input_id).order_by(InputFile.path)
    ).all()
    return Manifest(
        input_id=inp.id,
        name=inp.name,
        total_size=sum(f.size for f in files),
        files=[ManifestFile(path=f.path, size=f.size, sha256=f.sha256) for f in files],
    )


@router.get(
    "/inputs/{input_id}/files/{file_path:path}",
    summary="Download one file of a folder input (Range supported)",
    response_class=FileResponse,
    responses={200: {"content": {"application/octet-stream": {}}, "description": "The file"}},
)
def download_input_file(
    input_id: int,
    file_path: str,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_token),
) -> FileResponse:
    inp = _input(session, input_id)
    root = _ready_path(svc, inp)
    row = session.get(InputFile, (input_id, file_path))
    if inp.kind != InputKind.folder or row is None:
        raise not_found("File")
    p = (root / file_path).resolve()
    if not p.is_relative_to(root.resolve()) or not p.is_file():
        raise not_found("File")
    return FileResponse(
        p,
        filename=PurePosixPath(file_path).name,
        media_type="application/octet-stream",
        headers={"ETag": f'"{row.sha256}"'} if row.sha256 else None,
    )

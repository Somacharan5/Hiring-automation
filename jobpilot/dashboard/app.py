"""JobPilot monitoring dashboard — FastAPI + Jinja2 on Neon (Postgres).

    from .dashboard.app import run
    run(host="127.0.0.1", port=8000)

Pages:  / scraped jobs · /applications applied progress · /insights funnel+stats
        /todos · /inbox replies · /health agent · /settings
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, urlencode

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import db
from . import queries
from .actions import (ActionError, add_todo, mark_notifications_read, mark_reply_read,
                      set_job_status, toggle_todo)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
SETTINGS_PATH = ROOT / "config" / "settings.yaml"

DEFAULT_CAPS: dict[str, Any] = {"email_cap": 20, "apply_gate": 60, "dry_run": True}


def load_caps(settings_path: Path = SETTINGS_PATH) -> dict[str, Any]:
    caps = dict(DEFAULT_CAPS)
    try:
        import yaml
        data = yaml.safe_load(settings_path.read_text()) or {}
    except Exception:  # noqa: BLE001
        return caps
    outreach = data.get("outreach") or {}
    matching = data.get("matching") or {}
    caps["email_cap"] = outreach.get("daily_send_cap", caps["email_cap"])
    caps["apply_gate"] = matching.get("shortlist_threshold", caps["apply_gate"])
    caps["dry_run"] = bool(outreach.get("dry_run", True))
    caps["first_run_draft_only"] = bool(outreach.get("first_run_draft_only", True))
    caps["mode"] = outreach.get("mode", "autonomous")
    return caps


def dashboard_settings(settings_path: Path = SETTINGS_PATH) -> dict[str, Any]:
    host, port = "127.0.0.1", 8000
    try:
        import yaml
        data = yaml.safe_load(settings_path.read_text()) or {}
        cfg = data.get("dashboard") or {}
        host = str(cfg.get("host", host)); port = int(cfg.get("port", port))
    except Exception:  # noqa: BLE001
        pass
    return {"host": host, "port": port}


# ── Jinja filters ────────────────────────────────────────────────────

def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fmt_relative(value: Any) -> str:
    dt = _parse_ts(value)
    if dt is None:
        return "—"
    delta = (datetime.now(timezone.utc) - dt).total_seconds()
    suffix = "ago"
    if delta < 0:
        delta, suffix = abs(delta), "from now"
    for limit, div, unit in ((60, 1, "s"), (3600, 60, "m"), (86400, 3600, "h"), (604800, 86400, "d")):
        if delta < limit:
            n = int(delta // div)
            return "just now" if unit == "s" and n < 30 else f"{n}{unit} {suffix}"
    return dt.strftime("%d %b %Y")


def fmt_datetime(value: Any) -> str:
    dt = _parse_ts(value)
    return dt.strftime("%d %b %Y, %H:%M UTC") if dt else "—"


def fmt_date(value: Any) -> str:
    dt = _parse_ts(value)
    return dt.strftime("%d %b") if dt else "—"


def fmt_num(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "—"


def titlecase(value: Any) -> str:
    text = str(value or "").replace("_", " ").replace(":", " · ").strip()
    return text[:1].upper() + text[1:] if text else "—"


def paragraphs(value: Any) -> list[str]:
    if not value:
        return []
    chunks = [c.strip() for c in str(value).replace("\r\n", "\n").split("\n")]
    return [c for c in chunks if c]


# ── App factory ──────────────────────────────────────────────────────

def create_app(settings_path: Path | None = None) -> FastAPI:
    cfg_file = Path(settings_path) if settings_path else SETTINGS_PATH

    app = FastAPI(title="JobPilot Dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters.update(
        relative=fmt_relative, datetime=fmt_datetime, date=fmt_date,
        num=fmt_num, titlecase=titlecase, paragraphs=paragraphs)
    templates.env.globals.update(
        band_label=queries.band_label, SCORE_BANDS=queries.SCORE_BANDS,
        MANUAL_STATUSES=queries.MANUAL_STATUSES,
        APPLICATION_STATUSES=queries.APPLICATION_STATUSES)

    def get_conn() -> Iterator[psycopg.Connection]:
        conn = db.connect()
        try:
            yield conn
        finally:
            conn.close()

    def page(request: Request, name: str, conn: psycopg.Connection, **ctx: Any) -> HTMLResponse:
        base = {
            "request": request, "caps": load_caps(cfg_file),
            "nav": ctx.pop("nav", ""), "now": datetime.now(timezone.utc),
            "bell": queries.bell(conn),
        }
        base.update(ctx)
        return templates.TemplateResponse(request, name, base)

    async def read_body(request: Request) -> dict[str, str]:
        raw = await request.body()
        if not raw:
            return {}
        ctype = (request.headers.get("content-type") or "").lower()
        if "json" in ctype:
            try:
                data = json.loads(raw.decode("utf-8"))
            except ValueError:
                return {}
            return {str(k): ("" if v is None else str(v)) for k, v in data.items()} \
                if isinstance(data, dict) else {}
        parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
        return {k: v[0] for k, v in parsed.items()}

    def wants_json(request: Request) -> bool:
        return ("application/json" in (request.headers.get("accept") or "")
                or request.headers.get("x-requested-with") == "fetch")

    def _respond(request: Request, result: dict[str, Any], fallback: str):
        if wants_json(request):
            return JSONResponse(result)
        return RedirectResponse(request.headers.get("referer") or fallback, status_code=303)

    def _guard(request: Request, fn, *args):
        try:
            return fn(*args)
        except ActionError as exc:
            if wants_json(request):
                return JSONResponse({"error": exc.message}, status_code=exc.status_code)
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    # ── Pages ────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    def scraped(request: Request, status: str = Query(""), source: str = Query(""),
                country: str = Query(""), min_score: str = Query(""), remote: str = Query(""),
                q: str = Query(""), sort: str = Query("collected"), dir: str = Query("desc"),
                conn: psycopg.Connection = Depends(get_conn)):
        try:
            min_score_val = int(min_score) if str(min_score).strip() else None
        except ValueError:
            min_score_val = None
        remote_only = str(remote).lower() in {"1", "true", "on", "yes"}
        rows = queries.search_jobs(conn, status=status, source=source, country=country,
                                   min_score=min_score_val, remote_only=remote_only,
                                   q=q.strip(), sort=sort, direction=dir)
        filters = {"status": status, "source": source, "country": country,
                   "min_score": min_score if min_score_val is not None else "",
                   "remote": "on" if remote_only else "", "q": q.strip(), "sort": sort, "dir": dir}

        def sort_url(column: str) -> str:
            params = {k: v for k, v in filters.items() if v}
            params["sort"] = column
            params["dir"] = "asc" if (sort == column and dir == "desc") else "desc"
            return "/?" + urlencode(params)

        return page(request, "jobs.html", conn, nav="scraped", jobs=rows,
                    buckets=queries.job_buckets(conn), options=queries.job_filter_options(conn),
                    filters=filters, active_filters={k: v for k, v in filters.items()
                                                     if v and k not in {"sort", "dir"}},
                    sort_url=sort_url)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_detail(request: Request, job_id: str, conn: psycopg.Connection = Depends(get_conn)):
        detail = queries.job_detail(conn, job_id)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"No job {job_id}")
        return page(request, "job_detail.html", conn, nav="scraped", **detail)

    @app.get("/applications", response_class=HTMLResponse)
    def applications(request: Request, status: str = Query(""),
                     conn: psycopg.Connection = Depends(get_conn)):
        return page(request, "applications.html", conn, nav="applications",
                    applications=queries.all_applications(conn, status=status),
                    summary=queries.application_summary(conn), filters={"status": status})

    @app.get("/insights", response_class=HTMLResponse)
    def insights(request: Request, conn: psycopg.Connection = Depends(get_conn)):
        return page(request, "insights.html", conn, nav="insights",
                    stats=queries.overview(conn, load_caps(cfg_file)))

    @app.get("/todos", response_class=HTMLResponse)
    def todos(request: Request, conn: psycopg.Connection = Depends(get_conn)):
        return page(request, "todos.html", conn, nav="todos",
                    todos=queries.todos(conn), done=queries.todos(conn, include_done=True))

    @app.get("/inbox", response_class=HTMLResponse)
    def inbox(request: Request, conn: psycopg.Connection = Depends(get_conn)):
        return page(request, "inbox.html", conn, nav="inbox", replies=queries.inbox(conn))

    @app.get("/health", response_class=HTMLResponse)
    def health_page(request: Request, conn: psycopg.Connection = Depends(get_conn)):
        return page(request, "health.html", conn, nav="health", health=queries.health(conn),
                    caps=load_caps(cfg_file))

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, conn: psycopg.Connection = Depends(get_conn)):
        import yaml
        raw = yaml.safe_load(cfg_file.read_text()) or {}
        return page(request, "settings.html", conn, nav="settings", cfg=raw)

    # ── JSON ─────────────────────────────────────────────────────────

    @app.get("/api/bell")
    def api_bell(conn: psycopg.Connection = Depends(get_conn)):
        return JSONResponse(_jsonable(queries.bell(conn)))

    @app.get("/api/health")
    def api_health(conn: psycopg.Connection = Depends(get_conn)):
        return {"ok": True, "jobs": queries._scalar(conn, "SELECT COUNT(*) FROM jobs")}

    # ── Writes (no sending, ever) ────────────────────────────────────

    @app.post("/jobs/{job_id}/status")
    async def override_status(request: Request, job_id: str,
                              conn: psycopg.Connection = Depends(get_conn)):
        body = await read_body(request)
        result = _guard(request, set_job_status, conn, job_id,
                        body.get("status", ""), body.get("reason") or body.get("reject_reason"))
        return result if isinstance(result, JSONResponse) else _respond(request, result, f"/jobs/{job_id}")

    @app.post("/todos")
    async def create_todo(request: Request, conn: psycopg.Connection = Depends(get_conn)):
        b = await read_body(request)
        result = _guard(request, lambda: add_todo(
            conn, company=b.get("company", ""), title=b.get("title", ""),
            portal_url=b.get("portal_url", ""), note=b.get("note", ""),
            job_id=b.get("job_id") or None))
        return result if isinstance(result, JSONResponse) else _respond(request, result, "/todos")

    @app.post("/todos/{todo_id}/toggle")
    async def flip_todo(request: Request, todo_id: int,
                        conn: psycopg.Connection = Depends(get_conn)):
        b = await read_body(request)
        done = str(b.get("done", "true")).lower() in {"1", "true", "on", "yes"}
        result = _guard(request, toggle_todo, conn, todo_id, done)
        return result if isinstance(result, JSONResponse) else _respond(request, result, "/todos")

    @app.post("/notifications/read")
    async def clear_bell(request: Request, conn: psycopg.Connection = Depends(get_conn)):
        return _respond(request, mark_notifications_read(conn), "/")

    @app.post("/replies/{reply_id}/read")
    async def read_reply(request: Request, reply_id: int,
                         conn: psycopg.Connection = Depends(get_conn)):
        return _respond(request, mark_reply_read(conn, reply_id), "/inbox")

    # ── Errors ───────────────────────────────────────────────────────

    @app.exception_handler(404)
    async def not_found(request: Request, exc: Any):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "not found"}, status_code=404)
        return templates.TemplateResponse(request, "error.html",
            {"request": request, "caps": load_caps(cfg_file), "nav": "", "bell": {"unread": 0, "items": []},
             "code": 404, "message": getattr(exc, "detail", "Page not found")}, status_code=404)

    return app


def _jsonable(obj: Any) -> Any:
    """Make datetimes/rows JSON-serialisable for the bell API."""
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def run(host: str = "127.0.0.1", port: int = 8000, *, reload: bool = False) -> None:
    import uvicorn
    uvicorn.run(create_app(), host=host, port=port, reload=reload, log_level="info")


if __name__ == "__main__":  # pragma: no cover
    cfg = dashboard_settings()
    run(cfg["host"], cfg["port"])

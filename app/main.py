"""HTTP API (FastAPI).

Every endpoint except health/login requires `Authorization: Bearer <JWT>`.
Interactive API docs: /docs. The Streamlit frontend in `frontend/` is the UI.
"""
from __future__ import annotations

import logging
import uuid

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from .auth import AuthError
from .limits import RateLimitExceeded
from .logging_setup import request_id, setup_logging
from .security import AuthorizationError, Principal
from .service import HRCopilotService

setup_logging()
log = logging.getLogger("hrcopilot.api")
service = HRCopilotService()
app = FastAPI(title="HR Copilot API", version="1.0.0",
              description="Governed, agentic HR operations assistant. Authenticate via /api/auth/login (demo).")
app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in service.settings.cors_origins.split(",")],
                   allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"])
bearer = HTTPBearer(auto_error=False)


@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
    request_id.set(rid)
    if int(request.headers.get("content-length") or 0) > 64_000:
        return JSONResponse({"detail": "Request too large"}, status_code=413)
    response = await call_next(request)
    response.headers.update({"X-Request-ID": rid, "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                             "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})
    return response


@app.exception_handler(AuthError)
async def _auth_error(_, exc: AuthError):
    return JSONResponse({"detail": str(exc)}, status_code=401)


@app.exception_handler(AuthorizationError)
async def _forbidden(_, exc: AuthorizationError):
    return JSONResponse({"detail": str(exc)}, status_code=403)


@app.exception_handler(RateLimitExceeded)
async def _rate(_, exc: RateLimitExceeded):
    return JSONResponse({"detail": str(exc)}, status_code=429, headers={"Retry-After": "60"})


def current_principal(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Principal:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(401, "Missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    return service.principal(creds.credentials)


class LoginIn(BaseModel):
    employee_id: str = Field(min_length=2, max_length=20)
    passcode: str | None = Field(None, max_length=200)


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str = Field(min_length=1, max_length=100, pattern=r"^[\w-]+$")


class DecisionIn(BaseModel):
    approve: bool
    comment: str | None = Field(None, max_length=500)


@app.get("/health")
def health():
    return service.health()


@app.get("/ready")
def ready():
    return service.ready()


@app.get("/metrics", response_class=PlainTextResponse)
def prometheus():
    return service.c.metrics.prometheus()


@app.get("/api/personas")
def personas():
    return service.personas()


@app.post("/api/auth/login")
def login(body: LoginIn):
    return service.login(body.employee_id, body.passcode)


@app.get("/api/auth/me")
def me(p: Principal = Depends(current_principal)):
    return service.whoami(p)


@app.post("/api/chat")
def chat(body: ChatIn, p: Principal = Depends(current_principal)):
    return service.chat(p, body.session_id, body.message)


@app.post("/api/chat/{session_id}/reset")
def new_chat(session_id: str, p: Principal = Depends(current_principal)):
    service.new_chat(p, session_id)
    return {"status": "ok"}


@app.get("/api/actions")
def actions(p: Principal = Depends(current_principal)):
    return {"actions": service.actions(p)}


@app.post("/api/actions/{pid}/decide")
def decide(pid: int, body: DecisionIn, p: Principal = Depends(current_principal)):
    try:
        return service.decide(p, pid, body.approve, body.comment)
    except KeyError:
        raise HTTPException(404, "Proposal not found") from None


@app.get("/api/audit")
def audit(p: Principal = Depends(current_principal)):
    return service.audit(p)


@app.get("/api/metrics")
def metrics(p: Principal = Depends(current_principal)):
    return service.metrics()


@app.post("/api/reset")
def reset(p: Principal = Depends(current_principal)):
    service.reset(p)
    return {"status": "reset"}

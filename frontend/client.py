"""Frontend client: the ONLY way the Streamlit UI talks to HR Copilot.

* HttpClient  - calls the FastAPI backend (production layout: UI and API deployed
                separately; set API_URL).
* LocalClient - calls the same service layer in-process (self-contained live
                demo on Streamlit Community Cloud). Each visitor gets an isolated
                in-memory sandbox, so one visitor's actions never affect another's.

Both expose identical methods and both authenticate with a JWT, so the UI code
is the same and contains no business logic or authority of its own.
"""
from __future__ import annotations

from typing import Any


class ClientError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class LocalClient:
    kind = "in-process"

    def __init__(self) -> None:
        from app.config import Settings
        from app.service import HRCopilotService
        self.svc = HRCopilotService(Settings(), db_path=":memory:")

    def _wrap(self, fn, *a):
        from app.auth import AuthError
        from app.limits import RateLimitExceeded
        from app.security import AuthorizationError
        try:
            return fn(*a)
        except AuthError as exc:
            raise ClientError(str(exc), 401) from exc
        except AuthorizationError as exc:
            raise ClientError(str(exc), 403) from exc
        except RateLimitExceeded as exc:
            raise ClientError(str(exc), 429) from exc
        except KeyError as exc:
            raise ClientError(f"Not found: {exc}", 404) from exc

    def _p(self, token: str):
        return self.svc.principal(token)

    def health(self) -> dict:
        return self.svc.health()

    def personas(self) -> dict:
        return self.svc.personas()

    def login(self, employee_id: str, passcode: str | None) -> dict:
        return self._wrap(self.svc.login, employee_id, passcode)

    def chat(self, token: str, session_id: str, message: str) -> dict:
        return self._wrap(lambda: self.svc.chat(self._p(token), session_id, message))

    def new_chat(self, token: str, session_id: str) -> None:
        self._wrap(lambda: self.svc.new_chat(self._p(token), session_id))

    def actions(self, token: str) -> list[dict]:
        return self._wrap(lambda: self.svc.actions(self._p(token)))

    def decide(self, token: str, pid: int, approve: bool, comment: str | None = None) -> dict:
        return self._wrap(lambda: self.svc.decide(self._p(token), pid, approve, comment))

    def audit(self, token: str) -> dict:
        return self._wrap(lambda: self.svc.audit(self._p(token)))

    def metrics(self, token: str) -> dict:
        return self._wrap(lambda: (self._p(token), self.svc.metrics())[1])

    def reset(self, token: str) -> None:
        self._wrap(lambda: self.svc.reset(self._p(token)))


class HttpClient:
    kind = "http"

    def __init__(self, base_url: str) -> None:
        import requests
        self.base = base_url.rstrip("/")
        self.http = requests.Session()

    def _req(self, method: str, path: str, token: str | None = None, **kw) -> Any:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            r = self.http.request(method, self.base + path, headers=headers, timeout=180, **kw)
        except Exception as exc:  # network errors -> friendly message
            raise ClientError(f"Cannot reach the HR Copilot API ({exc.__class__.__name__}).", 503) from exc
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise ClientError(str(detail), r.status_code)
        return r.json()

    def health(self) -> dict:
        return self._req("GET", "/health")

    def personas(self) -> dict:
        return self._req("GET", "/api/personas")

    def login(self, employee_id: str, passcode: str | None) -> dict:
        return self._req("POST", "/api/auth/login", json={"employee_id": employee_id, "passcode": passcode})

    def chat(self, token: str, session_id: str, message: str) -> dict:
        return self._req("POST", "/api/chat", token, json={"message": message, "session_id": session_id})

    def new_chat(self, token: str, session_id: str) -> None:
        self._req("POST", f"/api/chat/{session_id}/reset", token)

    def actions(self, token: str) -> list[dict]:
        return self._req("GET", "/api/actions", token)["actions"]

    def decide(self, token: str, pid: int, approve: bool, comment: str | None = None) -> dict:
        return self._req("POST", f"/api/actions/{pid}/decide", token, json={"approve": approve, "comment": comment})

    def audit(self, token: str) -> dict:
        return self._req("GET", "/api/audit", token)

    def metrics(self, token: str) -> dict:
        return self._req("GET", "/api/metrics", token)

    def reset(self, token: str) -> None:
        self._req("POST", "/api/reset", token)

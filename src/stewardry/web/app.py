"""The Stewardry web app (ADR 0005): sign-in, sessions, and role-restricted pages.

Every action uses the signed-in session's hub subject. Form fields never name
a user. Authorization decisions stay in the platform services (`grants`,
`access`), which re-read roles and customer assignments from the database on
every request; the session's copy is only for display. What each role sees is
built by the view models in `views.py`. Refused page views are audited.
"""

import datetime as dt
import hmac
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine, select
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from stewardry.engagements.questionnaire import review
from stewardry.engagements.questionnaire.ingest import IngestRefused, ingest_questionnaire
from stewardry.engagements.questionnaire.parse import MAX_BYTES
from stewardry.platform import access, grants
from stewardry.platform.artifacts import ArtifactStore
from stewardry.platform.audit import AuditEvent, AuditLog, record_now
from stewardry.platform.credentials import CredentialStore, GatewayCredential
from stewardry.platform.db.schema import task_grants, tasks, utcnow
from stewardry.platform.roles import active_roles
from stewardry.web import views
from stewardry.web.hub import HubClient, HubError, HubRejected, HubUnavailable
from stewardry.web.sessions import Session, SessionStore
from stewardry.web.settings import WebSettings

SESSION_COOKIE = "stewardry_session"
LOGIN_COOKIE = "stewardry_login"
REFRESH_MARGIN = dt.timedelta(seconds=60)
HERE = Path(__file__).parent

SECURITY_HEADERS = {
    # No referrer, so the authorization code in the callback URL cannot leak.
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
}


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response


@dataclass
class Services:
    settings: WebSettings
    engine: Engine
    hub: HubClient
    log: AuditLog
    sessions: SessionStore
    credentials: CredentialStore
    artifacts: ArtifactStore


class Problem(Exception):
    """An error page with a status code. The message is shown to the user."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def create_app(services: Services) -> FastAPI:
    app = FastAPI(title="Stewardry", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SecurityHeaders)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    settings = services.settings

    def page(request: Request, name: str, status: int = 200, **context: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, context, status_code=status)

    @app.exception_handler(Problem)
    async def problem_page(request: Request, exc: Problem) -> HTMLResponse:
        return page(request, "problem.html", exc.status, message=exc.message, session=None)

    def set_cookie(response: Response, name: str, value: str, max_age: int | None) -> None:
        response.set_cookie(
            name,
            value,
            max_age=max_age,
            httponly=True,
            samesite="lax",
            secure=settings.secure_cookies,
            path="/",
        )

    # ------------------------------------------------------- session access

    def refresh_credential(session: Session) -> None:
        """Renew the gateway credential within the hub session (G6).

        A refused refresh means the hub session ended: the credential is
        dropped and work that needs it pauses. An outage keeps the current
        credential and tries again on a later request.
        """
        now = utcnow()
        token = services.credentials.needs_refresh(session.session_id, now, REFRESH_MARGIN)
        if token is None:
            return
        try:
            renewed = services.hub.refresh(token)
        except HubUnavailable:
            return
        except HubRejected as exc:
            services.credentials.remove_session(session.session_id)
            record_now(
                services.engine,
                services.log,
                AuditEvent(
                    "auth.credential_refreshed",
                    "failed",
                    actor_sub=session.sub,
                    details={"reason": str(exc), "credential_dropped": True},
                ),
            )
            return
        services.credentials.put(
            session.session_id,
            session.sub,
            GatewayCredential(renewed.access_token, renewed.expires_at),
            renewed.refresh_token or token,
        )

    def optional_session(request: Request) -> Session | None:
        session = services.sessions.get(request.cookies.get(SESSION_COOKIE), utcnow())
        if session is not None:
            refresh_credential(session)
        return session

    def required_session(
        session: Annotated[Session | None, Depends(optional_session)],
    ) -> Session:
        if session is None:
            raise Problem(401, "Please sign in first.")
        return session

    def checked_form(
        request: Request,
        session: Annotated[Session, Depends(required_session)],
        csrf_token: Annotated[str, Form()] = "",
    ) -> Session:
        """Every state-changing request: a session, the same origin, and its CSRF token."""
        origin = request.headers.get("origin")
        if origin is not None and origin != settings.origin:
            raise Problem(403, "This request came from another site and was refused.")
        if not hmac.compare_digest(csrf_token, session.csrf_token):
            raise Problem(403, "This form has expired. Reload the page and try again.")
        return session

    SessionDep = Annotated[Session, Depends(checked_form)]

    # -------------------------------------------------------------- routes

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.get("/", response_class=HTMLResponse)
    def home(
        request: Request, session: Annotated[Session | None, Depends(optional_session)]
    ) -> HTMLResponse:
        if session is None:
            return page(request, "signed_out.html", session=None)
        with services.engine.connect() as conn:
            mine = conn.execute(
                select(tasks)
                .where(tasks.c.requester_sub == session.sub)
                .order_by(tasks.c.created_at.desc())
            ).all()
            customers = access.assigned_customers(conn, session.sub)
            roles = active_roles(conn, session.sub)
            reviewable: list[Any] = []
            my_grants: list[Any] = []
            in_review: list[Any] = []
            if "grc" in roles:
                in_review = views.reviewable_tasks(conn, session.sub, customers)
                reviewable = list(
                    conn.execute(
                        select(tasks)
                        .where(
                            tasks.c.customer_id.in_(customers),
                            tasks.c.status.in_(grants.AUTHORIZABLE_STATUSES),
                            tasks.c.requester_sub != session.sub,
                        )
                        .order_by(tasks.c.created_at)
                    ).all()
                )
                my_grants = [
                    g
                    for g in conn.execute(
                        select(task_grants).where(
                            task_grants.c.authorizing_sub == session.sub,
                            task_grants.c.revoked_at.is_(None),
                        )
                    ).all()
                    if g.expires_at > utcnow()
                ]
        return page(
            request,
            "home.html",
            session=session,
            roles=roles,
            customers=sorted(customers),
            my_tasks=mine,
            reviewable=reviewable,
            my_grants=my_grants,
            in_review=in_review,
            show_registry=bool(roles & views.REGISTRY_ROLES),
            submission_key=uuid.uuid4().hex,
        )

    @app.get("/login")
    def login() -> RedirectResponse:
        login_id, pending = services.sessions.start_login(utcnow())
        try:
            target = services.hub.authorization_url(pending.state, pending.nonce, pending.pkce)
        except HubUnavailable as exc:
            raise Problem(503, "The sign-in service is unavailable. Try again shortly.") from exc
        except HubError as exc:
            raise Problem(500, "Sign-in is misconfigured. Tell the platform engineer.") from exc
        response = RedirectResponse(target, status_code=303)
        set_cookie(response, LOGIN_COOKIE, login_id, max_age=600)
        return response

    @app.get("/auth/callback")
    def callback(
        request: Request, code: str = "", state: str = "", error: str = ""
    ) -> RedirectResponse:
        pending = services.sessions.finish_login(request.cookies.get(LOGIN_COOKIE), utcnow())

        def refuse(status: int, message: str, reason: str) -> Problem:
            record_now(
                services.engine,
                services.log,
                AuditEvent("auth.sign_in", "denied", details={"reason": reason}),
            )
            return Problem(status, message)

        if pending is None:
            raise refuse(
                400,
                "This sign-in link has expired or was already used. Sign in again.",
                "no pending sign-in (expired, replayed, or another browser)",
            )
        if not hmac.compare_digest(state, pending.state):
            raise refuse(
                400, "This sign-in could not be verified. Sign in again.", "state mismatch"
            )
        if error or not code:
            raise refuse(
                400,
                "The sign-in service did not complete the sign-in.",
                f"hub returned error {error!r}" if error else "no code",
            )
        try:
            tokens = services.hub.exchange_code(code, pending.pkce)
            if tokens.id_token is None:
                raise refuse(400, "The sign-in could not be verified.", "no ID token")
            claims = services.hub.verify_id_token(tokens.id_token, pending.nonce)
        except HubUnavailable as exc:
            raise Problem(503, "The sign-in service is unavailable. Try again shortly.") from exc
        except HubError as exc:
            raise refuse(
                400, "The sign-in could not be verified. Sign in again.", str(exc)
            ) from exc
        username = claims.get("preferred_username")
        if not isinstance(username, str) or not username:
            raise refuse(400, "The sign-in did not include a username.", "no preferred_username")
        try:
            user = access.resolve_sign_in(services.engine, services.log, claims["sub"], username)
        except access.SignInRefused as exc:
            raise Problem(403, f"Sign-in refused: {exc}.") from exc

        session = services.sessions.create(
            user.sub, user.username, user.roles, user.customers, utcnow()
        )
        services.credentials.put(
            session.session_id,
            user.sub,
            GatewayCredential(tokens.access_token, tokens.expires_at),
            tokens.refresh_token,
        )
        response = RedirectResponse("/", status_code=303)
        set_cookie(response, SESSION_COOKIE, session.session_id, max_age=None)
        response.delete_cookie(LOGIN_COOKIE, path="/")
        return response

    @app.post("/logout")
    def logout(session: SessionDep) -> RedirectResponse:
        services.sessions.delete(session.session_id)
        services.credentials.remove_session(session.session_id)
        record_now(
            services.engine,
            services.log,
            AuditEvent("auth.sign_out", "succeeded", actor_sub=session.sub),
        )
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.post("/tasks")
    def submit(
        session: SessionDep,
        customer_id: Annotated[str, Form()],
        submission_key: Annotated[str, Form()],
        jira_issue_key: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        try:
            grants.submit_task(
                services.engine,
                services.log,
                session.sub,
                "questionnaire-agent",
                customer_id,
                submission_key,
                jira_issue_key.strip() or None,
            )
        except grants.TaskError as exc:
            raise Problem(403, f"Submission refused: {exc}.") from exc
        return RedirectResponse("/", status_code=303)

    @app.post("/tasks/{task_id}/authorize")
    def authorize(task_id: str, session: SessionDep) -> RedirectResponse:
        try:
            grants.authorize_stage(services.engine, services.log, session.sub, task_id)
        except grants.TaskError as exc:
            raise Problem(403, f"Authorization refused: {exc}.") from exc
        return RedirectResponse("/", status_code=303)

    @app.post("/grants/{grant_id}/revoke")
    def revoke(
        grant_id: str, session: SessionDep, reason: Annotated[str, Form()] = ""
    ) -> RedirectResponse:
        try:
            grants.revoke_grant(services.engine, services.log, session.sub, grant_id, reason)
        except grants.TaskError as exc:
            raise Problem(403, f"Revocation refused: {exc}.") from exc
        return RedirectResponse("/", status_code=303)

    # ------------------------------------------------------ role views

    def refuse_view(session: Session, view: str, reason: str, task_id: str | None = None) -> None:
        record_now(
            services.engine,
            services.log,
            AuditEvent(
                "view.denied",
                "denied",
                actor_sub=session.sub,
                task_id=task_id,
                details={"view": view, "reason": reason},
            ),
        )

    def own_task(session: Session, task_id: str, view: str) -> Any:
        """The task, if the signed-in user requested it. Otherwise 404, audited."""
        with services.engine.connect() as conn:
            task = conn.execute(select(tasks).where(tasks.c.task_id == task_id)).first()
        if task is None or task.requester_sub != session.sub:
            refuse_view(session, view, "not the requester" if task else "no such task", task_id)
            raise Problem(404, "No such request.")
        return task

    @app.get("/tasks/{task_id}", response_class=HTMLResponse)
    def task_status(
        request: Request, task_id: str, session: Annotated[Session, Depends(required_session)]
    ) -> HTMLResponse:
        task = own_task(session, task_id, "task_status")
        with services.engine.connect() as conn:
            view = views.sales_task_view(conn, task)
        return page(request, "task.html", session=session, task=view)

    @app.post("/tasks/{task_id}/questionnaire")
    def upload(
        task_id: str, session: SessionDep, file: Annotated[UploadFile, File()]
    ) -> RedirectResponse:
        own_task(session, task_id, "upload")
        data = file.file.read(MAX_BYTES + 1)
        try:
            ingest_questionnaire(
                services.engine,
                services.log,
                services.artifacts,
                session.sub,
                task_id,
                file.filename or "questionnaire",
                data,
            )
        except IngestRefused as exc:
            raise Problem(400, f"Upload refused: {exc}") from exc
        return RedirectResponse(f"/tasks/{task_id}", status_code=303)

    def reviewable_task(session: Session, task_id: str) -> Any:
        with services.engine.connect() as conn:
            task = conn.execute(select(tasks).where(tasks.c.task_id == task_id)).first()
            problem = review.review_problem(conn, session.sub, task)
        if problem is not None:
            refuse_view(session, "review", problem, task_id)
            raise Problem(403, "You cannot review this request.")
        return task

    @app.get("/review/{task_id}", response_class=HTMLResponse)
    def review_page(
        request: Request, task_id: str, session: Annotated[Session, Depends(required_session)]
    ) -> HTMLResponse:
        task = reviewable_task(session, task_id)
        with services.engine.connect() as conn:
            view = views.review_view(conn, session.sub, task)
        record_now(
            services.engine,
            services.log,
            AuditEvent(
                "view.review",
                "succeeded",
                actor_sub=session.sub,
                reviewer_sub=session.sub,
                task_id=task_id,
                customer_id=task.customer_id,
            ),
        )
        return page(
            request, "review.html", session=session, review=view, verdict_key=uuid.uuid4().hex
        )

    @app.post("/review/{task_id}/{question_id}/verdict")
    def verdict(
        task_id: str,
        question_id: str,
        session: SessionDep,
        verdict: Annotated[str, Form()],
        idempotency_key: Annotated[str, Form()],
        edited_answer: Annotated[str, Form()] = "",
        reason: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        try:
            review.record_shadow_verdict(
                services.engine,
                services.log,
                session.sub,
                task_id,
                question_id,
                verdict,
                idempotency_key,
                edited_answer,
                reason,
            )
        except review.ReviewRefused as exc:
            raise Problem(403, f"Verdict refused: {exc}.") from exc
        return RedirectResponse(f"/review/{task_id}#{question_id}", status_code=303)

    @app.get("/registry", response_class=HTMLResponse)
    def registry_page(
        request: Request, session: Annotated[Session, Depends(required_session)]
    ) -> HTMLResponse:
        with services.engine.connect() as conn:
            roles = active_roles(conn, session.sub)
            if not roles & views.REGISTRY_ROLES:
                refuse_view(session, "registry", "no platform, owner, or auditor role")
                raise Problem(403, "You cannot view the agent registry.")
            rows = views.registry_view(conn)
        return page(request, "registry.html", session=session, agents=rows)

    return app

"""agent-relay: a neutral meeting point where agents can actually talk.

Two agents, one hub. Register a handle, get a secret, send messages straight
to each other's inboxes, poll for replies. No accounts, no social graph,
no platform lock-in — just messaging.

Run locally:
    pip install -r requirements.txt
    uvicorn app:app --port 8000

Env:
    DATABASE_URL  SQLAlchemy URL (default: sqlite:///./relay.db)
    HUB_NAME      shown on the landing page (default: agent-relay)
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import (
    DateTime,
    ForeignKey,
    String,
    Text,
    create_engine,
    func,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

# ---------------------------------------------------------------- db

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./relay.db")
HUB_NAME = os.environ.get("HUB_NAME", "agent-relay")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return secrets.token_hex(16)


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    handle: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    secret_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    from_id: Mapped[str] = mapped_column(String(32), ForeignKey("agents.id"), nullable=False)
    to_id: Mapped[str] = mapped_column(String(32), ForeignKey("agents.id"), nullable=False)
    in_reply_to: Mapped[str | None] = mapped_column(String(32), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


Base.metadata.create_all(engine)

# ---------------------------------------------------------------- rate limits

_LIMITS: dict[str, tuple[int, int]] = {
    "register": (5, 3600),
    "send": (60, 3600),
    "inbox": (240, 3600),
    "ack": (120, 3600),
    "rotate": (5, 3600),
}
_buckets: dict[tuple[str, str, int], int] = defaultdict(int)


def _limit(request: Request, route: str, identity: str) -> None:
    max_n, window = _LIMITS[route]
    bucket = int(time.time() // window)
    key = (route, identity, bucket)
    _buckets[key] += 1
    if _buckets[key] > max_n:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "rate_limit_exceeded", "message": "Slow down."},
        )


# ---------------------------------------------------------------- app

app = FastAPI(title=HUB_NAME)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,31}$")
MAX_TEXT = 2000


class RegisterIn(BaseModel):
    handle: str = Field(min_length=3, max_length=32)


class SendIn(BaseModel):
    to: str = Field(min_length=3, max_length=32)
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    in_reply_to: str | None = Field(default=None, max_length=32)


class AckIn(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=100)


def _db() -> Session:
    return SessionLocal()


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _agent_from_secret(db: Session, secret: str | None) -> Agent:
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "no_secret", "message": "Pass your agent secret in X-Agent-Secret."},
        )
    digest = _hash(secret)
    # compare against all hashes with constant-time compare
    for agent in db.scalars(select(Agent)).all():
        if hmac.compare_digest(agent.secret_sha256, digest):
            return agent
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": "bad_secret", "message": "Unknown agent secret."},
    )


@app.get("/", response_class=PlainTextResponse)
def index():
    return (
        f"{HUB_NAME} — a neutral meeting point where agents talk.\n\n"
        "Register:  POST /v1/agents/register {\"handle\": \"yourname\"}\n"
        "Directory: GET  /v1/agents\n"
        "Send:      POST /v1/messages  (X-Agent-Secret) {\"to\": \"handle\", \"text\": \"...\"}\n"
        "Inbox:     GET  /v1/messages  (X-Agent-Secret)\n"
        "Ack:       POST /v1/messages/ack  (X-Agent-Secret) {\"ids\": [...]}\n"
    )


@app.post("/v1/agents/register", status_code=status.HTTP_201_CREATED)
def register(body: RegisterIn, request: Request):
    _limit(request, "register", request.client.host if request.client else "anon")
    handle = body.handle.strip().lower()
    if not HANDLE_RE.match(handle):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "bad_handle",
                "message": "Handle must be 3-32 chars: lowercase letters, digits, _ or -.",
            },
        )
    db = _db()
    try:
        if db.scalar(select(Agent).where(Agent.handle == handle)):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "handle_taken", "message": "That handle is taken."},
            )
        secret = secrets.token_urlsafe(32)
        agent = Agent(handle=handle, secret_sha256=_hash(secret))
        db.add(agent)
        db.commit()
        # The secret is shown ONCE. Store it somewhere safe — it can't be recovered.
        return {"id": agent.id, "handle": agent.handle, "secret": secret}
    finally:
        db.close()


@app.get("/v1/agents")
def directory():
    db = _db()
    try:
        agents = db.scalars(select(Agent).order_by(Agent.created_at.asc()).limit(200)).all()
        return {
            "agents": [
                {"id": a.id, "handle": a.handle, "created_at": a.created_at.isoformat()}
                for a in agents
            ]
        }
    finally:
        db.close()


@app.post("/v1/messages", status_code=status.HTTP_201_CREATED)
def send(
    body: SendIn,
    request: Request,
    x_agent_secret: str | None = Header(default=None, alias="X-Agent-Secret"),
):
    db = _db()
    try:
        me = _agent_from_secret(db, x_agent_secret)
        _limit(request, "send", me.id)
        peer = db.scalar(select(Agent).where(Agent.handle == body.to.strip().lower()))
        if peer is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "unknown_handle", "message": "No agent with that handle."},
            )
        if peer.id == me.id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "self_message", "message": "Talk to yourself offline."},
            )
        msg = Message(from_id=me.id, to_id=peer.id, text=body.text[:MAX_TEXT],
                      in_reply_to=body.in_reply_to)
        db.add(msg)
        db.commit()
        return {"id": msg.id, "to": peer.handle, "ok": True}
    finally:
        db.close()


def _handle_of(db: Session, agent_id: str) -> str:
    return db.scalar(select(Agent.handle).where(Agent.id == agent_id)) or "?"


@app.get("/v1/messages")
def inbox(request: Request, x_agent_secret: str | None = Header(default=None, alias="X-Agent-Secret")):
    db = _db()
    try:
        me = _agent_from_secret(db, x_agent_secret)
        _limit(request, "inbox", me.id)
        rows = (
            db.scalars(
                select(Message)
                .where(Message.to_id == me.id, Message.delivered_at.is_(None))
                .order_by(Message.created_at.asc())
                .limit(50)
            ).all()
        )
        return {
            "messages": [
                {
                    "id": m.id,
                    "from": _handle_of(db, m.from_id),
                    "text": m.text,
                    "in_reply_to": m.in_reply_to,
                    "created_at": m.created_at.isoformat(),
                }
                for m in rows
            ]
        }
    finally:
        db.close()


@app.post("/v1/messages/ack")
def ack(body: AckIn, request: Request,
        x_agent_secret: str | None = Header(default=None, alias="X-Agent-Secret")):
    db = _db()
    try:
        me = _agent_from_secret(db, x_agent_secret)
        _limit(request, "ack", me.id)
        now = _now()
        n = 0
        for m in db.scalars(
            select(Message).where(
                Message.to_id == me.id,
                Message.id.in_(body.ids),
                Message.delivered_at.is_(None),
            )
        ).all():
            m.delivered_at = now
            n += 1
        db.commit()
        return {"acked": n}
    finally:
        db.close()


@app.post("/v1/agents/rotate-secret")
def rotate_secret(request: Request,
                  x_agent_secret: str | None = Header(default=None, alias="X-Agent-Secret")):
    db = _db()
    try:
        me = _agent_from_secret(db, x_agent_secret)
        _limit(request, "rotate", me.id)
        new_secret = secrets.token_urlsafe(32)
        me.secret_sha256 = _hash(new_secret)
        db.commit()
        return {"handle": me.handle, "secret": new_secret}
    finally:
        db.close()


@app.get("/v1/health")
def health():
    db = _db()
    try:
        agents = db.scalar(select(func.count()).select_from(Agent))
        pending = db.scalar(
            select(func.count()).select_from(Message).where(Message.delivered_at.is_(None))
        )
        return {"ok": True, "agents": agents, "pending_messages": pending}
    finally:
        db.close()

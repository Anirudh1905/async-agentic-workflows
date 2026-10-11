"""Login API: register, log in (OAuth2 password flow → JWT), and the current_user dependency."""

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated

import asyncpg
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pwdlib import PasswordHash

from agentic_workflows.api.deps import get_db, get_limiter
from agentic_workflows.api.schemas import RegisterUser, Token, User
from agentic_workflows.config import get_settings

router = APIRouter(prefix="/auth", tags=["auth"])
log = logging.getLogger(__name__)

ALGORITHM = "HS256"
password_hash = PasswordHash.recommended()  # argon2id
# Verified against when the email is unknown, so a miss takes as long as a wrong password
# and response time doesn't reveal which emails are registered.
_DUMMY_HASH = password_hash.hash("not-a-real-password")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")
Db = Annotated[asyncpg.Pool, Depends(get_db)]

_unauthorized = HTTPException(
    status.HTTP_401_UNAUTHORIZED, "invalid or expired token", headers={"WWW-Authenticate": "Bearer"}
)


def create_access_token(user_id: str) -> str:
    s = get_settings()
    now = datetime.now(UTC)
    claims = {"sub": user_id, "iat": now, "exp": now + timedelta(minutes=s.jwt_ttl_minutes)}
    return jwt.encode(claims, s.jwt_secret, algorithm=ALGORITHM)


async def get_current_user(token: Annotated[str, Depends(oauth2_scheme)], db: Db) -> User:
    try:
        claims = jwt.decode(token, get_settings().jwt_secret, algorithms=[ALGORITHM], options={"require": ["sub", "exp"]})
    except jwt.InvalidTokenError as e:
        log.info("rejected token reason=%s", type(e).__name__)  # e.g. ExpiredSignatureError
        raise _unauthorized
    row = await db.fetchrow("SELECT id::text, email FROM users WHERE id::text = $1", claims["sub"])
    if row is None:  # token for a user that no longer exists
        log.info("rejected token reason=unknown_user user_id=%s", claims["sub"])
        raise _unauthorized
    return User(id=row["id"], email=row["email"])


CurrentUser = Annotated[User, Depends(get_current_user)]


async def limit_by_ip(request: Request, response: Response, limiter=Depends(get_limiter)) -> None:
    # Keyed by the client address: these routes run before anyone is logged in.
    # Behind a proxy this would need the proxy's X-Forwarded-For, which isn't trusted here.
    # No limiter (disabled, or not set up as in unit tests) means no limit.
    if limiter is not None:
        await limiter.enforce(response, "auth", request.client.host if request.client else "unknown")


@router.post("/register", status_code=201, response_model=User, dependencies=[Depends(limit_by_ip)])
async def register(body: RegisterUser, db: Db):
    try:
        row = await db.fetchrow(
            "INSERT INTO users (email, password_hash) VALUES ($1, $2) RETURNING id::text, email",
            body.email.lower(),
            password_hash.hash(body.password),
        )
    except asyncpg.UniqueViolationError:
        log.info("register rejected reason=email_taken")
        raise HTTPException(409, "email already registered")
    log.info("user registered user_id=%s", row["id"])
    return User(id=row["id"], email=row["email"])


@router.post("/login", response_model=Token, dependencies=[Depends(limit_by_ip)])
async def login(form: Annotated[OAuth2PasswordRequestForm, Depends()], db: Db, request: Request):
    """OAuth2 password flow: form fields `username` (the email) and `password`."""
    row = await db.fetchrow("SELECT id::text, password_hash FROM users WHERE email = $1", form.username.lower())
    ok = password_hash.verify(form.password, row["password_hash"] if row else _DUMMY_HASH)
    if not (row and ok):
        # No email in the log: failed logins are often typos of real addresses.
        log.warning("login failed client=%s", request.client.host if request.client else "?")
        raise HTTPException(401, "incorrect email or password", headers={"WWW-Authenticate": "Bearer"})
    log.info("login ok user_id=%s", row["id"])
    return Token(access_token=create_access_token(row["id"]), expires_in=get_settings().jwt_ttl_minutes * 60)


@router.get("/me", response_model=User)
async def me(user: CurrentUser):
    return user

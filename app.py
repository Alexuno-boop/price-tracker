"""Price History Tracker - public web app (Flask).

Each visitor gets an anonymous list identified by a random cookie token (their
"sync code"). Products and price history are shared between visitors who track
the same item, so a newly added product may already have history.

Env vars:
  DATABASE_URL   Postgres connection string (e.g. from Neon). Falls back to local SQLite.
  CRON_SECRET    Secret the scheduled job sends to /api/cron/check.
"""
from __future__ import annotations

import csv
import io
import os
import secrets
import time
from datetime import datetime, timedelta, timezone

from flask import Flask, abort, jsonify, make_response, request, send_from_directory
from sqlalchemy import (Float, ForeignKey, Index, Integer, String, Text, create_engine, delete, func,
                        select)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship
from werkzeug.middleware.proxy_fix import ProxyFix

from scraper import ScrapeError, check_url, get_product_info, normalize_url

# ------------------------------------------------------------------ config

MAX_PRODUCTS_PER_USER = 30
ADDS_PER_HOUR_PER_USER = 15
ADDS_PER_HOUR_PER_IP = 40
CHECKS_PER_HOUR_PER_USER = 30
RESTORES_PER_HOUR_PER_IP = 20
FRESH_FOR = timedelta(minutes=15)        # don't refetch a product checked this recently
CRON_STALE_AFTER = timedelta(hours=6)    # scheduled job refreshes products older than this
CRON_TIME_BUDGET = 90                    # seconds per scheduled run
NEW_POINT_AFTER = timedelta(hours=20)    # store an unchanged price at most ~once a day
COOKIE = "pt_uid"


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") + "Z" if dt else None


def db_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        os.makedirs(os.path.join(os.path.dirname(__file__), "data"), exist_ok=True)
        return "sqlite:///" + os.path.join(os.path.dirname(__file__), "data", "local.db")
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


# ------------------------------------------------------------------ database

class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(default=utcnow)


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(String(600), unique=True, index=True)
    site: Mapped[str] = mapped_column(String(20))
    title: Mapped[str | None] = mapped_column(Text)
    image: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_checked: Mapped[datetime | None] = mapped_column()
    last_price: Mapped[float | None] = mapped_column(Float)
    last_error: Mapped[str | None] = mapped_column(Text)
    prices: Mapped[list["Price"]] = relationship(order_by="Price.checked_at", cascade="all, delete-orphan")


class Price(Base):
    __tablename__ = "prices"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    price: Mapped[float] = mapped_column(Float)
    checked_at: Mapped[datetime] = mapped_column(default=utcnow)


class Tracking(Base):
    __tablename__ = "trackings"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), primary_key=True, index=True)
    target: Mapped[float | None] = mapped_column(Float)
    added_at: Mapped[datetime] = mapped_column(default=utcnow)
    product: Mapped[Product] = relationship()


class Event(Base):
    """Lightweight log used for rate limiting."""
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    user_id: Mapped[int | None] = mapped_column(Integer)
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    __table_args__ = (Index("ix_events_kind_time", "kind", "created_at"),)


engine = create_engine(db_url(), pool_pre_ping=True, pool_recycle=280)
Base.metadata.create_all(engine)

# ------------------------------------------------------------------ app

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)  # Render sits behind a proxy
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024


class ApiError(Exception):
    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.msg, self.code = msg, code


@app.errorhandler(ApiError)
def _api_error(e: ApiError):
    return finish(make_response(jsonify(error=e.msg), e.code))


@app.errorhandler(ScrapeError)
def _scrape_error(e: ScrapeError):
    return finish(make_response(jsonify(error=str(e)), 400))


@app.after_request
def _headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Frame-Options"] = "DENY"
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def client_ip() -> str:
    return (request.remote_addr or "?")[:64]


def json_body() -> dict:
    # Requiring a JSON content type blocks cross-site form posts (CSRF).
    if not request.is_json:
        raise ApiError("Expected JSON", 415)
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def current_user(s: Session, create: bool = False) -> User | None:
    token = request.cookies.get(COOKIE)
    user = s.scalar(select(User).where(User.token == token)) if token else None
    if user:
        if utcnow() - user.last_seen > timedelta(hours=1):
            user.last_seen = utcnow()
        return user
    if create:
        user = User(token=secrets.token_urlsafe(18))
        s.add(user)
        s.flush()
        request.environ["pt_new_token"] = user.token
    return user


def set_cookie(resp, token: str):
    resp.set_cookie(COOKIE, token, max_age=60 * 60 * 24 * 730, httponly=True,
                    secure=request.is_secure, samesite="Lax")


def finish(resp):
    tok = request.environ.get("pt_new_token")
    if tok:
        set_cookie(resp, tok)
    return resp


def rate_limit(s: Session, kind: str, limit: int, user: User | None = None, by_ip: bool = False):
    since = utcnow() - timedelta(hours=1)
    q = select(func.count()).select_from(Event).where(Event.kind == kind, Event.created_at > since)
    q = q.where(Event.ip == client_ip()) if by_ip else q.where(Event.user_id == (user.id if user else None))
    if s.scalar(q) >= limit:
        raise ApiError("You're doing that too often. Please wait a while and try again.", 429)


def log_event(s: Session, kind: str, user: User | None):
    s.add(Event(kind=kind, user_id=user.id if user else None, ip=client_ip()))


def record_price(s: Session, p: Product, price: float):
    now = utcnow()
    last = s.scalar(select(Price).where(Price.product_id == p.id).order_by(Price.checked_at.desc()).limit(1))
    if last is None or last.price != price or now - last.checked_at >= NEW_POINT_AFTER:
        s.add(Price(product_id=p.id, price=price, checked_at=now))
    p.last_price, p.last_checked, p.last_error = price, now, None


def refresh(s: Session, p: Product) -> bool:
    try:
        info = get_product_info(p.url)
    except ScrapeError as e:
        p.last_error, p.last_checked = str(e), utcnow()
        return False
    if info.title:
        p.title = info.title[:500]
    if info.image:
        p.image = info.image
    record_price(s, p, info.price)
    return True


def serialize(t: Tracking) -> dict:
    p = t.product
    return {
        "id": p.id, "url": p.url, "site": p.site, "title": p.title or p.url, "image": p.image,
        "target": t.target, "added": iso(t.added_at), "lastChecked": iso(p.last_checked),
        "lastError": p.last_error, "trackingSince": iso(p.created_at),
        "history": [{"t": iso(x.checked_at), "p": x.price} for x in p.prices],
    }


def user_tracking(s: Session, user: User | None, pid) -> Tracking:
    if not user:
        raise ApiError("Product not found", 404)
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        raise ApiError("Product not found", 404)
    t = s.get(Tracking, (user.id, pid))
    if not t:
        raise ApiError("Product not found", 404)
    return t


def parse_target(v) -> float | None:
    if v in (None, ""):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ApiError("Target price must be a number")
    if f <= 0:
        return None
    if f > 1e9:
        raise ApiError("Target price is too large")
    return round(f, 2)


# ------------------------------------------------------------------ routes

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/api/products")
def api_products():
    with Session(engine) as s:
        user = current_user(s)
        if not user:
            return jsonify([])
        ts = s.scalars(select(Tracking).where(Tracking.user_id == user.id).order_by(Tracking.added_at)).all()
        out = [serialize(t) for t in ts]
        s.commit()
        return jsonify(out)


@app.post("/api/add")
def api_add():
    body = json_body()
    url, site = check_url(str(body.get("url", "")))
    target = parse_target(body.get("target"))
    with Session(engine) as s:
        user = current_user(s, create=True)
        count = s.scalar(select(func.count()).select_from(Tracking).where(Tracking.user_id == user.id))
        if count >= MAX_PRODUCTS_PER_USER:
            raise ApiError(f"You can track up to {MAX_PRODUCTS_PER_USER} products. Remove one to add another.")
        rate_limit(s, "add", ADDS_PER_HOUR_PER_USER, user)
        rate_limit(s, "add", ADDS_PER_HOUR_PER_IP, by_ip=True)
        log_event(s, "add", user)
        s.commit()

        # Already known product? (direct link match avoids a fetch)
        p = s.scalar(select(Product).where(Product.url == normalize_url(url, site)))
        if p is None or p.last_price is None or utcnow() - (p.last_checked or datetime.min) > FRESH_FOR:
            info = get_product_info(url)  # raises ScrapeError with a friendly message
            p = s.scalar(select(Product).where(Product.url == info.url))
            if p is None:
                p = Product(url=info.url, site=info.site)
                s.add(p)
                s.flush()
            p.title = (info.title or p.title or info.url)[:500]
            p.image = info.image or p.image
            record_price(s, p, info.price)

        if s.get(Tracking, (user.id, p.id)):
            raise ApiError("You're already tracking this product.")
        t = Tracking(user_id=user.id, product_id=p.id, target=target)
        s.add(t)
        s.commit()
        s.refresh(t)
        return finish(jsonify(serialize(t)))


@app.post("/api/check")
def api_check():
    body = json_body()
    with Session(engine) as s:
        user = current_user(s)
        t = user_tracking(s, user, body.get("id"))
        p = t.product
        if p.last_checked and utcnow() - p.last_checked < FRESH_FOR and not p.last_error:
            return jsonify(serialize(t) | {"fresh": True})
        rate_limit(s, "check", CHECKS_PER_HOUR_PER_USER, user)
        log_event(s, "check", user)
        refresh(s, p)
        s.commit()
        s.refresh(t)
        return jsonify(serialize(t))


@app.post("/api/target")
def api_target():
    body = json_body()
    with Session(engine) as s:
        t = user_tracking(s, current_user(s), body.get("id"))
        t.target = parse_target(body.get("target"))
        s.commit()
        return jsonify(ok=True)


@app.post("/api/delete")
def api_delete():
    body = json_body()
    with Session(engine) as s:
        t = user_tracking(s, current_user(s), body.get("id"))
        s.delete(t)
        s.commit()
        return jsonify(ok=True)


@app.get("/api/me")
def api_me():
    with Session(engine) as s:
        user = current_user(s)
        return jsonify(code=user.token if user else None)


@app.post("/api/restore")
def api_restore():
    body = json_body()
    code = str(body.get("code", "")).strip()
    with Session(engine) as s:
        rate_limit(s, "restore", RESTORES_PER_HOUR_PER_IP, by_ip=True)
        log_event(s, "restore", None)
        s.commit()
        user = s.scalar(select(User).where(User.token == code)) if 10 <= len(code) <= 64 else None
        if not user:
            raise ApiError("That sync code wasn't found.", 404)
        resp = jsonify(ok=True)
        set_cookie(resp, user.token)
        return resp


@app.get("/api/export.csv")
def api_export():
    with Session(engine) as s:
        user = current_user(s)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["product", "site", "url", "timestamp_utc", "price"])
        if user:
            for t in s.scalars(select(Tracking).where(Tracking.user_id == user.id)):
                for x in t.product.prices:
                    w.writerow([t.product.title, t.product.site, t.product.url, iso(x.checked_at), x.price])
        resp = make_response(buf.getvalue())
        resp.headers["Content-Type"] = "text/csv; charset=utf-8"
        resp.headers["Content-Disposition"] = 'attachment; filename="price-history.csv"'
        return resp


@app.post("/api/cron/check")
def api_cron():
    secret = os.environ.get("CRON_SECRET", "")
    auth = request.headers.get("Authorization", "")
    if not secret or not secrets.compare_digest(auth, f"Bearer {secret}"):
        abort(403)
    started = time.monotonic()
    ok = failed = 0
    with Session(engine) as s:
        cutoff = utcnow() - CRON_STALE_AFTER
        tracked = select(Tracking.product_id).distinct()
        ids = s.scalars(
            select(Product.id).where(Product.id.in_(tracked))
            .where((Product.last_checked.is_(None)) | (Product.last_checked < cutoff))
            .order_by(Product.last_checked.asc().nulls_first()).limit(200)
        ).all()
        for i, pid in enumerate(ids):
            if time.monotonic() - started > CRON_TIME_BUDGET:
                break
            if i:
                time.sleep(1.5)  # be polite to the stores
            p = s.get(Product, pid)
            if refresh(s, p):
                ok += 1
            else:
                failed += 1
            s.commit()
        # keep the rate-limit log small
        s.execute(delete(Event).where(Event.created_at < utcnow() - timedelta(days=2)))
        s.commit()
        remaining = len(ids) - ok - failed
    return jsonify(checked=ok, failed=failed, remaining=remaining)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=False)

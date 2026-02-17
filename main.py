import os
import json
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException
import psycopg2
from psycopg2.extras import RealDictCursor

app = FastAPI()

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
WEBHOOK_TOKEN = os.getenv("KIWIFY_WEBHOOK_TOKEN", "").strip()


def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL não configurado no Render.")
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def init_db():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    email TEXT PRIMARY KEY,
                    active BOOLEAN NOT NULL DEFAULT FALSE,
                    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
                );
            """)
            # Auditoria dos webhooks (pra debug)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS webhook_events (
                    id SERIAL PRIMARY KEY,
                    received_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    event TEXT,
                    email TEXT,
                    raw JSONB
                );
            """)
            conn.commit()
    finally:
        conn.close()


@app.on_event("startup")
def on_startup():
    init_db()


@app.get("/")
def root():
    return {"LazyAndDark": "API ONLINE"}


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/status")
def status(email: str):
    email = email.strip().lower()
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT email, active, updated_at FROM subscriptions WHERE email=%s", (email,))
            row = cur.fetchone()
            if not row:
                return {"email": email, "active": False, "found": False}
            return {
                "email": row["email"],
                "active": bool(row["active"]),
                "found": True,
                "updated_at": str(row["updated_at"]),
            }
    finally:
        conn.close()


def _token_ok(request: Request) -> bool:
    if not WEBHOOK_TOKEN:
        return True

    qs_sigs = [s.strip() for s in request.query_params.getlist("signature") if s]
    if qs_sigs and WEBHOOK_TOKEN in qs_sigs:
        return True

    hdr_sig = (request.headers.get("X-Webhook-Token", "") or "").strip()
    if hdr_sig and hdr_sig == WEBHOOK_TOKEN:
        return True

    hdr_auth = (request.headers.get("Authorization", "") or "").strip()
    if hdr_auth.lower().startswith("bearer "):
        bearer = hdr_auth.split(" ", 1)[1].strip()
        if bearer == WEBHOOK_TOKEN:
            return True

    return False


def _pick_email(data: dict) -> str:
    """
    Tenta achar email em vários formatos que a Kiwify pode enviar.
    """
    candidates = []

    # Formatos comuns
    customer = data.get("customer") or {}
    if isinstance(customer, dict):
        candidates.append(customer.get("email"))

    candidates.append(data.get("email"))
    candidates.append(data.get("customer_email"))

    # Outros formatos possíveis
    order = data.get("order") or {}
    if isinstance(order, dict):
        candidates.append(order.get("customer_email"))
        cust2 = order.get("customer") or {}
        if isinstance(cust2, dict):
            candidates.append(cust2.get("email"))

    buyer = data.get("buyer") or {}
    if isinstance(buyer, dict):
        candidates.append(buyer.get("email"))

    # Pega o primeiro válido
    for c in candidates:
        if isinstance(c, str) and "@" in c:
            return c.strip().lower()

    return ""


def _normalize_event(data: dict) -> str:
    ev = (data.get("event") or data.get("type") or data.get("evento") or "").strip()
    ev = ev.lower().replace(" ", "_")
    return ev


@app.post("/webhook/kiwify")
async def kiwify_webhook(request: Request):
    if not _token_ok(request):
        raise HTTPException(status_code=401, detail="Invalid webhook token")

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event = _normalize_event(data)
    email = _pick_email(data)

    # Salva auditoria sempre (pra você enxergar o que chegou)
    conn = get_conn()
    try:
        with conn.cursor() as cur:

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
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS subscriptions (
                    email TEXT PRIMARY KEY,
                    active BOOLEAN NOT NULL DEFAULT FALSE,
                    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS webhook_events (
                    id SERIAL PRIMARY KEY,
                    received_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    event TEXT,
                    email TEXT,
                    raw JSONB
                );
                """
            )
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
            cur.execute(
                "SELECT email, active, updated_at FROM subscriptions WHERE email=%s",
                (email,),
            )
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


def token_ok(request: Request) -> bool:
    """
    Aceita token vindo de:
    - querystring: ?signature=...  (pode vir repetido)
    - header: X-Webhook-Token
    - header: Authorization: Bearer <token>
    """
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


def pick_email(data: dict) -> str:
    """
    Tenta encontrar e-mail em vários formatos possíveis.
    """
    candidates = []

    customer = data.get("customer") or {}
    if isinstance(customer, dict):
        candidates.append(customer.get("email"))

    candidates.append(data.get("email"))
    candidates.append(data.get("customer_email"))

    order = data.get("order") or {}
    if isinstance(order, dict):
        candidates.append(order.get("customer_email"))
        cust2 = order.get("customer") or {}
        if isinstance(cust2, dict):
            candidates.append(cust2.get("email"))

    buyer = data.get("buyer") or {}
    if isinstance(buyer, dict):
        candidates.append(buyer.get("email"))

    for c in candidates:
        if isinstance(c, str) and "@" in c:
            return c.strip().lower()

    return ""


def normalize_event(data: dict) -> str:
    ev = (data.get("event") or data.get("type") or data.get("evento") or "").strip()
    return ev.lower().replace(" ", "_")


@app.post("/webhook/kiwify")
async def kiwify_webhook(request: Request):
    if not token_ok(request):
        raise HTTPException(status_code=401, detail="Invalid webhook token")

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event = normalize_event(data)
    email = pick_email(data)

    # Auditoria (sempre salva o payload)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO webhook_events (event, email, raw) VALUES (%s, %s, %s)",
                (event or None, email or None, json.dumps(data)),
            )
            conn.commit()
    finally:
        conn.close()

    if not email:
        print(f"[KIWIFY] Recebido sem email. event={event}")
        return {"received": True, "note": "no email", "event": event}

    active_events = {
        "compra_aprovada",
        "purchase_approved",
        "subscription_renewed",
        "assinatura_renovada",
        "approved",
    }
    inactive_events = {
        "subscription_canceled",
        "assinatura_cancelada",
        "subscription_late",
        "chargeback",
        "reembolso",
        "refund",
        "canceled",
    }

    new_active = None
    if event in active_events:
        new_active = True
    elif event in inactive_events:
        new_active = False
    else:
        print(f"[KIWIFY] Evento ignorado: {event} | email={email}")
        return {"received": True, "note": f"ignored event: {event}", "email": email}

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO subscriptions (email, active, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (email)
                DO UPDATE SET active=EXCLUDED.active, updated_at=EXCLUDED.updated_at
                """,
                (email, new_active, datetime.utcnow()),
            )
            conn.commit()
    finally:
        conn.close()

    print(f"[KIWIFY] OK: {email} -> active={new_active} (event={event})")
    return {"received": True, "email": email, "active": new_active, "event": event}

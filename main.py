import os
import json
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException
import psycopg2
from psycopg2.extras import RealDictCursor

app = FastAPI()

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
WEBHOOK_TOKEN = os.getenv("KIWIFY_WEBHOOK_TOKEN", "").strip()


# =========================
# DATABASE
# =========================

def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL não configurado.")
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
def startup():
    init_db()


# =========================
# BASIC ROUTES
# =========================

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


# =========================
# TOKEN VALIDATION
# =========================

def token_ok(request: Request) -> bool:
    if not WEBHOOK_TOKEN:
        return True

    qs_sigs = [s.strip() for s in request.query_params.getlist("signature") if s]
    if qs_sigs and WEBHOOK_TOKEN in qs_sigs:
        return True

    hdr_sig = (request.headers.get("X-Webhook-Token", "") or "").strip()
    if hdr_sig == WEBHOOK_TOKEN:
        return True

    hdr_auth = (request.headers.get("Authorization", "") or "").strip()
    if hdr_auth.lower().startswith("bearer "):
        bearer = hdr_auth.split(" ", 1)[1].strip()
        if bearer == WEBHOOK_TOKEN:
            return True

    return False


# =========================
# HELPERS
# =========================

def pick_email(data: dict) -> str:
    """
    Procura o email em vários lugares possíveis do payload.
    """
    paths = [
        ["customer", "email"],
        ["buyer", "email"],
        ["order", "customer", "email"],
        ["order", "customer_email"],
        ["customer_email"],
        ["email"],
        ["Customer", "email"],
        ["data", "customer", "email"],
    ]

    for path in paths:
        ref = data
        for key in path:
            if isinstance(ref, dict):
                ref = ref.get(key)
            else:
                ref = None
        if isinstance(ref, str) and "@" in ref:
            return ref.strip().lower()

    return ""


def normalize_event(data: dict) -> str:
    ev = (
        data.get("event")
        or data.get("type")
        or data.get("evento")
        or data.get("Event")
        or data.get("name")
        or ""
    )

    return ev.strip().lower().replace(" ", "_")


# =========================
# WEBHOOK
# =========================

@app.post("/webhook/kiwify")
async def kiwify_webhook(request: Request):

    if not token_ok(request):
        raise HTTPException(status_code=401, detail="Invalid webhook token")

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # 🔥 LOGS IMPORTANTES (DEBUG)
    print("[KIWIFY] keys:", list(data.keys()))
    print("[KIWIFY] preview:", str(data)[:800])

    event = normalize_event(data)
    email = pick_email(data)

    # Auditoria sempre salva
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
        print("[KIWIFY] sem email. data:", str(data)[:1200])
        return {"received": True, "note": "no email", "event": event}

    # EVENTOS
    active_events = {
        "compra_aprovada",
        "purchase_approved",
        "approved",
        "subscription_renewed",
        "assinatura_renovada",
    }

    inactive_events = {
        "subscription_canceled",
        "assinatura_cancelada",
        "chargeback",
        "refund",
        "reembolso",
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

    # SALVAR ASSINATURA
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO subscriptions (email, active, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (email)
                DO UPDATE SET active=EXCLUDED.active, updated_at=EXCLUDED.updated_at
            """, (email, new_active, datetime.utcnow()))
            conn.commit()
    finally:
        conn.close()

    print(f"[KIWIFY] OK: {email} -> active={new_active} (event={event})")

    return {
        "received": True,
        "email": email,
        "active": new_active,
        "event": event
    }

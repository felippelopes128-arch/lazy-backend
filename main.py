import os
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException
import psycopg2
from psycopg2.extras import RealDictCursor

app = FastAPI()

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# (Opcional) segredo simples pro webhook. Se você não configurar nada, ele aceita sem validação.
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
            return {"email": row["email"], "active": bool(row["active"]), "found": True, "updated_at": str(row["updated_at"])}
    finally:
        conn.close()


@app.post("/webhook/kiwify")
async def kiwify_webhook(request: Request):
    # Se você quiser proteger, passe um token no header:
    # Ex: X-Webhook-Token: <seu_token>
    if WEBHOOK_TOKEN:
        token = request.headers.get("X-Webhook-Token", "")
        if token != WEBHOOK_TOKEN:
            raise HTTPException(status_code=401, detail="Invalid webhook token")

    data = await request.json()

    event = data.get("event") or data.get("type") or ""
    customer = data.get("customer", {}) or {}
    email = (customer.get("email") or data.get("email") or "").strip().lower()

    if not email:
        return {"received": True, "note": "no email"}

    active_events = {"compra_aprovada", "subscription_renewed"}
    inactive_events = {"subscription_canceled", "subscription_late", "chargeback"}

    new_active = None
    if event in active_events:
        new_active = True
    elif event in inactive_events:
        new_active = False

    if new_active is None:
        return {"received": True, "note": f"ignored event: {event}"}

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

    return {"received": True, "email": email, "active": new_active}

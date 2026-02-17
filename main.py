import os
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException
import psycopg2
from psycopg2.extras import RealDictCursor

app = FastAPI()

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# Se estiver vazio, aceita webhook sem validação.
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
            return {
                "email": row["email"],
                "active": bool(row["active"]),
                "found": True,
                "updated_at": str(row["updated_at"]),
            }
    finally:
        conn.close()


def _token_ok(request: Request) -> bool:
    """
    Aceita token vindo de:
    - querystring: ?signature=...
      (inclusive se vier repetido: signature=a&signature=b)
    - header: X-Webhook-Token
    - header: Authorization: Bearer <token>
    """
    if not WEBHOOK_TOKEN:
        return True  # sem validação

    # Query params (pode vir repetido)
    qs_sigs = [s.strip() for s in request.query_params.getlist("signature") if s]
    if qs_sigs and WEBHOOK_TOKEN in qs_sigs:
        return True

    # Header custom
    hdr_sig = (request.headers.get("X-Webhook-Token", "") or "").strip()
    if hdr_sig and hdr_sig == WEBHOOK_TOKEN:
        return True

    # Authorization Bearer
    hdr_auth = (request.headers.get("Authorization", "") or "").strip()
    if hdr_auth.lower().startswith("bearer "):
        bearer = hdr_auth.split(" ", 1)[1].strip()
        if bearer == WEBHOOK_TOKEN:
            return True

    return False


@app.post("/webhook/kiwify")
async def kiwify_webhook(request: Request):
    # Validação do token
    if not _token_ok(request):
        # logs úteis (sem vazar token)
        qs_sigs = request.query_params.getlist("signature")
        print("[KIWIFY] 401 - token inválido")
        print("[KIWIFY] signatures recebidas (qsp):", [("..." if s else "") for s in qs_sigs])
        print("[KIWIFY] tem X-Webhook-Token header?:", "X-Webhook-Token" in request.headers)
        print("[KIWIFY] tem Authorization header?:", "Authorization" in request.headers)
        raise HTTPException(status_code=401, detail="Invalid webhook token")

    # Parse do JSON
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Evento e email (Kiwify pode variar os nomes)
    event = (data.get("event") or data.get("type") or data.get("evento") or "").strip()

    customer = data.get("customer", {}) or {}
    email = (customer.get("email") or data.get("email") or data.get("customer_email") or "").strip().lower()

    if not email:
        return {"received": True, "note": "no email"}

    # Eventos possíveis (podem variar; se precisar, você ajusta depois)
    active_events = {
        "compra_aprovada",
        "purchase_approved",
        "compra_aprovada_pix",
        "subscription_renewed",
        "assinatura_renovada",
    }
    inactive_events = {
        "subscription_canceled",
        "assinatura_cancelada",
        "subscription_late",
        "chargeback",
        "reembolso",
        "refund",
    }

    new_active = None
    if event in active_events:
        new_active = True
    elif event in inactive_events:
        new_active = False

    if new_active is None:
        # deixa log pra você descobrir o nome exato do evento
        print(f"[KIWIFY] evento ignorado: {event} | email={email}")
        return {"received": True, "note": f"ignored event: {event}"}

    # Grava no banco
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
    return {"received": True, "email": email, "active": new_active, "event": event}

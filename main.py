from fastapi import FastAPI, Request
from datetime import datetime

app = FastAPI()

# banco temporário em memória
USERS = {}

@app.get("/")
def root():
    return {"LazyAndDark": "API ONLINE"}

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/webhook/kiwify")
async def kiwify_webhook(request: Request):
    data = await request.json()

    event = data.get("event") or data.get("type")
    customer = data.get("customer", {})
    email = (customer.get("email") or data.get("email") or "").lower()

    if not email:
        return {"received": True}

    if event in ["compra_aprovada", "subscription_renewed"]:
        USERS[email] = {"active": True, "updated_at": datetime.utcnow().isoformat()}

    if event in ["subscription_canceled", "subscription_late"]:
        USERS[email] = {"active": False, "updated_at": datetime.utcnow().isoformat()}

    return {"received": True}

@app.get("/status")
def status(email: str):
    email = email.lower()
    info = USERS.get(email)

    if not info:
        return {"active": False}

    return {"active": info["active"]}

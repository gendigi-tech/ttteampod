"""
POD Renew Tool — Simple Backend (API mode only)
"""
import asyncio, base64, json, logging, os, time, zipfile, io, hashlib
from pathlib import Path
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

BASE      = Path(__file__).parent
STATIC    = BASE / "static"
RESULTS   = BASE / "results"
USERS_F   = BASE / "users.json"
HISTORY_F = BASE / "history.json"
RESULTS.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

def hash_pw(p): return hashlib.sha256(p.encode()).hexdigest()

def load_users():
    if USERS_F.exists():
        try: return json.loads(USERS_F.read_text(encoding="utf-8"))
        except: pass
    users = {
        "admin": {"password": hash_pw(os.getenv("ADMIN_PASSWORD","admin123")),
                  "role": "admin", "name": "Admin"},
        "user1": {"password": hash_pw(os.getenv("USER1_PASSWORD","user123")),
                  "role": "user",  "name": "User 1"},
    }
    USERS_F.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8")
    return users

def save_users(u):
    USERS_F.write_text(json.dumps(u, ensure_ascii=False, indent=2), encoding="utf-8")

def load_history(username=None):
    if not HISTORY_F.exists(): return []
    try:
        h = json.loads(HISTORY_F.read_text(encoding="utf-8"))
        return [e for e in h if e.get("username")==username] if username else h
    except: return []

def append_history(entry):
    h = load_history()
    h.insert(0, entry)
    HISTORY_F.write_text(json.dumps(h[:1000], ensure_ascii=False, indent=2), encoding="utf-8")

@app.get("/api/auth")
async def auth(username: str = "", password: str = ""):
    u = load_users().get(username)
    if u and u["password"] == hash_pw(password):
        return {"ok": True, "username": username, "role": u["role"], "name": u["name"]}
    return {"ok": False}

def verify_admin(au, ap):
    u = load_users().get(au)
    if not (u and u["password"]==hash_pw(ap) and u["role"]=="admin"):
        raise HTTPException(403, "Admin only")

class UserPayload(BaseModel):
    username: str
    password: str = ""
    name: str = ""

@app.get("/api/admin/users")
async def list_users(admin_user: str, admin_pass: str):
    verify_admin(admin_user, admin_pass)
    return [{"username":k, "name":v["name"], "role":v["role"],
             "history_count": len(load_history(k))} for k,v in load_users().items()]

@app.post("/api/admin/users")
async def create_user(payload: UserPayload, admin_user: str, admin_pass: str):
    verify_admin(admin_user, admin_pass)
    users = load_users()
    if payload.username in users: raise HTTPException(400, "Đã tồn tại")
    users[payload.username] = {
        "password": hash_pw(payload.password or "password123"),
        "role": "user", "name": payload.name or payload.username,
    }
    save_users(users); return {"ok": True}

@app.delete("/api/admin/users/{username}")
async def delete_user(username: str, admin_user: str, admin_pass: str):
    verify_admin(admin_user, admin_pass)
    if username == admin_user: raise HTTPException(400, "Không xoá được chính mình")
    users = load_users()
    if username in users: del users[username]; save_users(users)
    return {"ok": True}

@app.put("/api/admin/users/{username}/password")
async def change_pw(username: str, new_password: str, admin_user: str, admin_pass: str):
    verify_admin(admin_user, admin_pass)
    users = load_users()
    if username not in users: raise HTTPException(404)
    users[username]["password"] = hash_pw(new_password)
    save_users(users); return {"ok": True}

@app.get("/api/admin/history")
async def admin_history(admin_user: str, admin_pass: str):
    verify_admin(admin_user, admin_pass)
    return [e for e in load_history() if (RESULTS / e["result"]).exists()]

@app.get("/api/history")
async def user_history(username: str = ""):
    return [e for e in load_history(username) if (RESULTS / e["result"]).exists()]

@app.delete("/api/history/{name}")
async def delete_one(name: str):
    f = RESULTS / name
    if f.exists(): f.unlink()
    h = [e for e in load_history() if e["result"] != name]
    HISTORY_F.write_text(json.dumps(h, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True}

@app.delete("/api/history")
async def clear_history(username: str = ""):
    h = load_history()
    if username:
        for r in [e["result"] for e in h if e.get("username")==username]:
            f = RESULTS / r
            if f.exists(): f.unlink()
        h = [e for e in h if e.get("username")!=username]
    else:
        for f in RESULTS.glob("*.png"): f.unlink()
        h = []
    HISTORY_F.write_text(json.dumps(h, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True}

@app.get("/api/download/{filename}")
async def download(filename: str):
    f = RESULTS / filename
    if not f.exists(): raise HTTPException(404)
    return FileResponse(str(f), media_type="image/png",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

@app.get("/api/download-all")
async def download_all(username: str = ""):
    files = ([RESULTS / e["result"] for e in load_history(username) if (RESULTS / e["result"]).exists()]
             if username else list(RESULTS.glob("*.png")))
    if not files: raise HTTPException(404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files: zf.write(f, f.name)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="pod_results.zip"'})

async def send_telegram(token, chat_id, text):
    if not token or not chat_id: return False
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
            return r.status_code == 200
    except: return False

@app.get("/api/test-telegram")
async def test_telegram(token: str = "", chat_id: str = ""):
    ok = await send_telegram(token, chat_id, "✅ POD Renew — Telegram OK!")
    return {"ok": ok}

@app.websocket("/ws/process")
async def ws_process(ws: WebSocket):
    await ws.accept()
    async def send(**kw): await ws.send_text(json.dumps(kw))
    try:
        data     = json.loads(await ws.receive_text())
        api_key  = data.get("api_key","")
        prompt   = data.get("prompt","")
        images   = data.get("images",[])
        folder   = data.get("folder_name","")
        username = data.get("username","guest")
        tg_token = data.get("telegram_token","")
        tg_chat  = data.get("telegram_chat_id","")

        if not api_key: await send(type="error", message="Chưa nhập API key!"); return
        if not prompt:  await send(type="error", message="Chưa nhập prompt!"); return
        if not images:  await send(type="error", message="Chưa chọn ảnh!"); return

        total = len(images)
        await send(type="start", total=total)
        ok = fail = 0
        for i, img in enumerate(images):
            name = img["name"]
            await send(type="progress", current=i+1, total=total, filename=name)
            try:
                out_b64, out_name = await process_api(
                    img["data"], img.get("type","image/jpeg"), name, prompt, api_key, send)
                if out_b64:
                    (RESULTS / out_name).write_bytes(base64.b64decode(out_b64))
                    ok += 1
                    append_history({
                        "id": f"{int(time.time()*1000)}-{i}",
                        "timestamp": time.strftime("%d/%m/%Y %H:%M"),
                        "original": name, "result": out_name,
                        "prompt": prompt[:120], "folder": folder,
                        "username": username,
                    })
                    await send(type="success", filename=name, result=out_name)
                else:
                    fail += 1; await send(type="fail", filename=name)
            except Exception as e:
                fail += 1
                await send(type="fail", filename=name, message=str(e)[:120])
            if i < total-1: await asyncio.sleep(1)

        await send(type="done", ok=ok, fail=fail, total=total)
        if tg_token and tg_chat:
            await send_telegram(tg_token, tg_chat,
                f"✅ <b>POD Renew</b>\n👤 {username}\n📁 {folder or 'N/A'}\n🖼 {ok}/{total} xong")
    except WebSocketDisconnect: pass
    except Exception as e:
        log.error(e)
        try: await ws.send_text(json.dumps({"type":"error","message":str(e)}))
        except: pass

async def process_api(b64, mt, name, prompt, api_key, send):
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    out_name = f"{Path(name).stem}_renewed.png"
    await send(type="log", message="  🎨 Gửi ảnh tới xAI...")
    async with httpx.AsyncClient(timeout=180) as c:
        r = await c.post("https://api.x.ai/v1/images/edits", headers=headers, json={
            "model": "grok-imagine-image", "prompt": prompt,
            "image": {"type": "image_url", "url": f"data:{mt};base64,{b64}"}})
        if r.status_code != 200:
            raise Exception(f"API {r.status_code}: {r.text[:150]}")
        item = r.json()["data"][0]
        out_b64 = item.get("b64_json")
        if not out_b64:
            dl = await c.get(item.get("url",""), timeout=60)
            out_b64 = base64.b64encode(dl.content).decode()
        return out_b64, out_name

@app.get("/")
async def root(): return FileResponse(str(STATIC / "index.html"))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))

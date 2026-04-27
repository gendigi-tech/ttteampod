"""
POD Renew Tool — Backend with Multi-User Management
"""
import asyncio, base64, json, logging, os, time, zipfile, io, re, hashlib, secrets
from pathlib import Path
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
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

# ── Auto-install Chromium ────────────────────────────────────
def ensure_chromium():
    try:
        import subprocess, sys
        subprocess.run([sys.executable,"-m","playwright","install","chromium","--with-deps"],
                      capture_output=True, timeout=300)
        log.info("Chromium ready")
    except Exception as e:
        log.warning(f"Chromium install: {e}")

import threading
threading.Thread(target=ensure_chromium, daemon=True).start()

# ── User helpers ──────────────────────────────────────────────
def hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

def load_users() -> dict:
    if USERS_F.exists():
        try: return json.loads(USERS_F.read_text(encoding="utf-8"))
        except: pass
    # Default: admin + 1 regular user
    default = {
        "admin": {
            "password": hash_pw(os.getenv("ADMIN_PASSWORD","admin123")),
            "role": "admin",
            "name": "Admin",
            "created": time.strftime("%d/%m/%Y")
        },
        "user1": {
            "password": hash_pw(os.getenv("USER1_PASSWORD","user123")),
            "role": "user",
            "name": "User 1",
            "created": time.strftime("%d/%m/%Y")
        }
    }
    USERS_F.write_text(json.dumps(default, ensure_ascii=False, indent=2), encoding="utf-8")
    return default

def save_users(users: dict):
    USERS_F.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8")

# ── History helpers ───────────────────────────────────────────
def load_history(username: str = None) -> list:
    if HISTORY_F.exists():
        try:
            all_h = json.loads(HISTORY_F.read_text(encoding="utf-8"))
            if username:
                return [e for e in all_h if e.get("username") == username]
            return all_h
        except: pass
    return []

def append_history(entry: dict):
    try:
        if HISTORY_F.exists():
            all_h = json.loads(HISTORY_F.read_text(encoding="utf-8"))
        else:
            all_h = []
        all_h.insert(0, entry)
        HISTORY_F.write_text(json.dumps(all_h[:1000], ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.error(f"History save error: {e}")

# ── Auth ──────────────────────────────────────────────────────
@app.get("/api/auth")
async def auth(username: str = "", password: str = ""):
    users = load_users()
    u = users.get(username)
    if u and u["password"] == hash_pw(password):
        return {"ok": True, "role": u["role"], "name": u.get("name", username), "username": username}
    return {"ok": False}

# ── User management (admin only) ─────────────────────────────
class UserPayload(BaseModel):
    username: str
    password: str = ""
    name: str = ""
    role: str = "user"

@app.get("/api/admin/users")
async def get_users(admin_user: str, admin_pass: str):
    users = load_users()
    u = users.get(admin_user)
    if not u or u["password"] != hash_pw(admin_pass) or u["role"] != "admin":
        raise HTTPException(403, "Admin only")
    # Return users without passwords
    return [{"username": k, "name": v.get("name",k), "role": v["role"],
              "created": v.get("created",""), "history_count": len(load_history(k))}
            for k, v in users.items()]

@app.post("/api/admin/users")
async def create_user(payload: UserPayload, admin_user: str, admin_pass: str):
    users = load_users()
    u = users.get(admin_user)
    if not u or u["password"] != hash_pw(admin_pass) or u["role"] != "admin":
        raise HTTPException(403, "Admin only")
    if payload.username in users:
        raise HTTPException(400, "Username đã tồn tại")
    users[payload.username] = {
        "password": hash_pw(payload.password or "password123"),
        "role": payload.role,
        "name": payload.name or payload.username,
        "created": time.strftime("%d/%m/%Y")
    }
    save_users(users); return {"ok": True}

@app.delete("/api/admin/users/{username}")
async def delete_user(username: str, admin_user: str, admin_pass: str):
    users = load_users()
    u = users.get(admin_user)
    if not u or u["password"] != hash_pw(admin_pass) or u["role"] != "admin":
        raise HTTPException(403, "Admin only")
    if username == admin_user: raise HTTPException(400, "Không thể xoá chính mình")
    if username not in users: raise HTTPException(404, "User không tồn tại")
    del users[username]; save_users(users); return {"ok": True}

@app.put("/api/admin/users/{username}/password")
async def change_password(username: str, new_password: str, admin_user: str, admin_pass: str):
    users = load_users()
    u = users.get(admin_user)
    if not u or u["password"] != hash_pw(admin_pass) or u["role"] != "admin":
        raise HTTPException(403, "Admin only")
    if username not in users: raise HTTPException(404)
    users[username]["password"] = hash_pw(new_password)
    save_users(users); return {"ok": True}

@app.get("/api/admin/history")
async def get_all_history(admin_user: str, admin_pass: str):
    users = load_users()
    u = users.get(admin_user)
    if not u or u["password"] != hash_pw(admin_pass) or u["role"] != "admin":
        raise HTTPException(403, "Admin only")
    all_h = load_history()
    return [e for e in all_h if (RESULTS / e["result"]).exists()]

# ── History (per user) ────────────────────────────────────────
@app.get("/api/history")
async def get_history(username: str = ""):
    h = load_history(username)
    return [e for e in h if (RESULTS / e["result"]).exists()]

@app.delete("/api/history/{result_name}")
async def delete_one(result_name: str):
    f = RESULTS / result_name
    if f.exists(): f.unlink()
    if HISTORY_F.exists():
        all_h = json.loads(HISTORY_F.read_text(encoding="utf-8"))
        all_h = [e for e in all_h if e["result"] != result_name]
        HISTORY_F.write_text(json.dumps(all_h, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True}

@app.delete("/api/history")
async def clear_history(username: str = ""):
    if HISTORY_F.exists():
        all_h = json.loads(HISTORY_F.read_text(encoding="utf-8"))
        if username:
            to_del = [e["result"] for e in all_h if e.get("username") == username]
            for r in to_del:
                f = RESULTS / r
                if f.exists(): f.unlink()
            all_h = [e for e in all_h if e.get("username") != username]
        else:
            for f in RESULTS.glob("*.png"): f.unlink()
            all_h = []
        HISTORY_F.write_text(json.dumps(all_h, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True}

# ── Download ─────────────────────────────────────────────────
@app.get("/api/download/{filename}")
async def download(filename: str):
    f = RESULTS / filename
    if not f.exists(): raise HTTPException(404)
    return FileResponse(str(f), media_type="image/png",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

@app.get("/api/download-all")
async def download_all(username: str = ""):
    if username:
        h = load_history(username)
        files = [RESULTS / e["result"] for e in h if (RESULTS / e["result"]).exists()]
    else:
        files = list(RESULTS.glob("*.png"))
    if not files: raise HTTPException(404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files: zf.write(f, f.name)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="pod_results.zip"'})

# ── Test Telegram ─────────────────────────────────────────────
@app.get("/api/test-telegram")
async def test_telegram(token: str = "", chat_id: str = ""):
    ok = await _send_telegram(token, chat_id, "✅ <b>POD Renew Tool</b> — Kết nối Telegram thành công!")
    return {"ok": ok}

async def _send_telegram(token, chat_id, text):
    if not token or not chat_id: return False
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
            return r.status_code == 200
    except: return False

# ── WebSocket ─────────────────────────────────────────────────
@app.websocket("/ws/process")
async def ws_process(ws: WebSocket):
    await ws.accept()
    async def send(**kw): await ws.send_text(json.dumps(kw))
    try:
        data        = json.loads(await ws.receive_text())
        api_key     = data.get("api_key","")
        prompt      = data.get("prompt","")
        images      = data.get("images",[])
        mode        = data.get("mode","api")
        folder      = data.get("folder_name","")
        username    = data.get("username","guest")
        tg_token    = data.get("telegram_token","")
        tg_chat     = data.get("telegram_chat_id","")
        app_url     = data.get("app_url","").rstrip("/")
        grok_cookie = data.get("grok_cookie","")

        if not prompt:  await send(type="error", message="Chưa nhập prompt!"); return
        if not images:  await send(type="error", message="Chưa chọn ảnh!"); return
        if mode=="api" and not api_key: await send(type="error", message="Chưa nhập API key!"); return

        total = len(images)
        await send(type="start", total=total)
        ok = fail = 0

        for i, img in enumerate(images):
            name = img["name"]
            await send(type="progress", current=i+1, total=total, filename=name)
            try:
                if mode == "chrome":
                    out_b64, out_name = await process_chrome(img["data"], img.get("type","image/jpeg"), name, prompt, grok_cookie, send)
                else:
                    out_b64, out_name = await process_api(img["data"], img.get("type","image/jpeg"), name, prompt, api_key, send)

                if out_b64:
                    out_path = RESULTS / out_name
                    out_path.write_bytes(base64.b64decode(out_b64))
                    ok += 1
                    append_history({
                        "id": f"{int(time.time()*1000)}-{i}",
                        "timestamp": time.strftime("%d/%m/%Y %H:%M"),
                        "original": name, "result": out_name,
                        "prompt": prompt[:120], "folder": folder,
                        "username": username, "mode": mode
                    })
                    await send(type="success", filename=name, result=out_name)
                else:
                    fail += 1; await send(type="fail", filename=name, message=f"❌ {name}")
            except Exception as e:
                fail += 1; await send(type="fail", filename=name, message=f"❌ {name}: {str(e)[:100]}")
            if i < total-1: await asyncio.sleep(1)

        await send(type="done", ok=ok, fail=fail, total=total)

        if tg_token and tg_chat:
            link = f'\n🔗 <a href="{app_url}">Mở tool</a>' if app_url else ""
            await _send_telegram(tg_token, tg_chat,
                f"✅ <b>POD Renew</b>\n👤 {username}\n📁 {folder or 'N/A'}\n🖼 {ok}/{total} xong" + link)

    except WebSocketDisconnect: pass
    except Exception as e:
        log.error(e)
        try: await ws.send_text(json.dumps({"type":"error","message":str(e)}))
        except: pass

# ── API Mode ──────────────────────────────────────────────────
async def process_api(b64, mt, name, prompt, api_key, send):
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    out_name = f"{Path(name).stem}_renewed.png"
    await send(type="log", message="  🔌 Gửi ảnh tới xAI API...")
    async with httpx.AsyncClient(timeout=180) as c:
        r = await c.post("https://api.x.ai/v1/images/edits", headers=headers, json={
            "model": "grok-imagine-image", "prompt": prompt,
            "image": {"type": "image_url", "url": f"data:{mt};base64,{b64}"}})
        if r.status_code != 200: raise Exception(f"API {r.status_code}: {r.text[:150]}")
        item = r.json()["data"][0]
        out_b64 = item.get("b64_json")
        if not out_b64:
            dl = await c.get(item.get("url",""), timeout=60)
            out_b64 = base64.b64encode(dl.content).decode()
        return out_b64, out_name

# ── Chrome Mode ───────────────────────────────────────────────
async def process_chrome(b64, mt, name, prompt, grok_cookie, send):
    try: from playwright.async_api import async_playwright
    except ImportError: raise Exception("Playwright chưa cài. Đợi server khởi động ~3 phút.")
    out_name = f"{Path(name).stem}_renewed.png"
    tmp = Path("/tmp/pod_uploads"); tmp.mkdir(exist_ok=True)
    tmp_img = tmp / name; tmp_img.write_bytes(base64.b64decode(b64))
    await send(type="log", message="  🌐 Mở Grok.com...")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox","--disable-dev-shm-usage"])
        ctx = await browser.new_context(user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36")
        if grok_cookie:
            cookies = []
            for part in grok_cookie.split(";"):
                part = part.strip()
                if "=" in part:
                    k, v = part.split("=", 1)
                    for domain in [".grok.com", ".x.com"]:
                        cookies.append({"name":k.strip(),"value":v.strip(),"domain":domain,"path":"/"})
            if cookies: await ctx.add_cookies(cookies)
        page = await ctx.new_page()
        await page.goto("https://grok.com", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)
        if any(k in page.url for k in ["login","signin","auth"]):
            raise Exception("Grok chưa đăng nhập. Kiểm tra cookie trong Settings.")
        await send(type="log", message="  📎 Upload ảnh...")
        uploaded = False
        for sel in ['input[type="file"]','input[accept*="image"]']:
            try:
                fi = page.locator(sel).first
                if await fi.count(): await fi.set_input_files(str(tmp_img)); uploaded=True; break
            except: pass
        if not uploaded:
            for sel in ['button[aria-label*="ttach"]','button[aria-label*="mage"]','[data-testid*="attach"]']:
                try:
                    btn = page.locator(sel).first
                    if await btn.count():
                        await btn.click(); await asyncio.sleep(0.8)
                        fi = page.locator('input[type="file"]').first
                        if await fi.count(): await fi.set_input_files(str(tmp_img)); uploaded=True; break
                except: pass
        if not uploaded: raise Exception("Không tìm được nút upload.")
        await asyncio.sleep(1.5)
        for sel in ['div[contenteditable="true"]','textarea']:
            try:
                inp = page.locator(sel).first
                if await inp.count(): await inp.click(); await page.keyboard.type(prompt, delay=20); break
            except: pass
        await asyncio.sleep(0.4)
        for sel in ['button[type="submit"]','button[aria-label*="end"]']:
            try:
                btn = page.locator(sel).first
                if await btn.count(): await btn.click(); break
            except: pass
        await send(type="log", message="  ⏳ Chờ Grok tạo ảnh...")
        n0 = len(await page.query_selector_all("img"))
        for _ in range(120):
            await asyncio.sleep(1)
            imgs = await page.query_selector_all("img")
            if len(imgs) > n0:
                await asyncio.sleep(2)
                src = await imgs[-1].get_attribute("src") or ""
                out_b64 = None
                if src.startswith("blob:"):
                    b64d = await page.evaluate("async s=>{const r=await fetch(s);const b=await r.arrayBuffer();return btoa(String.fromCharCode(...new Uint8Array(b)))}", src)
                    if b64d: out_b64 = b64d
                elif src.startswith("data:"):
                    m = re.search(r"base64,(.+)", src)
                    if m: out_b64 = m.group(1)
                elif src.startswith("http"):
                    async with httpx.AsyncClient(timeout=30) as dl_c:
                        resp = await dl_c.get(src)
                        out_b64 = base64.b64encode(resp.content).decode()
                if out_b64: await browser.close(); tmp_img.unlink(missing_ok=True); return out_b64, out_name
        raise Exception("Timeout: Grok chưa tạo ảnh sau 2 phút")

@app.get("/")
async def root(): return FileResponse(str(STATIC / "index.html"))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))

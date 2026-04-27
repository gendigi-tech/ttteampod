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

# ══════════════════════════════════════════════════════════════
# WOOCOMMERCE INTEGRATION
# ══════════════════════════════════════════════════════════════

import re as _re

WOO_SETTINGS_F = BASE / "woo_settings.json"

def load_woo_settings():
    if WOO_SETTINGS_F.exists():
        try: return json.loads(WOO_SETTINGS_F.read_text(encoding="utf-8"))
        except: pass
    return {"url": "", "consumer_key": "", "consumer_secret": ""}

def save_woo_settings(data):
    WOO_SETTINGS_F.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

class WooSettings(BaseModel):
    url: str = ""
    consumer_key: str = ""
    consumer_secret: str = ""

@app.get("/api/woo/settings")
async def get_woo_settings():
    return load_woo_settings()

@app.post("/api/woo/settings")
async def post_woo_settings(payload: WooSettings):
    s = payload.dict()
    s["url"] = s["url"].rstrip("/")
    save_woo_settings(s)
    return {"ok": True}

@app.get("/api/woo/test")
async def test_woo():
    s = load_woo_settings()
    if not s["url"] or not s["consumer_key"]:
        return {"ok": False, "error": "Chưa cấu hình WooCommerce"}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                f"{s['url']}/wp-json/wc/v3/products?per_page=1",
                auth=(s["consumer_key"], s["consumer_secret"])
            )
            return {"ok": r.status_code == 200, "status": r.status_code,
                    "error": r.text[:120] if r.status_code != 200 else ""}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}

class AnalyzeRequest(BaseModel):
    image_b64: str
    image_name: str
    api_key: str

@app.post("/api/woo/analyze")
async def woo_analyze(payload: AnalyzeRequest):
    """Dùng xAI vision để phân tích ảnh → gợi ý thông tin sản phẩm"""
    if not payload.api_key:
        return {"ok": False, "error": "Cần API key xAI"}
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(
                "https://api.x.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {payload.api_key}",
                         "Content-Type": "application/json"},
                json={
                    "model": "grok-2-vision-1212",
                    "messages": [{"role": "user", "content": [
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/jpeg;base64,{payload.image_b64}"}},
                        {"type": "text", "text": (
                            "This is a print-on-demand t-shirt product image. "
                            "Analyze it and respond ONLY with a JSON object (no markdown, no explanation):\n"
                            '{"name":"product name in English (max 60 chars)",'
                            '"short_description":"1-2 sentence description",'
                            '"description":"2-3 paragraph HTML description for WooCommerce",'
                            '"categories":["Main Category","Sub Category"],'
                            '"tags":["tag1","tag2","tag3","tag4","tag5"],'
                            '"sku_hint":"2-3 word slug, lowercase, hyphens"}'
                        )}
                    ]}],
                    "max_tokens": 600,
                }
            )
        if r.status_code != 200:
            return {"ok": False, "error": f"Vision API {r.status_code}"}
        text = r.json()["choices"][0]["message"]["content"].strip()
        # Strip markdown code fences if present
        text = _re.sub(r"```(?:json)?|```", "", text).strip()
        data = json.loads(text)
        # Build SKU from filename + hint
        stem = Path(payload.image_name).stem
        slug = data.get("sku_hint", stem).lower()
        slug = _re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
        data["sku"] = f"POD-{slug}-{int(time.time()) % 10000}"
        return {"ok": True, "data": data}
    except json.JSONDecodeError:
        # Vision returned something but not valid JSON — extract what we can
        return {"ok": True, "data": {
            "name": Path(payload.image_name).stem.replace("_", " ").title(),
            "short_description": "Print-on-demand t-shirt design.",
            "description": "<p>High-quality print-on-demand t-shirt.</p>",
            "categories": ["T-Shirts"],
            "tags": ["t-shirt", "print-on-demand"],
            "sku": f"POD-{int(time.time()) % 10000}"
        }}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}

class PublishRequest(BaseModel):
    products: list
    status: str = "draft"   # draft | publish

@app.post("/api/woo/publish")
async def woo_publish(payload: PublishRequest):
    """Đăng sản phẩm lên WooCommerce qua REST API"""
    s = load_woo_settings()
    if not s["url"] or not s["consumer_key"]:
        return {"ok": False, "error": "Chưa cấu hình WooCommerce"}

    auth = (s["consumer_key"], s["consumer_secret"])
    base_url = s["url"]
    results = []

    async with httpx.AsyncClient(timeout=60) as c:
        for prod in payload.products:
            try:
                image_url = None

                # 1. Upload ảnh lên WP Media Library (nếu có b64)
                if prod.get("image_b64"):
                    img_bytes = base64.b64decode(prod["image_b64"])
                    fname = prod.get("image_name", "product.jpg")
                    media_r = await c.post(
                        f"{base_url}/wp-json/wp/v2/media",
                        auth=auth,
                        headers={
                            "Content-Disposition": f'attachment; filename="{fname}"',
                            "Content-Type": prod.get("image_type", "image/jpeg"),
                        },
                        content=img_bytes,
                        timeout=60
                    )
                    if media_r.status_code in (200, 201):
                        image_url = media_r.json().get("source_url", "")

                # 2. Tạo sản phẩm
                product_data = {
                    "name": prod.get("name", ""),
                    "type": "simple",
                    "status": payload.status,
                    "short_description": prod.get("short_description", ""),
                    "description": prod.get("description", ""),
                    "sku": prod.get("sku", ""),
                    "regular_price": str(prod.get("price", "")),
                    "categories": [{"name": c} for c in prod.get("categories", [])],
                    "tags": [{"name": t} for t in prod.get("tags", [])],
                }
                if image_url:
                    product_data["images"] = [{"src": image_url}]

                prod_r = await c.post(
                    f"{base_url}/wp-json/wc/v3/products",
                    auth=auth,
                    json=product_data
                )
                if prod_r.status_code in (200, 201):
                    pid = prod_r.json().get("id")
                    link = prod_r.json().get("permalink", "")
                    results.append({"ok": True, "name": prod.get("name"),
                                    "id": pid, "link": link})
                else:
                    results.append({"ok": False, "name": prod.get("name"),
                                    "error": prod_r.text[:120]})
            except Exception as e:
                results.append({"ok": False, "name": prod.get("name","?"),
                                "error": str(e)[:100]})

    ok_count = sum(1 for r in results if r["ok"])
    return {"ok": True, "results": results,
            "published": ok_count, "failed": len(results) - ok_count}

@app.post("/api/woo/export-csv")
async def woo_export_csv(payload: PublishRequest):
    """Xuất file CSV chuẩn WooCommerce"""
    import csv, io as _io
    output = _io.StringIO()
    # WooCommerce CSV headers
    headers = [
        "ID","Type","SKU","Name","Published","Featured","Catalog visibility",
        "Short description","Description","Tax status","In stock?","Stock",
        "Regular price","Sale price","Categories","Tags","Images",
        "Position"
    ]
    writer = csv.DictWriter(output, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    for prod in payload.products:
        cats = ", ".join(prod.get("categories", []))
        tags = ", ".join(prod.get("tags", []))
        writer.writerow({
            "ID": "",
            "Type": "simple",
            "SKU": prod.get("sku", ""),
            "Name": prod.get("name", ""),
            "Published": "1" if payload.status == "publish" else "0",
            "Featured": "0",
            "Catalog visibility": "visible",
            "Short description": prod.get("short_description", ""),
            "Description": prod.get("description", ""),
            "Tax status": "taxable",
            "In stock?": "1",
            "Stock": "",
            "Regular price": str(prod.get("price", "")),
            "Sale price": "",
            "Categories": cats,
            "Tags": tags,
            "Images": prod.get("image_url", ""),
            "Position": "0",
        })
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue().encode("utf-8-sig")]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="woocommerce_products.csv"'}
    )

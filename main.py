"""
POD Renew Tool — Backend
Features: API mode, Chrome mode, Telegram notifications, History, Settings
"""
import asyncio, base64, json, logging, os, time, zipfile, io, re
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

BASE       = Path(__file__).parent
STATIC     = BASE / "static"
RESULTS    = BASE / "results"
HISTORY_F  = BASE / "history.json"
SETTINGS_F = BASE / "settings.json"
RESULTS.mkdir(exist_ok=True)

ACCESS_PW = os.getenv("ACCESS_PASSWORD", "pod2024")

app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

# ── Settings ──────────────────────────────────────────────────
def load_settings() -> dict:
    if SETTINGS_F.exists():
        try: return json.loads(SETTINGS_F.read_text(encoding="utf-8"))
        except: pass
    return {}

def save_settings(data: dict):
    SETTINGS_F.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

class SettingsPayload(BaseModel):
    telegram_token:  str = ""
    telegram_chat_id: str = ""
    grok_cookie:     str = ""
    app_url:         str = ""

@app.get("/api/settings")
async def get_settings():
    s = load_settings()
    return {k: s.get(k, "") for k in ["telegram_token","telegram_chat_id","grok_cookie","app_url"]}

@app.post("/api/settings")
async def post_settings(payload: SettingsPayload):
    save_settings(payload.dict())
    return {"ok": True}

@app.get("/api/test-telegram")
async def test_telegram(token: str = "", chat_id: str = ""):
    ok = await send_telegram(token, chat_id,
        "✅ <b>POD Renew Tool</b> — Kết nối Telegram thành công!")
    return {"ok": ok}

# ── Telegram ──────────────────────────────────────────────────
async def send_telegram(token: str, chat_id: str, text: str) -> bool:
    if not token or not chat_id:
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
            )
            return r.status_code == 200
    except Exception as e:
        log.warning(f"Telegram error: {e}")
        return False

# ── History ───────────────────────────────────────────────────
def load_history():
    if HISTORY_F.exists():
        try: return json.loads(HISTORY_F.read_text(encoding="utf-8"))
        except: pass
    return []

def append_history(entry: dict):
    h = load_history()
    h.insert(0, entry)
    HISTORY_F.write_text(json.dumps(h[:500], ensure_ascii=False, indent=2), encoding="utf-8")

@app.get("/api/history")
async def get_history():
    return [e for e in load_history() if (RESULTS / e["result"]).exists()]

@app.delete("/api/history/{result_name}")
async def delete_one(result_name: str):
    f = RESULTS / result_name
    if f.exists(): f.unlink()
    h = [e for e in load_history() if e["result"] != result_name]
    HISTORY_F.write_text(json.dumps(h, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True}

@app.delete("/api/history")
async def clear_history():
    for f in RESULTS.glob("*.png"): f.unlink()
    HISTORY_F.write_text("[]", encoding="utf-8")
    return {"ok": True}

# ── Auth ─────────────────────────────────────────────────────
@app.get("/api/auth")
async def auth(password: str):
    return {"ok": password == ACCESS_PW}

# ── Download ─────────────────────────────────────────────────
@app.get("/api/download/{filename}")
async def download(filename: str):
    f = RESULTS / filename
    if not f.exists(): raise HTTPException(404, "Not found")
    return FileResponse(str(f), media_type="image/png",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

@app.get("/api/download-all")
async def download_all():
    files = list(RESULTS.glob("*.png"))
    if not files: raise HTTPException(404, "No results")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files: zf.write(f, f.name)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="pod_results.zip"'})

# ── WebSocket ─────────────────────────────────────────────────
@app.websocket("/ws/process")
async def ws_process(ws: WebSocket):
    await ws.accept()

    async def send(**kw):
        await ws.send_text(json.dumps(kw))

    try:
        data     = json.loads(await ws.receive_text())
        api_key  = data.get("api_key", "")
        prompt   = data.get("prompt", "")
        images   = data.get("images", [])
        mode     = data.get("mode", "api")
        folder   = data.get("folder_name", "")

        if not prompt:  await send(type="error", message="Chưa nhập prompt!"); return
        if not images:  await send(type="error", message="Chưa chọn ảnh!"); return
        if mode == "api" and not api_key:
            await send(type="error", message="Chưa nhập API key!"); return

        total = len(images)
        await send(type="start", total=total)

        # Settings sent from browser localStorage (persistent on client side)
        tg_token    = data.get("telegram_token", "")
        tg_chat     = data.get("telegram_chat_id", "")
        app_url     = data.get("app_url", "").rstrip("/")
        grok_cookie = data.get("grok_cookie", "")

        ok = fail = 0
        for i, img in enumerate(images):
            name = img["name"]
            await send(type="progress", current=i+1, total=total, filename=name)
            try:
                if mode == "chrome":
                    out_b64, out_name = await process_chrome(
                        img["data"], img.get("type","image/jpeg"),
                        name, prompt, grok_cookie, send)
                else:
                    out_b64, out_name = await process_api(
                        img["data"], img.get("type","image/jpeg"),
                        name, prompt, api_key, send)

                if out_b64:
                    out_path = RESULTS / out_name
                    out_path.write_bytes(base64.b64decode(out_b64))
                    ok += 1
                    append_history({
                        "id": f"{int(time.time()*1000)}-{i}",
                        "timestamp": time.strftime("%d/%m/%Y %H:%M"),
                        "original": name, "result": out_name,
                        "prompt": prompt[:120], "folder": folder,
                    })
                    await send(type="success", filename=name, result=out_name)
                else:
                    fail += 1
                    await send(type="fail", filename=name, message=f"❌ Thất bại: {name}")

            except Exception as e:
                fail += 1
                await send(type="fail", filename=name, message=f"❌ Lỗi: {name} — {str(e)[:120]}")

            if i < total - 1:
                await asyncio.sleep(1)

        await send(type="done", ok=ok, fail=fail, total=total)

        # ── Telegram notification ─────────────────────────────
        if tg_token and tg_chat:
            link = f'\n🔗 <a href="{app_url}">Mở tool</a>' if app_url else ""
            msg  = (
                f"✅ <b>POD Renew Tool</b>\n"
                f"📁 Folder: <b>{folder or 'N/A'}</b>\n"
                f"🖼 {ok}/{total} ảnh thành công"
                + (f", {fail} lỗi" if fail else "")
                + link
            )
            await send_telegram(tg_token, tg_chat, msg)

    except WebSocketDisconnect: pass
    except Exception as e:
        log.error(e)
        try: await ws.send_text(json.dumps({"type":"error","message":str(e)}))
        except: pass

# ── API Mode ──────────────────────────────────────────────────
async def process_api(b64, mt, name, prompt, api_key, send):
    headers  = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    out_name = f"{Path(name).stem}_renewed.png"
    await send(type="log", message="  🔌 API: Gửi ảnh để chỉnh sửa...")

    async with httpx.AsyncClient(timeout=180) as c:
        r = await c.post("https://api.x.ai/v1/images/edits", headers=headers, json={
            "model": "grok-imagine-image", "prompt": prompt,
            "image": {"type": "image_url", "url": f"data:{mt};base64,{b64}"}
        })
        if r.status_code != 200:
            raise Exception(f"API lỗi {r.status_code}: {r.text[:200]}")

        item    = r.json()["data"][0]
        out_b64 = item.get("b64_json")
        if not out_b64:
            url = item.get("url", "")
            if not url: raise Exception("API không trả về ảnh")
            dl      = await c.get(url, timeout=60)
            out_b64 = base64.b64encode(dl.content).decode()

        return out_b64, out_name

# ── Chrome Mode (Playwright server-side) ──────────────────────
async def process_chrome(b64, mt, name, prompt, grok_cookie, send):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise Exception("Playwright chưa cài. Cần deploy lại Railway.")

    out_name = f"{Path(name).stem}_renewed.png"

    # Lưu ảnh tạm để upload
    tmp_dir = Path("/tmp/pod_uploads")
    tmp_dir.mkdir(exist_ok=True)
    tmp_img = tmp_dir / name
    tmp_img.write_bytes(base64.b64decode(b64))

    await send(type="log", message="  🌐 Chrome: Mở Grok.com...")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        )

        # Set Grok session cookies nếu có
        if grok_cookie:
            await send(type="log", message="  🍪 Dùng session cookie...")
            # Parse cookie string (key=val; key2=val2 format)
            cookies = []
            for part in grok_cookie.split(";"):
                part = part.strip()
                if "=" in part:
                    k, v = part.split("=", 1)
                    cookies.append({
                        "name": k.strip(), "value": v.strip(),
                        "domain": ".grok.com", "path": "/"
                    })
                    # Also add for x.com (Grok is also on x.com)
                    cookies.append({
                        "name": k.strip(), "value": v.strip(),
                        "domain": ".x.com", "path": "/"
                    })
            if cookies:
                await context.add_cookies(cookies)

        page = await context.new_page()
        await page.goto("https://grok.com", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        # Check login
        if any(k in page.url for k in ["login", "signin", "auth"]):
            raise Exception("Grok chưa đăng nhập! Cần nhập cookie trong Settings.")

        await send(type="log", message="  📎 Upload ảnh...")

        # Try file input selectors
        uploaded = False
        for sel in ['input[type="file"]', 'input[accept*="image"]']:
            try:
                fi = page.locator(sel).first
                if await fi.count():
                    await fi.set_input_files(str(tmp_img))
                    uploaded = True; break
            except: pass

        if not uploaded:
            for sel in ['button[aria-label*="ttach"]', 'button[aria-label*="mage"]',
                        '[data-testid*="attach"]', 'label[for*="file"]']:
                try:
                    btn = page.locator(sel).first
                    if await btn.count():
                        await btn.click(); await asyncio.sleep(0.8)
                        fi = page.locator('input[type="file"]').first
                        if await fi.count():
                            await fi.set_input_files(str(tmp_img))
                            uploaded = True; break
                except: pass

        if not uploaded:
            raise Exception("Không tìm được nút upload. Grok đã đổi giao diện.")

        await asyncio.sleep(1.5)

        # Type prompt
        for sel in ['div[contenteditable="true"]', 'textarea[placeholder]', 'textarea']:
            try:
                inp = page.locator(sel).first
                if await inp.count():
                    await inp.click()
                    await page.keyboard.type(prompt, delay=20); break
            except: pass

        await asyncio.sleep(0.4)

        # Submit
        submitted = False
        for sel in ['button[type="submit"]', 'button[aria-label*="end"]',
                    '[data-testid*="send"]']:
            try:
                btn = page.locator(sel).first
                if await btn.count():
                    await btn.click(); submitted = True; break
            except: pass
        if not submitted:
            await page.keyboard.press("Enter")

        await send(type="log", message="  ⏳ Chờ Grok xử lý ảnh (tối đa 2 phút)...")

        n_before = len(await page.query_selector_all("img"))
        found = False
        for _ in range(120):
            await asyncio.sleep(1)
            imgs_now = await page.query_selector_all("img")
            if len(imgs_now) > n_before:
                await asyncio.sleep(2)
                src = await imgs_now[-1].get_attribute("src") or ""

                if src.startswith("blob:"):
                    b64d = await page.evaluate(
                        "async s=>{const r=await fetch(s);const b=await r.arrayBuffer();"
                        "return btoa(String.fromCharCode(...new Uint8Array(b)))}", src)
                    if b64d:
                        out_b64 = b64d; found = True; break
                elif src.startswith("data:"):
                    m = re.search(r"base64,(.+)", src)
                    if m: out_b64 = m.group(1); found = True; break
                elif src.startswith("http"):
                    async with httpx.AsyncClient(timeout=30) as dl_c:
                        resp = await dl_c.get(src)
                        out_b64 = base64.b64encode(resp.content).decode()
                        found = True; break

                if not found:
                    await imgs_now[-1].screenshot(path=f"/tmp/{out_name}")
                    out_b64 = base64.b64encode(Path(f"/tmp/{out_name}").read_bytes()).decode()
                    found = True; break

        await browser.close()
        tmp_img.unlink(missing_ok=True)

        if not found:
            raise Exception("Không nhận được ảnh từ Grok sau 2 phút.")

        return out_b64, out_name

# ── Frontend ─────────────────────────────────────────────────
@app.get("/")
async def root():
    return FileResponse(str(STATIC / "index.html"))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))

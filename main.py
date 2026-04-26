"""
POD Renew Tool — Web App Backend (with history)
"""
import asyncio, base64, json, logging, os, time, zipfile, io
from pathlib import Path
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

BASE      = Path(__file__).parent
STATIC    = BASE / "static"
RESULTS   = BASE / "results"
HISTORY_F = BASE / "history.json"
RESULTS.mkdir(exist_ok=True)

ACCESS_PW = os.getenv("ACCESS_PASSWORD", "pod2024")

app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

# ── History helpers ───────────────────────────────────────────
def load_history():
    if HISTORY_F.exists():
        try: return json.loads(HISTORY_F.read_text(encoding="utf-8"))
        except: pass
    return []

def append_history(entry: dict):
    h = load_history()
    h.insert(0, entry)
    HISTORY_F.write_text(json.dumps(h[:500], ensure_ascii=False, indent=2), encoding="utf-8")

# ── Auth ─────────────────────────────────────────────────────
@app.get("/api/auth")
async def auth(password: str):
    return {"ok": password == ACCESS_PW}

# ── History ───────────────────────────────────────────────────
@app.get("/api/history")
async def get_history():
    h = load_history()
    return [e for e in h if (RESULTS / e["result"]).exists()]

@app.delete("/api/history/{result_name}")
async def delete_one(result_name: str):
    f = RESULTS / result_name
    if f.exists(): f.unlink()
    h = [e for e in load_history() if e["result"] != result_name]
    HISTORY_F.write_text(json.dumps(h, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True}

@app.delete("/api/history")
async def clear_all_history():
    for f in RESULTS.glob("*.png"): f.unlink()
    HISTORY_F.write_text("[]", encoding="utf-8")
    return {"ok": True}

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

# ── WebSocket processing ──────────────────────────────────────
@app.websocket("/ws/process")
async def ws_process(ws: WebSocket):
    await ws.accept()

    async def send(**kw):
        await ws.send_text(json.dumps(kw))

    try:
        data      = json.loads(await ws.receive_text())
        api_key   = data.get("api_key", "")
        prompt    = data.get("prompt", "")
        images    = data.get("images", [])

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
                out_b64, out_name = await process_image(
                    img["data"], img.get("type","image/jpeg"),
                    name, prompt, api_key, send)

                if out_b64:
                    out_path = RESULTS / out_name
                    out_path.write_bytes(base64.b64decode(out_b64))
                    ok += 1
                    # ── Lưu vào lịch sử ──
                    append_history({
                        "id":        f"{int(time.time()*1000)}-{i}",
                        "timestamp": time.strftime("%d/%m/%Y %H:%M"),
                        "original":  name,
                        "result":    out_name,
                        "prompt":    prompt[:120],
                    })
                    await send(type="success", filename=name, result=out_name)
                else:
                    fail += 1
                    await send(type="fail", filename=name, message=f"❌ Thất bại: {name}")

            except Exception as e:
                fail += 1
                await send(type="fail", filename=name, message=f"❌ Lỗi: {name} — {str(e)[:100]}")

            if i < total - 1:
                await asyncio.sleep(1)

        await send(type="done", ok=ok, fail=fail, total=total)

    except WebSocketDisconnect: pass
    except Exception as e:
        log.error(e)
        try: await ws.send_text(json.dumps({"type":"error","message":str(e)}))
        except: pass

# ── Image processing ──────────────────────────────────────────
async def process_image(b64, mt, name, prompt, api_key, send):
    headers  = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    out_name = f"{Path(name).stem}_renewed.png"

    async with httpx.AsyncClient(timeout=180) as c:
        await send(type="log", message="  ✏️  Gửi ảnh gốc để chỉnh sửa...")
        r = await c.post("https://api.x.ai/v1/images/edits", headers=headers, json={
            "model": "grok-imagine-image",
            "prompt": prompt,
            "image": {"type": "image_url", "url": f"data:{mt};base64,{b64}"}
        })
        if r.status_code != 200:
            raise Exception(f"Edit API lỗi {r.status_code}: {r.text[:200]}")

        item    = r.json()["data"][0]
        out_b64 = item.get("b64_json")
        if not out_b64:
            url = item.get("url","")
            if not url: raise Exception("API không trả về ảnh")
            dl      = await c.get(url, timeout=60)
            out_b64 = base64.b64encode(dl.content).decode()

        return out_b64, out_name

# ── Serve frontend ────────────────────────────────────────────
@app.get("/")
async def root():
    return FileResponse(str(STATIC / "index.html"))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))

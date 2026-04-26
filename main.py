"""
POD Renew Tool — Web App Backend
"""
import asyncio, base64, json, logging, os, re, time, zipfile, io
from pathlib import Path
from typing import Optional
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

BASE    = Path(__file__).parent
STATIC  = BASE / "static"
RESULTS = BASE / "results"
RESULTS.mkdir(exist_ok=True)

ACCESS_PW = os.getenv("ACCESS_PASSWORD", "pod2024")   # đổi trong Railway env

app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

# ── Auth ─────────────────────────────────────────────────────
@app.get("/api/auth")
async def auth(password: str):
    return {"ok": password == ACCESS_PW}

# ── Scan kết quả cũ ──────────────────────────────────────────
@app.get("/api/results")
async def list_results():
    files = sorted(RESULTS.glob("*.png"), key=lambda f: f.stat().st_mtime, reverse=True)
    return [{"name": f.name, "size": f.stat().st_size} for f in files[:50]]

# ── Download ảnh đã xử lý ────────────────────────────────────
@app.get("/api/download/{filename}")
async def download(filename: str):
    f = RESULTS / filename
    if not f.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(str(f), media_type="image/png",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

# ── Download tất cả dưới dạng ZIP ────────────────────────────
@app.get("/api/download-all")
async def download_all():
    files = list(RESULTS.glob("*.png"))
    if not files:
        raise HTTPException(404, "No results")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, f.name)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="pod_results.zip"'})

# ── WebSocket: xử lý batch ───────────────────────────────────
@app.websocket("/ws/process")
async def ws_process(ws: WebSocket):
    await ws.accept()

    async def send(**kw):
        await ws.send_text(json.dumps(kw))

    try:
        raw  = await ws.receive_text()
        data = json.loads(raw)

        api_key   = data.get("api_key", "")
        prompt    = data.get("prompt", "")
        images_b64= data.get("images", [])   # list of {name, data (base64), type}

        if not api_key:
            await send(type="error", message="Chưa nhập API key!"); return
        if not prompt:
            await send(type="error", message="Chưa nhập prompt!"); return
        if not images_b64:
            await send(type="error", message="Chưa chọn ảnh!"); return

        total = len(images_b64)
        await send(type="start", total=total)

        ok = fail = 0
        results = []

        for i, img_data in enumerate(images_b64):
            name    = img_data["name"]
            raw_b64 = img_data["data"]
            mt      = img_data.get("type", "image/jpeg")

            await send(type="progress", current=i+1, total=total,
                       filename=name, message=f"[{i+1}/{total}] → {name}")

            try:
                out_b64, out_name = await process_image(
                    raw_b64, mt, name, prompt, api_key, ws, send)

                if out_b64:
                    # Lưu vào results/
                    out_path = RESULTS / out_name
                    out_path.write_bytes(base64.b64decode(out_b64))
                    ok += 1
                    results.append(out_name)
                    await send(type="success", filename=name,
                               result=out_name,
                               preview=f"data:image/png;base64,{out_b64[:200]}...",
                               message=f"✅ Xong: {name}")
                else:
                    fail += 1
                    await send(type="fail", filename=name,
                               message=f"❌ Thất bại: {name}")

            except Exception as e:
                fail += 1
                await send(type="fail", filename=name,
                           message=f"❌ Lỗi: {name} — {str(e)[:100]}")

            if i < total - 1:
                await asyncio.sleep(1)

        await send(type="done", ok=ok, fail=fail, total=total,
                   results=results,
                   message=f"🎉 Hoàn thành! {ok}/{total} ảnh thành công.")

    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.error(e)
        try: await ws.send_text(json.dumps({"type":"error","message":str(e)}))
        except: pass

# ── Image processing logic ────────────────────────────────────
async def process_image(b64, mt, name, prompt, api_key, ws, send):
    headers = {"Authorization": f"Bearer {api_key}",
               "Content-Type": "application/json"}

    await send(type="log", message="  🎨 Đang chỉnh sửa ảnh với Grok Imagine...")

    async with httpx.AsyncClient(timeout=120) as c:
        # Dùng /images/edits — gửi ảnh gốc + prompt, nhận ảnh đã chỉnh sửa
        r = await c.post(
            "https://api.x.ai/v1/images/edits",
            headers=headers,
            json={
                "model": "grok-imagine-image",
                "prompt": prompt,
                "image": {
                    "type": "image_url",
                    "url": f"data:{mt};base64,{b64}"
                },
                "response_format": "b64_json"
            }
        )

        if r.status_code != 200:
            # Fallback: dùng generations nếu edits không hỗ trợ
            await send(type="log", message="  🔍 Thử phân tích ảnh trước...")
            r_vis = await c.post(
                "https://api.x.ai/v1/chat/completions",
                headers=headers,
                json={
                    "model": "grok-2-vision",
                    "messages": [{"role": "user", "content": [
                        {"type": "image_url",
                         "image_url": {"url": f"data:{mt};base64,{b64}"}},
                        {"type": "text",
                         "text": f"Describe this image for regeneration, then apply: {prompt}. Reply ONLY with a generation prompt in English."}
                    ]}],
                    "max_tokens": 400,
                }
            )
            if r_vis.status_code != 200:
                raise Exception(f"Vision API lỗi {r_vis.status_code}: {r_vis.text[:150]}")

            gen_prompt = r_vis.json()["choices"][0]["message"]["content"].strip()
            await send(type="log", message=f"  🖼️  Tạo ảnh mới...")

            r2 = await c.post(
                "https://api.x.ai/v1/images/generations",
                headers=headers,
                json={"model": "grok-2-image-1212", "prompt": gen_prompt,
                      "n": 1, "response_format": "b64_json"}
            )
            if r2.status_code != 200:
                raise Exception(f"Image gen lỗi {r2.status_code}: {r2.text[:150]}")
            r = r2

        item = r.json()["data"][0]
        out_b64 = item.get("b64_json")

        if not out_b64 and "url" in item:
            dl = await c.get(item["url"])
            out_b64 = base64.b64encode(dl.content).decode()

        stem = Path(name).stem
        out_name = f"{stem}_renewed.png"
        return out_b64, out_name

# ── Serve frontend ────────────────────────────────────────────
@app.get("/")
async def root():
    return FileResponse(str(STATIC / "index.html"))

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)

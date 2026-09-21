#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scribd_service.py — Scribd 下载服务（FastAPI）

服务器用内置 _scribd_session cookie 走完鉴权链，把 S3 预签名 URL 交给用户浏览器
（S3 签名只绑 host，用户浏览器可直接下载，服务器零带宽）。

环境变量:
  SCRIBD_COOKIE   必填, "_scribd_session=..." 会话 cookie
  SCRIBD_TOKEN    选填, 设置后所有接口需 ?token= 或 Authorization 头, 防止公网裸奔
  PORT            默认 8090

接口:
  GET /                        简易 WebUI
  GET /healthz                 健康检查
  GET /api/link?url=<文档链接>  返回 {filename, direct_url, expires_in}（JSON, 交给前端触发下载）
  GET /dl?url=<文档链接>        302 重定向到 S3 预签名 URL（浏览器直接下载, 最省流量）
  GET /api/proxy?url=<文档链接> 服务端代理流式传输（S3 被墙/客户端无法直连 S3 时用）

运行:
  SCRIBD_COOKIE="_scribd_session=..." uvicorn scribd_service:app --host 0.0.0.0 --port 8090
"""

import os
import re
import urllib.error
import urllib.parse
import urllib.request
from functools import lru_cache

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

COOKIE = os.environ.get("SCRIBD_COOKIE", "")
TOKEN = os.environ.get("SCRIBD_TOKEN", "")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")

app = FastAPI(title="Scribd Downloader")


# ---------- 核心链路（与 scribd_download.py 同款逻辑） ----------

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def extract_doc_id(target: str) -> str:
    target = target.strip()
    if target.isdigit():
        return target
    m = re.search(r"scribd\.com/(?:document|doc|doc-page)/(\d+)", target)
    if not m:
        raise ValueError("无法从链接提取文档 id")
    return m.group(1)


def follow_chain(cookie: str, url: str, timeout: int = 30, max_hops: int = 10):
    """手动跟随 302 链, 返回 (final_resp, final_headers, hops)。scribd 域带 cookie。"""
    hops = []
    cur = url
    opener = urllib.request.build_opener(NoRedirect())
    for _ in range(max_hops):
        host = urllib.parse.urlparse(cur).netloc
        headers = {"User-Agent": UA, "Accept": "*/*", "Upgrade-Insecure-Requests": "1"}
        if "scribd.com" in host and cookie:
            headers["Cookie"] = cookie
        req = urllib.request.Request(cur, headers=headers)
        try:
            resp = opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if 300 <= e.code < 400 and e.headers.get("Location"):
                nxt = e.headers["Location"]
                hops.append((e.code, cur, nxt))
                cur = nxt if nxt.startswith("http") else urllib.parse.urljoin(cur, nxt)
                continue
            raise
        hops.append((resp.status, cur, None))
        return resp, {k.lower(): v for k, v in resp.headers.items()}, hops
    raise RuntimeError("重定向跳数超限")


def resolve_download(doc_id: str, ext: str = "pdf"):
    """走完整链, 返回 (s3_resp, headers, hops)。全程要求 modal-props 可用。"""
    if not COOKIE:
        raise RuntimeError("服务端未配置 SCRIBD_COOKIE")
    # 1) modal-props: 拿 download_url + 权限校验
    api = f"https://www.scribd.com/doc-page/download-receipt-modal-props/{doc_id}"
    try:
        resp, headers, _ = follow_chain(COOKIE, api, timeout=30)
        body = resp.read(65536)
    except Exception as e:
        raise RuntimeError(f"modal-props 请求失败: {e}")
    if headers.get("content-type", "").startswith("text/html"):
        raise RuntimeError("被 Fastly 人机验证拦截（cookie 失效），请更新 SCRIBD_COOKIE")
    import json
    try:
        props = json.loads(body.decode("utf-8"))
    except Exception:
        raise RuntimeError(f"modal-props 非 JSON: {body[:200]!r}")
    download_url = props.get("download_url")
    if not download_url:
        raise RuntimeError(f"无 download_url（账号无下载权限）: {props}")
    # 2) 下载链
    resp, headers, hops = follow_chain(COOKIE, f"{download_url}/?secret_password=null&extension={ext}")
    if headers.get("content-type", "").startswith("text/html"):
        raise RuntimeError("下载链返回 HTML（cookie 失效或无权限）")
    return resp, headers, hops


def check_token(request: Request, token: str):
    if TOKEN and token != TOKEN:
        raise HTTPException(401, "invalid token")


def parse_target(url: str) -> str:
    try:
        return extract_doc_id(url)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------- 路由 ----------

@app.get("/healthz")
def healthz():
    return {"ok": True, "cookie_configured": bool(COOKIE)}


@app.get("/api/link")
def api_link(request: Request, url: str = Query(...), ext: str = "pdf", token: str = ""):
    check_token(request, token)
    doc_id = parse_target(url)
    try:
        resp, headers, hops = resolve_download(doc_id, ext)
        resp.close()
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    cd = headers.get("content-disposition", "")
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd) or re.search(r'filename="?([^";]+)"?', cd)
    filename = urllib.parse.unquote(m.group(1)) if m else f"{doc_id}.{ext}"
    s3_url = hops[-1][1] if hops and hops[-1][0] == 200 else None
    if not s3_url or "amazonaws.com" not in s3_url:
        raise HTTPException(502, "未取到 S3 预签名 URL")
    return {"doc_id": doc_id, "filename": filename, "direct_url": s3_url,
            "expires_in": 300, "size_hint": headers.get("content-length")}


@app.get("/dl")
def dl(request: Request, url: str = Query(...), ext: str = "pdf", token: str = ""):
    """浏览器直链: 302 到 S3 预签名 URL"""
    check_token(request, token)
    info = api_link(request, url=url, ext=ext, token=token)  # token 已校验
    return RedirectResponse(info["direct_url"], status_code=302)


@app.get("/api/proxy")
def api_proxy(request: Request, url: str = Query(...), ext: str = "pdf", token: str = ""):
    """服务端代理流式下载（客户端无法直连 S3 时用）"""
    check_token(request, token)
    doc_id = parse_target(url)
    try:
        resp, headers, _ = resolve_download(doc_id, ext)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    cd = headers.get("content-disposition", "")
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd) or re.search(r'filename="?([^";]+)"?', cd)
    filename = urllib.parse.unquote(m.group(1)) if m else f"{doc_id}.{ext}"
    def gen():
        try:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            resp.close()
    return StreamingResponse(gen(), media_type=headers.get("content-type", "application/octet-stream"),
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Scribd 下载</title>
<style>
body{font-family:-apple-system,sans-serif;max-width:640px;margin:60px auto;padding:0 16px;color:#222}
h1{font-size:22px}input{width:100%;box-sizing:border-box;padding:12px;border:1px solid #ccc;border-radius:8px;font-size:15px}
button{margin-top:12px;width:100%;padding:12px;border:0;border-radius:8px;background:#2563eb;color:#fff;font-size:15px;cursor:pointer}
button:disabled{opacity:.5}#msg{margin-top:14px;font-size:14px;min-height:20px;color:#666}
a.ok{color:#059669;word-break:break-all}
</style></head><body>
<h1>Scribd 文档下载</h1>
<input id="u" placeholder="粘贴 scribd 文档链接，如 https://www.scribd.com/document/...">
<button onclick="go()" id="b">解析并下载</button><div id="msg"></div>
<script>
async function go(){
  const u=document.getElementById('u').value.trim(), m=document.getElementById('msg'), b=document.getElementById('b');
  if(!u){m.textContent='请输入链接';return}
  b.disabled=true;m.textContent='解析中...';
  try{
    const r=await fetch('/api/link?url='+encodeURIComponent(u));
    const j=await r.json();
    if(!r.ok) throw new Error(j.detail||('HTTP '+r.status));
    m.innerHTML='✓ 文件名: <b>'+j.filename+'</b> · <a class="ok" href="'+j.direct_url+'" download>点此下载</a>（链接 5 分钟内有效，已自动开始）';
    const a=document.createElement('a');a.href=j.direct_url;a.download=j.filename;document.body.appendChild(a);a.click();a.remove();
  }catch(e){m.textContent='✗ '+e.message}
  b.disabled=false;
}
</script></body></html>"""


@app.get("/")
def index():
    return HTMLResponse(PAGE)

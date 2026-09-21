# scribd-dl-service

给定 Scribd 文档链接，自动完成下载的服务。基于对 Scribd 下载链路的逆向：

```
GET /doc-page/download-receipt-modal-props/{id}        → download_url
GET /document_downloads/{id}/?secret_password=null&extension=pdf
  → 302 /document_downloads/direct/{id}?extension=pdf&ft=&lt=&user_id=&uahk=
  → 302 S3 预签名 URL（X-Amz-Expires=300，签名只绑 host）
```

服务器只承担前两跳鉴权（持有 `_scribd_session` cookie），把 S3 预签名 URL 直接交给
用户浏览器下载——**服务器零带宽消耗**。

## 一键安装（Debian/Ubuntu，需 root 或 sudo）

```bash
git clone https://github.com/sd19092549191/scribd-dl-service.git
cd scribd-dl-service
SCRIBD_COOKIE='_scribd_session=你的cookie值' bash install.sh
```

- 有 Docker Compose → 自动容器化部署（`docker compose up -d --build`）
- 没有 → 自动 venv + systemd（服务名 `scribd-dl`，开机自启）

可选环境变量：`SCRIBD_TOKEN`（设置后所有接口需 `?token=xxx`，公网部署强烈建议）、`PORT`（默认 8090）。

## 如何拿 cookie

浏览器登录 scribd.com → F12 → Network → 任一 `scribd.com` 请求 → Request Headers
里 `Cookie:` 的值，只需要 `_scribd_session=...` 这一个即可。

## 接口

| 接口 | 说明 |
|---|---|
| `GET /` | WebUI，粘贴链接一键下载 |
| `GET /dl?url=<链接>` | 302 直链到 S3 预签名 URL，浏览器直接下载 |
| `GET /api/link?url=<链接>` | JSON: `{doc_id, filename, direct_url, expires_in, size_hint}` |
| `GET /api/proxy?url=<链接>` | 服务端流式代理下载（客户端无法直连 S3 时兜底） |
| `GET /healthz` | 健康检查 `{ok, cookie_configured}` |

`url` 参数支持完整链接或纯数字文档 id；`ext` 参数可选下载格式（默认 `pdf`）。

## 运维

```bash
systemctl status scribd-dl        # 查看 systemd 服务
docker compose logs -f            # 查看容器日志
nano .env && systemctl restart scribd-dl   # 换 cookie
```

## 注意

- `_scribd_session` 即账号登录态，请妥善保管，勿提交到任何仓库（`.gitignore` 已排除 `.env`）
- Scribd 账号有下载配额（`downloadLimit`），高频使用建议配置多账号 cookie 池
- 仅供个人学习研究，请尊重文档版权

# cs2th.cn 桌面账号接口契约（预留）

桌面端默认不访问公网。启用后使用 HTTPS JSON API：

## `POST /api/auth/login`

请求：

```json
{"username": "name", "password": "secret", "client": "cs2th-tools"}
```

响应：

```json
{"session_token": "...", "user": {"id": 1, "username": "name", "is_member": true, "member_until": 0}}
```

## `GET /api/auth/me`

请求头：`Authorization: Bearer <access_token>`。

响应：

```json
{"ok": true, "user": {"id": 1, "username": "name", "is_member": true, "member_until": 0}}
```

## `POST /api/auth/logout`

请求头同上。服务端可撤销 token；桌面端无论请求是否成功都会清理本地会话。

## `GET /api/desktop/product-price`

请求头：

```text
Authorization: Bearer <access_token>
X-CS2TH-Client: cs2th-tools
X-CS2TH-Version: 0.3.3
```

响应沿用炼金引擎价格结构，根对象至少包含 `ordinary` 或 `stat_trak`，
并带 `fetch_time`（格式 `YYYYmmdd_HHMMSS`）。桌面端使用原子写入缓存，
接口关闭时可读取已有缓存继续离线计算。

当前生产实现：

- APIData_MIP 从同轮现货行情生成 JSON，通过带 `ingest_token` 的内部接口
  向 CS2TH 推送 gzip 包，桌面端不直接访问数据库或 BUFF；
- 返回 `ETag`，客户端后续发送 `If-None-Match`，未更新时返回 `304`；
- 服务端校验账号权限：会员始终可用；后台开启“登录用户开放”时普通登录用户也可用。价格包以文件流返回，不常驻内存；
- 服务不可用时保留上次成功缓存，不中断离线计算；
- 价格响应应带 `snapshot_synced_at`、`snapshot_item_count` 和 `schema_version`，
  便于客户端校验版本与展示数据新鲜度。

## 饰品目录与磨损同步

饰品名称、paint index、品质、收藏品关系和 `min_float` / `max_float`
属于低频目录数据，不能混在高频价格接口里。建议提供：

### `GET /api/desktop/catalog/manifest`

请求头与价格接口相同。响应示例：

```json
{
  "schema_version": 1,
  "catalog_version": "2026-07-25.1",
  "generated_at": "2026-07-25T00:20:00+08:00",
  "price_schema_version": 1,
  "bundle_url": "/api/desktop/catalog/bundles/2026-07-25.1",
  "sha256": "...",
  "size": 1234567
}
```

### `GET /api/desktop/catalog/bundles/{catalog_version}`

返回压缩目录包，至少包含普通/暗金 `SkinTemplate` 与 `WeaponBox` 数据。
桌面端下载到临时目录，校验 SHA-256 和 `schema_version` 后再原子替换；
任一步失败都继续使用旧目录。目录版本变化后，服务端应同时发布与其匹配的
价格版本，客户端只有在 `catalog_version` 与价格响应一致时才切换，避免
新饰品模板和价格箱池不同步。

推荐检查频率：启动时检查一次，此后每 24 小时检查；价格仍按 5 分钟缓存。

安全要求：

- 生产环境仅允许 HTTPS；
- token 应短期有效、可撤销，不在本地保存密码；
- 登录和刷新需要限流；
- CORS 与浏览器无关，桌面 API 应单独验证客户端版本和 token；
- 小助手已使用 Windows DPAPI 加密 session token，不在普通 JSON 中保存明文。

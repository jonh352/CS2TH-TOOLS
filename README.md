# CS2TH 汰换小助手

与 CS2TH 配方广场保持一致视觉风格的 Windows 桌面工具：

- **炼金计算**：导入底物行情，支持扫描、目标磨损、特殊磨损模式，计算成本、期望、收益率和保本率。
- **炼金模拟**：五合一/十合一模拟，展示产物概率、磨损和估值，并可保存为配方。
- **配方管理**：文件夹分类、搜索、拖动排序、批量移动/删除，并可重新导入模拟。
- **特殊磨损查询**：输入目标皮肤与磨损前缀，按 CS2 `float32` 精度给出真实可达区间，并反算可作为底物的皮肤磨损。
- **材料采集**：按饰品和外观生成平台直达链接；也可粘贴 CS2TH
  配方链接，展示每种材料、数量和磨损区间，并使用接口返回的最新平台 ID
  跳转 BUFF、悠悠有品、C5GAME、ECOSteam、Steam；特殊磨损查询结果也可
  一键带入材料采集页。
- **Steam 库存管理**：本机 Chrome/Edge 登录、保存多账号轻量会话、获取库存、搜索和筛选；选中饰品可导入计算/模拟，右键可跳转交易平台。

性能处理：

- 主窗口先显示，六个功能页按首次访问延迟创建；
- Steam 网络、价格请求与炼金计算均不阻塞 Qt 主线程；
- 库存使用统一尺寸列表和可见区域图标懒加载；
- 配方页只在进入页面时刷新磁盘数据；
- Windows 炼金扫描进程数限制为 8，避免高核机器过度抢占。

账号区已接入 `cs2th.cn` 的真实账号接口，默认启用：

```powershell
$env:CS2TH_TOOLS_AUTH_ENABLED = "1"
$env:CS2TH_TOOLS_API_BASE = "https://cs2th.cn"
```

登录使用独立的 `cs2th-tools` 会话，不会挤掉网站或终端小助手会话。session token 使用
Windows DPAPI 加密后再保存到本机；启动时会在后台调用 `/api/auth/me`
校验并刷新账号权限。后端约定见 `docs/auth-api-contract.md`。

正式版默认从 CS2TH 价格接口同步，无需用户配置：

```powershell
$env:CS2TH_TOOLS_PRICE_ENABLED = "1"
```

客户端使用有权限的登录令牌访问 `/api/desktop/product-price`：会员始终可用，
后台开启“登录用户开放”时普通登录用户也可用。服务端返回 gzip，
并通过 `ETag` 避免重复下载。每 5 分钟最多检查一次；断网或服务暂时不可用时，
已有缓存仍可继续计算。缓存位置：

```text
%APPDATA%\CS2TH\Tools\alchemy\product_price_all.json
```

首次安装还没有缓存时，需要先登录有效会员账号并成功同步一次价格。

开发机默认直接检查 APIData_MIP 当前使用的现货快照：

```text
D:\APIData_BUFF\data\spot_price_snapshot.sqlite
```

点击开始计算时会在后台比较 `snapshot_meta.synced_at`；只有快照版本变化才
重新生成价格缓存。可以通过环境变量修改
路径，设为空字符串则关闭本地直连：

```powershell
$env:CS2TH_TOOLS_LOCAL_PRICE_SNAPSHOT = "D:\APIData_BUFF\data\spot_price_snapshot.sqlite"
```

饰品模板与磨损范围以 CS2TH 使用的目录版本为准。当前已同步“蔓藤纹收藏品”
（箱池 `536`），包含 paint index、品质、上下级关系、Steam 五档名称及
`min_float` / `max_float`。元数据更新后应重新生成价格缓存，避免出现
“模板已更新但新箱池没有价格”的半更新状态。

开发机也可以从 CS2TH 当前使用的现货 SQLite 快照生成缓存：

```powershell
.\.venv\Scripts\python.exe .\scripts\import_cs2th_prices.py `
  --snapshot D:\APIData_BUFF\data\spot_price_snapshot.sqlite `
  --tools-root D:\CS2TH-tools `
  --fallback D:\APIData_MIPNEW\CSMarketPrice\product_price_all.json `
  --output "$env:APPDATA\CS2TH\Tools\alchemy\product_price_all.json"
```

转换器读取 `bucket_min_prices` 作为产物估值，并按本项目皮肤元数据重建
炼金算法所需的箱子、品质、暗金、归一化磨损及 paint index 层级。

## 平台饰品 ID（发版数据）

小助手运行时只读取以下三个静态文件中的 `buff`、`yyyp`、`c5`、`eco`
映射，不会在线搜索饰品 ID，也不会从多个来源动态覆盖：

```text
meta\SkinTemplate.jsonl
meta\SkinTemplate_st.jsonl
meta\SkinTemplate_mem.jsonl
```

唯一主库位于 CS2TH 项目的 `data/market_platform_ids.json`。准备发布小助手时，
从 CS2TH 项目执行一键刷新与导出：

```powershell
cd D:\cs2th
$env:ECO_PARTNER_ID = "你的 PartnerId"
$env:ECO_PRIVATE_KEY_FILE = "D:\secrets\eco-private-key.pem"

.\.venv\Scripts\cs2th.exe refresh-platform-ids `
  --export-tools D:\CS2TH-tools
```

如只需从已经更新好的 CS2TH 主库手动生成，不再次请求 ECO，可使用备用脚本：

```powershell
cd D:\CS2TH-tools
.\.venv\Scripts\python.exe .\scripts\sync_platform_ids.py --replace --write
```

ECO PartnerId 和 RSA 私钥只配置在 CS2TH 开发环境，严禁写入仓库或打进安装包。
生成后先测试，最后再构建安装包。

## 开发运行

```powershell
cd D:\CS2TH-tools
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

已安装环境可直接执行：

```powershell
.\run.ps1
```

Steam 自动化使用系统已安装的 Edge 或 Chrome，不需要执行 `playwright install chromium`。

## 打包（Windows）

### 推荐：安装包 Setup（通常 <100MB，与 CS2CT 同思路）

先安装 [Inno Setup 6](https://jrsoftware.org/isinfo.php)，然后：

```powershell
cd D:\CS2TH-tools
powershell -ExecutionPolicy Bypass -File .\build_setup.ps1
```

产出：

```text
dist\CS2TH-Tools_Setup_v0.3.3.exe
```

流程：精简 onedir → Inno LZMA2 压缩。对 Playwright 的 `node.exe`、枪图等「未预压缩」文件压缩率更好。

### 可选：单文件便携 exe（约 110MB+）

```powershell
powershell -ExecutionPolicy Bypass -File .\build_setup.ps1 -OneFile
```

得到 `dist\CS2TH-Tools.exe`。PyInstaller onefile 内部已用 zlib 打包，再 zip/xz 几乎压不下去；要到 100MB 以内请用上面的 Setup。

说明：

- 需本机已装 Edge 或 Chrome（库存/平台登录）
- 体积优化：不整包收集 PySide6；Playwright 仅保留运行驱动
- 若 pip 超时：`pip install -r requirements-build.txt -i https://pypi.tuna.tsinghua.edu.cn/simple`

## 数据目录

用户配置、配方、价格缓存、Steam 登录态和库存快照保存在：

```text
%APPDATA%\CS2TH\Tools
```

登录态不会写入项目目录，也不会提交到 Git。

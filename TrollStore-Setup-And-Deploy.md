# TrollStore 安装与部署流程存档

> 本文档梳理本机（Windows）从零搭建 TrollStore 环境，到实现 Kline-TS 自动部署闭环的全过程，
> 作为会话任务存档。适用于 iPad mini 4（iPad5,2，iOS 15.8.8，UDID `b36adcb0…f81fe`）。

## 目录

- [一、总体两条线](#一总体两条线)
- [二、电脑端工具链搭建](#二电脑端工具链搭建)
  - [2.1 TrollRestore 目录来源](#21-trollrestore-目录来源)
  - [2.2 Python 虚拟环境](#22-python-虚拟环境)
  - [2.3 自研脚本清单](#23-自研脚本清单)
- [三、iPad 端 TrollStore 安装](#三ipad-端-trollstore-安装)
  - [3.1 sparserestore 注入 Persistence Helper](#31-sparserestore-注入-persistence-helper)
  - [3.2 安装 TrollStore 并设置持久化助手](#32-安装-trollstore-并设置持久化助手)
  - [3.3 安装 Kline（ad-hoc 签名 IPA）](#33-安装-klinead-hoc-签名-ipa)
- [四、自动化部署闭环（现行形态）](#四自动化部署闭环现行形态)
  - [4.1 GitHub Actions 构建产物规格](#41-github-actions-构建产物规格)
  - [4.2 助手自动部署流水线](#42-助手自动部署流水线)
  - [4.3 自动监控轮询规则](#43-自动监控轮询规则)
  - [4.4 关键端口与端点速查](#44-关键端口与端点速查)
- [五、踩坑记录与解决方案](#五踩坑记录与解决方案)

---

## 一、总体两条线

| 线 | 目标 | 关键工具 | 状态 |
| --- | --- | --- | --- |
| 电脑端 | 装好 TrollStore 安装/部署工具链 | TrollRestore + `.venv-ios` + 自研脚本 | ✅ 完成 |
| iPad 端 | 永久侧载 Kline（绕过 7 天签名） | TrollStore + ad-hoc 签名 IPA | ✅ 完成 |
| 闭环 | push 代码 → 自动构建 → 自动部署 | GitHub Actions + deploy_gui.py | ✅ 打通 |

核心思路：**TrollStore 利用 CoreTrust 漏洞实现未签名/临时签名 IPA 的永久安装**，因此 CI 产物不需要正式开发者签名，只做 ad-hoc 签名即可被 TrollStore 识别安装。

---

## 二、电脑端工具链搭建

### 2.1 TrollRestore 目录来源

- 核心工具来自开源项目 [`ssuukk/TrollRestore`](https://github.com/ssuukk/TrollRestore)（sparserestore 漏洞 + trollstore-ipatool，用于装 TrollStore）。
- 用户已 fork 该仓库，并把本地自研脚本（`deploy_gui.py`、`sandbox_cli.py`、`remote_update.py`、`deploy_kline_to_ipad.py`、`migrate_tdxdb.py` 等，venv 除外）全部合入自己的 fork。
- 目录内保留上游文件：
  - `trollstore.py`：TrollStore 安装器主程序（需要连接设备交互运行）。
  - `sparserestore/`：Sparserestore 库（CVE-2024-44252，通过备份恢复把 TrollHelper 二进制写入系统 App 容器）。
  - `TrollRestore.spec`、`requirements.txt`：打包与依赖定义。

### 2.2 Python 虚拟环境

| 环境 | 路径 | 用途 | 状态 |
| --- | --- | --- | --- |
| `.venv-ios` | `c:\Users\sunck\home\projects\ios\.venv-ios` | **现行主环境**（Python 3.10；pymobiledevice3 11.2.4 + PySide2 5.15.2.1） | ✅ 使用中 |
| `TrollRestore/venv` | `TrollRestore\venv` | 上游开源项目自带的旧环境 | ❌ 废弃，可删 |

> 所有操作统一走 `.venv-ios`：`c:\Users\sunck\home\projects\ios\.venv-ios\Scripts\python.exe`。

### 2.3 自研脚本清单

| 脚本 | 作用 | 状态 |
| --- | --- | --- |
| `deploy_gui.py` | PySide2 自动部署助手：拉取 IPA → 沙盒直连上传 → 触发 TrollStore 安装 → 版本检测闭环 | ✅ 现行主力 |
| `sandbox_cli.py` | 沙盒直连 CLI（`ls/get/put/rm/cat`），经 usbmux forward 5051 直接读写 Kline-TS 的 Documents | ✅ 现行 |
| `remote_update.py` | 远程更新：usbmux forward 5051 + POST `/install` / `/install-local` 触发安装 | ✅ 现行 |
| `deploy_kline_to_ipad.py` | AFC 推送 IPA 到公共区 `/var/mobile/Media/Downloads/` | ⚠️ 已退役（文件保留供参考） |
| `migrate_tdxdb.py` | 行情库迁移：把电脑上的 `tdx.db`（3611 标的）流式上传到沙盒并导入 | ✅ 已用（一次性） |
| `bench/method1~4_*.py` | 四种传输方式基准测试（house_arrest / webinspector / ES HTTP / AFC Downloads） | ✅ 调研产物 |
| 各类探针脚本 | `probe_house_arrest.py`、`list_services.py`、`open_url.py`、`push_to_*.py`、`cdp_navigate.py`、`wi_launch.py` 等 | ✅ 过程验证用 |

---

## 三、iPad 端 TrollStore 安装

### 3.1 sparserestore 注入 Persistence Helper

1. iPad 连接电脑，`.venv-ios` 中运行：
   ```sh
   python.exe trollstore.py
   ```
2. 选择要替换的可移除系统 App（选 Tips「提示」，因为它可删可重下）。
3. 工具利用备份漏洞（CVE-2024-44252）把 TrollHelper 二进制恢复到该系统 App 容器，替换其主二进制。

> 注意：替换后该 App 只剩 TrollHelper 功能，要恢复原状只能删除后从 App Store 重装。

### 3.2 安装 TrollStore 并设置持久化助手

1. iPad 上打开被替换的 App → 出现 TrollHelper 界面 → 点击安装 **TrollStore**。
2. 打开 TrollStore → **Settings → Install Persistence Helper**，仍选 Tips（推荐用同一个 App 作为持久化助手）。
3. 验证：重启后 TrollStore 仍可打开，持久化助手生效。

### 3.3 安装 Kline（ad-hoc 签名 IPA）

- Kline 的 CI 产物是 **ad-hoc 签名 + 注入 entitlements** 的 IPA（`Kline-uns*` 上传为 `ios/artifacts` 产物，显示名 **Kline-TS**）。
- 安装方式：TrollStore 右上角 + → 选择 IPA；或通过 `apple-magnifier://install?url=<IPA_URL>` 拉起 TrollStore 安装。
- **错误码 173 的根因与修复**：IPA 完全未签名时 TrollStore 报 173 → 在 build.yml 中加入 `codesign -f -s - --deep` 做 ad-hoc 签名即可。

---

## 四、自动化部署闭环（现行形态）

### 4.1 GitHub Actions 构建产物规格

build.yml（`.github/workflows/build.yml`）产物规格：

- 构建：`xcodebuild -project Kline.xcodeproj`（无 CocoaPods），archive 时加 `-allowProvisioningUpdates`。
- 远端 workflow 设 `CODE_SIGNING_ALLOWED=NO` 禁用正式签名 → 产物 `.app` 做 **ad-hoc 签名**（`codesign -f -s - --deep`）。
- 注入 entitlements（`Kline.entitlements`）：
  - `com.apple.private.security.no-sandbox`
  - `platform-application`（**no-sandbox 生效的必要条件**，否则启动闪退）
  - `com.apple.private.security.storage.AppDataContainers`（修复写自身容器 513）
- PlistBuddy 注入：
  - `CFBundleDisplayName = Kline-TS`（与 Xcode 编译版「Kline」区分）
  - `CFBundleVersion = $GITHUB_RUN_NUMBER`（唯一构建号，供部署助手区分新旧版本）
    - **坑**：PlistBuddy `Set` 不支持类型参数（会写入字面 `string 135`），必须 `Delete + Add`。

### 4.2 助手自动部署流水线

TRAE 侧工作流（只需两件事）：

1. 本地 push 代码，`gh run watch` 监控构建；失败则继续修。
2. 构建前先 `POST http://127.0.0.1:5052/autopoll -d '{"enabled":true}'` 确保助手自动轮询开启。
3. 构建成功 → **结束会话**，剩余由助手自动完成。

助手（deploy_gui.py）自动执行：

1. **PollWorker** 每 30s `gh run list` 发现新的 `completed + success` 且未处理过的 run。
2. `gh run download <run>` 拉取 IPA 到固定路径（`ios/artifacts`）。
3. 检查 Kline-TS 是否前台（不在前台则等待，超时提示）。
4. 经 usbmux forward 5051 + KlineHTTP `PUT /sandbox/Downloads/Kline.ipa`（流式上传到沙盒）。
5. `POST /install-local {"file":"Downloads/Kline.ipa","scope":"sandbox"}` → Kline 内 Safari 拉起 `apple-magnifier://install?url=http://127.0.0.1:5051/sandbox/Downloads/Kline.ipa`。
6. 用户在 iPad 上点「打开」+「Install」。
7. 助手保持 USB 转发，轮询 KlineHTTP `GET /`：新版 Kline（version 匹配）打开后 → 从「等待 iPad 确认安装」自动转为「✅ 部署完成」（超时 300s 提示重试）。

### 4.3 自动监控轮询规则

- **默认开启**：TRAE 每次 push 构建时 `POST /autopoll` 开启。
- **一次性触发语义**：部署一开始即**彻底关闭**自动轮询（`poll_ctrl(True)` → `set_autopoll(False)`，不恢复），直至下次 TRAE 推送构建才重新开启。
- 目的：避免旧构建重复触发、避免部署期间反复轮询。
- `GET /status` 返回 `autopoll` 字段可查轮询状态。

### 4.4 关键端口与端点速查

| 端口 | 归属 | 用途 |
| --- | --- | --- |
| 5051 | KlineHTTPServer（App 内） | 沙盒直连 + 安装触发 + 版本上报 |
| 5052 | deploy_gui.py | 通知 / 自动轮询控制 / 状态查询 |
| 5053 | deploy_gui.py | IPA 下载服务（TrollStore 从本机拉 IPA） |

KlineHTTP 关键端点：

| 端点 | 作用 |
| --- | --- |
| `GET /` | 状态 JSON，含 `version`（short + build），供助手验证新版已打开 |
| `GET /status` | 状态查询 |
| `GET/PUT/POST/DELETE /sandbox/<path>` | 沙盒直连读写（限 Documents 内，防路径穿越） |
| `POST /install-local` | 触发 TrollStore 安装（`scope=sandbox` 走 `/sandbox/` 路径） |

---

## 五、踩坑记录与解决方案

| # | 现象 | 根因 | 解决 |
| --- | --- | --- | --- |
| 1 | TrollStore 安装报**错误码 173** | IPA 完全未签名 | build.yml 加 `codesign -f -s - --deep` ad-hoc 签名 |
| 2 | `apps pull com.sunck.Kline` 报 InstallationLookupFailed | Xcode 版 bundle id 实际带 Team ID 后缀（`com.sunck.Kline.4G3V8W86TN`） | 先 `apps list` 查完整 id；查 Xcode 版容器必须用完整 id |
| 3 | HouseArrestService 对 TrollStore 侧载 App 报 `AppNotInstalledError` | 该安装方式不在系统安装注册表登记容器 | 用 AFC 公共区 `/var/mobile/Media/Downloads/` 或 KlineHTTP 沙盒直连 |
| 4 | 仅 no-sandbox（或 +container-required=false）启动即闪退 | exec 阶段被拦截 | 必须配 `platform-application`（no-sandbox 生效的必要条件） |
| 5 | UIActivityViewController 共享面板崩溃 | platform-application 副作用（CoreImage GL 崩溃） | 改用本地 HTTP + URL Scheme 安装（`apple-magnifier://install`） |
| 6 | 写自身容器报 NSCocoaErrorDomain 513 | platform-application 副作用② | 补 `com.apple.private.security.storage.AppDataContainers=true` |
| 7 | App 切后台后 HTTP 监听 socket 被冻结 | 无 VoIP/audio/location 等后台权限 | 部署时要求 Kline-TS 保持前台；助手检测前台状态并等待 |
| 8 | `com.apple.developer.*` 类后台权限注入后不生效 | 需 provisioning profile 对应 capability，AMFI 忽略纯 entitlement | 放弃注入，走前台方案 |
| 9 | PlistBuddy 注入 CFBundleVersion 写成字面 `string 135` | `Set` 命令不支持类型参数 | `Delete + Add` 显式指定 string 类型 |
| 10 | Kline 切后台导致助手卡在等待安装 | Safari 重定向把 App 切后台 | 手动重新打开 Kline 保持前台；助手版本检测逻辑需重启进程才生效 |
| 11 | Python urllib 非 ASCII URL 报错 | urllib 不支持未编码 URL | 客户端 `quote(path, safe="/")` |
| 12 | 沙盒直连 DELETE/子路径 404/500 | 服务器入口只解一处 URL 编码 | `handleNew` 与 `dispatch` 两处都要 `removingPercentEncoding` |
| 13 | 大文件上传 OOM | 攒内存再写盘 | KlineHTTP 流式写 FileHandle（1.35GB tdx.db 实测通过） |
| 14 | TrollStore 版（System 态）读不了 Xcode 版（User 态）容器 | data vault 隔离 | 跨态数据迁移必须经电脑中转（house_arrest 拉取 / 沙盒流式上传再导入） |
| 15 | 文件 App 只能看到一个 Kline 沙盒（初期困惑） | 双 Kline 并存：Xcode 版 User 态 + TrollStore 版 System 态（显示名 Kline-TS） | 靠显示名区分；文件 App 可看到两个沙盒，可互拷文件但够不到公共区 |
| 16 | 助手重启前旧检测逻辑不生效 | 进程内存中旧代码 | 改代码后必须重启 deploy_gui.py |

---

## 附：一次完整部署的操作流程（速查）

```sh
# 1. TRAE 侧：push 代码前确保助手轮询开启
curl -X POST http://127.0.0.1:5052/autopoll -d '{"enabled":true}'

# 2. push 代码并监控构建
git push
gh run watch

# 3. 构建成功 → TRAE 结束会话
# 助手自动：拉 IPA → 沙盒上传 → 触发安装 → 用户 iPad 点确认 → 版本验证 ✅

# 4. 手动状态查询
curl http://127.0.0.1:5052/status
```

---

## 六、未来设想：改造 TrollStore 实现 iPad 零触碰部署（2026-09-07 存档，暂不动手）

> 现状：闭环的最后一步「用户 iPad 点确认安装」是唯一手动动作。
> 设想：TrollStore 完全开源（MIT，`opa334/TrollStore`），改造它即可去掉这一步，甚至让部署基础设施下沉到系统层。

### 为什么可行

- **安装确认弹窗是 TrollStore 主 App 内的 UI，不是系统弹窗**。URL scheme（`apple-magnifier://install`）在 App 内 handler 处理，走「下载 → 弹确认 → 调 `trollstore_installer`」；改掉确认步即可静默安装。
- **TrollStore 可安装自己的修改版**：fork 后 macOS runner CI 构建 `.tipa`，用现有 TrollStore 覆盖安装 / OTA 更新（root 签名，不受普通签名限制）。
- **TrollStore 是 root + no-sandbox**，若内置 HTTP / 轮询能力，比 KlineHTTP 更强：
  - 直接读写**任何** App 沙盒（替代 `/sandbox` 端点）；
  - 直接读安装注册表拿 Kline 版本（替代 Kline 上报 `GET /` version）；
  - 自己就是安装器（替代 Kline 中转触发 `POST /install-local`）。

### 分层方案与代价

| 方案 | 改动量 | 风险 | 效果 |
| --- | --- | --- | --- |
| A. 只去掉确认弹窗 | 小（改 URL handler） | 低 | 静默安装打通，其余不变 |
| B. TrollStore 内置 GitHub 轮询 + 自动下载安装 | 中 | 中 | KlineHTTP 的触发 + 版本上报退役 |
| C. 全量下沉（吸收 KlineHTTP + 部署助手） | 大 | 中高 | iPad 自治：`git push` → 构建 → iPad 自动下载安装，deploy_gui.py 与 Kline 内嵌服务器全部退役 |

**共同代价**：
1. 需跟踪上游：TrollStore 频繁更新（新 iOS 支持、漏洞修复），fork 合并有冲突成本；
2. 系统级风险：root 安装器改错可能致设备级问题，需接受最坏刷机预期，建议先在 CI 构建验证；
3. 构建复杂度上升：需 macOS runner + installer 交叉编译，比现有 Kline 构建复杂。

**替代路径不可行**：非越狱环境只有 TrollStore 主进程能拉起带 entitlements 的 installer，Kline 无法自 spawn，改 TrollStore 是唯一正道。

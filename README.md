<div align="center">

# 闲鱼智控 Pro

**闲鱼自动发货、自动回复与 AI 智能议价一体化管理系统**

[![Language](https://img.shields.io/badge/Python-3.11-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux-4D4D4D?style=for-the-badge)
![UI](https://img.shields.io/badge/UI-Web%20React-0A8FCC?style=for-the-badge)
[![License](https://img.shields.io/badge/License-AGPL--3.0-2E7D32?style=for-the-badge&logo=gnu&logoColor=white)](LICENSE)

</div>

---

## 目录

- [项目简介](#项目简介)
- [界面预览](#界面预览)
- [核心功能](#核心功能)
- [系统架构](#系统架构)
- [技术栈](#技术栈)
- [环境要求](#环境要求)
- [快速开始](#快速开始)
- [容器化部署（Docker）](#容器化部署docker)
- [认证配置](#认证配置)
- [人工智能服务商配置](#人工智能服务商配置)
- [智能商品配图配置](#智能商品配图配置)
- [供应商代采配置](#供应商代采配置)
- [目录结构](#目录结构)
- [使用流程](#使用流程)
- [数据与安全](#数据与安全)
- [部署失败排查](#部署失败排查)
- [更新与验证](#更新与验证)
- [许可证](#许可证)
- [免责声明](#免责声明)
- [交流社群](#交流社群)

---

## 项目简介

本项目面向闲鱼卖家与电商自动化运营者，把「选品发现 — 智能做图 — 客服回复 — 下单发货 — 供应链代采 — 订单核对 — 买家自助补发 — 风控拉黑」整条无人值守全流程完全打通：

- **自动化履约发货**：买家付款后毫秒级识别订单，按商品绑定的卡密分组或关键词规则发放卡密、链接、图片或调用上游接口取卡。
- **供应商代采直发**：支持接入 91卡券云仓、亿权益、福禄等主流供应链。本地卡密不足时自动触发采购，内设 Decimal 级严格利润熔断与幂等防重机制。
- **买家自助补发**：买家在会话内回复「1」或「提取」即可在 2 小时安全窗口内自助提取，逐卡账本持久化，不漏发、不重发。
- **AI 智能商品配图**：内置高转化电商海报合成引擎，支持一键生成 800×800 高清主图、流程图与质保图，支持 12 款精选品牌调色板与外部 AI 接口智能降级。
- **蓝海机会与热词选品**：基于多维供需模型（供给 20%、真实需求 45%、供需比 25%、跨品类 10%）自动化挖掘商机，助力锁定高毛利、低竞争蓝海爆款。
- **智能客服与自动回复**：支持关键词精确/模糊匹配、账号默认回复，以及接入大模型的 AI 智能拟人对话与议价。
- **多账号集中托管**：同时管理多个闲鱼账号，支持扫码登录、Cookie 持久化与心跳保活、Token 刷新不掉线。
- **全方位风险控制**：恶意退款买家自动拉黑，命中黑名单的订单拦截发货；看门狗与异常报警实时守护。
- **全局运营看板**：营收趋势、商品分析、订单明细与库存余量多维实时监控。

> 说明：README 中所有界面截图均为**演示数据**，用于展示页面布局，与真实账号数据无关。
> 截图分浅色与深色两套，深色截图文件名带 `-dark` 后缀，均取自当前发布版本。

---

## 界面预览

### 仪表盘

店铺实时经营数据总览，包含累计营收、账号在线数、订单数与库存余量，以及营收趋势与商品分析。

<p align="center">
  <img src="docs/images/dashboard.png" alt="仪表盘" width="820">
</p>

### 账号管理

管理闲鱼授权账号，支持扫码添加、在线状态与自动回复开关。

<p align="center">
  <img src="docs/images/accounts.png" alt="账号管理" width="405">
  <img src="docs/images/accounts-dark.png" alt="深色主题 - 账号管理" width="405">
</p>

### 订单管理

订单列表支持状态筛选、一键同步、手动插入与立即发货。

<p align="center">
  <img src="docs/images/orders.png" alt="订单管理" width="820">
</p>

### 卡密库存

管理自动发货所需的卡密、链接或图片资源，支持批量库存、固定文本、API 取卡与图片四种类型。

<p align="center">
  <img src="docs/images/cards.png" alt="卡密库存" width="405">
  <img src="docs/images/cards-dark.png" alt="深色主题 - 卡密库存" width="405">
</p>

### 商品列表

监控并管理所有账号下的闲鱼商品，可单独设置自动发货与 AI 回复开关。

<p align="center">
  <img src="docs/images/items.png" alt="商品列表" width="820">
</p>

### 关键词回复

配置命中关键词后自动发送的回复内容，支持精确匹配与模糊匹配。

<p align="center">
  <img src="docs/images/keywords-reply.png" alt="关键词回复" width="820">
</p>

### 关键词发货

配置「商品标题/详情关键词 → 卡密分组」的兜底发货规则，并支持多规格条件匹配。

<p align="center">
  <img src="docs/images/keywords-delivery.png" alt="关键词发货" width="405">
  <img src="docs/images/keywords-delivery-dark.png" alt="深色主题 - 关键词发货" width="405">
</p>

### 账号默认回复

为指定账号配置默认回复内容，在未命中任何关键词时兜底发送。

<p align="center">
  <img src="docs/images/keywords-default.png" alt="账号默认回复" width="820">
</p>

### 黑名单管理

本地买家风控记录，支持退款凭证留档、永久白名单与按账号筛选。

<p align="center">
  <img src="docs/images/blacklist.png" alt="黑名单管理" width="405">
  <img src="docs/images/blacklist-dark.png" alt="深色主题 - 黑名单管理" width="405">
</p>

### 系统与 AI 设置

配置全局自动化规则、AI 智能回复、SMTP 邮件通知等系统参数。

<p align="center">
  <img src="docs/images/settings.png" alt="系统与 AI 设置" width="820">
</p>

### 深色主题

系统内置浅色 / 深色两套主题，点击界面右上角的主题按钮即可一键切换（切换结果保存在浏览器本地，首次访问会跟随系统偏好）。
下图为深色主题下的部分界面（完整 10 张深色截图见 `docs/images/`，命名规则为 `<页面>-dark.png`）。

**仪表盘**

<p align="center">
  <img src="docs/images/dashboard-dark.png" alt="深色主题 - 仪表盘" width="820">
</p>

**商品列表**

<p align="center">
  <img src="docs/images/items-dark.png" alt="深色主题 - 商品列表" width="820">
</p>

**订单管理**

<p align="center">
  <img src="docs/images/orders-dark.png" alt="深色主题 - 订单管理" width="820">
</p>

**关键词回复**

关键词管理页的选项卡与卡片在深色下使用深灰渐变（`#25282D → #1F2226`），替代浅色主题的浅灰渐变。

<p align="center">
  <img src="docs/images/keywords-reply-dark.png" alt="深色主题 - 关键词回复" width="820">
</p>

**账号默认回复**

<p align="center">
  <img src="docs/images/keywords-default-dark.png" alt="深色主题 - 账号默认回复" width="820">
</p>

**系统与 AI 设置**

<p align="center">
  <img src="docs/images/settings-dark.png" alt="深色主题 - 系统与 AI 设置" width="820">
</p>

---

## 核心功能

| 模块 | 能力 |
| --- | --- |
| 自动发货 | 付款触发、订单锁 + 冷却防重复、批量库存消费、API 动态取卡、图片发货、多数量发货 |
| 独立网页自提中心 | 吸收嗷嗷鱼 SaaS 闭环机制：聊天框只发动态加密提货短链，敏感卡密/网盘地址脱敏，规避 E07 违规封号；内置 IP 滑窗防枚举限流与 HMAC 令牌校验 |
| 阶梯降价促活引擎 | 吸收闲管家官方 ERP 机制：滞销商品每日小幅降价，利用闲鱼算法向潜客发送“降价红利”通知强拉曝光；带 `price_floor` 底价绝对保护与改价流水审计 |
| 真人拟态自动养号 | 吸收一尘/鱼游助手机制：每日随机时隙模拟真实用户浏览推荐流、随机超赞/点赞，消除“纯发货脚本号”特征，显著提升店铺权重与账号抗风控等级 |
| 捡漏雷达与多模态审图 | 吸收 ai-goofish-monitor 与 Craft-Anything：高频巡检低价货源，接入 Vision 多模态大模型智能识别成色与划痕，自动计算转售毛利，一键生成转售高清电商图 |
| 供应链代采直发 | 接入 91卡券云仓、亿权益、福禄等上游平台；本地库存不足自动代采，内嵌 Decimal 精度最低利润熔断校验（`MARGIN_BELOW_MINIMUM` 严格阻断），逐卡发货入账 |
| 自助补发与可靠履约 | 买家在会话发送「1」或「提取」2小时内自动补发卡密；逐卡持久化账本，不重复发送；发货后防漏提示，断网/重启断点续接 |
| AI 智能商品配图 | 原生 PIL 电商海报引擎，一键生成 800×800 高清主图、流程图与质保图，内置 12 套电商调色板，无缝支持外部 AI 生图 API 降级 |
| 蓝海机会与热词选品 | 供给(20%) + 真实需求(45%) + 供需比(25%) + 跨品类(10%) 多维加权评分模型，自动挖掘低竞争高需求品类与自动比价建议 |
| 自动回复与智能议价 | 关键词精确/模糊匹配、账号默认回复、图片回复、回复一次开关；接入大模型以商品标题/价格/详情为上下文智能应答与议价 |
| 账号管理与保活 | 扫码登录、Cookie 持久化、单连接 WebSocket 监听、Token 刷新心跳零中断（解决长时间挂机卡顿断线） |
| 商品管理与模板发布 | 多账号商品同步、封面自动回填、自动发货/AI 回复商品级开关、指定卡密分组；支持占位符模板与批量防风控限流发布 |
| 卡密库存与台账 | 批量/文本/API/图片四种类型、分组管理、库存消费与自动扣减、逐卡履约台账、发货后追加消息与变量替换 |
| 订单管理与导出 | 订单同步与导入、状态筛选、手动发货、订单详情、导出 Excel |
| 黑名单与风控 | 恶意退款买家自动拉黑、永久白名单、按账号与状态筛选、退款凭证留档、看门狗异常检测 |
| 消息通知与看门狗 | 企业微信、钉钉、飞书、Bark 及自定义 Webhook 多渠道推送；日志异步写盘（`enqueue=True`）不阻塞主线程 |
| 数据看板与经营诊断 | 营收趋势、商品分析、订单明细、店铺经营诊断与健康度评分；商业中枢 (`/automation-hub`) 集中监管自提、降价与养号 |

完整的发货判定流程与卡密消费规则见 [`自动发货逻辑说明.md`](自动发货逻辑说明.md)。

---

## 系统架构

```mermaid
flowchart TD
    subgraph 前端["前端（React 19 + TypeScript + Vite）"]
        UI[管理后台 UI / 仪表盘 / 机会选品 / 工作台]
    end

    subgraph 后端["后端（FastAPI + Asyncio）"]
        API[REST 接口与 WebSocket]
        AUTH[认证与令牌校验]
        CORE[自动化核心<br/>XianyuAutoAsync]
        RULE[发货判定与关键词匹配]
        AI[AI 回复与议价适配层]
        IMG[AI/本地商品配图引擎]
        SUPP[供应商代采框架<br/>91kami / 亿权益 / 福禄]
        HOT[蓝海热词与机会评分引擎]
        RELIABLE[逐卡发货账本与自助补发]
        NOTIFY[多渠道通知推送<br/>企微/钉钉/飞书/Bark]
    end

    subgraph 存储["本地存储与数据库（强制 SQLite WAL 模式）"]
        DB[(主数据库 xianyu_data.db<br/>商品/订单/卡密/黑名单)]
        WAL[(并发日志与台账库<br/>delivery_reliability / supplier_procurement)]
        CFG[global_config.yml<br/>管理员凭证 / 供应商秘钥 / AI 配置]
        COOKIE[Cookie 与浏览器持久化状态]
    end

    XY[闲鱼 WebSocket 与开放接口]
    LLM[OpenAI / Gemini 兼容模型]
    CLOUD[上游供应商开放平台<br/>91卡券 / 亿权益 / 福禄]

    UI -->|HTTP / WS| API
    API --> AUTH
    API --> CORE
    API --> RULE
    API --> AI
    API --> IMG
    API --> HOT
    CORE -->|心跳与消息收发| XY
    CORE --> COOKIE
    RULE --> DB
    AI -->|商品上下文应答| LLM
    CORE --> RELIABLE
    RELIABLE --> SUPP
    SUPP -->|采购发货请求| CLOUD
    RELIABLE --> WAL
    CORE --> DB
    API --> CFG
    CORE --> NOTIFY
    NOTIFY -->|Webhook 告警| EXT[移动端 / 报警群]
```

上图的调用关系：前端通过 REST 接口与 WebSocket 访问后端；`XianyuAutoAsync` 负责与闲鱼建立持久长连接、精准收发心跳与消息并执行发货动作；发货判定层联动供应商代采与逐卡可靠性账本，自动执行利润熔断；所有 SQLite 数据库在运行期强制启用 WAL 并发模式保障读写吞吐；管理员密码、供应商秘钥与 AI 服务商配置严格保留在根目录 `global_config.yml`，绝不外传或落库。

---

## 技术栈

**后端**：FastAPI、Python 3.11、SQLite（WAL 并发模式）、Playwright、WebSocket、Asyncio、Loguru（异步写盘）、PIL（电商海报合成）

**前端**：React 19、TypeScript、Vite、Tailwind CSS、Recharts、Lucide-React

**部署**：Docker Compose，可选 Nginx 反向代理

---

## 环境要求

- Python 3.11+
- Node.js 20+
- npm
- Windows 10/11 或常见 Linux 发行版

---

## 快速开始

### 一键启动（Windows）

Windows 用户可直接双击根目录的 `start.bat`，脚本会自动创建虚拟环境、安装依赖并启动服务。

### 手动启动

```bash
git clone https://github.com/245867/xianyu-auto-delivery.git
cd xianyu-auto-delivery

py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
playwright install chromium

cd frontend
npm install
npm run build
cd ..

python Start.py
```

启动完成后访问 <http://localhost:8080/>。

首次启动前请复制配置模板并修改其中的管理员密码：

```bash
copy global_config.example.yml global_config.yml   # Linux/macOS 使用 cp
```

---

## 容器化部署（Docker）

可选：在根目录创建 `.env`，至少设置强密码和随机密钥：

```env
ADMIN_USERNAME=admin
ADMIN_PASSWORD=replace-with-a-strong-password
JWT_SECRET_KEY=replace-with-at-least-32-random-characters
```

构建并启动：

```bash
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml ps
docker compose -f deploy/docker-compose.yml logs -f xianyu-app
```

国内镜像配置：

```bash
docker compose -f deploy/docker-compose-cn.yml up -d --build
```

可选 Nginx：

```bash
docker compose -f deploy/docker-compose.yml --profile with-nginx up -d --build
```

数据默认持久化到：

- `./data`：SQLite 数据库
- `./logs`：运行日志
- `./backups`：备份文件

---

## 认证配置

- 管理员账号和密码位于根目录 `global_config.yml` 的 `AUTH` 节。
- `access_token` 由你自行填写；可使用任意足够长的随机字符串。
- 可用 `python -c "import secrets; print(secrets.token_urlsafe(32))"` 生成随机 Token。
- `token_login_enabled: true` 开启 Token 登录。
- `token_only_enabled: true` 后只允许 Token 登录，账号密码和邮箱登录会被拒绝。
- 登录成功后服务端会另行签发有有效期的会话令牌，不会把根配置 Token 直接用作 API 会话令牌。

---

## 人工智能服务商配置

- 模型名、服务商模型标识、API 地址和 API Key 只在根目录 `global_config.yml` 的 `AI_PROVIDER` 节维护。
- 网页只显示配置状态，不显示或修改 API Key 与 API 地址。
- SQLite 数据库不会保存这些服务商敏感配置。

---

## 智能商品配图配置

系统支持内置高性能电商渲染引擎与外部生图 API：

- 配置文件位于根目录 `global_config.yml` 的 `IMAGE_GENERATION` 节。
- `provider`: 可选 `"auto"`（优先调用 AI 生图，未填 Key 或失败时平滑降级至内置高转化海报引擎）、`"canvas"`（纯本地 PIL 合成，零外部依赖，极速出图）、`"ai"`（仅使用外部 AI 生图）。
- 内置引擎专为闲鱼电商转化设计，自动从商品标题解析品牌并匹配 12 套高对比度渐变色板，支持一键批量输出 800×800 高清主图、使用步骤流程图与质保售后说明图。

---

## 供应商代采配置

系统支持在上游云仓对接自动提卡与代发：

- 供应商凭证在根目录 `global_config.yml` 的 `SUPPLIERS` 节集中配置，支持 91卡券云仓 (`91kami`)、亿权益 (`yiquanyi`)、福禄开放平台 (`fulu`) 等。
- 只有将对应平台的 `enabled` 设为 `true` 且填入商户凭证后，才会激活真实网络采购。
- **利润熔断保护机制**：每次代采前，系统均采用 `Decimal` 严格校验上游实时报价与售价差额。若毛利润低于预设门槛（`MARGIN_BELOW_MINIMUM`）或合同数据不完整，系统将坚决拦截扣款，并将订单自动转入人工复核台账，防止亏本出售。
- 凭证信息严格仅保存在本地配置文件中，严禁硬编码至源码或提交入库。

---

## 独立网页自提中心与交付防封 (吸收嗷嗷鱼 SaaS)

为了规避在闲鱼官方聊天窗口直接发送网盘链接、敏感卡密导致的 E07 违规封号风险，系统提供独立的 H5 自提中心：

- **买家凭证访问**：买家下单后，系统在聊天窗口仅发送一条专属的防重放提货链接（带 HMAC 签名短令牌，不透出敏感地址）。
- **独立 H5 界面**（`/pickup`）：买家在浏览器中输入订单号一键提取，支持卡密一键复制、百度网盘/夸克网盘链接一键跳转，且自带买家订单水印防截图倒卖。
- **频控与防枚举**：内置基于 SQLite 的滑窗限流器（`PickupRateLimiter`），同一 IP 频次超限或连续 3 次尝试伪造订单号自动拉黑封禁 15 分钟，彻底杜绝黑产撞库枚举。

---

## 动态阶梯降价促活配置 (吸收闲管家官方 ERP)

利用闲鱼推荐算法中“降价商品优先向浏览潜客 Push 通知”的官方流量红利：

- **滞销商品激活**：支持为指定商品或全店配置阶梯降价规则（例如每日 04:00 自动微降 0.5 ~ 1.0 元）。
- **底价刚性保护 (`price_floor`)**：严格锁定最低利润红线，一旦价格触及底价立即停止降价并触发通知，杜绝亏本成交。
- **完整改价审计**：写入 `price_ladder_log` 账本，具备 86400 秒改价冷却与失败重试退避。

---

## 真人拟态自动养号 (吸收一尘/鱼游助手)

平台对“只收发消息、从未浏览商品”的纯脚本账号会打上机器人标签并进行静默限频。拟态养号模块将账号洗成正常高权重买家号：

- **拟人动作链**：每日在随机时间窗口内，模拟真实用户在推荐瀑布流中翻页滑动、停留阅读商品详情（4~18秒随机停留）、随机执行超赞/收藏（每日 3~8 次）。
- **风控探针避让**：内置 `PlatformChallenge` 熔断护栏，一旦检测到滑块验证码或限频提示，立即冻结当日本轮养号，保障账号资产绝对安全。
- **商业中枢大屏**：通过 `/automation-hub` 统一监控自提状态、阶梯降价趋势与养号动作流水。

---

## 目录结构

```
├── backend/            # 后端服务、闲鱼自动化核心模块和验证码页面
├── frontend/           # React + Vite 前端源码
├── static/             # 前端构建产物和上传文件
├── utils/              # 验证码、登录、图片、订单等辅助模块
├── deploy/             # Docker Compose、Dockerfile 和 Nginx 配置
├── docs/               # 说明文档与界面截图
└── archive/            # 历史前端备份，仅供回溯，不参与运行
```

---

## 使用流程

1. **初始配置**：登录后台并修改默认管理员密码；配置好通知通道（企业微信/钉钉/飞书等）。
2. **账号托管**：在「账号」页面扫码登录闲鱼账号，等待状态显示「在线」和「监听中」（系统自动维持心跳长连接与平滑换 Token）。
3. **蓝海选品与机会挖掘**：在「商机/机会发现」中利用热词与供需评分锁定高毛利潜力品类，一键使用「智能生图」生成全套电商主图与说明图。
4. **商品同步与模板发布**：在「商品」页面同步现有在线商品；或使用占位符模板批量排队发布新商品。
5. **货源与库存管理**：在「卡密」页面创建卡券分组并导入现货卡密；若使用无货源模式，在「供应商配置」中关联上游 SKU 并开启自动代采。
6. **自动化策略设置**：在「自动回复」或「AI 智能回复」中设定咨询策略与议价底线。
7. **履约规则绑定**：在「自动发货」中为指定商品绑定卡券分组或设置多规格发货规则。
8. **订单核对与全自动履约**：买家下单付款后系统自动发货或触发上游代采，写入逐卡账本；买家回复「1」或「提取」可随时自助补发。
9. **健康监控与风控**：在「看门狗诊断」和「通知日志」中排查任何风控阻断、下架或掉线异常，恶意退款自动列入黑名单。

自动发货、卡密分组、商品级开关和 AI 回复的完整说明见 [`自动发货逻辑说明.md`](自动发货逻辑说明.md)。

扫码登录成功后，系统会先保存已验证的核心 Cookie 并更新账号状态，再在后台补全浏览器 Cookie。商品同步会对比页面声明数量、接口解析数量和数据库保存数量，数量不一致时给出明确提示。

---

## 数据与安全

- 不要提交数据库、日志、Cookie、Token、卡密、浏览器状态和本地配置。
- 日志不得输出完整 Cookie、Authorization、签名或模型 API Key。
- 自动发货、发送卡密、删除商品等操作应先用测试账号验证。
- 闲鱼页面和接口可能随平台更新变化，升级后应重新验证登录、消息、商品和订单链路。
- 旧版无盐 SHA-256 密码会在首次成功验证后自动迁移为带盐 PBKDF2 哈希。
- 旧版 `/api/captcha` 远程验证码控制接口没有独立鉴权，现已默认关闭。
- 如确需开发调试，可在 `WEB_SECURITY.remote_captcha_enabled` 中临时开启，并仅在可信本机网络使用。

---

## 部署失败排查

重复部署失败通常不是前端问题，优先按以下顺序检查：

1. **确认版本**：使用 Docker Engine 24+、Docker Compose v2 和至少 2 GB 可用内存。
2. **确认 .env**：未设置 `ADMIN_PASSWORD` 或 `JWT_SECRET_KEY` 时，Compose 会主动拒绝启动。
3. **确认端口**：8080 被占用时，在 `.env` 设置 `WEB_PORT=8081`，然后访问对应端口。
4. **确认目录权限**：容器必须能够写入 `data`、`logs` 和 `backups`。
5. **等待首次构建**：首次构建会安装 Python 依赖和 Playwright Chromium，网络较慢时耗时明显。
6. **检查健康状态**：
   ```bash
   curl http://localhost:8080/health
   ```
7. **查看真实错误**：
   ```bash
   docker compose -f deploy/docker-compose.yml logs --tail=200 xianyu-app
   ```
   不要只看浏览器「无法访问」。
8. **清理失败构建缓存**：确认数据已备份后运行
   ```bash
   docker compose -f deploy/docker-compose.yml build --no-cache xianyu-app
   ```

常用诊断命令：

```bash
docker compose -f deploy/docker-compose.yml config
docker compose -f deploy/docker-compose.yml ps
docker compose -f deploy/docker-compose.yml logs --tail=200 xianyu-app
curl http://localhost:8080/health
```

---

## 更新与验证

更新代码：

```bash
git pull
docker compose -f deploy/docker-compose.yml up -d --build
```

后端检查：

```bash
.\.venv\Scripts\python.exe -m compileall -q Start.py backend utils
```

前端检查：

```bash
cd frontend
.\node_modules\.bin\tsc.cmd --noEmit
npm run build
```

---

## 许可证

本项目采用 [AGPL-3.0](LICENSE) 许可证。

---

## 免责声明

**数据存储说明**

本系统在运行过程中，数据全部保存在使用者本地的 `data` 目录下，不会上传至任何第三方服务器。请在部署前自行确认这一点。

**用户须知**

1. **数据安全**：请妥善保管您的账号密码和服务器访问权限，因用户自身原因导致的数据泄露，本系统不承担责任。
2. **合规使用**：用户应确保使用本系统的行为符合相关平台的用户协议和法律法规，因违规使用导致的账号封禁或其他损失，由用户自行承担。
3. **数据备份**：建议用户定期备份重要数据，因系统故障、服务器问题、黑客攻击等导致的数据丢失，本系统不承担责任。
4. **服务中断**：本系统不保证服务的持续性和稳定性，因系统维护、升级或不可抗力导致的服务中断，本系统不承担责任。
5. **第三方平台**：本系统依赖第三方平台接口运行，因第三方平台政策变更、接口调整等导致的功能异常，本系统不承担责任。
6. **免费服务**：本项目未收取任何费用，没有义务解答任何疑问，没有义务解决任何问题。
7. **安全声明**：本人技术有限，不是专业安全人员，无法完全保证数据安全，请用户自行评估风险。

**隐私保护承诺**

- 所有用户数据仅用于本系统功能运行。
- 用户可随时自行删除其存储的数据。

---

## 交流社群

<div align="center">

<p><strong>遇到问题？欢迎加群交流 —— 版本更新与问题答疑第一时间同步</strong></p>

<p>
<a href="https://qm.qq.com/q/89nIPLRrCU" title="易语言+AI-吹牛逼（群号 607124662）"><img src="https://img.shields.io/badge/%E7%82%B9%E5%87%BB%E5%8A%A0%E5%85%A5-QQ%E7%BE%A4_607124662-12B7F5?style=for-the-badge&logo=qq&logoColor=white&labelColor=0A8FCC" alt="点击加入 QQ 群 607124662"></a>
&nbsp;&nbsp;
<a href="https://qm.qq.com/q/Fv9KjpGCEq" title="易语言 jadeView 前端UI（群号 1103426302）"><img src="https://img.shields.io/badge/%E7%82%B9%E5%87%BB%E5%8A%A0%E5%85%A5-QQ%E7%BE%A4_1103426302-8B5CF6?style=for-the-badge&logo=qq&logoColor=white&labelColor=6D3FD4" alt="点击加入 QQ 群 1103426302"></a>
</p>

<p>
<strong>易语言+AI-吹牛逼</strong> ｜ 群号 <code>607124662</code><br>
<strong>易语言 jadeView 前端UI</strong> ｜ 群号 <code>1103426302</code>
</p>

<p><sub>点击上方按钮即可一键加群，无需手动搜索群号</sub></p>

</div>

# 商品价格监控采集平台

面向多区域站点的商品价格抓取、规格归一、比价计算与变价告警的采集治理平台

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8020/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5200/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8020`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 采集站点目录 | `shop_site` | 站点配置 | 站点名称、站点域名、采集区域 |
| 价格爬取任务 | `price_crawler` | 爬取任务 | 任务名称、目标站点、商品关键词 |
| 页面解析模板 | `page_parser` | 解析模板 | 模板名称、适用站点、选择器路径 |
| 规格归一器 | `spec_normalizer` | 规格归一规则 | 规则名称、源规格文本、归一规格 |
| 货币换算器 | `currency_conv` | 汇率快照 | 快照日期、源货币、目标货币 |
| 价格基线库 | `price_baseline` | 价格基线 | 商品标识、归一规格、基线价格 |
| 比价计算器 | `price_compare` | 比价结果 | 比价批次、商品标识、最低价站点 |
| 变价告警规则 | `price_alert` | 告警规则 | 规则名称、监控商品、价差阈值 |
| 商品登记中心 | `goods_registry` | 商品档案 | 商品标识、商品名称、品牌 |
| 规格匹配器 | `sku_match` | 匹配记录 | 匹配批次、商品档案、候选规格 |
| 页面样本库 | `sample_store` | 页面样本 | 样本批次、站点名称、原始HTML摘要 |
| 解析质量审计 | `parse_audit` | 审计明细 | 审计时间、解析模板、样本总数 |
| 采集调度计划 | `crawl_schedule` | 调度计划 | 计划名称、关联任务、调度周期 |
| 抓取频控 | `rate_limit` | 频控规则 | 规则名称、目标站点、每分钟上限 |
| 代理线路池 | `proxy_route` | 代理线路 | 线路标识、线路区域、可用性评分 |
| 失败隔离区 | `failure_quarantine` | 隔离记录 | 样本指纹、失败阶段、失败原因 |
| 比价导出通道 | `price_export` | 导出任务 | 任务名称、商品范围、导出格式 |
| 变价历史流水 | `change_history` | 变价记录 | 商品标识、旧价格、新价格 |
| 人工复核队列 | `match_review` | 复核记录 | 记录编号、匹配记录、复核结论 |
| 采集量指标 | `collect_metrics` | 指标记录 | 统计时间、站点名称、抓取量 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA

## 历史文件回归准备（sample_store / page_parser 联动）

页面样本库顶部提供「从历史文件准备回归」工作台，覆盖：

1. **选择历史页面文件 / 上传回归材料**：可下拉选择自带站点与批次的演练文件，
   也可上传 JSON / JSONL / CSV / TSV / 键值文本 / HTML 表格（或直接粘贴内容）。
2. **导入回执（逐行校验 + 后续动作）**：每行给出 `已验证 / 跨站拒绝 / 损坏 / 重复来源 / 无法解析`
   结论、行内原因与后续动作。样本必须属于指定站点；**跨站同指纹只拒绝、绝不覆盖原样本**；
   半份 HTML 判为损坏，不会成为已验证样本。
3. **重试原导入**：回执号即“原导入请求号”，重传同一页按 `(导入请求, 页面编号)` 对账，
   同一页始终只有一份合格来源，价格更正（如 70 → 79）走更新并保留轨迹。
4. **同批模板试跑**：从同一批启动，或在「页面解析模板」页「领取回归样本」，
   用模板选择器 + 通用策略从真实 HTML 提取价格/标题/币种，生成回归批次与
   下载凭据（`credential`，支持 `?credential=&token=&key=`），可下载 CSV/JSON 试跑回执。
5. **互相关联**：回执号 ↔ `sample_store` 样本 ID ↔ 试跑批次号在 UI 与下载文件中双向可见。
   试跑窗口保持开启时，原导入的更正会自动跟进到同一回归批次；损坏页未补齐前状态为「等待补传」。

演练数据：站点 `云街甄选`、批次 `RG-20261126-I1I2`，历史文件
`HF-SCENARIO-70`（I1=70、I2 半传、跨站同指纹行）与 `HF-SCENARIO-79`（I1 更正为 79）。

主要接口（同时挂在 `/api/regression/*` 与 `/api/sample_store/*`、`/api/page_parser/regression/*` 别名下）：

```text
GET  /api/regression/history-files
POST /api/regression/imports                 # JSON / multipart / form / 原始文件体
GET  /api/regression/imports/{import_no}
POST /api/regression/imports/{import_no}/retry
POST /api/regression/trials
GET  /api/regression/trials/{trial_no}
POST /api/regression/trials/{trial_no}/refresh
POST /api/regression/trials/{trial_no}/reaccept
GET  /api/regression/trials/{trial_no}/receipt?credential=...&format=csv|json
POST /api/page_parser/regression/claim
```

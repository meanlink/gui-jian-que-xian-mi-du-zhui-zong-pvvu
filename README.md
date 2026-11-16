# 轨检缺陷密度追踪

密度聚类的真实业务分析小型项目。完整模拟客户、痛点、上下游、算法公式与一天工作剧本见 [BUSINESS.md](BUSINESS.md)。

## 模块与业务流程
数据接入（JSON/CSV、字段验证、批次指纹）→ 参数化分析（异步 Python worker）→ 结果复核（指标、图形、明细、解释、批准）→ 定时分发（本地收件箱、CSV/JSON、SHA256）；另有业务总览和审计轨迹。
三个贯穿全栈的实体：Dataset 数据批次、Job 分析任务、Report 分发计划；辅助 User、Audit、Delivery。

## 快速启动
```bash
cp .env.example .env
docker compose up -d --build --wait
docker compose exec -T backend python -m scripts.seed_demo
```
访问 http://localhost:18210；后端文档 http://localhost:19210/docs；数据库宿主端口 17710。
`SEED_DEMO=true` 自动幂等 seed；不覆盖用户修改。手动 seed 命令可重复执行。

## 演示账号与权限
|账号|密码|真实权限|
|---|---|---|
|admin@demo.local|Demo123!|接入、执行、批准、分发、暂停、读取与下载|
|analyst@demo.local|Demo123!|接入、执行、读取与下载；不能批准或安排分发|
|viewer@demo.local|Demo123!|只读与下载|

## 技术与本地开发
Python 3.12 / FastAPI / SQLAlchemy / NumPy / SciPy / scikit-learn；PostgreSQL 16；Redis 7；Nginx；原生 HTML CSS JavaScript ES modules（用户指定）。后端所有校验、持久化和算法在服务端。无前端打包步骤。
后端：安装 backend/requirements.txt，设置 DATABASE_URL、REDIS_URL、JWT_SECRET、ARTIFACT_DIR 后在 backend/ 执行 `python -m migrations.initial`、`python -m scripts.seed_demo`、`uvicorn src.main:app`，另开 `python -m src.worker`。前端使用带 /api 反向代理的 Nginx，直接打开 index.html 不支持 API。

## 目录与工程约定
backend/src/{models,schemas,services,routes,controllers,middlewares,constants,config,utils}；backend/scripts/seed_demo.py；backend/migrations/initial.py。
frontend/src/{api,stores,types,components/common,hooks,pages,router,utils,constants}。
Role 枚举：admin/analyst/viewer；JobStatus：queued/running/completed/failed。后端 backend/src/constants/enums.py，前端 frontend/src/constants/enums.js；路由权限在 middlewares/auth.py；页面按相同角色控制操作入口。

## 环境变量与 Docker
COMPOSE_PROJECT_NAME=rail-density；FRONTEND_PORT=18210；BACKEND_PORT=19210；DB_PORT=17710；DB_PASSWORD、JWT_SECRET 见 .env.example；SEED_DEMO 默认 true。共享命名产物卷供后端下载与 worker 写入，数据库/Redis 各持久卷。
恰好五服务均有健康检查。Redis 队列加 SQL 排队状态兜底；worker 按 next_run 生成本地收件箱文件。周期 60 秒至 30 天。分发结果为批准分析的固定快照；若需新数据重新计算并建立新计划。当前不连接邮件或企业聊天。
错误响应、请求编号、Redis 每 IP 每分钟 240 次限流、JWT 和数据库操作审计。演示密钥与密码仅供本机模拟；生产部署前配置独立密钥与账号。

## 数据与验收
所有数据为固定随机种子生成的模拟业务数据，不能作为实际生产决策证据。项目独立算法、输入边界及业务局限详见 BUSINESS.md。测试与截图统一存放 ../../test-artifacts/gb-1011/，源码不含日志/截图/测试报告。
清理本项目演示数据：`docker compose down -v --remove-orphans`（会删除本项目命名卷）。

## 可核验账本与独立见证
针对“审计可被删改、只显示最近记录”的追责风险，导入（`dataset.create`）、试验状态（`job.create/running/completed/failed/recovered`）、批准（`job.approve`）与分发（`report.schedule/toggle/delivered`）每一次操作都在**同一数据库事务**内同时写审计行与哈希链账本条目，任一步失败整体回滚，业务变更不会在缺审计或缺凭证时落地。

- **本库哈希链**：每条含序号、前序哈希与内容 SHA256（`ledger_entries`）。追加经 `ledger_lock` 行级锁（PostgreSQL）串行化，多进程并发记账序号连续、不分叉、不漏项；SQLite 本地模式用进程内互斥等价保证。
- **独立见证端**：`witness/` 是独立容器、独立 SQLite 持久卷（`witness-data`）的独立故障域。业务侧只保存条目的哈希与链指针，见证端另存凭证并再维护一条凭证回执链，HMAC 签名鉴权。
- **断线不阻塞**：见证端不可达时业务照常提交，条目停在 `pending`（待确认尾段）；worker 每轮与手动 `POST /ledger/relay` 按原序补送。重试同序号同内容幂等返回原回执、不新增凭证；同序异内容、链分叉、跳号均被见证端拒绝，本库标 `conflict`。确认响应丢失时先查回执再接回，不产生重复凭证。
- **可查出改写与截尾**：`GET /ledger/verify` 做固定起止范围的只读核验（核验期间新操作照常进行），重算链内哈希定位**首处差异**（内容改写/断链/缺序号），并与见证端独立凭证交叉比对；本库整段截尾时，见证端链尾更长即暴露。
- **旧记录不倒填**：账本上线前的历史审计回填为 `legacy` 链，仅锚定哈希、永不标记为已见证或可信。前端“可核验账本”页可核验全账、定位首处差异并查看待确认积压。

账本接口（均需登录；`/ledger/relay` 仅 admin）：`GET /ledger/entries`、`GET /ledger/verify?start=&end=`、`GET /ledger/pending`、`POST /ledger/relay`。见证端管理口默认 `WITNESS_PORT=19310`，生产部署前通过 `WITNESS_SECRET` 注入独立密钥。后端离线测试：`PYTHONPATH=<依赖目录> python -m unittest discover backend/tests`。

## License
MIT，见 LICENSE。

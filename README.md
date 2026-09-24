# 地震灾害科学协同服务

这是一个面向地震台网与应急指挥中心的模块化后端，集中管理地震事件、台站观测、震情计算、灾情协同、用户权限、会话、审计和可恢复后台任务。项目使用 FastAPI 与 SQLite，所有运行数据保存在单个本地数据库文件中，不依赖另行部署的数据库、缓存或消息队列。

## 主要模块

- 震情档案：登记地震事件、震源参数和台站观测，保留计算输入摘要。
- 余震序列：新事件按时间窗、空间距离和震级差自动归入活动序列，归属依据（命中规则、实测差值、阈值）逐事件留痕；支持人工拆分/合并并保留完整操作历史；按滑动窗口增量统计频次、最大震级与趋势；告警策略支持主震后抑制窗口，出现更大新主震时抑制窗口自动失效；序列、成员版本与窗口游标全部持久化，重启后恢复。
- 科学计算：提供震级、距离和烈度的确定性计算，以及可恢复后台任务。
- 灾情协同：管理灾情报告、公告、部门责任和跨部门办理状态。
- 身份与权限：用户、角色、细粒度权限、会话令牌、账号停用和会话撤销。
- 审计记录：关键身份操作留痕，并对口令和令牌等敏感字段做过滤。
- 后台任务：使用 SQLite 保存待执行任务，支持去重、租约、重试和完成回执。

## 运行环境

- Python 3.11
- SQLite 3，由 Python 标准库提供
- Linux、macOS 或 Windows

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

配置项均以 `TOWNSHIP_` 开头。可以复制 `.env.example` 后按需设置，默认数据库位于 `./data/township.db`。

## 初始化与检查

```bash
python -m app.cli init-db
python -m app.cli check-db
```

## 启动服务

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

首次部署可创建唯一的初始管理员：

```bash
curl -sS -X POST http://127.0.0.1:8432/api/auth/bootstrap   -H 'Content-Type: application/json'   -d '{"username":"admin","password":"Admin!23456","client_label":"initial-setup"}'
```

之后通过 `/api/auth/login` 获取会话令牌，并在管理接口请求头中使用 `Authorization: Bearer <token>`。

## 测试

```bash
python -m pytest
```

测试覆盖身份初始化、登录、用户与角色维护、权限计算、账号停用后的会话撤销、审计脱敏、事件与台站观测、烈度计算、后台任务去重与领取，以及余震自动关联、人工拆分/合并、滑动窗口游标恢复、抑制窗口与新主震失效，以及数据库时间格式。

## 余震序列接口

事件创建后会立即返回 `sequence_membership`（含归属规则、参照主震、时间/距离/震级差实测值、阈值与 `stats_version`），`GET /api/seismic/events/{id}` 也会附带该信息及最新窗口统计版本。

```text
GET    /api/seismic/sequences[?status=active]        序列列表
GET    /api/seismic/sequences/{id}                    序列详情（成员、策略、窗口、告警计数）
POST   /api/seismic/sequences/{id}/split              人工拆分（event_ids、新主震、原因）
POST   /api/seismic/sequences/{id}/merge              并入其他活动序列
GET    /api/seismic/sequences/{id}/operations         操作历史
PATCH  /api/seismic/sequences/{id}/policy             更新抑制窗口/开关/余震告警
POST   /api/seismic/sequences/{id}/windows/advance    推进滑动窗口（持久化游标）
GET    /api/seismic/sequences/{id}/windows            查询窗口游标与最新统计桶
GET    /api/seismic/sequences/{id}/alerts             告警判定流水
GET    /api/seismic/sequences/recover                 重启后核对序列与游标
```

关联默认参数：时间窗 7 天、距离 100 km、震级差 ≥ 0.5 判定为新主震、主震后抑制窗口 1 小时，随序列保存在 `params_json` 中。人工拆分/合并会推高序列 `stats_version`，检测到游标版本滞后时旧窗口桶自动废弃并按当前成员重算。

## 编译检查

```bash
python -m compileall -q app tests
```

## API 冒烟

```bash
python -m app.cli smoke
```

该命令在进程内启动应用并检查服务根路径与健康接口，适合部署前快速确认路由和数据库初始化是否正常。

## 目录结构

```text
app/
  api/             用户、角色、审计、认证和系统接口
  core/            时钟、安全、异常和分页能力
  repositories/    SQLite 查询与持久化读取
  routers/         灾情、事件、公告、部门和信访业务接口
  seismic/         地震事件、余震序列、滑动窗口、告警策略和科学计算服务
  schemas/         管理接口输入模型
  services/        身份、审计和后台任务领域服务
  cli.py           初始化、检查和冒烟入口
  database.py      SQLite 连接、事务、表结构与基础权限
tests/             核心、管理接口和原有业务回归测试
tools/             本地维护脚本
```

## 数据一致性

SQLite 连接默认启用外键、WAL、busy timeout 与同步写入策略。需要跨多张表更新的管理操作在即时事务中执行，失败会整体回滚。会话令牌只保存摘要；用户停用会撤销仍有效的会话。审计事件保存操作者、动作、资源、结果和前后状态，但不会保存明文密码或令牌。

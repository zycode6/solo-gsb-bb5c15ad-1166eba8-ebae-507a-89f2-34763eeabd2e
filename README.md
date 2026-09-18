# 冷链温度事件分析 API

基于 **FastAPI + SQLite** 的冷链温度监控服务：接收设备按时间上报的温度读数，
按每个设备配置的上下限与容忍时长识别**连续超温事件**，返回事件起止时间、
峰值温度与累计超限时长。规则支持**版本化**：每条读数应用其当时已生效的
规则版本，规则切换点强制结束旧事件。

## 功能特性

- 设备级规则配置：温度上下限、容忍时长、最大上报间隔
- **规则版本机制**：
  - 更新规则时可指定 `effective_at`（生效时间），同一设备各版本按生效时间排序
  - 不传 `effective_at` 时保持原行为：**立即生效**
  - 查询任意时段事件时，对每条读数应用当时已生效的规则版本
  - **规则切换点强制结束旧事件**，即使旧事件尚未观察到恢复读数，也禁止跨版本合并
  - 事件结果返回 `rule_version` 与 `rule_effective_at`
  - 拒绝早于（或等于）该设备最新版本生效时间的倒序写入（HTTP 409）
- 批量温度读数上报（同一时间重复上报幂等覆盖）
- 连续超温事件识别：
  - **短暂恢复不切断**：恢复正常不超过容忍时长后再次超限，仍算同一事件
  - **间隔过长则分段**：相邻读数间隔超过 `max_gap_seconds` 强制分段
  - 恢复超过容忍时长后再超限，自动开启新事件
- 事件输出：起止时间、事件类型（high/low/mixed）、峰值温度与时间、
  累计超限时长、是否仍在持续、适用规则版本
- 按时间范围查询单设备或全部设备的事件

## 快速开始（Docker Compose 一键启动）

```bash
docker compose up --build
```

服务监听端口：**8000**

- API 文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

SQLite 数据持久化在命名卷 `coldchain-data`（容器内路径 `/data/coldchain.db`），
重启不丢数据。升级到版本化规则后首次启动会自动把旧的单值规则迁移为 v1，
旧卷数据可直接继续使用。

## 示例请求

### 1. 配置设备规则（立即生效，原有用法不变）

```bash
curl -X PUT http://localhost:8000/api/v1/devices/truck-01/rule \
  -H 'Content-Type: application/json' \
  -d '{
    "upper_limit": 8.0,
    "lower_limit": 2.0,
    "tolerance_seconds": 300,
    "max_gap_seconds": 1800
  }'
```

响应（未传 `effective_at`，服务端以当前 UTC 时间作为生效时间，版本为 1）：

```json
{
  "device_id": "truck-01",
  "version": 1,
  "upper_limit": 8.0,
  "lower_limit": 2.0,
  "tolerance_seconds": 300,
  "max_gap_seconds": 1800,
  "effective_at": "2026-09-18T08:00:00Z",
  "created_at": "2026-09-18T08:00:00Z",
  "updated_at": "2026-09-18T08:00:00Z"
}
```

### 2. 追加一个未来生效的规则版本

```bash
curl -X PUT http://localhost:8000/api/v1/devices/truck-01/rule \
  -H 'Content-Type: application/json' \
  -d '{
    "upper_limit": 5.0,
    "lower_limit": 2.0,
    "tolerance_seconds": 300,
    "max_gap_seconds": 1800,
    "effective_at": "2026-09-18T12:00:00Z"
  }'
```

- 每次 PUT 都追加一个新版本，设备内版本号从 1 开始递增。
- `effective_at` 为 ISO 8601 时间，无时区按 UTC 处理。
- 新版本生效时间必须**严格晚于**该设备最新版本的生效时间，否则返回
  `409 Conflict`，且不会写入任何数据：

```json
{
  "detail": "设备 'truck-01' 最新规则版本 v2 的生效时间为 2026-09-18T12:00:00+00:00，新版本生效时间必须严格晚于该时间"
}
```

### 3. 查询设备的全部规则版本

```bash
curl http://localhost:8000/api/v1/devices/truck-01/rule/versions
```

响应按版本号（即生效时间）升序排列：

```json
[
  {
    "device_id": "truck-01",
    "version": 1,
    "upper_limit": 8.0,
    "lower_limit": 2.0,
    "tolerance_seconds": 300,
    "max_gap_seconds": 1800,
    "effective_at": "2026-09-18T08:00:00Z",
    "created_at": "2026-09-18T08:00:00Z",
    "updated_at": "2026-09-18T08:00:00Z"
  },
  {
    "device_id": "truck-01",
    "version": 2,
    "upper_limit": 5.0,
    "lower_limit": 2.0,
    "tolerance_seconds": 300,
    "max_gap_seconds": 1800,
    "effective_at": "2026-09-18T12:00:00Z",
    "created_at": "2026-09-18T09:30:00Z",
    "updated_at": "2026-09-18T09:30:00Z"
  }
]
```

| 字段 | 说明 |
| --- | --- |
| `upper_limit` / `lower_limit` | 温度上下限（℃），超出即超限 |
| `tolerance_seconds` | 容忍时长：恢复正常不超过该时长再超限，仍算同一事件（默认 300） |
| `max_gap_seconds` | 最大上报间隔：相邻读数间隔超过该值则强制分段（默认 1800） |
| `effective_at` | 该版本生效时间（UTC）；不传时取当前 UTC 时间（立即生效） |
| `version` | 设备内规则版本号，从 1 开始递增 |

### 4. 批量上报温度读数

```bash
curl -X POST http://localhost:8000/api/v1/readings \
  -H 'Content-Type: application/json' \
  -d '{
    "readings": [
      {"device_id": "truck-01", "timestamp": "2026-09-18T08:00:00Z", "temperature": 5.0},
      {"device_id": "truck-01", "timestamp": "2026-09-18T08:10:00Z", "temperature": 9.0},
      {"device_id": "truck-01", "timestamp": "2026-09-18T08:20:00Z", "temperature": 10.2},
      {"device_id": "truck-01", "timestamp": "2026-09-18T08:24:00Z", "temperature": 6.0},
      {"device_id": "truck-01", "timestamp": "2026-09-18T08:28:00Z", "temperature": 9.5},
      {"device_id": "truck-01", "timestamp": "2026-09-18T08:40:00Z", "temperature": 5.0},
      {"device_id": "truck-01", "timestamp": "2026-09-18T12:00:00Z", "temperature": 1.0},
      {"device_id": "truck-01", "timestamp": "2026-09-18T12:10:00Z", "temperature": 4.0}
    ]
  }'
```

时间戳为 ISO 8601 格式，无时区时按 UTC 处理。

### 5. 按时间范围查询事件

```bash
curl "http://localhost:8000/api/v1/devices/truck-01/events?start=2026-09-18T00:00:00Z&end=2026-09-19T00:00:00Z"
```

每条事件都会带 `rule_version` 与 `rule_effective_at`，表示该事件适用的
规则版本。响应示例（08:24 的短暂恢复未切断第一个事件；12:00 的低温因
间隔过长单独成段）：

```json
[
  {
    "device_id": "truck-01",
    "start_time": "2026-09-18T08:10:00Z",
    "end_time": "2026-09-18T08:40:00Z",
    "ongoing": false,
    "duration_seconds": 1800.0,
    "violation_seconds": 1560.0,
    "event_type": "high",
    "peak_temp": 10.2,
    "peak_time": "2026-09-18T08:20:00Z",
    "max_temp": 10.2,
    "min_temp": 5.0,
    "reading_count": 5,
    "rule_version": 1,
    "rule_effective_at": "2026-09-18T08:00:00Z"
  },
  {
    "device_id": "truck-01",
    "start_time": "2026-09-18T12:00:00Z",
    "end_time": "2026-09-18T12:10:00Z",
    "ongoing": false,
    "duration_seconds": 600.0,
    "violation_seconds": 600.0,
    "event_type": "low",
    "peak_temp": 1.0,
    "peak_time": "2026-09-18T12:00:00Z",
    "max_temp": 4.0,
    "min_temp": 1.0,
    "reading_count": 2,
    "rule_version": 1,
    "rule_effective_at": "2026-09-18T08:00:00Z"
  }
]
```

### 6. 规则切换点切分事件（示例）

假设 v1（上限 8℃）08:00 生效、v2（上限 5℃）09:00 生效，读数为
07:00=9、08:00=9.2、09:00=6、09:10=4：

- 07:00–08:00 按 v1 判定为超上限事件；09:00 规则切换，**旧事件在 08:00
  被强制结束**（`end_time=08:00`、`ongoing=false`），不与切换后的读数合并。
- 09:00 的 6℃ 在 v1 下本属正常，但按 v2（上限 5℃）判定为超限，
  开启一个 `rule_version=2` 的新事件，09:10 恢复后结束。

### 7. 查询全部设备的事件

```bash
curl "http://localhost:8000/api/v1/events?start=2026-09-18T00:00:00Z&end=2026-09-19T00:00:00Z"
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `PUT` | `/api/v1/devices/{device_id}/rule` | 创建/追加设备规则版本（可带 `effective_at`，否则立即生效） |
| `GET` | `/api/v1/devices` | 列出全部设备（各返回最新版本规则） |
| `GET` | `/api/v1/devices/{device_id}/rule` | 查询设备最新版本规则 |
| `GET` | `/api/v1/devices/{device_id}/rule/versions` | 按版本号升序列出设备全部规则版本 |
| `DELETE` | `/api/v1/devices/{device_id}` | 删除设备全部规则版本及其读数 |
| `POST` | `/api/v1/readings` | 批量上报温度读数 |
| `GET` | `/api/v1/devices/{device_id}/readings` | 按时间范围查原始读数 |
| `GET` | `/api/v1/devices/{device_id}/events` | 按时间范围查单设备事件（按读数当时版本计算） |
| `GET` | `/api/v1/events` | 按时间范围查全部设备事件 |
| `GET` | `/health` | 健康检查 |

## 事件判定与时长口径

- **超限**：`temperature > upper_limit` 或 `temperature < lower_limit`（边界值不算超限）。
- **规则选择**：对时间戳为 ts 的读数，应用 `effective_at <= ts` 的最新版本；
  早于设备首个版本生效时间的历史读数，回退应用首个版本（初始规则）。
- **规则切换**：当相邻读数适用的版本不同时，旧版本下未闭合的事件在其最后
  一条读数处强制结束（`ongoing=false`），新版本下的超限读数另开新事件，
  **绝不跨版本合并**（容忍时长也不能跨越切换点合并）。
- **事件起点**：该版本下首个超限读数的时间。
- **事件终点**：最后一次超限之后首个恢复正常读数的时间；若之后没有读数，
  终点取最后超限读数时间且 `ongoing=true`；若事件在规则切换或数据中断处
  被强制结束，终点同样取最后超限读数时间，但 `ongoing=false`。
- **累计超限时长** `violation_seconds`：事件窗口内，凡左端点超限的相邻读数
  区间长度之和（离散采样的区间归属近似）。
- **峰值** `peak_temp`：事件窗口内偏离该版本限值最大的读数温度。
- 事件在**查询时**基于各历史版本实时计算，追加新版本后重新查询，
  切换点之后的事件会按新规则重新切分；历史版本不可修改。

## 本地开发

```bash
pip install -r requirements-dev.txt
python -m pytest tests/          # 运行测试
uvicorn app.main:app --reload    # 本地启动（默认 coldchain.db，可用 DB_PATH 覆盖）
```

## 项目结构

```
app/
  main.py       # FastAPI 路由
  events.py     # 版本化事件识别算法（纯函数，可单测）
  database.py   # SQLite 存储层（规则版本表 + 旧库自动迁移）
  schemas.py    # Pydantic 模型
tests/
  test_events.py             # 单规则算法单元测试
  test_events_versioned.py   # 版本化算法单元测试
  test_api.py                # API 集成测试
  test_rule_versions_api.py  # 规则版本 API 与旧库迁移测试
Dockerfile
docker-compose.yml
```

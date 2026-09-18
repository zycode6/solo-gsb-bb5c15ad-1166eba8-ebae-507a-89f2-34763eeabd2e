# 冷链温度事件分析 API

基于 **FastAPI + SQLite** 的冷链温度监控服务：接收设备按时间上报的温度读数，
按每个设备配置的上下限与容忍时长识别**连续超温事件**，返回事件起止时间、
峰值温度与累计超限时长。

## 功能特性

- 设备级规则配置：温度上下限、容忍时长、最大上报间隔
- 批量温度读数上报（同一时间重复上报幂等覆盖）
- 连续超温事件识别：
  - **短暂恢复不切断**：恢复正常不超过容忍时长后再次超限，仍算同一事件
  - **间隔过长则分段**：相邻读数间隔超过 `max_gap_seconds` 强制分段
  - 恢复超过容忍时长后再超限，自动开启新事件
- 事件输出：起止时间、事件类型（high/low/mixed）、峰值温度与时间、
  累计超限时长、是否仍在持续
- 按时间范围查询单设备或全部设备的事件

## 快速开始（Docker Compose 一键启动）

```bash
docker compose up --build
```

服务监听端口：**8000**

- API 文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

SQLite 数据持久化在命名卷 `coldchain-data`（容器内路径 `/data/coldchain.db`），
重启不丢数据。

## 示例请求

### 1. 配置设备规则

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

| 字段 | 说明 |
| --- | --- |
| `upper_limit` / `lower_limit` | 温度上下限（℃），超出即超限 |
| `tolerance_seconds` | 容忍时长：恢复正常不超过该时长再超限，仍算同一事件（默认 300） |
| `max_gap_seconds` | 最大上报间隔：相邻读数间隔超过该值则强制分段（默认 1800） |

### 2. 批量上报温度读数

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

### 3. 按时间范围查询事件

```bash
curl "http://localhost:8000/api/v1/devices/truck-01/events?start=2026-09-18T00:00:00Z&end=2026-09-19T00:00:00Z"
```

响应示例（08:24 的短暂恢复未切断第一个事件；12:00 的低温因间隔过长单独成段）：

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
    "reading_count": 5
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
    "reading_count": 2
  }
]
```

### 4. 查询全部设备的事件

```bash
curl "http://localhost:8000/api/v1/events?start=2026-09-18T00:00:00Z&end=2026-09-19T00:00:00Z"
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `PUT` | `/api/v1/devices/{device_id}/rule` | 创建/更新设备规则 |
| `GET` | `/api/v1/devices` | 列出全部设备规则 |
| `GET` | `/api/v1/devices/{device_id}/rule` | 查询单个设备规则 |
| `DELETE` | `/api/v1/devices/{device_id}` | 删除设备规则及其读数 |
| `POST` | `/api/v1/readings` | 批量上报温度读数 |
| `GET` | `/api/v1/devices/{device_id}/readings` | 按时间范围查原始读数 |
| `GET` | `/api/v1/devices/{device_id}/events` | 按时间范围查单设备事件 |
| `GET` | `/api/v1/events` | 按时间范围查全部设备事件 |
| `GET` | `/health` | 健康检查 |

## 事件判定与时长口径

- **超限**：`temperature > upper_limit` 或 `temperature < lower_limit`（边界值不算超限）。
- **事件起点**：首个超限读数的时间。
- **事件终点**：最后一次超限之后首个恢复正常读数的时间；若之后没有读数，
  终点取最后超限读数时间且 `ongoing=true`。
- **累计超限时长** `violation_seconds`：事件窗口内，凡左端点超限的相邻读数
  区间长度之和（离散采样的区间归属近似）。
- **峰值** `peak_temp`：事件窗口内偏离限值最大的读数温度。
- 事件在**查询时**基于最新规则实时计算，修改规则后重新查询即可生效。

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
  events.py     # 事件识别算法（纯函数，可单测）
  database.py   # SQLite 存储层
  schemas.py    # Pydantic 模型
tests/
  test_events.py  # 算法单元测试
  test_api.py     # API 集成测试
Dockerfile
docker-compose.yml
```

# 航段数据工程工作台 · DST 清洗与分区回填计划

面向航司数据工程师的网页工具，包含两个模块：

1. **时间清洗（DST 安全入仓）**：把地服导出的**当地时间**航段链解析为唯一
   **UTC** 时间链，避免夏令时（DST）切换时被错误入仓；
2. **回填计划**：对比同一张航段事实表的**仓内版**与**更正版**全量 CSV，
   按 UTC 起飞日分区计算必须一起替换的回填批次，并下载 JSON 计划。

## 快速启动

```bash
docker compose up --build
```

浏览器打开 <http://localhost:8000>，顶部两个 Tab 分别演示：

- **① 时间清洗**：点击「📦 载入内置夏令时样例」→「开始清洗」；
- **② 回填计划**：点击「📦 载入回填演示样例（两份）」→「生成回填计划」
  →「⬇ 下载 JSON 计划」。

本地开发（不用 Docker）：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

自检（仅标准库，无需安装依赖）：

```bash
python3 tests/test_solver.py     # DST 清洗求解器
python3 tests/test_backfill.py   # 分区回填计划
```

---

## 模块一：时间清洗（DST）

### 输入 CSV 格式

UTF-8 编码，必需表头：

```csv
tail_number,seq,dep_local,dep_tz,arr_local,arr_tz,duration_min_minutes,duration_max_minutes
B-7001,1,2026-10-25 00:30,Europe/London,2026-10-25 02:15,Europe/Berlin,90,130
```

| 列 | 含义 |
| --- | --- |
| `tail_number` | 机尾号（同一机尾构成一条航段链） |
| `seq` | 航段顺序，整数，同一机尾内唯一，按升序链接 |
| `dep_local` / `arr_local` | 起飞/落地当地时间，`YYYY-MM-DD HH:MM`（也接受 `T` 分隔与秒） |
| `dep_tz` / `arr_tz` | 两端 IANA 时区名，如 `Europe/Berlin`、`America/New_York` |
| `duration_min_minutes` / `duration_max_minutes` | 飞行时长下/上限（分钟，**闭区间**） |

### 求解规则

1. 当地时间 → UTC 候选：
   - 普通时刻 → 1 个候选；
   - **重复时刻**（秋季拨回重叠，如柏林 2026-10-25 02:30）→ 保留 **2 个候选**参与整链求解；
   - **不存在的时刻**（春季拨快空洞，如柏林 2026-03-29 02:30）→ 该行报错，整架机尾禁止导出。
2. 每个航段的 (起飞, 落地) 候选组合必须满足：飞行时长 ∈ `[下限, 上限]`（闭区间）。
3. 按 `seq` 链接同一机尾：下一段起飞 **不得早于** 上一段落地（相等允许）。
4. 判定：
   - **整链唯一解** → 允许导出该机尾的 UTC CSV；
   - **无解** → 展示冲突航段（前向扫描定位第一个无法衔接的航段及其候选），禁止导出；
   - **多解** → 展示全部可行候选链（最多展示 20 条），禁止导出；
   - **行级错误**（不存在时刻 / 非法时区 / 格式错误 / seq 重复等）→ 禁止导出。

---

## 模块二：回填计划（UTC 起飞日分区）

### 输入 CSV 格式

两份文件必须是**同一张航段事实表**的全量数据，表头一致（UTF-8，容忍 BOM）：

```csv
segment_id,tail_number,dep_utc,arr_utc
SEG-001,B-7001,2026-10-24T23:30:00Z,2026-10-25T01:15:00Z
```

| 列 | 含义 |
| --- | --- |
| `segment_id` | 唯一航段 ID，单份文件内**不得重复** |
| `tail_number` | 机尾号 |
| `dep_utc` / `arr_utc` | 起飞/落地 **UTC** 时间，ISO 8601 带 `Z` 或偏移量（如 `+08:00`，内部统一转 UTC） |

### 分区与分批规则

1. **分区键**：`dep_utc` 的 UTC 日期（`YYYY-MM-DD`），与本地时区无关；
2. **行序无关**：比较分区内容前对行按业务字段规范化排序，CSV 行序任意；
3. **只替换有变分区**：两版行指纹集合完全一致的分区不进入计划；
4. **迁日同批**：同一航段 ID 的起飞日发生变化时，**迁出分区与迁入分区必须在同一批次
   替换**（并查集合并；多跳迁日链 A→B→C 会把三个分区并为一批），避免分批回填时
   重复或漏读；互不牵连的分区分开成批，可独立回填；
5. **全量替换**：每个待替换分区携带**更正版的全部行**（`replacement_rows`）；
   更正版中已无航段的分区同样列出，`cleared=true` 且行为空，表示回填时清空该分区；
6. **硬性校验**：航段 ID 重复、UTC 时间无法解析、缺列、落地早于起飞等 →
   汇总全部错误并**停止生成计划**，响应中不包含任何批次。

### JSON 计划结构

```json
{
  "generated_at": "2026-11-11T11:48:18Z",
  "partition_key": "dep_utc 的 UTC 日期（YYYY-MM-DD）",
  "summary": { "batch_count": 4, "partitions_to_replace": 5, "cleared_partitions": 1, "...": "..." },
  "batches": [
    {
      "batch_id": 1,
      "reason": "航段迁日，迁出/迁入分区同批替换",
      "partition_count": 2,
      "total_replacement_rows": 4,
      "partitions": [
        {
          "date": "2026-10-25",
          "cleared": false,
          "warehouse_row_count": 2,
          "corrected_row_count": 2,
          "replacement_rows": [
            { "segment_id": "SEG-004", "tail_number": "B-7003",
              "dep_utc": "2026-10-25T14:00:00Z", "arr_utc": "2026-10-25T16:20:00Z" }
          ]
        }
      ]
    }
  ],
  "changes": { "migrated": [], "modified": [], "added": [], "removed": [] }
}
```

### 内置演示样例

| 文件 | 说明 |
| --- | --- |
| `samples/backfill_warehouse.csv` | 仓内版（8 行，覆盖 10-24 ~ 10-29 六个起飞日） |
| `samples/backfill_corrected.csv` | 更正版（行序故意打乱），包含迁日、行内修改、新增、删除与清空分区 |

演示样例的预期计划：

| 批次 | 分区 | 触发原因 |
| --- | --- | --- |
| 1 | `2026-10-25` + `2026-10-26` | SEG-003 从 25 迁到 26，迁出/迁入同批 |
| 2 | `2026-10-27` | SEG-007 删除，分区**清空**（0 行） |
| 3 | `2026-10-28` | SEG-009 新增，全新分区 |
| 4 | `2026-10-29` | SEG-010 落地时间行内修改 |
| — | `2026-10-24` | 两版完全一致，**跳过不回填** |

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/` | 网页入口（两个 Tab） |
| `GET` | `/api/sample` | 下载 DST 清洗样例 CSV |
| `POST` | `/api/solve` | 上传 CSV（字段 `file`），返回逐机尾求解结果 JSON |
| `POST` | `/api/export` | 字段 `file` + `tail`（`__all` 表示全部可导出机尾），返回 UTC CSV；非唯一解 409 |
| `GET` | `/api/backfill/sample/warehouse` | 下载回填演示样例 · 仓内版 |
| `GET` | `/api/backfill/sample/corrected` | 下载回填演示样例 · 更正版 |
| `POST` | `/api/backfill/plan` | 字段 `warehouse` + `corrected` 两份 CSV，返回回填计划 JSON；校验失败返回 400，`errors` 为全部错误 |
| `GET` | `/docs` | FastAPI 自动文档 |
| `GET` | `/api/health` | 健康检查 |

## 目录结构

```
app/
  main.py            # FastAPI 路由（清洗 + 回填）
  solver.py          # DST 候选生成 + 整链求解 + 冲突定位（纯标准库）
  backfill.py        # 分区对比 + 并查集分批 + 校验（纯标准库）
  static/index.html  # 单页前端（Tab 切换）
samples/
  dst_sample.csv
  backfill_warehouse.csv
  backfill_corrected.csv
tests/
  test_solver.py
  test_backfill.py
Dockerfile / docker-compose.yml / requirements.txt
```

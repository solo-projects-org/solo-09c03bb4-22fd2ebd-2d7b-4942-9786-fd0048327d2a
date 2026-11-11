"""回填计划核心逻辑：按 UTC 起飞日分区，计算必须同批替换的分区批次。

业务背景
--------
航段事实表按 UTC 起飞日（dep_utc 的日期）做分区。工程师拿到同一张事实表的
「仓内版」与「更正版」全量 CSV 后，需要知道哪些起飞日分区必须一起回填：

- 只安排替换**内容有变**的分区，内容完全一致的分区不动；
- 航段迁日（同一航段 ID 的起飞 UTC 从一天变到另一天）时，迁出分区与迁入
  分区必须在**同一批**替换，否则分批回填会重复或漏读；
- 互不牵连的分区分成不同批次，可独立回填；
- 每个待替换分区携带**更正版的全部行**；更正版已清空的分区同样列出
  （行数为 0，标记 cleared=true）；
- 航段 ID 重复或 UTC 时间无法解析时，收集全部错误并停止生成计划。

本模块只依赖 Python 标准库，行序不影响结果（比较前按行内容规范化排序）。
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

# 事实表必需列（表头大小写/空白不敏感，容忍 UTF-8 BOM）
REQUIRED_COLUMNS = ["segment_id", "tail_number", "dep_utc", "arr_utc"]


class BackfillValidationError(Exception):
    """输入文件无法通过校验（缺列、ID 重复、UTC 非法等），计划不得生成。"""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


# ---------------------------------------------------------------------------
# 解析与校验
# ---------------------------------------------------------------------------

def parse_utc(text: str, *, row_number: int, column: str,
              errors: list, label: str = "") -> datetime | None:
    """把 UTC 时间字符串解析为 aware datetime（UTC）。

    接受 ISO 8601 的 Z / 偏移量写法（2026-10-25T23:30:00Z、
    2026-10-25 23:30:00+00:00 等）；失败时向 errors 追加信息并返回 None。
    label 为文件角色名（"仓内版" / "更正版"），用于错误定位。
    """
    prefix = f"{label} " if label else ""
    raw = (text or "").strip()
    if not raw:
        errors.append(
            f"{prefix}第 {row_number} 行 {column} 为空，要求 ISO 8601 UTC 时间")
        return None
    normalized = raw.replace("Z", "+00:00").replace("z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        errors.append(
            f"{prefix}第 {row_number} 行 {column} 不是合法的 UTC 时间: {raw!r}"
            "（支持 2026-10-25T23:30:00Z 这类 ISO 8601 写法）")
        return None
    if dt.tzinfo is None:
        errors.append(
            f"{prefix}第 {row_number} 行 {column} 缺少时区信息: {raw!r}"
            "，UTC 列必须带 Z 或 +00:00 偏移")
        return None
    return dt.astimezone(timezone.utc)


def parse_fact_csv(csv_text: str, label: str) -> list[dict]:
    """解析一份航段事实表 CSV，返回规范化行列表。

    label 为文件角色名（"仓内版" / "更正版"），用于错误信息定位。
    任何校验错误都汇总后抛 :class:`BackfillValidationError`，不做部分返回。
    """
    reader = csv.reader(io.StringIO(csv_text))
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    if not rows:
        raise BackfillValidationError([f"{label} CSV 内容为空"])

    header = [c.strip().lower().lstrip("﻿") for c in rows[0]]
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise BackfillValidationError([
            f"{label} 缺少必需列: {', '.join(missing)}。需要的表头为: "
            + ", ".join(REQUIRED_COLUMNS)
        ])
    idx = {name: header.index(name) for name in REQUIRED_COLUMNS}

    def get(r, name):
        j = idx[name]
        return r[j].strip() if j < len(r) else ""

    out: list[dict] = []
    errors: list[str] = []
    seen_ids: set[str] = set()
    duplicate_ids: list[str] = []

    for row_number, r in enumerate(rows[1:], start=1):
        seg_id = get(r, "segment_id")
        tail = get(r, "tail_number")
        dep_raw = get(r, "dep_utc")
        arr_raw = get(r, "arr_utc")

        if not seg_id:
            errors.append(f"{label} 第 {row_number} 行 segment_id（航段 ID）为空")
        elif seg_id in seen_ids and seg_id not in duplicate_ids:
            duplicate_ids.append(seg_id)
        seen_ids.add(seg_id)

        if not tail:
            errors.append(f"{label} 第 {row_number} 行 tail_number（机尾号）为空")

        dep_dt = parse_utc(dep_raw, row_number=row_number,
                           column="dep_utc", errors=errors, label=label)
        arr_dt = parse_utc(arr_raw, row_number=row_number,
                           column="arr_utc", errors=errors, label=label)
        if dep_dt is not None and arr_dt is not None and arr_dt < dep_dt:
            errors.append(
                f"{label} 第 {row_number} 行落地 UTC 早于起飞 UTC: "
                f"{dep_raw!r} > {arr_raw!r}")

        out.append({
            "row_number": row_number,
            "segment_id": seg_id,
            "tail_number": tail,
            "dep_utc": dep_dt,
            "arr_utc": arr_dt,
            "dep_utc_raw": dep_raw,
            "arr_utc_raw": arr_raw,
        })

    if duplicate_ids:
        errors.append(
            f"{label} 存在重复航段 ID: {', '.join(sorted(duplicate_ids))}"
            "，每个 segment_id 在一份文件中必须唯一")
    if not out and not errors:
        errors.append(f"{label} CSV 没有任何数据行")
    if errors:
        raise BackfillValidationError(errors)
    return out


# ---------------------------------------------------------------------------
# 并查集：把迁日牵连的分区合并为同一批次
# ---------------------------------------------------------------------------

class _UnionFind:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def add(self, x: str):
        self.parent.setdefault(x, x)

    def find(self, x: str) -> str:
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str):
        self.add(a)
        self.add(b)
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_signature(row: dict) -> tuple:
    """行内容指纹：忽略行序，只看四个业务字段的取值。"""
    return (row["segment_id"], row["tail_number"],
            _iso_z(row["dep_utc"]), _iso_z(row["arr_utc"]))


def _partition_rows(rows: list[dict]) -> dict[str, list[dict]]:
    """按 UTC 起飞日把行分桶，桶内按指纹排序以保证行序无关。"""
    parts: dict[str, list[dict]] = {}
    for row in rows:
        day = row["dep_utc"].strftime("%Y-%m-%d")
        parts.setdefault(day, []).append(row)
    for day in parts:
        parts[day].sort(key=_row_signature)
    return parts


# ---------------------------------------------------------------------------
# 计划生成
# ---------------------------------------------------------------------------

def build_backfill_plan(warehouse_text: str, corrected_text: str) -> dict:
    """对比仓内版与更正版全量 CSV，生成分区回填计划。

    返回结构（同时用于网页展示与 JSON 下载）::

        {
          "summary": {...},
          "batches": [
            {"batch_id": 1, "partitions": [
              {"date": "2026-10-24", "cleared": false,
               "warehouse_row_count": 2, "corrected_row_count": 2,
               "replacement_rows": [ {segment_id, tail_number,
                                      dep_utc, arr_utc}, ... ]}
            ]}
          ],
          "changes": {"migrated": [...], "modified": [...],
                      "added": [...], "removed": [...]}
        }

    校验失败（ID 重复 / UTC 非法 / 缺列等）抛
    :class:`BackfillValidationError`，其 .errors 为全部错误信息。
    """
    warehouse = parse_fact_csv(warehouse_text, "仓内版")
    corrected = parse_fact_csv(corrected_text, "更正版")

    wh_by_id = {r["segment_id"]: r for r in warehouse}
    co_by_id = {r["segment_id"]: r for r in corrected}
    wh_parts = _partition_rows(warehouse)
    co_parts = _partition_rows(corrected)

    # 1) 行级变更分类（同 ID 两版都存在）
    migrated: list[dict] = []
    modified: list[dict] = []
    uf = _UnionFind()
    migration_days: set[str] = set()

    for seg_id in sorted(set(wh_by_id) & set(co_by_id)):
        old, new = wh_by_id[seg_id], co_by_id[seg_id]
        old_day = old["dep_utc"].strftime("%Y-%m-%d")
        new_day = new["dep_utc"].strftime("%Y-%m-%d")
        if old_day != new_day:
            migrated.append({
                "segment_id": seg_id,
                "tail_number": new["tail_number"],
                "from_partition": old_day,
                "to_partition": new_day,
                "warehouse_dep_utc": _iso_z(old["dep_utc"]),
                "corrected_dep_utc": _iso_z(new["dep_utc"]),
            })
            # 迁出/迁入分区必须同批
            uf.add(old_day)
            uf.add(new_day)
            uf.union(old_day, new_day)
            migration_days.update((old_day, new_day))
        elif _row_signature(old) != _row_signature(new):
            modified.append({
                "segment_id": seg_id,
                "tail_number": new["tail_number"],
                "partition": new_day,
                "warehouse": _row_payload(old),
                "corrected": _row_payload(new),
            })

    added = sorted(set(co_by_id) - set(wh_by_id))
    removed = sorted(set(wh_by_id) - set(co_by_id))

    # 2) 找出内容有变的全部分区（行指纹集合不同即有变，行序不影响）
    all_days = sorted(set(wh_parts) | set(co_parts))
    changed_days: set[str] = set()
    for day in all_days:
        wh_sig = {_row_signature(r) for r in wh_parts.get(day, [])}
        co_sig = {_row_signature(r) for r in co_parts.get(day, [])}
        if wh_sig != co_sig:
            changed_days.add(day)
    # 迁日端点本身必然内容有变，这里显式并入，避免依赖隐式推理
    affected_days = changed_days | migration_days

    # 3) 并查集分组：迁日牵连的分区同批，其余分区各自成批
    groups: dict[str, list[str]] = {}
    standalone: list[str] = []
    for day in sorted(affected_days):
        if day in uf.parent:
            groups.setdefault(uf.find(day), []).append(day)
        else:
            standalone.append(day)

    grouped_partitions = [sorted(days) for days in groups.values()]
    grouped_partitions.sort(key=lambda days: days[0])
    standalone.sort()

    batches: list[dict] = []
    batch_index = 0
    for days in grouped_partitions + [[d] for d in standalone]:
        batch_index += 1
        partitions = []
        for day in days:
            co_rows = co_parts.get(day, [])
            wh_rows = wh_parts.get(day, [])
            partitions.append({
                "date": day,
                "cleared": len(co_rows) == 0,
                "warehouse_row_count": len(wh_rows),
                "corrected_row_count": len(co_rows),
                "replacement_rows": [_row_payload(r) for r in co_rows],
            })
        reason = "航段迁日，迁出/迁入分区同批替换" if len(days) > 1 \
            else "该分区内容有变，独立替换"
        batches.append({
            "batch_id": batch_index,
            "reason": reason,
            "partition_count": len(partitions),
            "total_replacement_rows": sum(p["corrected_row_count"]
                                          for p in partitions),
            "partitions": partitions,
        })

    cleared = [p["date"] for b in batches for p in b["partitions"]
               if p["cleared"]]
    summary = {
        "warehouse_rows": len(warehouse),
        "corrected_rows": len(corrected),
        "total_partitions": len(all_days),
        "partitions_to_replace": len(affected_days),
        "cleared_partitions": len(cleared),
        "unchanged_partitions": len(all_days) - len(affected_days),
        "batch_count": len(batches),
        "migrated_segments": len(migrated),
        "modified_segments": len(modified),
        "added_segments": len(added),
        "removed_segments": len(removed),
    }
    return {
        "generated_at": _iso_z(datetime.now(timezone.utc)),
        "partition_key": "dep_utc 的 UTC 日期（YYYY-MM-DD）",
        "summary": summary,
        "batches": batches,
        "changes": {
            "migrated": migrated,
            "modified": modified,
            "added": [_row_payload(co_by_id[s]) for s in added],
            "removed": [_row_payload(wh_by_id[s]) for s in removed],
        },
    }


def _row_payload(row: dict) -> dict:
    """对外 JSON 中的行结构：只保留业务四列，时间统一为 Z 格式。"""
    return {
        "segment_id": row["segment_id"],
        "tail_number": row["tail_number"],
        "dep_utc": _iso_z(row["dep_utc"]),
        "arr_utc": _iso_z(row["arr_utc"]),
    }

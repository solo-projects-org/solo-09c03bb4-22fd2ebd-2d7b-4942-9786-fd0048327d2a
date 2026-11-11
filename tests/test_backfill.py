"""回填计划自检：直接运行 `python3 tests/test_backfill.py`（仅依赖标准库）。

覆盖：样例批次结构、行序无关、迁日并查集、清空分区、ID 重复、UTC 非法、
缺列、内容未变分区跳过等规则。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backfill import (  # noqa: E402
    BackfillValidationError, build_backfill_plan, parse_fact_csv, parse_utc,
)

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
WH_SAMPLE = SAMPLES / "backfill_warehouse.csv"
CO_SAMPLE = SAMPLES / "backfill_corrected.csv"

_failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), "-", name)
    if not cond:
        _failures.append(name)


def plan_sample():
    return build_backfill_plan(
        WH_SAMPLE.read_text(encoding="utf-8"),
        CO_SAMPLE.read_text(encoding="utf-8"),
    )


def main():
    plan = plan_sample()
    s = plan["summary"]

    # 1) 样例整体统计
    check("仓内版 8 行", s["warehouse_rows"] == 8)
    check("更正版 8 行", s["corrected_rows"] == 8)
    check("总分区数 6（24~29 中出现过的日子）", s["total_partitions"] == 6)
    check("待替换分区 5 个", s["partitions_to_replace"] == 5)
    check("未变分区 1 个（10-24 不动）", s["unchanged_partitions"] == 1)
    check("清空分区 1 个（10-27）", s["cleared_partitions"] == 1)
    check("批次 4 个", s["batch_count"] == 4)
    check("迁日航段 1 个（SEG-003）", s["migrated_segments"] == 1)
    check("行内修改 2 个（SEG-004 / SEG-010）", s["modified_segments"] == 2)
    check("新增 2 个（SEG-008 / SEG-009）", s["added_segments"] == 2)
    check("删除 2 个（SEG-006 / SEG-007）", s["removed_segments"] == 2)

    # 2) 批次结构：迁日的 25/26 必须同批，其余独立
    batches = plan["batches"]
    b1_dates = [p["date"] for p in batches[0]["partitions"]]
    check("第 1 批为迁日批，含 2026-10-25 与 2026-10-26",
          b1_dates == ["2026-10-25", "2026-10-26"])
    check("迁日批 reason 注明同批替换", "同批" in batches[0]["reason"])
    check("迁日批 batch_id=1", batches[0]["batch_id"] == 1)

    all_batch_dates = [p["date"] for b in batches for p in b["partitions"]]
    check("批次日期有序且无重复",
          all_batch_dates == sorted(all_batch_dates)
          and len(all_batch_dates) == len(set(all_batch_dates)))
    check("10-24 不在任何批次中", "2026-10-24" not in all_batch_dates)

    by_date = {p["date"]: p for b in batches for p in b["partitions"]}
    check("10-27 标记为清空且无替换行",
          by_date["2026-10-27"]["cleared"] is True
          and by_date["2026-10-27"]["replacement_rows"] == [])
    check("10-28 为新分区，带更正版 1 行",
          by_date["2026-10-28"]["corrected_row_count"] == 1
          and by_date["2026-10-28"]["warehouse_row_count"] == 0)

    # 3) 每个待替换分区都带更正版全部行
    p25 = by_date["2026-10-25"]
    check("10-25 替换行 = 更正版该日全部行（SEG-004, SEG-008）",
          sorted(r["segment_id"] for r in p25["replacement_rows"])
          == ["SEG-004", "SEG-008"])
    p26 = by_date["2026-10-26"]
    check("10-26 替换行 = SEG-003（迁入）+ SEG-005",
          sorted(r["segment_id"] for r in p26["replacement_rows"])
          == ["SEG-003", "SEG-005"])
    check("替换行时间统一为 Z 格式",
          p25["replacement_rows"][0]["dep_utc"].endswith("Z"))

    # 4) 迁日明细
    mig = plan["changes"]["migrated"][0]
    check("迁日明细 SEG-003: 25 -> 26",
          mig["segment_id"] == "SEG-003"
          and mig["from_partition"] == "2026-10-25"
          and mig["to_partition"] == "2026-10-26")

    # 5) 行序无关：打乱更正版行序，计划必须完全一致
    co_lines = CO_SAMPLE.read_text(encoding="utf-8").splitlines()
    header, body = co_lines[0], co_lines[1:]
    shuffled = "\n".join([header] + body[::-1]) + "\n"
    plan_shuffled = build_backfill_plan(
        WH_SAMPLE.read_text(encoding="utf-8"), shuffled)
    check("行序打乱后计划一致（忽略 generated_at）",
          {k: v for k, v in plan_shuffled.items() if k != "generated_at"}
          == {k: v for k, v in plan.items() if k != "generated_at"})

    # 6) 内容完全一致 -> 零批次
    same = build_backfill_plan(
        WH_SAMPLE.read_text(encoding="utf-8"),
        WH_SAMPLE.read_text(encoding="utf-8"))
    check("两版完全一致时无批次、无待替换分区",
          same["batches"] == [] and same["summary"]["partitions_to_replace"] == 0)

    # 6b) 同一分区内行序打乱、内容未变 -> 仍然跳过该分区
    wh_one_day = ("segment_id,tail_number,dep_utc,arr_utc\n"
                  "A,B-X,2026-11-01T00:00:00Z,2026-11-01T02:00:00Z\n"
                  "B,B-X,2026-11-01T05:00:00Z,2026-11-01T07:00:00Z\n")
    co_reordered = ("segment_id,tail_number,dep_utc,arr_utc\n"
                    "B,B-X,2026-11-01T05:00:00Z,2026-11-01T07:00:00Z\n"
                    "A,B-X,2026-11-01T00:00:00Z,2026-11-01T02:00:00Z\n")
    reordered = build_backfill_plan(wh_one_day, co_reordered)
    check("分区内仅行序不同 -> 无批次", reordered["batches"] == [])

    # 6c) 只改某一行的机尾号（行指纹变化）-> 仅该分区独立成批
    co_tail = ("segment_id,tail_number,dep_utc,arr_utc\n"
               "A,B-Y,2026-11-01T00:00:00Z,2026-11-01T02:00:00Z\n"
               "B,B-X,2026-11-01T05:00:00Z,2026-11-01T07:00:00Z\n")
    tail_plan = build_backfill_plan(wh_one_day, co_tail)
    check("机尾号修改 -> 1 个独立批次、1 个替换分区、带更正版全部 2 行",
          tail_plan["summary"]["batch_count"] == 1
          and tail_plan["batches"][0]["partitions"][0]["corrected_row_count"] == 2)

    # 7) 迁日链：A 日 1->2，B 日 2->3，三个分区必须同批
    wh = ("segment_id,tail_number,dep_utc,arr_utc\n"
          "A,B-X,2026-11-01T00:00:00Z,2026-11-01T02:00:00Z\n"
          "B,B-X,2026-11-02T00:00:00Z,2026-11-02T02:00:00Z\n"
          "C,B-X,2026-11-03T00:00:00Z,2026-11-03T02:00:00Z\n")
    co = ("segment_id,tail_number,dep_utc,arr_utc\n"
          "A,B-X,2026-11-02T00:00:00Z,2026-11-02T02:00:00Z\n"
          "B,B-X,2026-11-03T00:00:00Z,2026-11-03T02:00:00Z\n"
          "C,B-X,2026-11-03T05:00:00Z,2026-11-03T07:00:00Z\n")
    chain = build_backfill_plan(wh, co)
    check("迁日链 1->2->3 合并为单一批次",
          chain["summary"]["batch_count"] == 1
          and [p["date"] for p in chain["batches"][0]["partitions"]]
          == ["2026-11-01", "2026-11-02", "2026-11-03"])
    check("迁日链中 11-01 被清空",
          chain["batches"][0]["partitions"][0]["cleared"] is True)

    # 8) 互不牵连的两个修改分区 -> 两个批次
    wh2 = ("segment_id,tail_number,dep_utc,arr_utc\n"
           "A,B-X,2026-11-01T00:00:00Z,2026-11-01T02:00:00Z\n"
           "B,B-X,2026-11-05T00:00:00Z,2026-11-05T02:00:00Z\n")
    co2 = ("segment_id,tail_number,dep_utc,arr_utc\n"
           "A,B-X,2026-11-01T00:30:00Z,2026-11-01T02:00:00Z\n"
           "B,B-X,2026-11-05T00:30:00Z,2026-11-05T02:00:00Z\n")
    sep = build_backfill_plan(wh2, co2)
    check("两个无关分区各自成批", sep["summary"]["batch_count"] == 2)

    # 9) 错误：ID 重复 / UTC 非法，必须停止生成并汇总错误
    dup = ("segment_id,tail_number,dep_utc,arr_utc\n"
           "A,B-X,2026-11-01T00:00:00Z,2026-11-01T02:00:00Z\n"
           "A,B-X,2026-11-02T00:00:00Z,2026-11-02T02:00:00Z\n")
    try:
        parse_fact_csv(dup, "仓内版")
        check("重复 segment_id 报错", False)
    except BackfillValidationError as exc:
        check("重复 segment_id 报错",
              any("重复" in e and "A" in e for e in exc.errors))

    bad_utc = ("segment_id,tail_number,dep_utc,arr_utc\n"
               "A,B-X,not-a-time,2026-11-01T02:00:00Z\n"
               "B,B-X,2026-11-01T03:00:00Z,2026-11-01 02:00\n")
    try:
        parse_fact_csv(bad_utc, "更正版")
        check("非法 UTC / 无时区均报错并汇总", False)
    except BackfillValidationError as exc:
        check("非法 UTC / 无时区均报错并汇总", len(exc.errors) == 2)

    # build_backfill_plan 同样向上抛错，不产生计划
    try:
        build_backfill_plan(dup, co2)
        check("校验失败时不生成计划", False)
    except BackfillValidationError:
        check("校验失败时不生成计划", True)

    # 10) 缺列
    bad_header = "segment_id,tail_number,dep_utc\nA,B-X,2026-11-01T00:00:00Z\n"
    try:
        parse_fact_csv(bad_header, "仓内版")
        check("缺列报错并指出列名", False)
    except BackfillValidationError as exc:
        check("缺列报错并指出列名", "arr_utc" in exc.errors[0])

    # 11) parse_utc 兼容 Z 与偏移量写法
    errors = []
    d1 = parse_utc("2026-11-01T00:30:00Z", row_number=1, column="dep_utc",
                   errors=errors)
    d2 = parse_utc("2026-11-01 00:30:00+00:00", row_number=1, column="dep_utc",
                   errors=errors)
    d3 = parse_utc("2026-11-01T08:30:00+08:00", row_number=1, column="dep_utc",
                   errors=errors)
    check("Z / 偏移 / 东八区均解析为同一 UTC 时刻",
          d1 == d2 == d3 and not errors)

    # 12) BOM 表头容忍
    bom = ("﻿segment_id,tail_number,dep_utc,arr_utc\n"
           "A,B-X,2026-11-01T00:00:00Z,2026-11-01T02:00:00Z\n")
    rows = parse_fact_csv(bom, "仓内版")
    check("带 UTF-8 BOM 的表头可正常解析", len(rows) == 1
          and rows[0]["segment_id"] == "A")

    # 13) 落地早于起飞报错
    inverted = ("segment_id,tail_number,dep_utc,arr_utc\n"
                "A,B-X,2026-11-01T05:00:00Z,2026-11-01T02:00:00Z\n")
    try:
        parse_fact_csv(inverted, "仓内版")
        check("落地早于起飞报错", False)
    except BackfillValidationError as exc:
        check("落地早于起飞报错", any("落地" in e for e in exc.errors))

    if _failures:
        print(f"\n{len(_failures)} 项自检失败 ❌")
        raise SystemExit(1)
    print("\n全部自检通过 ✅")


if __name__ == "__main__":
    main()

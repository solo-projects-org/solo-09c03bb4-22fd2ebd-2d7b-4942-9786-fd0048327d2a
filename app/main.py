"""FastAPI 入口：航段时间清洗（DST 安全入仓）与起飞日分区回填计划。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from .backfill import BackfillValidationError, build_backfill_plan
from .solver import CsvFormatError, build_export_csv, solve_csv

BASE_DIR = Path(__file__).resolve().parent
SAMPLE_PATH = BASE_DIR.parent / "samples" / "dst_sample.csv"
BACKFILL_WAREHOUSE_SAMPLE = BASE_DIR.parent / "samples" / "backfill_warehouse.csv"
BACKFILL_CORRECTED_SAMPLE = BASE_DIR.parent / "samples" / "backfill_corrected.csv"
INDEX_PATH = BASE_DIR / "static" / "index.html"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB

app = FastAPI(title="航段时间清洗 · DST 安全入仓 / 分区回填计划", version="2.0.0")


async def _read_csv_text(file: UploadFile) -> str:
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "文件超过 10 MB 上限")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "文件不是合法的 UTF-8 编码 CSV")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(INDEX_PATH)


@app.get("/api/sample", include_in_schema=False)
async def sample():
    """内置夏令时样例，可直接下载或在前端一键载入。"""
    return FileResponse(SAMPLE_PATH, media_type="text/csv; charset=utf-8",
                        filename="dst_sample.csv")


@app.post("/api/solve")
async def solve(file: UploadFile = File(...)):
    """上传 CSV，返回每个机尾的求解结果（唯一解/无解/多解/错误）。"""
    text = await _read_csv_text(file)
    try:
        return solve_csv(text)
    except CsvFormatError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/export")
async def export(file: UploadFile = File(...), tail: str = Form(...)):
    """导出机尾的 UTC CSV。仅当整链唯一解时允许；tail=__all__ 导出全部可导出机尾。"""
    text = await _read_csv_text(file)
    try:
        result = solve_csv(text)
    except CsvFormatError as exc:
        raise HTTPException(400, str(exc))

    if tail == "__all__":
        tails = [t["tail"] for t in result["tails"] if t["status"] == "unique"]
        if not tails:
            raise HTTPException(409, "没有可导出的机尾：仅整链唯一解的机尾允许导出")
        filename = "utc_export_all.csv"
    else:
        match = next((t for t in result["tails"] if t["tail"] == tail), None)
        if match is None:
            raise HTTPException(404, f"CSV 中不存在机尾 {tail!r}")
        if match["status"] != "unique":
            raise HTTPException(
                409, f"机尾 {tail} 状态为 {match['status']}，非整链唯一解，禁止导出")
        tails = [tail]
        filename = f"utc_export_{tail}.csv"

    csv_text = build_export_csv(result, tails)
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# 回填计划
# ---------------------------------------------------------------------------

@app.get("/api/backfill/sample/warehouse", include_in_schema=False)
async def backfill_sample_warehouse():
    """回填演示用：仓内版事实表样例。"""
    return FileResponse(
        BACKFILL_WAREHOUSE_SAMPLE, media_type="text/csv; charset=utf-8",
        filename="backfill_warehouse.csv")


@app.get("/api/backfill/sample/corrected", include_in_schema=False)
async def backfill_sample_corrected():
    """回填演示用：更正版事实表样例。"""
    return FileResponse(
        BACKFILL_CORRECTED_SAMPLE, media_type="text/csv; charset=utf-8",
        filename="backfill_corrected.csv")


@app.post("/api/backfill/plan")
async def backfill_plan(
    warehouse: UploadFile = File(..., description="仓内版全量 CSV"),
    corrected: UploadFile = File(..., description="更正版全量 CSV"),
):
    """对比同一张航段事实表的仓内版与更正版，返回分区回填计划 JSON。

    - 按 dep_utc 的 UTC 起飞日分区，行序不影响结果；
    - 只安排内容有变的分区；航段迁日时迁出/迁入分区同批；
    - 每个待替换分区带更正版全部行，清空分区也列出；
    - ID 重复或 UTC 非法时返回 400 且 detail 为全部错误，不生成计划。
    """
    wh_text = await _read_csv_text(warehouse)
    co_text = await _read_csv_text(corrected)
    try:
        plan = build_backfill_plan(wh_text, co_text)
    except BackfillValidationError as exc:
        return JSONResponse(status_code=400, content={
            "detail": "输入校验失败，已停止生成计划",
            "errors": exc.errors,
        })
    return plan


@app.get("/api/health", include_in_schema=False)
async def health():
    return {"status": "ok"}

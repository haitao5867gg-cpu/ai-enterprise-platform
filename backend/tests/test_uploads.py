"""Uploads are validated, converted and published as one dataset, or leave nothing."""

import io
import json
from pathlib import Path

import duckdb
import httpx
import pytest
from openpyxl import Workbook

from aep import uploads
from aep.config import Settings, get_settings
from aep.datasources import MemoryDataSourceStore
from aep.main import create_app


@pytest.fixture
async def api(tmp_path: Path):
    app = create_app()
    app.state.datasources = MemoryDataSourceStore()
    app.dependency_overrides[get_settings] = lambda: Settings(
        uploads_dir=tmp_path, files_connector_url="http://files.test:8003/"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def upload(api, files, name="本地数据"):
    return await api.post("/datasources/files", data={"name": name}, files=files)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "gbk"])
async def test_csv_encodings_and_metadata(api, tmp_path, encoding):
    response = await upload(
        api, [("files", ("客户.csv", "姓名,金额\n张三,12.5\n李四,20\n".encode(encoding)))]
    )
    assert response.status_code == 201, response.text
    body = response.json()
    ds = body["datasource"]
    assert ds["name"] == "本地数据" and ds["kind"] == "file"
    assert body["schema"] == "upload_" + body["dataset_id"][:8]
    assert ds["connector_url"] == f"http://files.test:8003/datasets/{body['dataset_id']}/mcp"
    [table] = body["tables"]
    assert table["original_filename"] == "客户.csv"
    assert table["original_name"] == "客户"
    assert table["row_count"] == 2
    assert [c["name"] for c in table["columns"]] == ["姓名", "金额"]
    assert table["columns"][1]["data_type"] == "DOUBLE"
    directory = tmp_path / body["dataset_id"]
    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest["tables"] == body["tables"] and manifest["uploaded_at"]
    assert sorted(p.suffix for p in directory.iterdir()) == [".json", ".parquet"]
    assert directory.stat().st_mode & 0o777 == 0o755
    assert all(p.stat().st_mode & 0o777 == 0o644 for p in directory.iterdir())
    with duckdb.connect() as conn:
        rows = conn.read_parquet(str(directory / table["file"])).fetchall()
    assert rows == [("张三", 12.5), ("李四", 20.0)]
    assert (await api.get("/datasources")).json() == [ds]
    assert ds["description"] == f"上传文件：{table['table']}"


def xlsx(sheets):
    book = Workbook()
    book.remove(book.active)
    for title, rows in sheets:
        sheet = book.create_sheet(title)
        for row in rows:
            sheet.append(row)
    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()


async def test_many_files_sheets_and_name_collisions(api, tmp_path):
    response = await upload(
        api,
        [
            ("files", ("Sales-2026.csv", b"id,active\n1,true\n")),
            ("files", ("Sales 2026.csv", b"id,active\n2,false\n")),
            (
                "files",
                (
                    "book.xlsx",
                    xlsx(
                        [
                            ("Sales_2026", [["id", "amount"], [3, 4.5]]),
                            ("库存", [["sku", "quantity"], ["X", 5]]),
                        ]
                    ),
                ),
            ),
        ],
    )
    assert response.status_code == 201, response.text
    tables = response.json()["tables"]
    assert [t["table"] for t in tables] == ["sales_2026", "sales_2026_2", "sales_2026_3", "table"]
    assert tables[2]["original_filename"] == "book.xlsx"
    assert tables[3]["original_name"] == "库存"
    assert all(t["row_count"] == 1 for t in tables)
    assert tables[2]["columns"][0]["data_type"] == "BIGINT"


@pytest.mark.parametrize(
    "filename,content,detail",
    [
        ("bad.txt", b"id\n1", "只支持"),
        ("empty.csv", b"", "为空"),
        ("blank.csv", b"\n\n", "表头"),
        ("header.csv", b"id,name\n", "至少需要一行"),
        ("none.csv", b"1,2\n3,4", "缺少表头"),
        ("missing.csv", b"id,\n1,x", "表头"),
        ("duplicate.csv", b"id, ID \n1,2", "表头重复"),
        ("ragged.csv", b"id,name\n1,x,y", "列数"),
        ("quotes.csv", b'id,name\n1,"unterminated', "解析"),
        ("bad-encoding.csv", b"id\n\xff", "编码"),
        ("binary.csv", b"id\n\x00", "空字符"),
        ("broken.xlsx", b"not-a-zip", "解析"),
        ("empty.xlsx", xlsx([("Empty", [])]), "表头"),
        ("bad-header.xlsx", xlsx([("Bad", [[1, 2], [3, 4]])]), "表头"),
        ("duplicate.xlsx", xlsx([("Bad", [["id", "id"], [1, 2]])]), "表头重复"),
    ],
)
async def test_rejections_name_file_and_leave_no_partial_data(
    api, tmp_path, filename, content, detail
):
    response = await upload(
        api,
        [
            ("files", ("good.csv", b"id\n1\n")),
            ("files", (filename, content)),
        ],
    )
    assert response.status_code == 422, response.text
    assert filename in response.json()["detail"] and detail in response.json()["detail"]
    assert list(tmp_path.iterdir()) == []
    assert (await api.get("/datasources")).json() == []


async def test_limits_and_names(api, tmp_path, monkeypatch):
    # Use a small byte limit while exercising both the multipart size and read guards.
    monkeypatch.setattr(uploads, "MAX_FILE_BYTES", 8)
    assert (await upload(api, [("files", ("ok.csv", b"id\n12345"))])).status_code == 201
    response = await upload(api, [("files", ("big.csv", b"id\n123456"))], name="too big")
    assert response.status_code == 422
    assert "big.csv" in response.json()["detail"] and "50 MB" in response.json()["detail"]
    too_many = [("files", (f"{i}.csv", b"id\n1")) for i in range(21)]
    assert "20" in (await upload(api, too_many)).json()["detail"]
    assert (await upload(api, [], name="empty")).status_code == 422
    for name in [" ", "x" * 101, "x\x00"]:
        assert (await upload(api, [("files", ("a.csv", b"id\n1"))], name)).status_code == 422


async def test_twenty_files_and_large_csv_cells_are_supported(api):
    files = [("files", (f"data-{i}.csv", b"id\n1\n")) for i in range(20)]
    response = await upload(api, files)
    assert response.status_code == 201
    assert len(response.json()["tables"]) == 20
    content = ("payload\n" + "x" * 150_000 + "\n").encode()
    response = await upload(api, [("files", ("large-cell.csv", content))], "大字段")
    assert response.status_code == 201
    assert response.json()["tables"][0]["row_count"] == 1


async def test_more_tables_than_a_run_can_explore_are_rejected(api, tmp_path):
    sheets = xlsx([(f"S{i}", [["id"], [i]]) for i in range(21)])
    response = await upload(api, [("files", ("wide.xlsx", sheets))])
    assert response.status_code == 422
    assert "wide.xlsx" in response.json()["detail"] and "20 张表" in response.json()["detail"]
    files = [("files", (f"data-{i}.csv", b"id\n1\n")) for i in range(19)]
    files.append(("files", ("two.xlsx", xlsx([("A", [["id"], [1]]), ("B", [["id"], [2]])]))))
    response = await upload(api, files, "合计超过")
    assert response.status_code == 422 and "20 张表" in response.json()["detail"]
    assert list(tmp_path.iterdir()) == []
    assert (await api.get("/datasources")).json() == []


async def test_duplicate_registration_and_store_failure_clean_published_directory(
    api, tmp_path, monkeypatch
):
    files = [("files", ("a.csv", b"id\n1"))]
    assert (await upload(api, files)).status_code == 201
    existing = set(tmp_path.iterdir())
    response = await upload(api, files)
    assert response.status_code == 409 and "本地数据" in response.json()["detail"]
    assert set(tmp_path.iterdir()) == existing

    async def fail(*args, **kwargs):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(MemoryDataSourceStore, "create", fail)
    with pytest.raises(RuntimeError, match="store unavailable"):
        await upload(api, files, "failure")
    assert set(tmp_path.iterdir()) == existing


async def test_conversion_failure_and_later_bad_sheet_are_atomic(api, tmp_path, monkeypatch):
    response = await upload(
        api,
        [
            (
                "files",
                (
                    "book.xlsx",
                    xlsx(
                        [
                            ("Good", [["id"], [1]]),
                            ("Bad", [["id", "id"], [1, 2]]),
                        ]
                    ),
                ),
            )
        ],
    )
    assert response.status_code == 422 and "Bad" in response.json()["detail"]
    assert list(tmp_path.iterdir()) == []

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(uploads, "_convert_table", fail)
    response = await upload(api, [("files", ("disk.csv", b"id\n1"))])
    assert response.status_code == 422 and "disk.csv" in response.json()["detail"]
    assert list(tmp_path.iterdir()) == []

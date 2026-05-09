from __future__ import annotations

import pytest

from dtcc_upload.catalog import Catalog, DatasetKeyConflict


def test_init_schema_creates_principals(db_path):
    catalog = Catalog(db_path)
    catalog.init_schema()

    principals = catalog.list_principals()

    assert {row["principal_id"] for row in principals} == {"logg", "vasnas", "browser"}


def test_first_dataset_claim_wins(db_path):
    catalog = Catalog(db_path)
    catalog.init_schema()

    first_claim = catalog.claim_dataset("smoke-slice", "vasnas")
    repeat_claim = catalog.claim_dataset("smoke-slice", "vasnas")

    assert first_claim["owner_principal_id"] == "vasnas"
    assert repeat_claim["owner_principal_id"] == "vasnas"
    with pytest.raises(DatasetKeyConflict):
        catalog.claim_dataset("smoke-slice", "logg")


def test_version_counter_allocates_distinct_increasing_numbers(db_path):
    catalog = Catalog(db_path)
    catalog.init_schema()
    catalog.claim_dataset("smoke-slice", "vasnas")

    first = catalog.allocate_version_number("smoke-slice")
    second = catalog.allocate_version_number("smoke-slice")

    assert first == 1
    assert second == 2


def test_insert_pending_and_commit_version(db_path):
    catalog = Catalog(db_path)
    catalog.init_schema()
    catalog.claim_dataset("smoke-slice", "vasnas")

    version = catalog.insert_pending_version(
        dataset_key="smoke-slice",
        version_id="v1",
        version_number=1,
        principal_id="vasnas",
        manifest_sha256="a" * 64,
        file_set_sha256="b" * 64,
        manifest_size=100,
        format="geojson",
        media_type="application/geo+json",
        data_kind="vector",
        product="slice",
        title="Smoke",
        bounds_json="[0,0,1,1]",
        total_bytes=123,
        file_count=1,
        request_id="req",
        upload_id="upload",
    )

    assert version["status"] == "pending"
    catalog.commit_version("smoke-slice", "v1")
    committed = catalog.get_version("smoke-slice", "v1")

    assert committed["status"] == "committed"
    assert catalog.get_dataset("smoke-slice")["latest_committed_version_id"] == "v1"

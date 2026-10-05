"""统一响应 envelope 与分页 Schema 测试（ENG-02 第 3-4 步）。

对应 `03-api-contract.md` 第 2.2 节与 `shared/api-contract/openapi.json`（LEAD-03 冻结基线）。

第 4 步按裁决 ①③ 对齐基线后：

- envelope 模型不再设置 `extra="forbid"`（基线未声明 `additionalProperties: false`）；
- `PageEnvelope.page.next_cursor` 为必填字段（可为 `null`）；
- `PaginationQuery` 仍是查询参数模型，对未知参数保持严格拒绝。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.common import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    Envelope,
    PageEnvelope,
    PageMeta,
    PaginationQuery,
)


def test_envelope_shape() -> None:
    envelope = Envelope(data={"a": 1}, request_id="req_1")

    assert envelope.model_dump() == {"data": {"a": 1}, "request_id": "req_1"}


def test_envelope_allows_empty_data_object() -> None:
    assert Envelope(data={}, request_id="req_1").model_dump() == {
        "data": {},
        "request_id": "req_1",
    }


def test_envelope_allows_any_json_data() -> None:
    assert Envelope(data=[1, 2], request_id="req_1").data == [1, 2]
    assert Envelope(data=None, request_id="req_1").data is None


def test_envelope_requires_request_id() -> None:
    with pytest.raises(ValidationError):
        Envelope.model_validate({"data": {}})


def test_page_meta_requires_next_cursor() -> None:
    """基线 `required` 含 `next_cursor`（类型 `string|null`）。"""

    with pytest.raises(ValidationError):
        PageMeta.model_validate({"has_more": True})


def test_page_meta_requires_has_more() -> None:
    with pytest.raises(ValidationError):
        PageMeta.model_validate({"next_cursor": None})


def test_page_meta_shape() -> None:
    assert PageMeta(next_cursor="opaque-cursor", has_more=True).model_dump() == {
        "next_cursor": "opaque-cursor",
        "has_more": True,
    }
    assert PageMeta(next_cursor=None, has_more=False).model_dump() == {
        "next_cursor": None,
        "has_more": False,
    }


def test_page_envelope_shape() -> None:
    envelope = PageEnvelope(
        data=[{"id": 1}], page=PageMeta(next_cursor=None, has_more=False), request_id="req_2"
    )

    assert envelope.model_dump() == {
        "data": [{"id": 1}],
        "page": {"next_cursor": None, "has_more": False},
        "request_id": "req_2",
    }


def test_page_envelope_requires_page_metadata() -> None:
    with pytest.raises(ValidationError):
        PageEnvelope.model_validate({"data": [], "request_id": "req_2"})


def test_pagination_query_defaults() -> None:
    query = PaginationQuery()

    assert query.cursor is None
    assert query.limit == DEFAULT_PAGE_LIMIT == 20


@pytest.mark.parametrize("limit", [1, 20, 100])
def test_pagination_query_accepts_allowed_limits(limit: int) -> None:
    assert PaginationQuery(limit=limit).limit == limit


@pytest.mark.parametrize("limit", [0, -1, 101])
def test_pagination_query_rejects_out_of_range_limits(limit: int) -> None:
    with pytest.raises(ValidationError):
        PaginationQuery(limit=limit)


def test_pagination_query_accepts_opaque_cursor() -> None:
    assert PaginationQuery(cursor="eyJvZmZzZXQiOjIwfQ").cursor == "eyJvZmZzZXQiOjIwfQ"


def test_pagination_query_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        PaginationQuery.model_validate({"limit": 20, "offset": 0})


def test_max_page_limit_matches_contract() -> None:
    assert MAX_PAGE_LIMIT == 100


def test_envelope_does_not_declare_additional_properties_false() -> None:
    """基线未声明 `additionalProperties: false`，模型不得引入该约束。"""

    for model in (Envelope, PageEnvelope, PageMeta):
        schema = model.model_json_schema()
        assert schema.get("additionalProperties") is None, model.__name__


# --------------------------------------------------------------------------- #
# 经 HTTP 管线的 envelope 形状（FastAPI 序列化 + response_model 校验）
# --------------------------------------------------------------------------- #
def test_success_envelope_through_http(client) -> None:
    response = client.get("/probe/envelope")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"data", "request_id"}
    assert payload["data"] == {"probe": True}
    assert response.headers["X-Request-Id"] == payload["request_id"]


def test_pagination_envelope_through_http(client) -> None:
    response = client.get("/probe/paginated")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"data", "page", "request_id"}
    assert payload["data"] == [{"id": "synthetic-1"}]
    assert payload["page"] == {"next_cursor": "opaque-cursor", "has_more": True}
    assert response.headers["X-Request-Id"] == payload["request_id"]

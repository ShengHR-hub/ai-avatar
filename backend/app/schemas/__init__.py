"""Pydantic Schema 层（ENG-02）。

- `app.schemas.common`：统一错误响应 envelope（本卡第 2 步）；
- 成功响应 envelope（`data` / `page`）与分页 Schema、health 响应属本卡第 3 步；
- 业务 Schema（User、Generation、MediaAsset 等）属 `ENG-03` 及以后，
  必须与 `03-api-contract.md` 第 5 节逐项一致。
"""

# shared/ — V1.0 语言无关契约产物（ENG-00 骨架）

> 状态：骨架占位。
> 依据：`docs/architecture/09-repository-boundary.md`

## 职责

只保存可生成、可审查的协议产物，不放业务运行时代码。

| 目录 | 职责 |
|---|---|
| `api-contract/` | FastAPI 导出的 `openapi.json` |
| `enums/` | 必要的协议枚举文档/产物 |
| `state-machines/` | 状态机契约文档/产物 |

## 边界约束

- Python 与 TypeScript 不共享源代码，只通过 OpenAPI 共享协议；
- 不得通过复制 Python/TypeScript 源码制造「共享业务层」；
- 本目录内容为生成产物或契约文档，不得承载运行时业务逻辑。

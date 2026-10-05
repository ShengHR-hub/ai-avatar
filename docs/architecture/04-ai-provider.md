# AI Avatar Studio V1.0 Provider 与 Storage 契约

> 状态：LEAD-05 技术审核稿
> 目标：Generation Service 仅依赖内部契约，不依赖供应商、SDK 或存储驱动
> 范围：契约冻结；不代表任何真实 Provider 能力已验证或实现

## 1. 依赖边界

```text
Generation Service
  ├── AIProvider ── Provider Adapter ── 外部 AI Provider
  └── StorageAdapter ── Driver ── MinIO（本地）/ COS、OSS 或 S3（生产）

Webhook HTTP ingress ── 验签/防重放 ── ProviderAdapter.normalize_webhook()
Polling ──────────────────────────────┘
                                      ↓
                         reconcile_provider_status()
```

Service 只使用本文件的内部类型。适配器负责 SDK/HTTP 协议、供应商状态和错误转换、外部图片下载，以及对象存储 API 转换。业务服务、API、Worker 任务不得导入供应商或存储 SDK。V1.0 固定配置单一 Provider，不实现多模型自动路由。`PrimaryProvider` 仅为未来适配器边界，不代表供应商、模型或能力已选定、验证或上线。

## 2. Provider 内部类型与接口

以下为 Python 类型契约示意，不是实现代码；时间均为 UTC aware datetime。敏感原文和供应商对象不得跨越适配器边界。

```python
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Mapping, Protocol, Sequence

class ProviderState(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    PENDING = "PENDING"
    FAILED = "FAILED"

class ProviderErrorCategory(StrEnum):
    VALIDATION = "VALIDATION"
    AUTHENTICATION = "AUTHENTICATION"
    CONTENT_POLICY = "CONTENT_POLICY"
    RATE_LIMIT = "RATE_LIMIT"
    TEMPORARY = "TEMPORARY"
    TIMEOUT = "TIMEOUT"
    UNKNOWN = "UNKNOWN"

@dataclass(frozen=True)
class ImageInput:
    mime_type: str
    content: bytes
    sha256: str

@dataclass(frozen=True)
class GenerateRequest:
    mode: str
    prompt: str
    source_images: Sequence[ImageInput]
    reference_images: Sequence[ImageInput]
    style: Mapping[str, str] | None
    output_count: int
    model: str
    client_request_id: str

@dataclass(frozen=True)
class ProviderOutput:
    content: bytes
    mime_type: str
    provider_output_id: str | None = None

@dataclass(frozen=True)
class ProviderUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    image_count: int | None = None
    estimated_cost: Decimal | None = None
    currency: str | None = None

@dataclass(frozen=True)
class ProviderError:
    category: ProviderErrorCategory
    code: str
    safe_message: str
    retryable: bool
    request_accepted: bool | None

@dataclass(frozen=True)
class ProviderCallError(Exception):
    category: ProviderErrorCategory
    code: str
    safe_message: str
    request_accepted: bool | None

@dataclass(frozen=True)
class ProviderResult:
    state: ProviderState
    client_request_id: str
    provider_request_id: str | None
    outputs: Sequence[ProviderOutput] = ()
    usage: ProviderUsage | None = None
    error: ProviderError | None = None
    retry_after_seconds: int | None = None
    completed_at: str | None = None

class AIProvider(Protocol):
    def generate(self, request: GenerateRequest) -> ProviderResult: ...
    def check_status(self, provider_request_id: str) -> ProviderResult: ...
    def cancel(self, provider_request_id: str) -> ProviderResult: ...
```

Contract invariants:

- Every result is exactly one of `SUCCEEDED`, `PENDING`, `FAILED`; `PROCESSING`, `CANCELLED`, and `UNKNOWN` are not Provider result states. A provider-side cancellation request may resolve to `PENDING`, `SUCCEEDED`, or `FAILED` according to observed terminal outcome; cancellation acceptance is not proof of cancellation.
- `SUCCEEDED` may have zero outputs at the adapter boundary; the service treats zero valid outputs as an uncertain/incomplete generation and sends it to `NEEDS_REVIEW`, never bills it as success.
- `PENDING` must include a stable `provider_request_id`. An adapter may return `PENDING` from `generate()` even for an internally synchronous API when the result is not yet safely available.
- `FAILED` carries a normalized `ProviderError`. It must not include secrets, raw request/response bodies, or unfiltered provider messages.
- `FAILED` means a definite Provider failure. If an adapter operation cannot produce a trustworthy normalized result, it raises `ProviderCallError`; an unrecognized external state or ambiguous acceptance maps to attempt `UNKNOWN` (not a fourth Provider state), and the service retains reserved points for review.
- `cancel()` is best-effort and idempotent. V1.0 has no public user cancellation API; internal Service/Worker/operations commands may invoke it without adding a public route. If unsupported, the adapter reports a normalized non-retryable error; Generation Service still owns the task state transition, mapping confirmed cancellation to `FAILED + RELEASED` and ambiguous cancellation to `NEEDS_REVIEW + RESERVED`.
- Image bytes returned by adapters are ephemeral internal data. The adapter fetches remote outputs itself, under configured provider-host/network policy, and returns bytes for the common image validator; it does not return provider URLs for browser use.

### 2.1 Image understanding port (`ImageAnalyzer`) — reference-image inspiration

Seeing and drawing are two capabilities, two model slots and two price sheets, so the
"参考图片找灵感" flow uses a **separate** port instead of being folded into `AIProvider`.
Keeping them apart is what prevents an image-description call from looking like, or
turning into, a generation task: it creates no task row, reserves no points, never enters
the generation state machine and produces no image.

```python
@dataclass(frozen=True, slots=True)
class ImageDescriptionRequest:
    image: ImageInput                       # server-validated, already downscaled bytes
    language: str = "zh"
    max_output_tokens: int = 300

@dataclass(frozen=True, slots=True)
class ImageDescriptionResult:
    description: str                        # approximate scene description, <= 600 chars
    model: str
    usage: ProviderUsage | None = None      # unknown stays None, never fabricated 0

@runtime_checkable
class ImageAnalyzer(Protocol):
    def describe_image(self, request: ImageDescriptionRequest) -> ImageDescriptionResult: ...
```

Contract invariants:

- `description` is an **approximate description of what is visible**. It is not the original
  author's prompt and the adapter must never claim or reconstruct one; the instruction sent
  to the model says so explicitly, along with "do not copy text/watermarks out of the image"
  and "do not name people, brands or identifiable identities".
- Failure is always `ProviderCallError` — the port has no `FAILED` result channel and never
  returns a partially-built description. `classify()` therefore does not arbitrate this port
  (it exists for generation attempts); the adapter itself never retries.
- Timeouts: connect 10s, read 45s. Seeing an image is far cheaper and faster than drawing
  one, so a 45s silence is treated as *acceptance unknown* and surfaced to the user —
  retrying would be a second paid call. A failure that provably never left the machine
  (`ConnectTimeout` / `ConnectError` / pool timeout) reports `request_accepted=False`.
- The model comes from `AI_VISION_MODEL` only. Falling back to `AI_MODEL` is forbidden:
  a drawing model asked to read an image produces confident nonsense, and a missing
  `AI_VISION_MODEL` under a real provider must fail loudly at assembly rather than
  silently substituting `MockImageAnalyzer` (which would show the user a canned string
  as if it had been read from their picture, and then let them spend points generating from it).
- Bytes sent to the model are re-encoded to JPEG with the long edge capped at 1024px.
  This is a cost measure (tokens track pixels) and a privacy measure in one move: the
  re-encode drops camera metadata (GPS, device, author) that the user never agreed to ship.
- The description is *derived personal data*: it is not written to PostgreSQL, not logged,
  and only lives in Redis for 6 hours as a replay cache so a repeated click on the same
  picture costs nothing.
- Adapter `usage` is recorded for internal cost accounting only and never crosses the API
  response boundary; `estimated_cost`/`currency` stay `NULL` while the relay's quota-to-USD
  conversion remains uncalibrated.
- Paid-call gates live above the port, in the service (`services/analysis/guard.py`):
  same-image cache replay, single-flight lock, per-user and global daily quotas, all
  **fail-closed** — an unreachable gate store refuses the call instead of dispatching
  a request whose cost state it cannot see.

## 3. Request IDs, attempts, timeouts and retries

`client_request_id` is generated by the service, scoped to one `provider_attempts` attempt, stable across transport retries and status checks for that attempt, and sent as the provider idempotency/correlation key when supported. A deliberate new provider attempt gets a new value. `provider_request_id` is assigned by the external service and is used for status, cancel, callback correlation, and incident investigation. The database's existing `provider_attempts.request_id` stores `provider_request_id`; `client_request_id` is retained in sanitized request metadata. No database column is added. `request_id` in the public API/logging contract remains the application's request correlation ID and is not either provider ID.

Timeout defaults remain those already defined by V1.0: connect 10s, read 180s, overall attempt/task processing limit 240s. A pre-acceptance timeout is retryable only when the adapter has reliable evidence that the Provider did not accept the request and no external execution occurred. A timeout after acceptance, with unknown acceptance, connection interruption, incomplete response, or missing result is attempt `UNKNOWN`, task `NEEDS_REVIEW + RESERVED`, and is never automatically retried.

Retry only normalized `RATE_LIMIT`, `TEMPORARY`, or pre-acceptance `TIMEOUT` failures when the adapter can establish that no billable job was accepted and no external execution occurred, or can safely resume/query the same request using the same `client_request_id`. Maximum three retries with 5s, 30s, 120s backoff; every retry creates and retains a distinct `provider_attempts` row. `VALIDATION`, `AUTHENTICATION`, and `CONTENT_POLICY` are not retried. A post-acceptance timeout, unknown acceptance, connection interruption, incomplete response, malformed terminal response, missing/ambiguous external ID, result absence, or provider status beyond the overall deadline becomes attempt `UNKNOWN` and task `NEEDS_REVIEW`; billing stays `RESERVED` pending explicit review. Retries never silently switch provider/model.

## 4. Provider status sources and webhook protocol

Polling and Webhook are two transports into the same normalized status/reconciliation path. `generate()` returns immediate status or submission identity. Polling invokes `check_status(provider_request_id)`. The provider-specific ingress verifies the raw webhook then maps it into an internal `ProviderResult`; both sources call `reconcile_provider_status()` under a generation/attempt lock. Only a valid transition can update the attempt/task. Terminal outcomes are monotonic: a duplicate or late `PENDING` event cannot reopen a terminal attempt, and repeated terminal events are no-ops after the first committed reconciliation.

Webhook security requirements:

1. Use HTTPS. Verify the provider-specific signature over the exact raw request bytes before parsing; compare MAC/signature in constant time. The signing secret is server-side only and has a rotation procedure.
2. Require a signed timestamp within a five-minute acceptance window and a signed provider event ID/nonce. Atomically record the event ID as consumed with a TTL at least as long as the acceptance window plus clock-skew allowance. Reject missing, invalid, or expired IDs. A correctly signed event ID already consumed is acknowledged as a no-op and must not be reconciled again.
3. Correlate only by `provider_request_id` to an existing attempt for the configured provider. Never trust a callback-supplied user/task ID as authorization. Unknown IDs are rejected and security-logged without payload.
4. After verification, persist/reconcile idempotently. Database attempt locking and terminal-state checks remain authoritative if the short-lived replay cache is unavailable or evicted. A callback must never directly settle points or write outputs outside Generation Service.
5. Respond success only after a valid event is durably accepted or identified as a safe duplicate; invalid events receive a non-success response. Do not log signature headers, secrets, full callback payload, image URLs, or prompt content.

Exact header names, canonicalization, signature algorithm and event envelope are provider-specific and must be verified against the selected provider before ENG-07; no provider format is assumed by this contract.

## 5. Error taxonomy and attempt record mapping

| Normalized category | Meaning | Default action |
|---|---|---|
| `VALIDATION` | Request rejected as invalid | No retry; task failure and release according to LEAD-04 |
| `AUTHENTICATION` | Credentials/account rejected | No retry; operational alert; do not expose credential details |
| `CONTENT_POLICY` | Provider policy refusal | No retry; normalized failure and release according to LEAD-04 |
| `RATE_LIMIT` | Throttled | Retry only if acceptance is known safe; honor bounded retry-after/backoff |
| `TEMPORARY` | Transient provider/service failure | Retry only if known safe; otherwise UNKNOWN |
| `TIMEOUT` | Deadline elapsed | Retry only for reliable pre-acceptance/no-execution evidence; otherwise attempt `UNKNOWN`, task `NEEDS_REVIEW`, billing `RESERVED` |
| `UNKNOWN` | Unmapped error or outcome cannot be established | No blind retry; attempt `UNKNOWN`, task `NEEDS_REVIEW` |

The existing `provider_attempts` row is one logical provider attempt, not one HTTP packet. Map its existing fields without schema changes:

| Existing field | Contract meaning |
|---|---|
| `status` | `STARTED` before call; `SUBMITTED` once accepted/pending; then `SUCCEEDED`, `FAILED`, `TIMEOUT`, or `UNKNOWN` |
| `request_id` | External `provider_request_id` (nullable before submission) |
| `request_metadata` | Allowlisted, redacted inputs including `client_request_id`, mode, model and non-sensitive dimensions; never prompt, image bytes, credentials or signed URLs |
| `response_metadata` | Allowlisted normalized state/output count/provider-safe diagnostics; never raw provider response |
| `error_code`, `error_message` | Stable normalized category/code and safe message |
| usage/cost fields | `input_tokens`, `output_tokens`, `image_count`, `estimated_cost`, `currency`; unknown/not supplied is `NULL`, never fabricated zero |
| timestamps | `started_at` at first outbound attempt; `finished_at` when result is known or classified timeout/unknown; duration is derived |

Usage and estimated cost are recorded per logical provider attempt from provider-reported usage when available, otherwise from the versioned internal price snapshot applicable at call time. Keep estimate distinct from provider-reported usage in sanitized metadata. Store decimal cost at the existing precision and ISO 4217 currency when known. Do not convert unknown cost to zero, do not sum transport retries as separate billable generations, and do not use provider cost to change user points. If actual cost is later reconciled, update only the corresponding attempt's cost record under audit; retain the original request/response redaction boundary.

## 6. MockProvider behavior protocol

Mock scenarios are selected only by server-side test configuration or an explicit test fixture, never by public user input. Use deterministic fake IDs/content and a controllable clock/sleeper; no real network, provider credentials, or billable calls. Default mode is `success`. Each scenario must drive the same `AIProvider`, reconciliation, image validation, storage, and billing paths as a future adapter.

| Scenario | Required behavior | Expected service outcome |
|---|---|---|
| `success` | `generate()` returns `SUCCEEDED` with exactly requested-count valid deterministic raster images and usage/cost fixture | Store validated outputs, complete task and consume once |
| `delayed` | First `generate()` returns `PENDING` with stable fake provider ID; configured number of polls remain pending, then status succeeds | Poll/restart recovery reaches success once |
| `failure` | Return normalized `FAILED` with configurable category/code, default `TEMPORARY` known not accepted | Retry only when category/rules permit; final definite failure releases once |
| `timeout` | Simulate timeout before acceptance and a separate timeout-after-acceptance variant | Pre-accept retries only with reliable no-acceptance/no-execution evidence and retained attempts; post-accept or ambiguous timeout becomes `UNKNOWN`/`NEEDS_REVIEW + RESERVED` with no automatic retry |
| `unknown` | Accepted request has no resolvable status/result by deadline, with no false terminal assertion | Attempt `UNKNOWN`, task `NEEDS_REVIEW`, reservation retained |
| `success_without_image` | Return `SUCCEEDED` with empty output list | Task `NEEDS_REVIEW`; no successful billing or invented image |
| `duplicate_webhook` | Emit the same valid terminal callback at least twice, and optionally a late `PENDING` after terminal | One reconciliation/settlement only; terminal state is not reopened |

Scenario controls and fake provider artifacts must not alter API or database contracts. Tests assert observable attempt/task/billing/storage outcomes, not implementation-specific sleep timing.

## 7. Storage Adapter contract

```python
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Protocol

@dataclass(frozen=True)
class StoredObject:
    key: str
    size_bytes: int
    etag: str | None = None

@dataclass(frozen=True)
class ObjectMetadata:
    size_bytes: int
    content_type: str | None
    etag: str | None = None

@dataclass(frozen=True)
class SignedUrl:
    url: str
    expires_at: datetime

class StorageAdapter(Protocol):
    def upload(self, key: str, content: bytes, content_type: str) -> StoredObject: ...
    def read(self, key: str) -> tuple[bytes, ObjectMetadata]: ...
    def create_signed_url(
        self, key: str, method: str, expires_in: timedelta,
        content_type: str | None = None,
    ) -> SignedUrl: ...
    def delete(self, key: str) -> None: ...  # idempotent
    def exists(self, key: str) -> bool: ...
    def cleanup(self, expired_keys: Iterable[str]) -> int: ...  # idempotent batch delete
```

Adapter contract: keys are opaque server-generated values, never user paths; buckets remain private; upload overwrites are prevented through unique keys/conditional create; reads and deletes never accept browser-provided raw keys. `read()` and `upload()` are server-side operations. Browser uploads/downloads use API-authorized short-lived URLs, and an upload is not a usable `media_asset` until the completion endpoint verifies it. Signed URL default TTL is at most 5 minutes for reads and 10 minutes for uploads; callers may shorten but not exceed these limits. Never persist/log signed URLs.

`StorageLifecycleService` owns lifecycle selection from existing `media_assets.expires_at`, visibility/deletion checks, and batches expired `storage_key`s into `cleanup()`. Cleanup is idempotent, bounded, and retries transient failures; metadata is marked `deleted_at` only after deletion succeeds or the object is confirmed absent. Default lifetimes remain originals/references 7 days and results 30 days. A provider-native lifecycle policy may be defense in depth but does not replace DB-driven cleanup/audit.

Driver boundary: local uses MinIO through the S3-compatible interface; production selects exactly one configured COS, OSS, or S3 driver. `storage_provider` records the configured driver label. Only the driver module imports its SDK. No SDK, bucket endpoint, credentials, provider ACL, or native exception escapes the adapter. Switching driver is configuration plus deployment setup, not Generation Service changes.

## 8. Image validation and asset access rules

Every client upload and every provider output passes the same server-side validation before use or persistence:

1. Enforce request and streamed byte limits before buffering. Initial V1 proposed maximum is 10 MiB per image; reject empty files and declared/observed size mismatches.
2. Allow only still-image `image/jpeg`, `image/png`, and `image/webp`. Treat client MIME and filename extension as hints only; sniff file signature and require decoder-detected format to match. Reject SVG, GIF/animated formats, unknown formats, polyglots, malformed/truncated files, and unsupported color/profile/decode cases.
3. Fully decode with a maintained image library in a resource-limited worker. Reject zero/invalid dimensions, either side above 4096 px, or total pixels above 16 megapixels. Apply EXIF orientation before recording width/height; strip metadata such as EXIF/GPS before storing any normalized/display rendition.
4. Compute SHA-256 over exact received bytes and compare with the declared digest when supplied. Persist the verified digest and actual byte count. Hash is integrity metadata, not proof of trust or authorization.
5. Re-encode display/result renditions to an allowed raster format where processing is part of the workflow; never serve unvalidated original bytes inline from a public origin. Provider output URLs, if any, are fetched by the adapter with HTTPS, redirect and destination restrictions to prevent SSRF; arbitrary private/link-local destinations are forbidden.
6. Any validation failure prevents asset completion and Provider submission. Remove/quarantine temporary objects through the lifecycle path; return the existing stable API error mapping, not parser internals.

Access rules use existing `media_assets.visibility` values; visibility is not itself an authorization check:

| Visibility | Read rule |
|---|---|
| `PRIVATE` | Default for `ORIGINAL`, `REFERENCE`, and `RESULT`; only owner-authorized service operations may mint a short-lived URL. Other users and anonymous callers are denied. |
| `SHARED` | Only for the asset attached to an active, unexpired share; anonymous share-page access is scoped to that share. A share revocation/expiry stops issuing URLs immediately; existing signed URLs may remain usable only until their short TTL expires. |
| `PUBLIC` | Only intentionally published catalog/public content. Never used for user uploads/results by default; public bucket/ACL access remains disabled and access still passes application authorization. |

The API validates owner, asset type, share state and feature policy before asking the adapter to sign. The browser cannot choose an object key, visibility, bucket, MIME, or URL expiry. `REFERENCE_IMAGE` is rejected with the existing `FEATURE_NOT_AVAILABLE` API error whenever `ENABLE_REFERENCE_IMAGE=false`, before accepting/associating reference assets or enqueueing work; it is never silently ignored.

## 9. Existing schema/API compatibility

This contract does not add or rename API fields, errors, database fields, enums, or migrations. It uses the existing `provider_attempts`, `media_assets`, `generation_inputs`, and `generation_outputs` fields and the existing `FEATURE_NOT_AVAILABLE` error. In particular, provider request correlation uses existing `request_id`; visibility values, `storage_provider`, MIME/size/hash fields and lifecycle timestamps remain unchanged. Any later need for a durable webhook inbox, additional cost dimensions, asset scan state, or provider capability registry requires a separately reviewed LEAD-02/LEAD-03 change before implementation.

## 10. Next engineering cards

- `ENG-05` implements only StorageAdapter, local MinIO driver, upload completion validation, ownership/visibility authorization, and DB-driven expiry cleanup. It must use existing API/schema contracts, private buckets, short-lived signatures and the image rules above. Do not implement a provider, change fields, or enable reference-image mode.
- `ENG-06` implements Generation Service orchestration, Celery polling/recovery, reconciliation and MockProvider scenarios. It depends only on the frozen AIProvider/StorageAdapter types and LEAD-04 billing/state rules. Tests must cover retries, UNKNOWN, empty outputs and duplicate Webhooks without real services/provider credentials beyond local infrastructure.
- `ENG-07` is not authorized by this document. After ENG-06's Mock loop passes and a fresh LEAD-05 review, it may implement one specifically selected PrimaryProvider adapter and its verified webhook/auth/usage behavior. Before coding, record actual model capability, sync/async mode, idempotency, status lookup, cancellation, image formats, costs, webhook signature details and retention constraints. Do not infer unsupported capabilities or add routing.

## 11. Open decisions for technical review

1. Confirm initial image byte/pixel limits (proposed 10 MiB, 4096 px per side, 16 MP) against product and provider constraints.
2. Confirm signed URL TTLs (proposed read 5 minutes, upload 10 minutes) and whether the browser upload URL requires a signed checksum header.
3. Select image decoder/re-encoder and decide whether malware scanning is required for V1.0 uploads.
4. Select production object-storage vendor/region and its encryption, retention, deletion-verification and lifecycle policy; COS/OSS/S3 remain alternatives, not selected implementations.
5. Select a PrimaryProvider and verify its idempotency, query/cancel, callback and cost/usage capabilities before ENG-07. Some providers may have no Webhook or reliable cancellation; the adapter must then rely on polling/best-effort semantics.
6. Decide whether a durable webhook event inbox is required before production. Current schema permits idempotent reconciliation and short-lived replay protection but has no dedicated event table; adding one requires LEAD-02 approval.
7. Confirm whether final provider-reported cost reconciliation needs immutable history rather than the current per-attempt estimate fields; no additional cost columns are authorized here.

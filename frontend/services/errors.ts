/**
 * 错误归一：只认稳定错误码（契约第 7 节），不拿 `message` 文本做业务判断。
 */
import type { Error as ContractError, ErrorResponse } from "@/generated/types.gen";

export type FailureAction =
  | "login"
  | "verify_email"
  | "recharge"
  | "retry"
  | "wait"
  | "review"
  | "unsupported"
  | "none";

const ACTION_BY_CODE: Readonly<Record<string, FailureAction>> = {
  AUTH_REQUIRED: "login",
  SESSION_INVALID: "login",
  EMAIL_NOT_VERIFIED: "verify_email",
  EMAIL_TOKEN_INVALID: "verify_email",
  EMAIL_IN_USE: "none",
  INVITE_INVALID: "retry",
  INVITE_ALREADY_USED: "none",
  INSUFFICIENT_POINTS: "recharge",
  ACCOUNT_SUSPENDED: "none",
  IDEMPOTENCY_REQUIRED: "none",
  IDEMPOTENCY_INVALID: "none",
  IDEMPOTENCY_IN_PROGRESS: "wait",
  IDEMPOTENCY_CONFLICT: "retry",
  RATE_LIMITED: "wait",
  FEATURE_NOT_AVAILABLE: "unsupported",
  ASSET_NOT_READY: "wait",
  ASSET_INVALID: "retry",
  UPLOAD_TOO_LARGE: "retry",
  MEDIA_TYPE_UNSUPPORTED: "unsupported",
  ASSET_NOT_FOUND: "none",
  ASSET_FORBIDDEN: "none",
  GENERATION_NOT_FOUND: "none",
  GENERATION_NOT_RETRYABLE: "none",
  // ENG-09：分享/反馈的稳定码映射到最贴近的既有动作与标题
  GENERATION_NOT_SHAREABLE: "unsupported",
  NOT_READY: "wait",
  PROVIDER_UNAVAILABLE: "wait",
  PROVIDER_BAD_GATEWAY: "wait",
  INTERNAL_ERROR: "none",
  VALIDATION_ERROR: "none",
};

const TITLE_BY_ACTION: Readonly<Record<FailureAction, string>> = {
  login: "请先登录",
  verify_email: "邮箱尚未验证",
  recharge: "点数不足",
  retry: "这次没有成功",
  wait: "稍后再试",
  review: "需要人工复核",
  unsupported: "当前版本未开放",
  none: "操作未完成",
};

/**
 * 少数错误码的展示标题需要比动作默认文案更准确，且**不改变动作语义**
 * （`action` 仍决定按钮引导方向，只有标题按码细化）。
 */
const TITLE_BY_CODE: Readonly<Record<string, string>> = {
  GENERATION_NOT_SHAREABLE: "这个作品还不能分享",
};

function isErrorResponse(value: unknown): value is ErrorResponse {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as ErrorResponse).error === "object" &&
    (value as ErrorResponse).error !== null
  );
}

/** 可抛出的失败：`code` 是唯一业务判据，`requestId` 供用户报障时对照。 */
export class ApiFailure extends Error {
  readonly code: string;
  readonly status: number;
  readonly requestId: string | null;
  readonly details: Readonly<Record<string, unknown>>;

  constructor(input: {
    code: string;
    message: string;
    status: number;
    requestId?: string | null;
    details?: Record<string, unknown>;
  }) {
    super(input.message);
    this.name = "ApiFailure";
    this.code = input.code;
    this.status = input.status;
    this.requestId = input.requestId ?? null;
    this.details = Object.freeze({ ...(input.details ?? {}) });
  }

  get action(): FailureAction {
    return failureAction(this.code);
  }

  get title(): string {
    return TITLE_BY_ACTION[this.action];
  }

  get isAuth(): boolean {
    return this.status === 401 || this.code === "AUTH_REQUIRED" || this.code === "SESSION_INVALID";
  }
}

/**
 * 后端非 2xx 载荷按 Schema 解析后进入这里。形状不符（网关 HTML、空体）时保留状态码，
 * 码记为 `UNKNOWN_RESPONSE`：不伪造成功，也不把原始响应体透出到界面。
 */
export function toApiFailure(payload: unknown, status: number, requestId?: string | null): ApiFailure {
  if (isErrorResponse(payload)) {
    const error: ContractError = payload.error;
    return new ApiFailure({
      code: error.code,
      message: error.message,
      status,
      requestId: payload.request_id ?? requestId ?? null,
      details: { ...(error.details ?? {}) },
    });
  }
  const record = (payload ?? {}) as Record<string, unknown>;
  return new ApiFailure({
    code: typeof record.code === "string" ? record.code : "UNKNOWN_RESPONSE",
    // 非契约载荷（网关 HTML、传输层 TypeError）不得把原始异常文本抬成界面文案：
    // 它既不是稳定码，也可能带内部细节。判断只靠 `code`。
    message: "服务返回了未预期的响应，请稍后再试。",
    status,
    requestId: requestId ?? null,
    details:
      typeof record.details === "object" && record.details !== null
        ? (record.details as Record<string, unknown>)
        : {},
  });
}

export function failureAction(code: string): FailureAction {
  return ACTION_BY_CODE[code] ?? "none";
}

export function failureTitle(code: string): string {
  return TITLE_BY_CODE[code] ?? TITLE_BY_ACTION[failureAction(code)];
}

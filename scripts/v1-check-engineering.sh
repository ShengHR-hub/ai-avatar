#!/usr/bin/env bash
# ============================================================================
# v1-check-engineering.sh — AI Avatar Studio V1.0 只读工程检查
# ----------------------------------------------------------------------------
# 归属：ENG-00 步骤 4（建立最小工程检查）
# 依据：docs/architecture/09-repository-boundary.md
#      docs/task-cards/ENG-00-engineering-foundation.md
#      docs/task-cards/ENGINEERING-STRICT-PROTOCOL.md
#
# 性质：只读。本脚本只做三件事：
#   1) 判断文件与目录是否存在；
#   2) 读取文本文件内容（grep / sed / awk）；
#   3) 调用 `docker compose ... config` 解析配置，不创建任何容器。
# 本脚本不连接数据库、不执行迁移、不调用 AI Provider、不写业务数据、
# 不启动 Compose 服务栈、不修改任何环境文件或仓库文件。
# 上述只读保证由脚本末尾的只读自审（检查 6-10）逐项校验，而非仅靠声明。
#
# 前置条件（当前项目为 Windows 开发环境）：
#   1) Git Bash（bash 4+）或 WSL，需提供 grep / sed / awk；
#   2) Docker Desktop 已安装并启动，`docker compose` v2 在 PATH 中。
#   运行方式（PowerShell，仓库根目录）：
#       bash scripts/v1-check-engineering.sh
#   运行方式（Git Bash / WSL，仓库根目录）：
#       ./scripts/v1-check-engineering.sh
#
# 退出码：
#   0 = 全部检查通过
#   1 = 存在失败检查项（逐项打印 [FAIL] 与原因）
#   2 = 前置条件不满足（缺少必需命令或无法进入仓库根目录）
# ============================================================================

set -u

SCRIPT_PATH="${BASH_SOURCE[0]}"
SCRIPT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
SCRIPT_ABS="${SCRIPT_DIR}/$(basename "${SCRIPT_PATH}")"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

ENV_TEMPLATE="docker/.env.example"
COMPOSE_FILE="docker/compose.yml"

PASSED=0
FAILED=0

pass() { PASSED=$((PASSED + 1)); printf '[PASS] %s\n' "$1"; }
fail() { FAILED=$((FAILED + 1)); printf '[FAIL] %s\n' "$1"; }
info() { printf '       %s\n' "$1"; }

printf '============================================================\n'
printf 'AI Avatar Studio V1.0 — 只读工程检查（ENG-00 步骤 4）\n'
printf '仓库根目录：%s\n' "${REPO_ROOT}"
printf '============================================================\n'

cd "${REPO_ROOT}" || { printf '[FAIL] 无法进入仓库根目录：%s\n' "${REPO_ROOT}"; exit 2; }

# ---------------------------------------------------------------------------
# 前置条件：必需命令
# ---------------------------------------------------------------------------
missing_tools=0
for tool in grep sed awk docker; do
  if ! command -v "${tool}" >/dev/null 2>&1; then
    info "缺少必需命令：${tool}"
    missing_tools=$((missing_tools + 1))
  fi
done
if [ "${missing_tools}" -ne 0 ]; then
  printf '\n前置条件不满足（缺少 %s 个命令），退出码 2。\n' "${missing_tools}"
  printf '请在 Git Bash / WSL 中运行，并确认 Docker Desktop 已启动。\n'
  exit 2
fi

# ---------------------------------------------------------------------------
# 变量契约（只读引用，脚本不修改这些值）
# ---------------------------------------------------------------------------
REQUIRED_VARS='APP_ENV APP_BASE_URL DATABASE_URL REDIS_URL STORAGE_DRIVER STORAGE_ENDPOINT STORAGE_BUCKET STORAGE_REGION STORAGE_ACCESS_KEY STORAGE_SECRET_KEY AI_PROVIDER AI_BASE_URL AI_MODEL AI_API_KEY SESSION_SECRET ENABLE_REFERENCE_IMAGE POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD MINIO_ROOT_USER MINIO_ROOT_PASSWORD NEXT_PUBLIC_API_BASE_URL NEXT_PUBLIC_APP_URL'

SERVER_SIDE_VARS='APP_ENV APP_BASE_URL DATABASE_URL REDIS_URL STORAGE_DRIVER STORAGE_ENDPOINT STORAGE_BUCKET STORAGE_REGION STORAGE_ACCESS_KEY STORAGE_SECRET_KEY AI_PROVIDER AI_BASE_URL AI_MODEL AI_API_KEY SESSION_SECRET ENABLE_REFERENCE_IMAGE POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD MINIO_ROOT_USER MINIO_ROOT_PASSWORD'

ALLOWED_PUBLIC_VARS='NEXT_PUBLIC_API_BASE_URL NEXT_PUBLIC_APP_URL'

SENSITIVE_TOKEN_REGEX='(DATABASE|REDIS|STORAGE|SESSION|POSTGRES|MINIO|SECRET|PASSWORD|TOKEN|CREDENTIAL|_KEY)'

EXPECTED_SERVICES='backend beat frontend minio postgres redis worker'

COMPOSE_CONFIG_TEXT=''

# ---------------------------------------------------------------------------
# 检查 1：V1.0 目录骨架存在
# ---------------------------------------------------------------------------
check_01_skeleton() {
  local missing=0 d f
  for d in \
    frontend \
    frontend/app frontend/components frontend/hooks frontend/services \
    frontend/types frontend/generated frontend/public \
    backend backend/app \
    backend/app/api backend/app/services backend/app/repositories backend/app/db \
    backend/app/tasks backend/app/providers backend/app/storage backend/app/schemas \
    backend/alembic backend/tests \
    shared shared/api-contract shared/enums shared/state-machines \
    docker scripts ; do
    if [ ! -d "${d}" ]; then
      info "缺少目录：${d}"
      missing=$((missing + 1))
    fi
  done
  for f in \
    frontend/package.json \
    frontend/app/README.md frontend/services/README.md \
    backend/app/main.py backend/app/config.py \
    backend/app/api/README.md backend/app/services/README.md \
    backend/app/repositories/README.md backend/app/tasks/README.md \
    backend/alembic.ini \
    shared/api-contract/README.md \
    docker/compose.yml docker/.env.example \
    scripts/README.md ; do
    if [ ! -f "${f}" ]; then
      info "缺少文件：${f}"
      missing=$((missing + 1))
    fi
  done
  if ! compgen -G 'frontend/next.config.*' >/dev/null 2>&1; then
    info "缺少文件：frontend/next.config.*"
    missing=$((missing + 1))
  fi
  [ "${missing}" -eq 0 ]
}

if check_01_skeleton; then
  pass '检查 1：V1.0 目录骨架存在'
else
  fail '检查 1：V1.0 目录骨架存在'
fi

# ---------------------------------------------------------------------------
# 检查 2：docker/.env.example 存在且必需变量齐全
# ---------------------------------------------------------------------------
check_02_env_template() {
  local ok=1 v n
  if [ ! -f "${ENV_TEMPLATE}" ]; then
    info "缺少文件：${ENV_TEMPLATE}"
    return 1
  fi
  for v in ${REQUIRED_VARS}; do
    n="$(grep -cE "^${v}=" "${ENV_TEMPLATE}" || true)"
    if [ "${n}" -eq 0 ]; then
      info "模板缺少必需变量：${v}"
      ok=0
    elif [ "${n}" -gt 1 ]; then
      info "模板重复定义变量：${v}（${n} 次）"
      ok=0
    fi
  done
  [ "${ok}" -eq 1 ]
}

if check_02_env_template; then
  pass '检查 2：docker/.env.example 存在且 23 项必需变量齐全'
else
  fail '检查 2：docker/.env.example 存在且 23 项必需变量齐全'
fi

# ---------------------------------------------------------------------------
# 检查 3：docker compose 配置可解析且七个服务齐全
# ---------------------------------------------------------------------------
check_03_compose_config() {
  local out rc ok=1 s actual
  out="$(docker compose --env-file "${ENV_TEMPLATE}" -f "${COMPOSE_FILE}" config 2>&1)"
  rc=$?
  if [ "${rc}" -ne 0 ]; then
    info "配置解析失败，退出码 ${rc}"
    printf '%s\n' "${out}" | head -20 | while IFS= read -r line; do info "| ${line}"; done
    return 1
  fi
  COMPOSE_CONFIG_TEXT="${out}"

  actual="$(docker compose --env-file "${ENV_TEMPLATE}" -f "${COMPOSE_FILE}" config --services 2>/dev/null | sort | tr '\n' ' ' | sed -E 's/[[:space:]]+$//')"
  for s in ${EXPECTED_SERVICES}; do
    case " ${actual} " in
      *" ${s} "*) ;;
      *) info "缺少 Compose 服务：${s}"; ok=0 ;;
    esac
  done
  for s in ${actual}; do
    case " ${EXPECTED_SERVICES} " in
      *" ${s} "*) ;;
      *) info "出现未预期的 Compose 服务：${s}"; ok=0 ;;
    esac
  done
  [ "${ok}" -eq 1 ]
}

if check_03_compose_config; then
  pass '检查 3：docker compose 配置可解析且七个服务齐全'
else
  fail '检查 3：docker compose 配置可解析且七个服务齐全'
fi

# ---------------------------------------------------------------------------
# 检查 4：前端公开变量仅包含允许的两个
# ---------------------------------------------------------------------------
check_04_public_whitelist() {
  local ok=1 v actual
  actual="$(grep -oE '^NEXT_PUBLIC_[A-Z0-9_]*' "${ENV_TEMPLATE}" 2>/dev/null | sort -u)"
  if [ -z "${actual}" ]; then
    info '模板中未定义任何前端公开变量'
    return 1
  fi
  for v in ${actual}; do
    case " ${ALLOWED_PUBLIC_VARS} " in
      *" ${v} "*) ;;
      *) info "未授权的前端公开变量：${v}"; ok=0 ;;
    esac
  done
  for v in ${ALLOWED_PUBLIC_VARS}; do
    if ! printf '%s\n' "${actual}" | grep -qx "${v}"; then
      info "缺少前端公开变量：${v}"
      ok=0
    fi
  done
  [ "${ok}" -eq 1 ]
}

if check_04_public_whitelist; then
  pass '检查 4：前端公开变量仅 NEXT_PUBLIC_API_BASE_URL / NEXT_PUBLIC_APP_URL'
else
  fail '检查 4：前端公开变量仅 NEXT_PUBLIC_API_BASE_URL / NEXT_PUBLIC_APP_URL'
fi

# ---------------------------------------------------------------------------
# 检查 5：服务端密钥没有进入前端公开变量
# ---------------------------------------------------------------------------
check_05_no_secret_in_public() {
  local ok=1 v np injected
  for v in ${SERVER_SIDE_VARS}; do
    if grep -qE "^NEXT_PUBLIC_${v}=" "${ENV_TEMPLATE}" 2>/dev/null; then
      info "服务端变量被暴露为前端公开变量：NEXT_PUBLIC_${v}"
      ok=0
    fi
  done

  np="$(grep -oE '^NEXT_PUBLIC_[A-Z0-9_]*' "${ENV_TEMPLATE}" 2>/dev/null | sort -u)"
  for v in ${np}; do
    if printf '%s\n' "${v}" | grep -qE "${SENSITIVE_TOKEN_REGEX}"; then
      info "前端公开变量名含敏感词：${v}"
      ok=0
    fi
  done

  if [ -n "${COMPOSE_CONFIG_TEXT}" ]; then
    injected="$(printf '%s\n' "${COMPOSE_CONFIG_TEXT}" | awk '
      /^services:/ { in_services = 1; next }
      in_services && /^[A-Za-z][A-Za-z0-9_-]*:/ { in_services = 0 }
      in_services && /^  [A-Za-z0-9_-]+:$/ { svc = $0; sub(/^  /, "", svc); sub(/:$/, "", svc) }
      in_services && /NEXT_PUBLIC_/ { print svc }
    ' | sort -u)"
    for v in ${injected}; do
      if [ "${v}" != 'frontend' ]; then
        info "NEXT_PUBLIC_* 被注入非 frontend 服务：${v}"
        ok=0
      fi
    done
  fi
  [ "${ok}" -eq 1 ]
}

if check_05_no_secret_in_public; then
  pass '检查 5：服务端密钥未进入前端公开变量，且仅注入 frontend'
else
  fail '检查 5：服务端密钥未进入前端公开变量，且仅注入 frontend'
fi

# ---------------------------------------------------------------------------
# 检查 6-10：只读自审
#   模式清单位于文件末尾 AUDIT-PATTERNS 区，不参与扫描，避免自匹配。
# ---------------------------------------------------------------------------
run_readonly_self_audit() {
  local body line cid ctitle cregex
  body="$(sed '/^# ===AUDIT-PATTERNS-BEGIN===$/,$d' "${SCRIPT_ABS}")"

  while IFS= read -r line; do
    [ -n "${line}" ] || continue
    case "${line}" in
      *@@*@@*) ;;
      *) continue ;;
    esac
    cid="${line%%@@*}"
    ctitle="${line#*@@}"; ctitle="${ctitle%%@@*}"
    cregex="${line##*@@}"
    if printf '%s\n' "${body}" | grep -qE "${cregex}"; then
      info "命中禁止模式：${cregex}"
      fail "检查 ${cid}：${ctitle}"
    else
      pass "检查 ${cid}：${ctitle}"
    fi
  done < <(sed -n '/^# ===AUDIT-PATTERNS-BEGIN===$/,/^# ===AUDIT-PATTERNS-END===$/p' "${SCRIPT_ABS}" | grep -vE '^#|^:|^PATTERNS$')
}

run_readonly_self_audit

# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
printf '============================================================\n'
printf '检查结果：通过 %s 项，失败 %s 项\n' "${PASSED}" "${FAILED}"
if [ "${FAILED}" -eq 0 ]; then
  printf '结论：V1.0 工程基线检查全部通过。\n'
  exit 0
fi
printf '结论：存在失败项，请按上方 [FAIL] 逐项修复后重新运行。\n'
exit 1

# ============================================================================
# 只读自审模式清单（检查 6-10 的数据源）
# ----------------------------------------------------------------------------
# 说明：本段位于 AUDIT-PATTERNS-BEGIN 之后，被自审扫描显式排除，
#      因此模式文本不会与自身匹配。heredoc 由 `:` 空命令消费，不会被执行，
#      且脚本在此之前已 exit，运行时不会走到这里。
# 格式：检查号@@检查标题@@禁止模式（扩展正则）
# ============================================================================
# ===AUDIT-PATTERNS-BEGIN===
: <<'PATTERNS'
6@@不连接数据库@@psql|pg_isready|redis-cli
7@@不执行迁移@@alembic +(upgrade|downgrade|revision|stamp)
8@@不调用 Provider@@curl|wget
9@@不写业务数据@@insert into|update |delete from
10@@不启动 Compose 服务栈@@compose +(up|down|build|exec|run|restart|stop|rm|kill|pull|push|create|start)([[:space:]]|$)
PATTERNS
# ===AUDIT-PATTERNS-END===

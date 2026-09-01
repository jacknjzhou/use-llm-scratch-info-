#!/usr/bin/env bash
# Doc Intelligence 一键提取脚本
# 用法: extract.sh [--schema SCHEMA_ID] [--out RESULT.json] [--xlsx RESULT.xlsx] [--priority N] FILE...
# 依赖: bash、curl、python3（macOS 自带）
# 环境变量: DOCINT_BASE_URL(默认 http://localhost:8008)、DOCINT_API_KEY(可选)、
#           DOCINT_TIMEOUT(默认 600 秒)、DOCINT_POLL_INTERVAL(默认 3 秒)
set -euo pipefail

BASE_URL="${DOCINT_BASE_URL:-http://localhost:8008}"
API_KEY="${DOCINT_API_KEY:-}"
TIMEOUT="${DOCINT_TIMEOUT:-600}"
INTERVAL="${DOCINT_POLL_INTERVAL:-3}"

SCHEMA_ID="" OUT_FILE="" XLSX_FILE="" PRIORITY=5
FILES=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --schema)   SCHEMA_ID="$2"; shift 2 ;;
    --out)      OUT_FILE="$2"; shift 2 ;;
    --xlsx)     XLSX_FILE="$2"; shift 2 ;;
    --priority) PRIORITY="$2"; shift 2 ;;
    -h|--help)  grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)          FILES+=("$1"); shift ;;
  esac
done

[[ ${#FILES[@]} -gt 0 ]] || { echo "错误: 至少提供一个文件" >&2; exit 1; }
for f in "${FILES[@]}"; do
  [[ -f "$f" ]] || { echo "错误: 文件不存在: $f" >&2; exit 1; }
done

# 有 API_KEY 时包装 curl，自动附加认证头（避免 bash 3.2 空数组在 set -u 下报错）
if [[ -n "$API_KEY" ]]; then
  curl() { command curl -H "X-API-Key: $API_KEY" "$@"; }
fi

# 1) 上传
echo "==> 上传 ${#FILES[@]} 个文件..." >&2
upload_args=()
for f in "${FILES[@]}"; do upload_args+=(-F "files=@$f"); done
upload_resp=$(curl -sS "${upload_args[@]}" "$BASE_URL/api/files")
FILE_IDS=$(python3 -c "
import json,sys
d=json.loads(sys.argv[1])
if isinstance(d,dict): sys.exit('上传失败: '+d.get('detail','未知错误'))
print(json.dumps([x['file_id'] for x in d]))
" "$upload_resp") || { echo "$FILE_IDS" >&2; exit 1; }

# 2) 提交任务
echo "==> 提交提取任务 (schema=${SCHEMA_ID:-auto}, priority=$PRIORITY)..." >&2
body=$(python3 -c "
import json,sys
print(json.dumps({'file_ids':json.loads(sys.argv[1]),
                  'schema_id':sys.argv[2] or None,'priority':int(sys.argv[3])}))
" "$FILE_IDS" "$SCHEMA_ID" "$PRIORITY")
submit_resp=$(curl -sS -H "Content-Type: application/json" \
  -d "$body" "$BASE_URL/api/extract")
TASK_ID=$(python3 -c "
import json,sys
d=json.loads(sys.argv[1])
if 'task_id' not in d: sys.exit('提交失败: '+json.dumps(d,ensure_ascii=False))
print(d['task_id'])
" "$submit_resp") || { echo "$TASK_ID" >&2; exit 1; }
echo "==> task_id: $TASK_ID" >&2

# 3) 轮询直到终态
elapsed=0
while :; do
  task_json=$(curl -sS "$BASE_URL/api/tasks/$TASK_ID")
  read -r STATUS PROGRESS <<<"$(python3 -c "
import json,sys
d=json.loads(sys.argv[1])
print(d.get('status',''),d.get('progress') or 0)
" "$task_json")"
  echo "    [$elapsed s] status=$STATUS progress=${PROGRESS}%" >&2
  case "$STATUS" in
    succeeded|failed|partially_succeeded|cancelled) break ;;
  esac
  if (( elapsed >= TIMEOUT )); then
    echo "错误: 等待超时（${TIMEOUT}s），任务仍在 $STATUS。可稍后用 task_id=$TASK_ID 查询" >&2
    exit 1
  fi
  sleep "$INTERVAL"; elapsed=$((elapsed + INTERVAL))
done

# 4) 结果处理
if [[ -n "$OUT_FILE" ]]; then
  printf '%s' "$task_json" > "$OUT_FILE"
  echo "==> 结果已保存: $OUT_FILE" >&2
else
  printf '%s\n' "$task_json"
fi

if [[ -n "$XLSX_FILE" ]]; then
  curl -sS -o "$XLSX_FILE" "$BASE_URL/api/tasks/$TASK_ID/export?format=xlsx"
  echo "==> Excel 已导出: $XLSX_FILE" >&2
fi

case "$STATUS" in
  succeeded)            exit 0 ;;
  partially_succeeded)  echo "警告: 部分文件提取失败，详见输出中各文件的 status/error" >&2; exit 3 ;;
  *)                    echo "任务终态: ${STATUS}"; python3 -c "
import json,sys
print('error:', json.loads(sys.argv[1]).get('error'))
" "$task_json" >&2; exit 2 ;;
esac

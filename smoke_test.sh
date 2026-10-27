#!/usr/bin/env bash
set -euo pipefail

unset TEMP TMP TMPDIR

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_URL=${1:?smoke base URL is required}
PYTHON="${CODEX_WEB_PYTHON:-$APP_DIR/.venv/bin/python}"
MAX_TRANSIENT_ATTEMPTS="${SMOKE_TRANSIENT_ATTEMPTS:-3}"
RETRY_DELAY="${SMOKE_RETRY_DELAY:-1}"
WORK_DIR=$(mktemp -d)
cookie_header=""
origin_header=""
conversation_id=""
LAST_CODE=""
LAST_OUTPUT=""

fail() {
    local step=$1
    local detail=$2
    printf 'smoke_test=failed step=%s detail=%s\n' "$step" "$detail" >&2
    exit 1
}

new_cookie() {
    local cookie
    if ! cookie=$(cd "$APP_DIR" && "$PYTHON" -c \
        'from shared.config import ACCOUNT; from shared.security import signer; print(signer().dumps(ACCOUNT))'); then
        fail auth_cookie signer_failed
    fi
    cookie_header="codex_web_session=$cookie"
}

cleanup() {
    if [[ -n "$conversation_id" && -n "$cookie_header" ]]; then
        local -a args=(-sS -o /dev/null --cookie "$cookie_header")
        [[ -z "$origin_header" ]] || args+=(-H "Origin: $origin_header")
        curl "${args[@]}" -X POST "$BASE_URL/conversation/$conversation_id/delete" || true
    fi
    rm -rf "$WORK_DIR"
}
trap cleanup EXIT

contains() {
    local pattern=$1
    local file=$2
    if command -v rg >/dev/null 2>&1; then
        rg -q "$pattern" "$file"
    else
        grep -q "$pattern" "$file"
    fi
}

response_detail() {
    "$PYTHON" - "$1" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
raw = path.read_bytes()
try:
    value = json.loads(raw.decode("utf-8"))
except Exception:
    print("non_json_bytes=" + str(len(raw)))
    raise SystemExit
error = value.get("error") if isinstance(value, dict) else None
if isinstance(error, dict):
    error = error.get("code") or error.get("message")
if error is not None:
    text = " ".join(str(error).split())
    print("json=" + text[:180])
elif isinstance(value, dict):
    keys = ",".join(sorted(str(key) for key in value)[:12])
    print("json_keys=" + keys)
else:
    print("json_type=" + type(value).__name__)
PY
}

is_transient_status() {
    case "$1" in
        000|401|429|500|502|503|504) return 0 ;;
        *) return 1 ;;
    esac
}

http_request() {
    local method=$1
    local path=$2
    local authenticated=$3
    local curl_rc
    LAST_OUTPUT=$(mktemp "$WORK_DIR/response.XXXXXX")
    local -a args=(-sS -o "$LAST_OUTPUT" -w '%{http_code}' -X "$method")
    if [[ -n "$origin_header" ]]; then
        args+=(-H "Origin: $origin_header")
    fi
    if [[ "$authenticated" == yes ]]; then
        args+=(--cookie "$cookie_header")
    fi
    set +e
    LAST_CODE=$(curl "${args[@]}" "$BASE_URL$path")
    curl_rc=$?
    set -e
    if [[ "$curl_rc" -ne 0 ]]; then
        LAST_CODE=000
    fi
}

request_until_status() {
    local method=$1
    local path=$2
    local expected=$3
    local step=$4
    local authenticated=${5:-yes}
    local attempt detail
    for ((attempt=1; attempt<=MAX_TRANSIENT_ATTEMPTS; attempt++)); do
        http_request "$method" "$path" "$authenticated"
        if [[ "$LAST_CODE" == "$expected" ]]; then
            return 0
        fi
        if is_transient_status "$LAST_CODE" && [[ "$attempt" -lt "$MAX_TRANSIENT_ATTEMPTS" ]]; then
            if [[ "$LAST_CODE" == 401 && "$authenticated" == yes ]]; then
                new_cookie
            fi
            sleep $((RETRY_DELAY * attempt))
            continue
        fi
        detail=$(response_detail "$LAST_OUTPUT")
        fail "$step" "status=$LAST_CODE expected=$expected attempts=$attempt $detail"
    done
}

check_page() {
    local path=$1
    local expected=$2
    request_until_status GET "$path" 200 "page:$path"
    if ! contains "$expected" "$LAST_OUTPUT"; then
        fail "page:$path" "status=200 marker_missing"
    fi
}

new_cookie
if ! origin_header=$(cd "$APP_DIR" && "$PYTHON" - "$BASE_URL" <<'PY_DEPLOYED_ORIGIN'
import sys
from shared.config import PASSKEY_ORIGIN
from shared.web_push import canonical_https_origin
try:
    value = PASSKEY_ORIGIN or (sys.argv[1] if sys.argv[1].startswith('https://') else '')
    print(canonical_https_origin(value) if value else '')
except ValueError:
    raise SystemExit('smoke_origin=invalid_configuration')
PY_DEPLOYED_ORIGIN
); then
    fail origin_configuration invalid
fi

check_page /home '今天想继续什么'
check_page /projects '新建项目'
check_page /projects/history '历史对话'
check_page /settings '设置与安全'
request_until_status GET /settings/api/config 200 settings_configuration
if ! "$PYTHON" - "$LAST_OUTPUT" 2>/dev/null <<'PY_SETTINGS'
import json, sys
value = json.load(open(sys.argv[1]))
assert value['catalog_error'] is False
assert type(value['account']['revision']) is int
assert value['effective']['model']
assert value['effective']['permission'] in {'read-only', 'workspace-write', 'danger-full-access'}
assert any(row.get('available') for row in value['models'])
PY_SETTINGS
then
    fail settings_configuration invalid_or_unavailable
fi
# Startup inherits this trusted deployment origin. Exercise the same Host-port
# loss as the gateway, but deliberately fail schema validation before any write.
if [[ -n "${CODEX_WEB_PASSKEY_ORIGIN:-}" ]]; then
    if ! CODEX_WEB_ORIGIN_PROBE_BASE="$BASE_URL" "$PYTHON" - <<'PY_ORIGIN'
import json, os
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler
from shared.config import ACCOUNT, PASSKEY_ORIGIN
from shared.security import signer

base = os.environ['CODEX_WEB_ORIGIN_PROBE_BASE']
if urlsplit(base).hostname not in {'127.0.0.1', 'localhost', '::1'}:
    raise SystemExit('settings_origin_probe=failed reason=non_loopback_target')
opener = build_opener(ProxyHandler({}))
for origin, status, expected in [(PASSKEY_ORIGIN, 400, '设置请求包含不支持的字段。'),
                                 ('https://cross-site.invalid', 403, '请求来源不匹配，请从工作台页面重试。')]:
    request = Request(base.rstrip('/') + '/settings/api/config', method='POST',
                      data=b'{"__origin_probe":true}', headers={
                          'Host': urlsplit(PASSKEY_ORIGIN).hostname,
                          'X-Forwarded-Proto': 'https', 'Origin': origin,
                          'Content-Type': 'application/json',
                          'Cookie': 'codex_web_session=' + signer().dumps(ACCOUNT)})
    try:
        response = opener.open(request, timeout=10)
    except HTTPError as error:
        response = error
    with response:
        if response.status != status or json.loads(response.read()).get('error') != expected:
            raise SystemExit('settings_origin_probe=failed reason=unexpected_response')
print('settings_origin_probe=passed writes=none')
PY_ORIGIN
    then
        fail settings_origin validation_failed
    fi
fi
check_page /inbox '收件箱整理'
check_page /manifest.webmanifest 'standalone'
check_page /login 'fingerprint-action'

for path in manifest.webmanifest sw.js static/icon-192.png static/icon-maskable-512.png static/conversation.js static/auth-recovery.js static/reading-preferences.js static/settings-center.js static/css/settings-center.css static/skill-picker.js static/css/skill-picker.css; do
    request_until_status GET "/$path" 200 "asset:/$path" no
done

for path in /upload /operations /operations/admin-test; do
    for method in GET POST; do
        request_until_status "$method" "$path" 410 "retired:$method:$path" no
        contains 'legacy_employee_portal_retired' "$LAST_OUTPUT" || fail "retired:$path" marker_missing
    done
done

request_until_status POST /conversation/draft 201 conversation_draft
if ! conversation_id=$("$PYTHON" -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["conversation"]["id"])' \
    "$LAST_OUTPUT" 2>/dev/null); then
    fail conversation_draft 'status=201 invalid_json'
fi
if [[ ! "$conversation_id" =~ ^c[0-9a-f]{32}$ ]]; then
    fail conversation_draft 'status=201 invalid_conversation_id'
fi

check_page "/conversation/$conversation_id" 'native-conversation'
if ! catalog_detail=$("$PYTHON" - "$LAST_OUTPUT" <<'PY'
from html.parser import HTMLParser
import pathlib
import sys


class CatalogParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_model = False
        self.models = 0
        self.warning = False

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        classes = values.get("class", "").split()
        if "native-settings-warning" in classes:
            self.warning = True
        if tag == "select" and values.get("name") == "model":
            self.in_model = True
        elif tag == "option" and self.in_model:
            self.models += 1

    def handle_endtag(self, tag):
        if tag == "select":
            self.in_model = False


parser = CatalogParser()
parser.feed(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
if parser.warning or parser.models < 1:
    print(f"warning={int(parser.warning)} model_options={parser.models}")
    raise SystemExit(1)
print(f"model_options={parser.models}")
PY
); then
    fail conversation_catalog "$catalog_detail"
fi
check_page "/conversation/$conversation_id/snapshot?text_previews=true" 'schema_version'

request_until_status POST "/conversation/$conversation_id/delete" 200 conversation_delete
conversation_id=""

for path in /task/new-form /takeover /bridge/mcp/retired; do
    request_until_status GET "$path" 404 "retired:$path"
done

printf 'smoke_test=passed\n'

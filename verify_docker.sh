#!/usr/bin/env bash
# MiniVectorDB 提测前的容器层复验脚本。
#
# 用法（在 WSL2 里执行，仓库根目录下）：
#   bash tools/mvdb_docker_check.sh [镜像名:标签]
#
# 做四件事：
#   1. 从当前仓库源码构建镜像（不使用任何本地预编译文件）；
#   2. 用官方 protocol_check/run.py 跑握手检查与全部公开用例；
#   3. 按 STDIO_PROTOCOL.md 第 7 节跑一次容器自测（--read-only、--network=none 等约束）；
#   4. 跑一次强杀恢复：容器被 kill 后重启，已提交数据必须还在。
#
# 官方 protocol_check/run.py 只支持 POSIX，本脚本在 WSL2 里执行正合适。

set -euo pipefail

IMAGE="${1:-minivectordb:local}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RESULTS="protocol-results/docker-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$RESULTS"

echo "==> 仓库根目录: $ROOT"
echo "==> 镜像: $IMAGE"

echo "==> [1/4] 构建镜像"
docker build -t "$IMAGE" .

echo "==> [2/4] 官方握手检查"
python3 protocol_check/run.py --image "$IMAGE" --handshake-only --output "$RESULTS/handshake"

echo "==> [2/4] 官方公开用例"
python3 protocol_check/run.py --image "$IMAGE" --output "$RESULTS/public"

echo "==> [3/4] 容器约束自测（STDIO_PROTOCOL.md 第 7 节）"
test_root="$(mktemp -d)"
container_name="vectordb-protocol-check-$(basename "$test_root")"
mkdir "$test_root/data"
chmod 777 "$test_root/data"
trap 'docker rm -f "$container_name" >/dev/null 2>&1 || true' EXIT
cat > "$test_root/requests.jsonl" <<'JSONL'
{"id":1,"op":"execute","sql":"CREATE TABLE docs (id BIGINT PRIMARY KEY, content VARCHAR(16));","search_mode":"exact"}
{"id":2,"op":"execute","sql":"INSERT INTO docs VALUES (1, '你好');","search_mode":"exact"}
{"id":3,"op":"execute","sql":"SELECT id, content FROM docs ORDER BY id;","search_mode":"exact"}
{"id":4,"op":"shutdown"}
JSONL
timeout 180s docker run --rm -i --name "$container_name" \
  --network=none --cpus=2 --memory=2g --memory-swap=2g --pids-limit=128 \
  --read-only --cap-drop=ALL --security-opt=no-new-privileges \
  --mount "type=bind,src=$test_root/data,dst=/data" \
  "$IMAGE" \
  < "$test_root/requests.jsonl" \
  > "$test_root/stdout.jsonl" 2> "$test_root/stderr.log"
python3 - "$test_root/stdout.jsonl" <<'PY'
import json
import sys
from pathlib import Path

raw = Path(sys.argv[1]).read_bytes()
assert raw.endswith(b'\n'), '最后一条响应必须以换行结束'
lines = raw.decode('utf-8').splitlines()
assert len(lines) == 5, f'预期5行输出，实际{len(lines)}行：{lines[:6]!r}'
messages = [json.loads(line) for line in lines]
assert messages[0] == {'type': 'ready'}, messages[0]
for ident, response in enumerate(messages[1:], 1):
    assert type(response.get('id')) is int and response['id'] == ident, response
    assert response.get('ok') is True, response
assert type(messages[2].get('affected_rows')) is int, messages[2]
assert messages[2]['affected_rows'] == 1, messages[2]
assert messages[3].get('columns') == ['id', 'content'], messages[3]
assert messages[3].get('rows') == [[1, '你好']], messages[3]
print('基础协议样例通过')
PY

echo "==> [4/4] 强杀恢复（容器被 kill 后已提交数据必须保留）"
kill_root="$(mktemp -d)"
mkdir "$kill_root/data"
chmod 777 "$kill_root/data"
kill_name="vectordb-kill-check-$(basename "$kill_root")"
trap 'docker rm -f "$kill_name" >/dev/null 2>&1 || true' EXIT

printf '%s\n' \
  '{"id":1,"op":"execute","sql":"CREATE TABLE t (id BIGINT PRIMARY KEY, v BIGINT);","search_mode":"exact"}' \
  '{"id":2,"op":"execute","sql":"INSERT INTO t VALUES (1, 42);","search_mode":"exact"}' \
  > "$kill_root/warm.jsonl"
timeout 120s docker run --rm -i --name "$kill_name" \
  --network=none --cpus=2 --memory=2g --memory-swap=2g --pids-limit=128 \
  --read-only --cap-drop=ALL --security-opt=no-new-privileges \
  --mount "type=bind,src=$kill_root/data,dst=/data" \
  "$IMAGE" < "$kill_root/warm.jsonl" > "$kill_root/warm.out" 2> "$kill_root/warm.err" || true

printf '%s\n' \
  '{"id":1,"op":"execute","sql":"SELECT id, v FROM t ORDER BY id;","search_mode":"exact"}' \
  '{"id":2,"op":"shutdown"}' \
  > "$kill_root/read.jsonl"
timeout 120s docker run --rm -i --name "$kill_name" \
  --network=none --cpus=2 --memory=2g --memory-swap=2g --pids-limit=128 \
  --read-only --cap-drop=ALL --security-opt=no-new-privileges \
  --mount "type=bind,src=$kill_root/data,dst=/data" \
  "$IMAGE" < "$kill_root/read.jsonl" > "$kill_root/read.out" 2> "$kill_root/read.err"
python3 - "$kill_root/read.out" <<'PY'
import json
import sys
from pathlib import Path

lines = Path(sys.argv[1]).read_text(encoding='utf-8').splitlines()
messages = [json.loads(line) for line in lines]
assert messages[0] == {'type': 'ready'}, messages[0]
rows = messages[1].get('rows')
assert rows == [[1, 42]], f'重启后已提交数据丢失：{rows!r}'
print('重启后已提交数据仍在')
PY

echo
echo "==> 全部通过。产物目录：$RESULTS"
echo "    强杀恢复产物：$kill_root"

#!/usr/bin/env bash
# Service 연결 장애: api Service의 selector를 잘못된 레이블로 바꾼다.
source "$(dirname "$0")/../_lib.sh"
k -n demo patch svc api -p '{"spec":{"selector":{"app":"api-v2"}}}'

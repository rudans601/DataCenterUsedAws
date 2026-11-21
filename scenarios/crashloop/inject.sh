#!/usr/bin/env bash
# Pod 반복 재시작: 존재하지 않는 플래그(오타)가 들어간 새 버전을 배포한다.
source "$(dirname "$0")/../_lib.sh"
k -n demo patch deploy api --type=json -p '[
 {"op":"replace","path":"/spec/template/spec/containers/0/args","value":["--port=9898","--level=info","--cache-servr=tcp://redis:6379"]},
 {"op":"add","path":"/metadata/annotations/kubernetes.io~1change-cause","value":"release 1.3.0: 캐시 서버 연동 설정 추가"}]'

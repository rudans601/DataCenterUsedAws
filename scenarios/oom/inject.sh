#!/usr/bin/env bash
# 메모리 부족: 시작할 때 256MB를 메모리에 올리는 새 버전 (limit은 128Mi 그대로).
source "$(dirname "$0")/../_lib.sh"
k -n demo patch deploy api --type=json -p '[
 {"op":"replace","path":"/spec/template/spec/containers/0/args","value":["--port=9898","--level=info","--stress-memory=256"]},
 {"op":"add","path":"/metadata/annotations/kubernetes.io~1change-cause","value":"release 1.3.1: 시작 시 데이터 사전 적재 기능"}]'

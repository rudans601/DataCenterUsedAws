#!/usr/bin/env bash
# Probe 실패: readinessProbe 경로를 잘못 바꾼 새 버전을 배포한다.
source "$(dirname "$0")/../_lib.sh"
k -n demo patch deploy api --type=json -p '[
 {"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/httpGet/path","value":"/ready"},
 {"op":"add","path":"/metadata/annotations/kubernetes.io~1change-cause","value":"release 1.4.0: 헬스체크 경로 정리"}]'

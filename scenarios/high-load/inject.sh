#!/usr/bin/env bash
# 부하 생성기 두 개가 각각 지정된 초당 요청량을 목표로 실행한다. 기본 합계는 초당 4,000건이다.
# 고정 속도라서 Pod를 늘리면 Pod당 부하가 줄어 증설 효과를 확인할 수 있다.
source "$(dirname "$0")/../_lib.sh"
qps=${KUBEOPS_LAB_QPS:-2000}
case "$qps" in 600|1000|2000) ;; *) exit 2 ;; esac
k -n traffic patch deploy loadgen-api --type=json -p '[{"op":"replace","path":"/spec/template/spec/containers/0/args","value":["load","-quiet","-qps","'"$qps"'","-c","16","-t","0","http://api.demo.svc.cluster.local:9898/"]}]'
k -n traffic scale deploy loadgen-api --replicas=2

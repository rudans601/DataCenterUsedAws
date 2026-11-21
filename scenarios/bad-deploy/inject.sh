#!/usr/bin/env bash
# 배포 후 성능 저하: 모든 요청에 0.3~0.7초 지연이 생기는 새 버전을 배포한다.
# (Pod와 헬스체크는 정상이라 상태만 보면 문제가 안 보이는 배포 결함)
source "$(dirname "$0")/../_lib.sh"
case "${KUBEOPS_DELAY_PROFILE:-standard}" in
  standard) minimum=300; maximum=700 ;;
  slow) minimum=550; maximum=850 ;;
  *) echo "지원하는 지연 조건: standard, slow" >&2; exit 2 ;;
esac
k -n demo patch deploy api --type=json -p '[
 {"op":"replace","path":"/spec/template/spec/containers/0/args","value":["--port=9898","--level=info","--random-delay","--random-delay-unit=ms","--random-delay-min='"$minimum"'","--random-delay-max='"$maximum"'"]},
 {"op":"add","path":"/metadata/annotations/kubernetes.io~1change-cause","value":"release 1.5.0: 응답 포맷 개선"}]'

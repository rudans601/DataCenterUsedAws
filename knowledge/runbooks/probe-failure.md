# 런북: Probe 실패로 Ready가 되지 않음

## 증상
- Pod는 Running인데 READY가 0/1이다.
- 이벤트에 `Unhealthy: Readiness probe failed: HTTP probe failed with statuscode: 404` 같은 메시지가 반복된다.
- 롤링 업데이트가 끝나지 않고 updated 복제본 수가 desired보다 작다. 시간이 지나면 `ProgressDeadlineExceeded`가 된다.
- maxUnavailable=0이면 이전 리비전 Pod가 계속 트래픽을 받아서 서비스는 당장 유지되지만 배포는 멈춘 상태다.

## 원인 찾기
1. 이벤트 메시지의 상태 코드를 본다.
   - 404: Probe 경로가 애플리케이션에 없는 경로다 (경로 오타, 경로 변경 누락).
   - 503: 애플리케이션이 준비되지 않았다고 응답한다 (의존 서비스 대기, 초기화 지연).
   - connection refused: 포트가 다르거나 앱이 아직 포트를 열지 않았다.
2. 배포 이력에서 readinessProbe 경로·포트가 직전 리비전과 달라졌는지 비교한다.
3. liveness Probe가 실패하면 Pod가 재시작되고, readiness Probe가 실패하면 Service 엔드포인트에서 빠진다.

## 조치
- 최근 배포에서 Probe 설정이 바뀌어 새 Pod만 Ready가 안 되면 **직전 리비전으로 롤백**해서 멈춘 롤아웃을 정리한다.
- 이후 올바른 Probe 경로로 매니페스트를 고쳐 다시 배포한다 (운영자 작업).
- 초기화가 오래 걸리는 앱이면 initialDelaySeconds나 startupProbe 추가를 검토한다.

## 검증
- 모든 Pod가 Ready이고 Deployment의 updated, available 수가 desired와 같은지 확인한다.

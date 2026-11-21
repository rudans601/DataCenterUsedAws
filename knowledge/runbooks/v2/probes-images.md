# Readiness 검사와 이미지 다운로드 실패
버전 2.0 · Unhealthy / ErrImagePull / ImagePullBackOff
이벤트: Unhealthy, ErrImagePull, ImagePullBackOff

## 원인 구별
readiness HTTP 404가 반복되면 컨테이너가 실제 제공하는 경로와 이전 정상 리비전의 경로를 비교한다. 기동 중 잠깐의 connection refused 후 Ready가 됐다면 다음 표본을 본다. liveness 실패는 재시작으로 이어질 수 있다. readiness 실패는 해당 Pod가 서비스 트래픽에서 빠지는 신호다.
이미지 다운로드 실패는 이벤트의 태그 없음, 인증 거절, 레지스트리 연결 실패를 구분한다. 여러 리비전과 노드가 동시에 실패하는지 확인한다.

## 복구와 인계
경로만 바꾼 새 버전이 실패하고 직전 버전이 정상 요청을 처리하면 격리·롤백 절차의 자동 조건을 대조한다.
[approval.rollback] 새 이미지 태그를 잘못 배포했고 직전 정상 이미지가 확인되면 운영자 승인 후 직전 리비전으로 롤백한다.

인증·네트워크 문제이면 레지스트리 담당자에게 인계한다. Probe 비활성화, Secret 수정, 검사 실패를 숨기기 위한 재시작은 수행하지 않는다.

## 검증
이미지 다운로드와 컨테이너 시작, 새 Pod의 Ready와 엔드포인트 등록을 확인한다. 기존 정상 Pod가 있었다면 서비스 중단 시간과 배포 실패 시간을 따로 기록한다.

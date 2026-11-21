# 런북: Service 연결 장애 (엔드포인트 없음)

## 증상
- 다른 서비스에서 호출하면 `connection refused`, `no healthy upstream`, 타임아웃이 난다.
- Pod는 모두 Running·Ready로 정상인데 해당 서비스로 가는 요청량이 0으로 떨어진다.
- 호출하는 쪽(프론트엔드)의 5xx 오류율이 오른다.

## 원인 찾기
1. Service의 Ready 엔드포인트 수를 확인한다 (`kubectl get endpointslices -l kubernetes.io/service-name=<svc>`).
2. 엔드포인트가 0개면 Service의 selector와 Pod 레이블을 비교한다. 레이블 오타나 버전 레이블 변경(`app: api-v2` 등)이 흔한 원인이다.
3. 엔드포인트는 있는데 연결이 안 되면 Service의 port·targetPort가 컨테이너 포트와 맞는지 확인한다.
4. NetworkPolicy가 트래픽을 막고 있는지도 확인한다.

## 조치
- Service selector나 포트를 올바르게 고친다. Service 수정은 KubeOps Copilot의 자동 실행 범위 밖이므로 운영자가 매니페스트를 수정해 적용한다.
- Deployment 재시작, 롤백, 복제본 증설은 Service 설정 문제를 해결하지 못한다. Pod 쪽은 정상이기 때문이다.

## 검증
- Ready 엔드포인트 수가 Ready Pod 수와 같아지고, 호출하는 쪽의 오류율이 정상으로 돌아오는지 확인한다.

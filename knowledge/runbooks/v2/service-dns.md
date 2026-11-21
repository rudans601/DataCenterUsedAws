# Service 연결 단절과 클라이언트 DNS
버전 2.0 · EndpointSlice / selector / DNS lookup

## 비교할 경로
정상 서비스 이름, 같은 클라이언트의 ClusterIP 직접 호출, Ready Pod와 EndpointSlice를 대조한다. 실제 selector와 Pod 레이블, Service port와 targetPort를 확인한다. 다른 웹 화면의 HTTP 200만으로 API 성공을 판단할 수 없다.

## 조치
Pod가 Ready인데 selector와 일치하는 Pod가 0이고 정상 서비스 주소의 요청도 실패하면 네트워크 담당자에게 Service 설정 수정을 인계한다.
한 클라이언트만 no such host를 내고 정상 서비스 주소와 IP로는 응답하면 그 클라이언트의 주소, 네임스페이스, DNS 검색 경로를 점검하도록 전달한다. 여러 클라이언트에 같은 현상이 있으면 DNS 담당자에게 범위를 알린다. Deployment 재시작과 증설은 이 경로를 복구하지 못한다.

## 검증
담당자 수정 뒤 올바른 selector의 Ready 엔드포인트 수와 원래 실패한 클라이언트·대표 클라이언트 양쪽 요청을 다시 확인한다.

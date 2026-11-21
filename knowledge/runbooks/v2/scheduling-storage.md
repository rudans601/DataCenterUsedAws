# Pending Pod와 볼륨 대기
버전 2.0 · FailedScheduling / Insufficient cpu / PersistentVolumeClaim
이벤트: FailedScheduling, FailedBinding, FailedMount, FailedAttachVolume, ProvisioningFailed

## 사건 구분
Pending인 Pod의 이벤트에서 CPU 요청을 만족할 노드가 없는지, 바인딩되지 않은 PVC를 기다리는지 구분한다. CPU의 순간 사용률과 예약 가능한 CPU는 다르다. 워커의 Ready와 cordon 상태, Pod의 요청량, 배치 조건을 확인한다. PVC라면 연결된 claim, 상태, StorageClass와 프로비저닝 이벤트를 함께 본다.

## 조치
CPU 요청과 노드 용량이 맞지 않으면 필요한 CPU·메모리와 대기 중인 Pod 수를 인프라 담당자에게 인계한다. Pending PVC는 데이터 보존·바인딩 방식·가용 영역을 확인할 스토리지 담당자에게 인계한다.
같은 요청으로 복제본을 추가해도 배치 문제는 계속된다. PVC 삭제·재생성, 노드 변경, requests/limits 변경은 사람이 결정한다.

## 검증
담당자 변경 뒤 PVC가 Bound이고 해당 Pod가 Ready인지 확인한다. 사용자가 접근하는 서비스의 엔드포인트와 오류율도 따로 확인한다.

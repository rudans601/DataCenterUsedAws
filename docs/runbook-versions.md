# 운영 런북 버전 기록

## v2.0 · 2026-11-20

기존의 일반적인 증상 설명에 더해 맥북 kind의 demo/api에서 확인할 측정값, 사람이 맡을 변경, 제한 자동 실행의 전제와 복구 확인 기준을 문서화했다. RAG는 현행 문서 원문을 검색해 진단에 사용한다. 실행 보고서에는 프롬프트 버전, 사용한 문서 경로·버전·인용문을 남긴다.

| 운영 질문 | 현행 문서 | 추가한 내용 |
|---|---|---|
| 정상 요청인데 경고가 있거나 워커를 점검한다면? | [정상·경고·점검](../knowledge/runbooks/v2/normal-maintenance.md) | 일시 경고, Pod 이동과 실제 사용자 영향 구별 |
| 메모리 부족과 일반 반복 재시작은 어떻게 확인하나? | [컨테이너 종료](../knowledge/runbooks/v2/container-exits.md) | 종료 사유·이전 로그·리비전 비교와 재시작 조건 |
| 새 Pod가 검사를 통과하지 못하거나 이미지를 내려받지 못한다면? | [Probe와 이미지](../knowledge/runbooks/v2/probes-images.md) | 기동 중 일시 경고·지속 404·인증 실패 구별 |
| 기존 버전은 정상인데 새 버전이 실패했다면? | [실패 리비전 복구](../knowledge/runbooks/v2/revision-recovery.md) | 제한 자동 롤백의 적용 범위·중단·검증 |
| Ready인데 배포 직후 느려졌다면? | [배포 후 지연](../knowledge/runbooks/v2/release-latency.md) | 부하와의 구분, 트래픽을 받는 버전의 승인 롤백 |
| 트래픽이 몰리면 언제 늘릴까? | [CPU 포화와 증설](../knowledge/runbooks/v2/high-load-scaling.md) | 두 구간 지연, 용량, 정확히 +1, 같은 부하에서 효과 검증 |
| 서비스 연결 또는 일부 클라이언트의 DNS가 끊긴다면? | [서비스와 DNS](../knowledge/runbooks/v2/service-dns.md) | 정상 주소와 IP 대조, selector와 클라이언트별 인계 |
| CPU 부족·볼륨 대기로 배치할 수 없다면? | [배치와 스토리지](../knowledge/runbooks/v2/scheduling-storage.md) | 예약 용량·PVC 상태와 담당자 확인 사항 |
| 지표가 없는데 서비스가 응답한다면? | [수집 공백](../knowledge/runbooks/v2/observability.md) | 0과 미수집 구분, 직접 호출, 반복 공백 인계 |
| 누가 변경할 수 있으며 무엇을 확인해야 할까? | [공통 운영 정책](../knowledge/runbooks/v2/operations-policy.md) | 영향 등급, 네 가지 경로, 불확실성·권한·변경 후 검증 |

### 앱 복구와 운영자 인계 보강

공통 운영 정책에 실제 echo 재시도, 지속 실패 시 인계, 유료 증설 검토, 담당자 접수와 종료 기준을 추가했다. 이 변경은 `rag-decision-v2.1` 프롬프트와 문서 내용 해시로 이전 평가와 구분한다.

## v1 · 보관본

`knowledge/runbooks/` 바로 아래의 기존 7개 파일은 이전 버전이다. 현재 색인 범위는 `knowledge/runbooks/v2/`와 검토된 `knowledge/incidents/` 사례다. 검증 성공 사례를 기록하는 옵션이 켜져 있으면 이후 검토용 초안은 `knowledge/candidates/`에 쌓인다. 기존 자동 생성 초안도 검색에서 제외한다.

## 다음 버전 반영

반복된 실패의 실행 ID와 관측 원문을 기록하고, 담당자가 재현 조건·적용 예외·검증 결과를 검토한다. 변경 문서와 정책 테스트를 함께 수정하고 활성 버전을 올린다. 같은 시드를 다시 평가할 때는 이전 결과를 보존한다.

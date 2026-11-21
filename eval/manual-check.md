# 수동 점검 기록표 (Copilot과 비교용)

같은 시나리오를 팀원이 `kubectl`과 Prometheus만으로 진단한 시간과 결과를 적는다.
Copilot 결과는 `eval/results/*.md`에서 가져온다.

## 진행 방법
1. `./scenarios/reset.sh` → `./scenarios/<이름>/inject.sh` → `scenario.yaml`의 `wait_seconds`만큼 기다린다.
2. 점검자는 시나리오 이름을 모르는 상태에서 질문만 받고 진단을 시작한다. 타이머를 켠다.
3. 원인을 말하고 조치를 정하면 타이머를 멈춘다.

## 기록

| 날짜 | 점검자 | 시나리오 | 진단한 원인 | 정한 조치 | 걸린 시간(초) | 정답 여부 | 사용한 명령 수 |
|---|---|---|---|---|---|---|---|
|  |  | crashloop |  |  |  |  |  |
|  |  | oom |  |  |  |  |  |
|  |  | probe-fail |  |  |  |  |  |
|  |  | service-broken |  |  |  |  |  |
|  |  | high-load |  |  |  |  |  |
|  |  | bad-deploy |  |  |  |  |  |

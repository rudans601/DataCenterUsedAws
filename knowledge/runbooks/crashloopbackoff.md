# 런북: Pod 반복 재시작 (CrashLoopBackOff)

## 증상
- Pod 상태가 `CrashLoopBackOff`이고 restarts 값이 계속 늘어난다.
- 이벤트에 `BackOff: Back-off restarting failed container`가 반복된다.
- `lastState.terminated.exitCode`가 0이 아닌 값(예: 1, 2)이다. 137이면 OOM 런북을 본다.

## 원인 찾기
1. 재시작 전 컨테이너 로그(`kubectl logs <pod> --previous`)에서 시작 직후 출력된 에러를 확인한다.
   - `unknown flag`, `invalid argument`, `config` 관련 에러 → 실행 인자·설정 오류
   - `connection refused`, `no such host` → 의존 서비스 연결 실패
   - `panic`, `segmentation fault` → 애플리케이션 버그
2. 배포 이력에서 문제 Pod의 리비전과 직전 리비전의 차이(이미지, 실행 인자, 환경 변수)를 비교한다.
3. 이전 리비전 Pod는 정상인데 새 리비전 Pod만 재시작하면 최근 배포가 원인일 가능성이 높다.

## 조치
- 최근 배포 직후 새 리비전에서만 발생: **직전 리비전으로 롤백**한다. 롤백은 maxUnavailable=0 설정이면 서비스 중단 없이 진행된다.
- 모든 리비전에서 발생하고 외부 의존성 장애가 원인: 의존 서비스를 먼저 복구한다. 재시작은 효과가 없다.
- 일시적인 초기화 실패(의존 서비스가 잠깐 늦게 뜬 경우)로 보이면 **재시작**을 시도할 수 있다.

## 검증
- 롤아웃 완료 후 모든 Pod가 Ready이고 restarts가 더 늘지 않는지 확인한다.
- 오류율과 응답 시간이 장애 이전 수준으로 돌아왔는지 확인한다.

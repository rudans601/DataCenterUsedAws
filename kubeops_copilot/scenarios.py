"""로컬 실습 유형과 재현 가능한 생성 조건."""
import random

# 생성기는 주입할 신호만 정의한다.
CATALOG = {
    "normal": ("정상 운영", "INFO api Ready=2/2"),
    "transient-warning": ("일시적 타임아웃", "WARN api upstream timeout"),
    "bad-deploy": ("배포 후 응답 지연", "WARN api latency after rollout"),
    "service-broken": ("Service 연결 단절", "ERROR api endpoints=0 connection refused"),
    "crashloop": ("Pod 반복 재시작", "ERROR api unknown flag; container restarting"),
    "oom": ("컨테이너 메모리 부족", "ERROR api last termination OOMKilled"),
    "probe-fail": ("Readiness 검사 실패", "WARN api readiness probe returned 404"),
    "high-load": ("트래픽 급증", "WARN traffic offered load increased; CPU throttling"),
    "pending": ("CPU 부족으로 배치 실패", "WARN scheduler FailedScheduling: Insufficient cpu"),
    "image-pull": ("이미지 다운로드 실패", "WARN kubelet ErrImagePull; image unavailable"),
    "dns": ("클라이언트 DNS 조회 실패", "ERROR client DNS lookup: no such host"),
    "maintenance": ("워커 점검과 API Pod 이동", "INFO scheduler worker cordoned; replacement scheduled elsewhere"),
    "metrics-gap": ("API 요청 지표 누락", "WARN monitoring API request series missing; direct probe OK"),
    "pvc": ("볼륨 할당 대기", "WARN scheduler pod has unbound PersistentVolumeClaims"),
}
NAMES = {name: data[0] for name, data in CATALOG.items()}
DELAYS = {"standard": (300, 700), "slow": (550, 850)}


def plan(seed):
    rng = random.Random(seed)
    names = list(NAMES)
    rng.shuffle(names)
    result = []
    for name in names:
        case = {"case": name}
        if name == "bad-deploy":
            case["delay_profile"] = rng.choice(list(DELAYS))
        elif name == "transient-warning":
            case["timeout_ms"] = rng.choice([500, 750, 1000])
        elif name == "high-load":
            case["qps"] = rng.choice([600, 1000, 2000])
        conditions = random.Random(f"{seed}/conditions/{name}")
        if name == "pending":
            case["requested_cpu"] = conditions.choice([256, 512, 999])
        elif name == "pvc":
            case["volume_mib"] = conditions.choice([1, 8, 16])
        elif name == "dns":
            case["bad_namespace"] = conditions.choice([True, False])
        elif name == "maintenance":
            case["pod_index"] = conditions.randrange(2)
        result.append(case)
    return result


def single_plan(seed):
    """시드에 따라 단일 유형별 재현 조건과 실행 순서를 결정한다."""
    cases = plan(seed)
    rollout_faults = {"crashloop", "oom", "probe-fail", "pending", "image-pull", "pvc"}
    for case in cases:
        name = case["case"]
        rng = random.Random(f"single-v1/{seed}/{name}")
        background = rng.choice(["steady", "healthy_rollout"])
        pattern = "baseline"
        if name == "high-load":
            pattern = rng.choice(["burst", "step", "plateau"])
        if pattern == "burst":
            seconds = 10
        elif name == "high-load":
            seconds = rng.choice([60, 180])
        else:
            seconds = rng.choice([10, 60, 180])
        repeat = rng.choice([1, 3]) if name in {"high-load", "transient-warning"} else 1
        scope = "api"
        if name in rollout_faults:
            scope = rng.choice(["new_revision", "api"])
        elif name == "dns":
            scope = rng.choice(["one_client", "two_clients"])
        elif name == "maintenance":
            scope = "one_worker"
        # 복원 환경을 설정한다. 에이전트의 담당자·조치에는 정답을 지정하지 않는다.
        recovery = "hold"
        if name == "high-load":
            recovery = "load_ends" if pattern == "burst" else "hold_load"
        elif name in {"normal", "transient-warning", "maintenance"}:
            recovery = "observe"
        case["conditions"] = {
            "pattern": pattern, "baseline_qps": rng.choice([15, 20, 25]),
            "background": background, "duration_s": seconds,
            "visibility": rng.choice(["all", "no_app_logs", "no_request_metrics"]),
            "scope": scope, "onset_s": 30 if background == "healthy_rollout" else rng.choice([10, 60]),
            "repeat": repeat, "gap_s": rng.choice([10, 30]) if repeat > 1 else 0,
            "replicas": rng.choice([2, 3] if name == "maintenance" else [1, 2, 3]),
            "recovery": recovery,
        }
    return cases


PATTERNS = {"baseline": "평상시", "burst": "짧은 급증", "step": "계단식 증가", "plateau": "높은 부하 유지"}
VISIBILITY = {"all": "전체 제공", "no_app_logs": "앱 로그 제외", "no_request_metrics": "요청 지표 제외"}
SCOPES = {"api": "시험 API 전체", "new_revision": "새 리비전", "one_worker": "워커 한 대",
          "one_client": "클라이언트 한 개", "two_clients": "클라이언트 두 개"}
RECOVERY = {"hold": "판단·검증 후 실험 정리", "hold_load": "같은 요청량으로 조치 검증",
            "agent": "에이전트 실행", "fixture": "실습 도구 복원", "load_ends": "부하 종료", "approved_rollback": "승인 후 롤백",
            "app": "앱 재시도로 요청 복구", "operator": "운영자 설정 복원", "observe": "변경 없이 측정 종료"}


def condition_rows(case):
    c = case["conditions"]
    load = f"평상시 {c['baseline_qps']} req/s"
    if case["case"] == "high-load":
        load += f" → {PATTERNS[c['pattern']]} (추가 목표 {case['qps']} req/s)"
    return {
        "요청량": load, "배포 배경": "정상 배포 완료" if c["background"] == "healthy_rollout" else "정상 운영",
        "회차당 지속 시간": f"{c['duration_s']}초", "관측 가능한 정보": VISIBILITY[c["visibility"]],
        "적용 범위": SCOPES[c["scope"]], "발생 시점": f"정상 준비 후 {c['onset_s']}초",
        "반복": f"{c['repeat']}회" + (f" · 회차 사이 {c['gap_s']}초" if c["gap_s"] else ""),
        "시작 복제본": str(c["replicas"]), "실험 유지·종료 조건": RECOVERY[c["recovery"]],
    }


def single_timeline(case):
    c = case["conditions"]
    start = c["onset_s"]
    lines = [f"+0초  정상 준비 · 배경 요청 {c['baseline_qps']} req/s · 복제본 {c['replicas']}개"]
    for i in range(c["repeat"]):
        detail = CATALOG[case["case"]][-1]
        if case["case"] == "high-load":
            rate = case["qps"] // 2 if c["pattern"] == "step" else case["qps"]
            detail = f"추가 부하 {rate} req/s 시작"
        elif case["case"] == "transient-warning":
            detail = f"UpstreamTimeout {case['timeout_ms']}ms 경고 한 건 · 진단 모드 첫 echo 요청 503"
        lines.append(f"+{start}초  {i+1}회차 · {detail}")
        if c["pattern"] == "step":
            lines.append(f"+{start + c['duration_s']//2}초  목표 {case['qps']} req/s로 증가")
        start += c["duration_s"]
        lines.append(f"+{start}초  회차 관측 종료")
        start += c["gap_s"]
    return lines

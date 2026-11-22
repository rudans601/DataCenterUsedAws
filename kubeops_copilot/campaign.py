"""목표 등급별 주입 계획. 실제 등급은 실행 후 독립 관측으로 채점한다."""
from collections import Counter
import random

from .scenarios import single_plan
from . import control_replay

DEFAULT_MIX = {"healthy": 35, "warning": 45, "degraded": 15, "critical": 5}
ROLLOUT_CASES = ("crashloop", "oom", "pending", "image-pull", "probe-fail", "pvc")
CANDIDATES = {
    "healthy": ("normal", "maintenance"),
    "warning": ("transient-warning", "dns", "metrics-gap", *ROLLOUT_CASES),
    "degraded": ("bad-deploy", "high-load"),
    "critical": ("service-broken", *ROLLOUT_CASES),
}


def validate_mix(mix, include_replay):
    if (set(mix) != set(DEFAULT_MIX) or any(type(v) is not int or v < 0 for v in mix.values())
            or sum(mix.values()) != 100):
        raise ValueError("네 등급의 비율은 0 이상의 정수이며 합계가 100이어야 합니다.")
    if include_replay and (mix["healthy"] < 2 or mix["degraded"] < 2 or mix["warning"] < 7
                           or mix["critical"] < 1 or mix["warning"] + mix["critical"] < 14):
        raise ValueError("EKS 18종을 포함하려면 정상 2회, 주의 7회, 장애 2회, 중대 1회 이상이며 주의와 중대의 합이 14회 이상이어야 합니다.")


def draw_case(rng, name, tier, trial, used, include_replay, preferred_seed=None):
    while True:
        trial_seed = preferred_seed if preferred_seed is not None else rng.randrange(2**31)
        preferred_seed = None
        if trial_seed in used:
            continue
        case = next(c for c in single_plan(trial_seed, include_replay) if c["case"] == name)
        conditions = case["conditions"]
        if name in ROLLOUT_CASES and conditions["scope"] != ("api" if tier == "critical" else "new_revision"):
            continue
        if name == "high-load" and conditions["pattern"] == "burst":
            continue
        used.add(trial_seed)
        return {**case, "trial_seed": trial_seed, "target_tier": tier, "trial": trial}


def campaign_plan(seed, mix=None, include_replay=False, completed=()):
    mix = DEFAULT_MIX if mix is None else mix
    validate_mix(mix, include_replay)
    rng = random.Random(seed)
    candidates = dict(CANDIDATES)
    if include_replay:
        candidates["warning"] += tuple(control_replay.NAMES)
    tiers = [tier for tier in DEFAULT_MIX for _ in range(mix[tier])]
    rng.shuffle(tiers)
    cases, used = [], set()
    for i, tier in enumerate(tiers):
        name = rng.choice(candidates[tier])
        cases.append(draw_case(rng, name, tier, i + 1, used, include_replay))
    # 구버전 보고서의 완료 회차는 동일한 원래 계획과 대조한 뒤 고정한다.
    for case, row in zip(cases, completed):
        if any(row.get(k) != v for k, v in case.items()):
            raise ValueError("저장된 완료 회차가 기존 시드의 계획과 다릅니다.")
    if not include_replay:
        return cases

    names = [case["case"] for case in cases]
    counts = Counter(names)

    def place(name, visited):
        # 같은 등급의 중복을 먼저 교체한다. 유일한 유형을 밀어내면 다른 호환 회차에 옮긴다.
        slots = [i for i in range(len(completed), len(cases)) if i not in visited and name in candidates[tiers[i]]]
        for i in sorted(slots, key=lambda i: counts[names[i]] == 1):
            if i in visited:
                continue
            visited.add(i)
            old = names[i]
            if counts[old] > 1 or place(old, visited):
                names[i] = name
                counts[name] += 1
                counts[old] -= 1
                return True
        return False

    for name in dict.fromkeys(n for group in candidates.values() for n in group):
        if not counts[name] and not place(name, set()):
            raise ValueError(f"완료된 회차와 등급별 건수를 유지하면서 남은 계획에 {name} 유형을 넣을 수 없습니다.")
    for i, name in enumerate(names):
        case = cases[i]
        if case["case"] != name:
            used.remove(case["trial_seed"])
            cases[i] = draw_case(rng, name, tiers[i], i + 1, used, True, case["trial_seed"])
    return cases

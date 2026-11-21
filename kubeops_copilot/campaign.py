"""목표 등급별 주입 계획. 실제 등급은 실행 후 독립 관측으로 채점한다."""
import random

from .scenarios import single_plan

DEFAULT_MIX = {"healthy": 35, "warning": 45, "degraded": 15, "critical": 5}
ROLLOUT_CASES = ("crashloop", "oom", "pending", "image-pull", "probe-fail", "pvc")
CANDIDATES = {
    "healthy": ("normal", "maintenance"),
    "warning": ("transient-warning", "dns", "metrics-gap", *ROLLOUT_CASES),
    "degraded": ("bad-deploy", "high-load"),
    "critical": ("service-broken", *ROLLOUT_CASES),
}


def campaign_plan(seed, mix=None):
    mix = DEFAULT_MIX if mix is None else mix
    if (set(mix) != set(DEFAULT_MIX) or any(type(v) is not int or v < 0 for v in mix.values())
            or sum(mix.values()) != 100):
        raise ValueError("네 등급의 비율은 0 이상의 정수이며 합계가 100이어야 합니다.")
    rng = random.Random(seed)
    tiers = [tier for tier in DEFAULT_MIX for _ in range(mix[tier])]
    rng.shuffle(tiers)
    cases, used = [], set()
    for tier in tiers:
        name = rng.choice(CANDIDATES[tier])
        while True:
            trial_seed = rng.randrange(2**31)
            if trial_seed in used:
                continue
            case = next(c for c in single_plan(trial_seed) if c["case"] == name)
            conditions = case["conditions"]
            if name in ROLLOUT_CASES and conditions["scope"] != ("api" if tier == "critical" else "new_revision"):
                continue
            if name == "high-load" and conditions["pattern"] == "burst":
                continue
            break
        used.add(trial_seed)
        cases.append({**case, "trial_seed": trial_seed, "target_tier": tier, "trial": len(cases) + 1})
    return cases

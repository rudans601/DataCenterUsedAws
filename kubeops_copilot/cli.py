"""터미널에서 KubeOps Copilot 사용하기.

  uv run kubeops check                       # 연결 상태 점검
  uv run kubeops ask "api 상태 어때?"          # 질문 (조치 제안 시 승인 여부를 물어봄)
  uv run kubeops ask "..." --approve 지민구    # 승인까지 자동으로 (평가·시연용)
  uv run kubeops index                       # 운영 문서 다시 색인
  uv run kubeops cases                       # 조치 이력 보기
"""
from __future__ import annotations

import argparse
import sys

from . import audit, k8s_tools, metrics, rag
from .config import settings
from .graph import ask, decide
from .llm import is_configured


def _print_result(res: dict) -> None:
    a = res.get("analysis") or {}
    print("\n" + "=" * 70)
    print(res.get("answer") or a.get("answer_markdown", ""))
    print("-" * 70)
    if a:
        g = a.get("grounding", {})
        print(f"분류: {a.get('root_cause_category')} | 확신도: {a.get('confidence')} | "
              f"근거 인용 {g.get('grounded')}/{g.get('findings')} | 분석 {res.get('timings', {}).get('analysis_total')}초")
    v = res.get("verification")
    if v and v.get("rows"):
        print("\n[조치 전후 비교]")
        for r in v["rows"]:
            print(f"  {r['지표']:<14} {r['조치 전']:>10} → {r['조치 후']:>10}  {r['변화']}")


def cmd_check(_: argparse.Namespace) -> int:
    ok = True
    try:
        deps = k8s_tools.list_deployments(settings.namespace)
        print(f"[OK] Kubernetes ({settings.namespace}): Deployment {[d['name'] for d in deps]}")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"[실패] Kubernetes: {e}")
    print(f"[{'OK' if metrics.is_up() else '실패'}] Prometheus: {settings.prometheus_url}")
    llm_status = "설정됨" if is_configured() else "미설정"
    print(f"[{llm_status}] {settings.llm_provider} ({settings.llm_model})")
    return 0 if ok else 1


def cmd_ask(args: argparse.Namespace) -> int:
    res = ask(args.question)
    if res.get("pending_approval"):
        req = res["approval_request"]
        p = req["proposal"]
        _print_result(res)
        print(f"\n[승인 요청] {p['action']} {p['namespace']}/{p['deployment']}"
              f"{' → replicas ' + str(p['replicas']) if p.get('replicas') else ''}")
        print(f"  이유: {req.get('reason')}\n  예상 영향: {req.get('expected_impact')}\n  위험: {req.get('risk')}")
        if args.approve:
            approved, approver = True, args.approve
        elif args.reject:
            approved, approver = False, args.reject
        else:
            approver = input("승인자 이름: ").strip() or "operator"
            approved = input("실행할까요? [y/N] ").strip().lower() == "y"
        print("\n조치 실행과 결과 검증을 진행해요..." if approved else "\n거절했어요.")
        res = decide(res["thread_id"], approved, approver)
    _print_result(res)
    return 0


def cmd_index(_: argparse.Namespace) -> int:
    store = rag.get_store(force=True)
    print(f"문서 조각 {len(store.store)}개를 색인했어요.")
    return 0


def cmd_cases(_: argparse.Namespace) -> int:
    for c in audit.list_cases(30):
        print(f"#{c['id']:<4} {c['created_at']} {c['status']:<9} {c['category'] or '-':<17} "
              f"{c['action'] or '-':<8} {c['target'] or '-':<12} {c['verdict'] or '-':<8} {c['question'][:40]}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="kubeops", description="KubeOps Copilot CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    p = sub.add_parser("ask")
    p.add_argument("question")
    p.add_argument("--approve", metavar="승인자", help="조치 제안을 이 이름으로 승인")
    p.add_argument("--reject", metavar="승인자", help="조치 제안을 이 이름으로 거절")
    p.set_defaults(fn=cmd_ask)
    sub.add_parser("index").set_defaults(fn=cmd_index)
    sub.add_parser("cases").set_defaults(fn=cmd_cases)
    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

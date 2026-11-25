"""한 회차의 고정된 EKS 실측 근거로 기존/런북 보강/이력 보강을 비교한다. 실제 조치는 하지 않는다."""
import argparse
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval.single_detection import QUESTION, save_report
from kubeops_copilot import graph
from kubeops_copilot.config import settings
from kubeops_copilot.detection import experiment_impact

HISTORY_TITLE = "장애 전 정상 HTTP의 리비전 기록과 현재 직전 템플릿 대조"
RUNBOOK = "knowledge/runbooks/v3/revision-recovery.md"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def variants(row, old_docs, new_docs):
    evidence = deepcopy(row["evidence"])
    history = [e for e in evidence if e.get("title") == HISTORY_TITLE]
    if len(history) != 1:
        raise ValueError("정상 리비전 대조 근거가 한 개 있는 재검증 보고서가 필요합니다.")
    base = [e for e in evidence if e.get("title") != HISTORY_TITLE]
    return [("A_existing", base, old_docs), ("B_runbook_only", base, new_docs),
            ("C_runbook_and_history", evidence, new_docs)]


def inputs(evidence, docs):
    return {"namespace": "demo", "question": QUESTION + " 저장된 실측 시점의 근거만으로 판단하세요.",
            "evidence": deepcopy(evidence), "docs": deepcopy(docs), "intent": {"targets": ["api"]},
            "signals": [], "snapshot": {}}


def run(source, old_commit):
    raw = source.read_bytes()
    report = json.loads(raw)
    if report.get("environment") != "eks" or len(report.get("cases", [])) != 1:
        raise ValueError("EKS 한 회차 재검증 파일을 지정하세요.")
    os.environ["KUBEOPS_EKS_LAB"] = "1"
    os.environ["KUBEOPS_LOCAL_LAB"] = "0"
    if report.get("model") != settings.llm_model:
        raise ValueError("실측 보고서와 같은 모델을 사용하세요.")
    row = report["cases"][0]
    old_docs, new_docs = [], []
    for entry in row["documents"]:
        relative = Path(entry["source"])
        if relative.is_absolute() or ".." in relative.parts or relative.parts[0] != "knowledge":
            raise ValueError("knowledge 아래의 문서만 사용합니다.")
        content = (ROOT / relative).read_text()
        old_content = (subprocess.check_output(["git", "show", f"{old_commit}:{relative.as_posix()}"],
                                              cwd=ROOT, text=True) if relative.as_posix() == RUNBOOK else content)
        base = {**entry, "kind": "runbooks" if "runbooks" in relative.parts else "incidents"}
        old_docs.append({**base, "content": old_content})
        new_docs.append({**base, "content": content})
    at = datetime.now().astimezone()
    output = ROOT / "eval/comparisons" / f"revision-trial{row['trial']}-{at:%Y%m%d-%H%M%S}.json"
    output.parent.mkdir(exist_ok=True)
    result = {"mode": "frozen-evidence-model-comparison", "started": at.isoformat(),
              "source_report": source.name, "source_sha256": hashlib.sha256(raw).hexdigest(),
              "source_trial": row["trial"], "model": settings.llm_model, "old_runbook_commit": old_commit,
              "expected": {"impact": experiment_impact(row)[0], **row["reference"]}, "variants": []}
    for name, evidence, docs in variants(row, old_docs, new_docs):
        state = inputs(evidence, docs)
        item = {"name": name, "input_sha256": digest(graph._context(state)),
                "evidence": evidence, "documents": docs, "status": "started"}
        result["variants"].append(item)
        save_report(output, result)
        print("모델 비교", name, flush=True)
        try:
            answer = graph.analyze(state)
            item.update(status="complete", analysis=answer["analysis"], timings=answer["timings"])
            a = answer["analysis"]
            print(name, a["impact"], a["route"], a["proposal"]["action"], flush=True)
        except Exception as exc:
            item.update(status="error", error=type(exc).__name__)
            raise
        finally:
            save_report(output, result)
    if source.read_bytes() != raw:
        raise RuntimeError("비교 중 원본이 변경됐습니다.")
    result["finished"] = datetime.now().astimezone().isoformat()
    save_report(output, result)
    print("REPORT", output.relative_to(ROOT), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--old-commit", required=True)
    args = parser.parse_args()
    run(args.report, args.old_commit)

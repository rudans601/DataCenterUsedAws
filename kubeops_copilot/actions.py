"""운영 조치: 허용 범위 검사(가드)와 실행.

LLM은 조치를 '제안'만 하고, 실제 실행은 이 모듈이 정해진 방식으로만 한다.
- 허용 조치: restart, rollback, scale
- 허용 네임스페이스: demo
- 복제본 수: 1~6
"""
from __future__ import annotations

import datetime as dt
from copy import deepcopy

from kubernetes.client.exceptions import ApiException

from .config import policy
from .k8s_tools import api_client, apps


def _norm(proposal: dict) -> dict:
    replicas = proposal.get("replicas")
    try:
        replicas = int(replicas) if replicas not in (None, "") else None
    except (TypeError, ValueError):
        replicas = None
    return {
        "action": (proposal.get("action") or "none").strip().lower(),
        "namespace": (proposal.get("namespace") or "").strip(),
        "deployment": (proposal.get("deployment") or "").strip(),
        "replicas": replicas,
    }


def _owned_replicasets(ns: str, name: str):
    return [
        rs for rs in apps().list_namespaced_replica_set(ns).items
        if any(o.kind == "Deployment" and o.name == name for o in (rs.metadata.owner_references or []))
    ]


def _rev(obj) -> int:
    return int((obj.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "0") or 0)


def target_version(dep):
    return {"uid": dep.metadata.uid, "generation": dep.metadata.generation, "revision": _rev(dep)}


def rollback_facts(dep, sets):
    """실제 ReplicaSet의 소유자, 가용 수, 두 템플릿 차이를 확인한다."""
    owned = [rs for rs in sets if any(ref.uid == dep.metadata.uid and ref.kind == "Deployment"
                                      for ref in rs.metadata.owner_references or [])]
    current = next((rs for rs in owned if _rev(rs) == _rev(dep)), None)
    prior = max((rs for rs in owned if 0 < _rev(rs) < _rev(dep)), key=_rev, default=None)
    if not current or not prior:
        return {}
    templates = []
    for rs in (current, prior):
        template = deepcopy(api_client().sanitize_for_serialization(rs.spec.template))
        template.get("metadata", {}).get("labels", {}).pop("pod-template-hash", None)
        templates.append(template)
    new, old = templates
    template_changed = new != old
    allowed_diff = False
    containers = [t["spec"]["containers"] for t in templates]
    if all(len(cs) == 1 and cs[0].get("name") == "podinfo"
           and cs[0].get("image") == "ghcr.io/stefanprodan/podinfo:6.7.1" for cs in containers) and all(
        not t["spec"].get("initContainers") and not t["spec"].get("volumes") for t in templates
    ):
        for cs in containers:
            cs[0].pop("args", None)
            cs[0].get("readinessProbe", {}).get("httpGet", {}).pop("path", None)
        allowed_diff = new == old and template_changed
    return {"previous_revision": _rev(prior), "previous_available": prior.status.available_replicas or 0,
            "new_pods": current.status.replicas or 0, "new_ready": current.status.ready_replicas or 0,
            "only_args_or_readiness": allowed_diff}


def check(proposal: dict) -> dict:
    """제안된 조치가 실행 가능한지 검사한다. executable=False면 실행할 조치가 없는 것."""
    p = _norm(proposal)
    if p["action"] in ("none", "manual", ""):
        return {"executable": False, "allowed": False, "reasons": ["실행이 필요한 조치가 없어요."], "normalized": p}

    reasons = []
    if not p["deployment"]:
        reasons.append("변경할 Deployment 이름이 필요합니다.")
    elif p["deployment"] not in policy.allowed_deployments:
        reasons.append("demo/api와 demo/web만 변경할 수 있어요.")
    dep = None
    if p["action"] not in policy.allowed_actions:
        reasons.append(f"'{p['action']}' 작업은 허용 범위(재시작·롤백·복제본 조정) 밖이에요.")
    if p["namespace"] not in policy.allowed_namespaces:
        reasons.append(f"'{p['namespace'] or '(미지정)'}' 네임스페이스는 조치 허용 대상이 아니에요. 허용: {sorted(policy.allowed_namespaces)}")
    if not reasons:
        try:
            dep = apps().read_namespaced_deployment(p["deployment"], p["namespace"])
        except ApiException as e:
            dep = None
            reasons.append(f"대상 Deployment '{p['deployment']}'를 확인할 수 없어요 ({e.status} {e.reason}).")
        if dep is not None and p["action"] == "scale":
            if p["replicas"] is None:
                reasons.append("복제본 수가 지정되지 않았어요.")
            elif not policy.min_replicas <= p["replicas"] <= policy.max_replicas:
                reasons.append(f"복제본 수 {p['replicas']}는 허용 범위({policy.min_replicas}~{policy.max_replicas}) 밖이에요.")
            elif p["replicas"] == (dep.spec.replicas or 0):
                reasons.append(f"이미 복제본이 {p['replicas']}개라서 바꿀 것이 없어요.")
        if dep is not None and p["action"] == "rollback":
            current = _rev(dep)
            if not any(0 < _rev(rs) < current and any(ref.uid == dep.metadata.uid for ref in rs.metadata.owner_references or [])
                       for rs in _owned_replicasets(p["namespace"], p["deployment"])):
                reasons.append("되돌릴 이전 리비전이 없어요.")
    return {"executable": True, "allowed": not reasons, "reasons": reasons or ["허용 범위 안의 조치예요."], "normalized": p,
            "target_version": target_version(dep) if dep else None}


def execute(proposal: dict, approver: str, *, expected=None, automatic=False) -> dict:
    """가드를 통과하고 승인된 조치만 실행한다. 실행 직전에 가드를 한 번 더 확인한다."""
    guard = check(proposal)
    p = guard["normalized"]
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    base = {"action": p["action"], "namespace": p["namespace"], "deployment": p["deployment"], "at": now, "approver": approver}
    if not (guard["executable"] and guard["allowed"]):
        return {**base, "ok": False, "message": "가드 재검사에서 차단됨: " + " ".join(guard["reasons"])}

    ns, name = p["namespace"], p["deployment"]
    try:
        dep = apps().read_namespaced_deployment(name, ns)
        if expected is not None and expected != target_version(dep):
            return {**base, "ok": False, "message": "판단 후 대상이나 리비전이 바뀌어 실행을 중단했습니다."}
        ops = [{"op": "test", "path": "/metadata/resourceVersion", "value": dep.metadata.resource_version}]
        annotations = dict(dep.metadata.annotations or {})
        if automatic:
            annotations["kubeops.local/last-auto-at"] = now
        if p["action"] == "scale":
            before = dep.spec.replicas
            ops.append({"op": "replace", "path": "/spec/replicas", "value": p["replicas"]})
            msg = f"{ns}/{name} 복제본을 {before}개에서 {p['replicas']}개로 바꿨어요."
        elif p["action"] == "restart":
            template_annotations = dict(dep.spec.template.metadata.annotations or {})
            template_annotations["kubectl.kubernetes.io/restartedAt"] = now
            ops.append({"op": "add", "path": "/spec/template/metadata/annotations", "value": template_annotations})
            annotations["kubernetes.io/change-cause"] = f"restart (KubeOps Copilot, {approver})"
            msg = f"{ns}/{name} 재시작을 시작했어요."
        elif p["action"] == "rollback":
            sets = _owned_replicasets(ns, name)
            prev = max((rs for rs in sets if 0 < _rev(rs) < _rev(dep)
                        and any(ref.uid == dep.metadata.uid for ref in rs.metadata.owner_references or [])),
                       key=_rev, default=None)
            if prev is None:
                return {**base, "ok": False, "message": "직전 리비전을 확인할 수 없습니다."}
            template = api_client().sanitize_for_serialization(prev.spec.template)
            template.setdefault("metadata", {}).setdefault("labels", {}).pop("pod-template-hash", None)
            ops.append({"op": "replace", "path": "/spec/template", "value": template})
            annotations["kubernetes.io/change-cause"] = f"rollback to revision {_rev(prev)} (KubeOps Copilot, {approver})"
            msg = f"{ns}/{name}을 리비전 {_rev(dep)}에서 {_rev(prev)}의 설정으로 롤백했어요."
        else:
            return {**base, "ok": False, "message": "지원하지 않는 조치"}
        ops.append({"op": "add", "path": "/metadata/annotations", "value": annotations})
        applied = apps().patch_namespaced_deployment(name, ns, ops)
    except ApiException as e:
        return {**base, "ok": False, "message": f"Kubernetes API 거부: {e.status} {e.reason}"}
    return {**base, "ok": True, "message": msg, "uid": applied.metadata.uid, "generation": applied.metadata.generation,
            "replicas": p["replicas"] if p["action"] == "scale" else None}

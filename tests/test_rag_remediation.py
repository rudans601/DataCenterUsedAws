import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from eval import cluster_fixtures, single_detection as runner
from eval.remediation_score import recovery_outcome
from kubeops_copilot import actions, detection, graph, rag, triage


class RunbookTests(unittest.TestCase):
    def test_only_active_runbooks_and_reviewed_cases_are_searchable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = ["runbooks/old.md", "runbooks/v2/live.md", "incidents/reviewed.md",
                     "incidents/auto-from-old-version.md", "candidates/unreviewed.md"]
            for name in paths:
                p = root / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("# " + name)
            with patch.object(rag, "settings", SimpleNamespace(knowledge_dir=root)):
                docs = rag.load_chunks()
                self.assertEqual([(d.metadata["kind"], d.metadata["version"]) for d in docs],
                                 [("runbooks", "2.0"), ("incidents", "case")])
                before = rag.corpus_revision()
                (root / "candidates/unreviewed.md").write_text("unreviewed change")
                self.assertEqual(before, rag.corpus_revision())
                (root / "runbooks/v2/live.md").write_text("# revision")
                self.assertNotEqual(before, rag.corpus_revision())

    def test_no_experiment_labels_in_retrieval_or_model_messages(self):
        evidence = [{"id": "E1", "source": "events", "target": "demo/api", "severity": "warning",
                     "title": "Readiness", "detail": "HTTP probe returned 404"}]
        state = {"question": "운영 점검", "namespace": "demo", "signals": [], "intent": {"targets": ["api"]},
                 "case": "DO_NOT_LEAK", "reference": {"action": "DO_NOT_LEAK"}, "truth": "DO_NOT_LEAK",
                 "snapshot": {"secret_answer": "DO_NOT_LEAK"}, "evidence": evidence}
        p = {"action": "rollback", "deployment": "api", "namespace": "demo", "replicas": None}
        quote = "대상을 확인하고 운영자 승인 후 롤백한다."
        doc = {"id": "D1", "source": "knowledge/runbooks/v2/example.md", "kind": "runbooks",
               "version": "2.0", "content": "[approval.rollback] " + quote}
        with patch.object(rag, "search", return_value=[doc]) as search:
            state.update(graph.retrieve(state))
            self.assertIn("404", search.call_args.args[0])
            self.assertNotIn("DO_NOT_LEAK", str(search.call_args))
        with patch.object(graph, "chat_model") as model:
            call = model.return_value.with_structured_output.return_value.invoke
            for text, version, ids, expected in [(quote, "2.0", ["D1"], True), ("인용을 지어냈다", "2.0", ["D1"], False),
                                                 (quote, "1.0", ["D1"], False), (quote, "2.0", ["D9"], False)]:
                state["docs"] = [{**doc, "version": version}]
                call.return_value.model_dump.return_value = {
                    "proposal": p, "doc_ids": ids, "runbook_citation": {"doc_id": "D1", "quote": text},
                    "findings": [{"claim":"probe 실패", "evidence_ids":["E1", "E99"]}]}
                out = graph.analyze(state)["analysis"]
                self.assertEqual(bool(out["citation_source"]), expected)
                self.assertEqual(out["cited_procedures"], ["approval.rollback"] if expected else [])
                self.assertEqual(out["findings"][0]["evidence_ids"], ["E1"])
                self.assertTrue(out["target_grounded"])
                self.assertNotIn("DO_NOT_LEAK", str(call.call_args))
        self.assertEqual(rag.cited_procedures(doc["content"], "운영자 승인"), [])

    def test_auto_procedures_in_active_version_match_execution_guard(self):
        docs = rag.load_chunks()
        for action, filename in triage.AUTO_PROCEDURES.items():
            doc = next(d for d in docs if d.metadata["source"].endswith("/" + filename))
            self.assertIn("auto." + action, rag.cited_procedures(doc.page_content, doc.page_content))

    def test_versioned_disk_index_and_retrieval_use_complete_runbooks(self):
        from langchain_core.embeddings import DeterministicFakeEmbedding
        with tempfile.TemporaryDirectory() as temp:
            config = SimpleNamespace(knowledge_dir=rag.settings.knowledge_dir, index_path=Path(temp)/"index.json",
                                     openai_api_key="test-only", llm_provider="test", openai_embedding_model="fake")
            cache = {"store": None, "fingerprint": None}
            with patch.object(rag,"settings",config), patch.object(rag,"_cache",cache), \
                 patch.object(rag,"embeddings",return_value=DeterministicFakeEmbedding(size=6)):
                first = rag.search("Pod readiness 404")
                cache.update(store=None, fingerprint=None)
                loaded = rag.search("Pod readiness 404")
            self.assertEqual([r["source"] for r in first], [r["source"] for r in loaded])
            self.assertEqual(len(list(Path(temp).glob("index-*.json"))), 1)
            sources = {r["source"]: r for r in loaded}
            for name in ("operations-policy.md", *triage.AUTO_PROCEDURES.values()):
                src = "knowledge/runbooks/v2/" + name
                self.assertEqual(sources[src]["content"], (config.knowledge_dir / "runbooks"/"v2"/name).read_text().strip())

    def test_observed_warning_events_prioritize_complete_runbook(self):
        docs = rag.load_chunks()
        store = SimpleNamespace(store={str(i): {"metadata": d.metadata, "text": d.page_content}
                                       for i, d in enumerate(docs)})
        def ranked(query, k, filter):
            return sorted(
                ((doc, -0.9 if doc.metadata["source"].endswith("scheduling-storage.md") else 0.9)
                 for doc in docs if filter(doc)), key=lambda hit: -hit[1]
            )[:k]
        store.similarity_search_with_score = ranked
        item = {"source": "events", "severity": "warning", "title": "events",
                "detail": "[Warning] FailedScheduling Pod/api-new x1 (1초 전): Insufficient cpu\n"
                          "[Normal] Scheduled Pod/api-old x1 (1분 전): bound"}
        with patch.object(rag, "get_store", return_value=store):
            result = graph.retrieve({"question": "서비스 상태?", "evidence": [item]})["docs"]
            self.assertEqual(result[1]["matched_events"], ["FailedScheduling"])
            self.assertTrue(result[1]["source"].endswith("scheduling-storage.md"))
            self.assertIn("## 검증", result[1]["content"])
            for reason, filename in (("FailedMount", "scheduling-storage.md"), ("Unhealthy", "probes-images.md"),
                                     ("BackOff", "container-exits.md"), ("ErrImagePull", "probes-images.md"),
                                     ("UpstreamTimeout", "normal-maintenance.md")):
                document = rag.search("상태", event_reasons={reason}, k_runbooks=1)[1]
                self.assertTrue(document["source"].endswith(filename), reason)
            # 질문이나 앱 로그 속 단어만으로 이벤트 우선순위를 부여하지 않는다.
            result = graph.retrieve({"question": "FailedScheduling", "evidence": [{**item, "source": "logs"}]})["docs"]
            self.assertFalse(any(r["matched_events"] for r in result))

    def test_failed_retrieval_cannot_authorize_change(self):
        with patch.object(rag, "search", side_effect=RuntimeError("테스트")):
            state = graph.retrieve({"question": "상태?", "evidence": []})
        self.assertFalse(any(d["content"] for d in state["docs"]))


class ChangeTests(unittest.TestCase):
    def test_auto_rollback_needs_failed_new_pod_serving_predecessor_and_unchanged_target(self):
        p = {"namespace": "demo", "deployment": "api", "action": "rollback"}
        filename = triage.AUTO_PROCEDURES["rollback"]
        a = {"route": "auto", "proposal": p, "confidence": "high", "service_detection": "normal", "impact": "warning", "target_grounded": True,
             "citation_source": "knowledge/runbooks/v2/" + filename,
             "cited_procedures": ["auto.rollback"]}
        dep = {"name":"api", "uid":"a", "revision":"8", "generation":9, "observed_generation":9, "desired":2}
        snap = {"namespace":"demo", "deployments":[dep], "services":[{"name":"api", "ready_endpoints":2}],
                "metrics":{"api":{"rps":19, "p95_ms":7,"error_ratio":0}},
                "pods":[{"name":"api-new","revision":"8","ready":False,"restarts":1}],
                "recovery_facts":{"previous_available":2,"new_pods":1,"new_ready":0,"only_args_or_readiness":True}}
        check = {"executable":True,"allowed":True,"normalized":p, "target_version":triage.observed_version(snap,p)}
        def route(local=True):
            return triage.classify(snap, a, check, local=local)["route"]
        self.assertEqual(route(), "auto")
        self.assertEqual(route(False), "handoff")
        with patch.dict(a, {"cited_procedures": ["approval.restart"]}):
            self.assertEqual(route(), "handoff")
        with patch.dict(dep, {"generation":10}):
            self.assertEqual(route(), "handoff")
        with patch.dict(dep, {"last_auto_at": datetime.now(timezone.utc).isoformat()}):
            self.assertEqual(route(), "handoff")
        for changes in [{"new_ready":1}, {"previous_available":1}, {"only_args_or_readiness":False}]:
            with patch.dict(snap["recovery_facts"],changes):
                self.assertEqual(route(), "handoff")

    def test_disallowed_and_stale_changes_never_write(self):
        p = {"namespace":"demo","deployment":"api","action":"scale","replicas":3}
        guard = {"executable":True,"allowed":True,"normalized":p}
        with patch.object(actions,"check",return_value=guard), patch.object(actions,"apps") as apps:
            dep = apps.return_value.read_namespaced_deployment.return_value
            dep.metadata.annotations = {"deployment.kubernetes.io/revision":"8"}
            dep.metadata.uid = "uid"
            dep.metadata.generation = 10
            res = actions.execute(p, "operator", expected={"uid":"uid","generation":9,"revision":8})
            self.assertFalse(res["ok"])
            apps.return_value.patch_namespaced_deployment.assert_not_called()
            dep.metadata.resource_version = "101"
            dep.spec.replicas = 2
            self.assertTrue(actions.execute(p, "local", expected=actions.target_version(dep), automatic=True)["ok"])
            ops = apps.return_value.patch_namespaced_deployment.call_args.args[-1]
            self.assertEqual(ops[0], {"op":"test","path":"/metadata/resourceVersion","value":"101"})
            self.assertIn("kubeops.local/last-auto-at", ops[-1]["value"])

    def test_approval_is_bound_to_one_request_and_can_timeout(self):
        p = {"namespace":"demo","deployment":"api","action":"rollback"}
        state = {"guard":{"normalized":p,"target_version":{"generation":1}},
                 "proposal":{"reason":"설정 변경","expected_impact":"롤백","risk":"배포 변경"},
                 "analysis":{"runbook_citation":{"quote":"절차"}}}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/"expired.json").write_text('{"approved":true}')
            self.assertFalse(runner.operator_decision(state,None,root,timeout=0)["approved"])
            def answer(_):
                request=json.loads((root/"pending.json").read_text())
                self.assertEqual(request["proposal"],p)
                (root/(request["id"]+".json")).write_text('{"approved":true,"approver":"operator"}')
            with patch.object(runner.time, "sleep", side_effect=answer):
                self.assertTrue(runner.operator_decision(state,None,root,timeout=2)["approved"])
            self.assertFalse((root/"pending.json").exists())

    def test_fresh_checks_can_stop_an_automatic_decision(self):
        p={"namespace":"demo","deployment":"api","action":"scale","replicas":3}
        state={"guard":{"normalized":p,"target_version":{"uid":"a","generation":1,"revision":1}},
               "triage":{"route":"auto"},"analysis":{"proposal":p}}
        with patch.object(graph.k8s_tools,"deployment_snapshot",return_value={}), \
             patch.object(graph.k8s_tools,"collect_evidence",return_value={}), \
             patch.object(graph,"local_auto_enabled",return_value=False), patch.object(actions,"execute") as write:
            receipt = graph.execute(state)["execution"]
            self.assertFalse(receipt["ok"])
            self.assertEqual(receipt["mode"], "auto")
            self.assertIsNotNone(datetime.fromisoformat(receipt["at"]).tzinfo)
            write.assert_not_called()
        for route in ("handoff", "observe", "approval"):
            with self.assertRaises(ValueError):
                graph.execute({**state,"triage":{"route":route}, "approval":{"approved":False}})


class WorkflowTests(unittest.TestCase):
    def test_graph_keeps_observation_automatic_approval_and_handoff_distinct(self):
        from contextlib import ExitStack
        from unittest.mock import Mock
        for route in ("observe","auto","approval","handoff"):
            with self.subTest(route=route), ExitStack() as patches:
                graph.build_graph.cache_clear()
                patches.callback(graph.build_graph.cache_clear)
                proposal={"action":"rollback","namespace":"demo","deployment":"api"}
                outputs={"understand":{"intent":{"targets":["api"]}}, "collect":{"snapshot":{},"evidence":[]},
                         "retrieve":{"docs":[]}, "analyze":{"proposal":proposal},
                         "guard":{"triage":{"route":route},"guard":{"normalized":proposal}},
                         "execute":{"execution":{"ok":True}}, "verify":{"verification":{"verdict":"success"}},
                         "report":{"answer":"기록"}}
                nodes={}
                for name, result in outputs.items():
                    nodes[name] = patches.enter_context(patch.object(graph,name,Mock(return_value=result)))
                state=graph.ask("운영 확인")
                self.assertEqual(state["pending_approval"],route=="approval")
                if route=="approval":
                    nodes["execute"].assert_not_called()
                    graph.decide(state["thread_id"],False,"test")
                    nodes["execute"].assert_not_called()
                    state=graph.ask("다른 사건")
                    graph.decide(state["thread_id"],True,"test")
                count=int(route in ("auto","approval"))
                self.assertEqual(nodes["execute"].call_count,count)
                self.assertEqual(nodes["verify"].call_count,count)


class ScoreTests(unittest.TestCase):
    def test_cleanup_preserves_a_different_operators_rollback(self):
        field = cluster_fixtures.CAUSE
        current = {"metadata": {"uid": "demo-uid", "annotations": {
            "kubernetes.io/change-cause": "rollback to revision 8 (KubeOps Copilot, another operator)"}}}
        with patch.object(cluster_fixtures, "resource", return_value=current), patch.object(cluster_fixtures, "patch") as write:
            with self.assertRaisesRegex(RuntimeError, "외부 변경"):
                cluster_fixtures.restore_fields("deploy/api", "demo", "demo-uid",
                                                {field: "baseline"}, {field: "release under test"})
            write.assert_not_called()

    def test_healthy_replacement_during_verification_is_not_credited(self):
        after = {"uid": "replaced", "generation": 6, "observed_generation": 6, "updated": 2, "available": 2,
                 "ready": 2, "desired": 2, "problem_pods": 0, "metrics": {"rps": 20, "error_ratio": 0, "p95_ms": 12}}
        state = {"before": {}, "execution": {"uid": "old", "generation": 5, "ok": True,
                                             "action": "rollback", "namespace": "demo", "deployment": "api"}}
        with patch.object(graph, "_wait_rollout", return_value=(True, 1)), patch.object(graph.time, "sleep"), \
             patch.object(graph.k8s_tools, "deployment_snapshot", return_value=after):
            self.assertEqual(graph.verify(state)["verification"]["verdict"], "unknown")

    def test_impact_matrix_uses_independent_service_observations(self):
        row = {"protocol": detection.PROTOCOL, "measurement_status": "complete", "model_impact": "warning",
               "injected": {"ready": 0, "desired": 2, "endpoints": 0, "api_code": 503},
               "probes": [{"count": 4, "failed": 4}, {"count": 4, "failed": 4}]}
        pairs, missing = detection.decision_matrix([row, {**row, "measurement_status": "started"}], "impact")
        self.assertEqual(dict(pairs), {("critical", "warning"): 1})
        self.assertEqual(missing, 1)

    def test_choice_matrix_uses_first_model_answer_and_skips_ungraded_and_v1(self):
        r={"protocol":detection.PROTOCOL, "reference":{"route":"handoff","action":"manual"},"model_route":"auto",
           "effective_route":"handoff","proposed_action":"scale","proposal":{"namespace":"other","deployment":"api"}}
        rows=[r, {**r,"protocol":detection.LEGACY_PROTOCOL},{**r,"fixtures_only":True},
              {**r,"reference":{}}, {**r,"model_route":None}]
        pairs, missing=detection.decision_matrix(rows,"route")
        self.assertEqual(dict(pairs),{("handoff","auto"):1,("handoff","미판정"):1})
        self.assertEqual(missing,1)
        self.assertEqual(detection.decision_matrix(rows,"action")[0]["manual","대상 불일치"],2)

    def test_recovery_needs_actual_change_and_pre_cleanup_service_observations(self):
        good={"count":4,"failed":0,"p95_ms":10}
        row={"case":"bad-deploy","truth":"incident","after_fixture_cleanup":{"ready":2,"api_code":200}}
        self.assertEqual(recovery_outcome(row),"미실행")
        row.update(action_result={"ok":True}, verification={"verdict":"success"})
        self.assertEqual(recovery_outcome(row),"검증 불가")
        row["after_action_probes"]=[{**good,"failed":1}]
        self.assertEqual(recovery_outcome(row),"복구 실패")
        row["after_action_probes"]=[good,good]
        self.assertEqual(recovery_outcome(row),"검증 불가")
        row["pre_action_probes"]=[{**good,"failed":1}]
        self.assertEqual(recovery_outcome(row),"서비스 복구")
        row["case"]="high-load"
        self.assertEqual(recovery_outcome(row),"검증 불가")
        row["verification_load_active"]=True
        self.assertEqual(recovery_outcome(row),"서비스 복구")
        row.update(case="probe-fail",truth="normal",pre_action_probes=[good],before_action={"problem_pods":1})
        self.assertEqual(recovery_outcome(row),"배포 복구")

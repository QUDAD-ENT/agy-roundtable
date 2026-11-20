"""
Comprehensive Automated Test Suite for TeamOrchestrator Lifecycle & Safety Invariants.
Tests concurrency locks, write-scope serialization, next_round/abort_now config switches,
2-round auto-repair cap, stale-round conflict guard, fallback session labeling,
and crash recovery ('needs_verification').
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest

PROTO_DIR = os.path.dirname(os.path.abspath(__file__))
if PROTO_DIR not in sys.path:
    sys.path.insert(0, PROTO_DIR)

from agy_adapter import AgyAdapter
from codex_adapter import CodexAdapter, CodexExecutionResult, get_runtime_dirs
from team_runtime import TeamOrchestrator
from team_state_manager import TeamStateManager


class TestTeamRuntimeMechanisms(unittest.TestCase):
    def setUp(self) -> None:
        tmp_dir, _ = get_runtime_dirs()
        self.tmp_dir = tmp_dir
        self.state_path = os.path.join(tmp_dir, f"test_team_state_{time.time_ns()}.json")
        if os.path.exists(self.state_path):
            os.remove(self.state_path)

    def tearDown(self) -> None:
        if os.path.exists(self.state_path):
            try:
                os.remove(self.state_path)
            except Exception:
                pass

    def test_01_roles_members_and_model_validation(self) -> None:
        orch = TeamOrchestrator(state_file=self.state_path, auto_start_scheduler=False)
        # Create custom role & set as coordinator
        r = orch.upsert_role("sec_audit", "安全审计", "审查权限边界与策略合规", set_as_coordinator=True)
        self.assertTrue(r["is_coordinator"])
        self.assertEqual(orch.store.snapshot()["binding"]["coordinator_role_id"], "sec_audit")

        # Reject nonexistent model
        with self.assertRaises(ValueError):
            orch.upsert_member("bad_mem", "implementation", model="nonexistent-gpt-999", reasoning_effort="low")

        # Add valid second member in same role ('同职责可增员')
        m2 = orch.upsert_member("codex_author_2", "implementation", name="Codex 实施员 2", model="gpt-6.1-sol", reasoning_effort="medium")
        self.assertEqual(m2["role"], "implementation")
        self.assertEqual(m2["reasoning_effort"], "medium")

    def test_02_bounded_auto_repair_stops_at_max_2_rounds(self) -> None:
        call_counts = {"author": 0, "reviewer": 0}

        def fake_runner(prompt, session_id, **kwargs):
            if "独立审查输出格式契约" in prompt:
                call_counts["reviewer"] += 1
                # Always reject with distinct problem strings to test numeric cap=2
                n = call_counts["reviewer"]
                return CodexExecutionResult(
                    session_id=session_id or "sess-rev-1",
                    response_text=json.dumps({
                        "verdict": "REJECT",
                        "problem": f"第{n}次发现不同边界遗漏",
                        "evidence": f"line {n * 10}",
                        "impact": "high",
                        "expected_fix": "补齐边界判断",
                        "scope_expanded": False,
                    }, ensure_ascii=False),
                    partial_text="",
                    exit_code=0,
                    turn_completed=True,
                    requested_config=kwargs["requested_config"],
                    accepted_config=kwargs["accepted_config"],
                )
            else:
                call_counts["author"] += 1
                return CodexExecutionResult(
                    session_id=session_id or "sess-auth-1",
                    response_text=f'{{"draft_v": {call_counts["author"]}}}',
                    partial_text="",
                    exit_code=0,
                    turn_completed=True,
                    requested_config=kwargs["requested_config"],
                    accepted_config=kwargs["accepted_config"],
                )

        orch = TeamOrchestrator(
            state_file=self.state_path,
            adapter=CodexAdapter(runner_override=fake_runner),
            agy_adapter=AgyAdapter(runner_override=fake_runner),
            auto_start_scheduler=True,
        )
        try:
            task = orch.create_task(
                title="自动修复上限测试",
                goal="测试最多2轮自动修复后停在需要裁决",
                dispatch_mode="coordinator_auto",
                max_auto_repairs=2,
            )
            deadline = time.time() + 20.0
            final_t = None
            while time.time() < deadline:
                snap = orch.store.snapshot()
                final_t = next(x for x in snap["tasks"] if x["id"] == task["id"])
                if final_t["status"] == "needs_ruling":
                    break
                time.sleep(0.1)

            self.assertIsNotNone(final_t)
            self.assertEqual(final_t["status"], "needs_ruling")
            # Initial draft (1) + 2 auto repairs = 3 author attempts, 3 reviewer checks, then stops!
            self.assertEqual(call_counts["author"], 3)
            self.assertEqual(call_counts["reviewer"], 3)
            self.assertEqual(final_t["steps"][0]["repair_count"], 2)
            # Verify original sessions were preserved across all 3 rounds
            self.assertEqual(final_t["steps"][0]["worker_session_id"], "sess-auth-1")
            self.assertEqual(final_t["steps"][1]["worker_session_id"], "sess-rev-1")
        finally:
            orch.stop_scheduler()

    def test_03_write_scope_conflict_serializes_while_disjoint_runs_parallel(self) -> None:
        intervals = []

        def slow_runner(prompt, session_id, **kwargs):
            t0 = time.time()
            time.sleep(0.45)
            t1 = time.time()
            intervals.append((prompt[-6:], t0, t1))
            return CodexExecutionResult(
                session_id=session_id or f"sess-{time.time_ns()}",
                response_text="ok",
                partial_text="ok",
                exit_code=0,
                turn_completed=True,
                requested_config=kwargs["requested_config"],
                accepted_config=kwargs["accepted_config"],
            )

        orch = TeamOrchestrator(
            state_file=self.state_path,
            adapter=CodexAdapter(runner_override=slow_runner),
            auto_start_scheduler=True,
        )
        try:
            orch.upsert_member("codex_author_2", "implementation", model="gpt-6.1-sol", reasoning_effort="low")
            # Case A: Overlapping write_scope -> MUST serialize even with 2 idle members!
            t_conflict = orch.create_task(
                title="写范围冲突串行测试",
                goal="conflict",
                dispatch_mode="coordinator_auto",
                sandbox="workspace-write",
                steps=[
                    {"title": "W1", "member_id": "codex_author", "prompt": "STEP_A", "depends_on": [], "read_only": False, "write_scope": ["Codex-Antigravity-方案/qa/shared"]},
                    {"title": "W2", "member_id": "codex_author_2", "prompt": "STEP_B", "depends_on": [], "read_only": False, "write_scope": ["Codex-Antigravity-方案/qa/shared/sub"]},
                ],
            )
            deadline = time.time() + 5.0
            while time.time() < deadline:
                st = orch.store.snapshot()
                tc = next(x for x in st["tasks"] if x["id"] == t_conflict["id"])
                if tc["status"] == "succeeded":
                    break
                time.sleep(0.08)

            self.assertEqual(len(intervals), 2)
            _, a0, a1 = intervals[0]
            _, b0, b1 = intervals[1]
            # Strictly non-overlapping (one started at or after the other ended - 20ms jitter tolerance)
            self.assertTrue(b0 >= a1 - 0.02 or a0 >= b1 - 0.02, f"Expected serial execution, got {intervals}")

            # Case B: Disjoint write_scope -> MUST overlap in parallel!
            intervals.clear()
            t_par = orch.create_task(
                title="不冲突写范围并行测试",
                goal="parallel",
                dispatch_mode="coordinator_auto",
                sandbox="workspace-write",
                steps=[
                    {"title": "P1", "member_id": "codex_author", "prompt": "PARA_1", "depends_on": [], "read_only": False, "write_scope": ["Codex-Antigravity-方案/qa/dir1"]},
                    {"title": "P2", "member_id": "codex_author_2", "prompt": "PARA_2", "depends_on": [], "read_only": False, "write_scope": ["Codex-Antigravity-方案/qa/dir2"]},
                ],
            )
            deadline = time.time() + 5.0
            while time.time() < deadline:
                st = orch.store.snapshot()
                tp = next(x for x in st["tasks"] if x["id"] == t_par["id"])
                if tp["status"] == "succeeded":
                    break
                time.sleep(0.08)

            self.assertEqual(len(intervals), 2)
            _, p1_s, p1_e = intervals[0]
            _, p2_s, p2_e = intervals[1]
            overlap = min(p1_e, p2_e) - max(p1_s, p2_s)
            self.assertGreater(overlap, 0.15, f"Expected parallel overlap > 0.15s, got {overlap:.3f}s")
        finally:
            orch.stop_scheduler()

    def test_04_next_round_config_switch_and_fallback_session_and_stale_conflict(self) -> None:
        def runner(prompt, session_id, **kwargs):
            if session_id == "dead-session-000":
                return CodexExecutionResult(
                    session_id=session_id,
                    response_text="",
                    partial_text="",
                    exit_code=1,
                    turn_completed=False,
                    session_not_found=True,
                    error="Session not found",
                    requested_config=kwargs["requested_config"],
                    accepted_config=kwargs["accepted_config"],
                )
            time.sleep(0.35)
            return CodexExecutionResult(
                session_id=session_id or "fresh-replacement-999",
                response_text="done",
                partial_text="done",
                exit_code=0,
                turn_completed=True,
                requested_config=kwargs["requested_config"],
                accepted_config=kwargs["accepted_config"],
            )

        orch = TeamOrchestrator(
            state_file=self.state_path,
            adapter=CodexAdapter(runner_override=runner),
            auto_start_scheduler=True,
        )
        try:
            # Start task with dead session ID to verify fallback session creation AND mid-run next_round switch
            t1 = orch.create_task(
                title="会话降级与运行中切换测试",
                goal="test",
                dispatch_mode="coordinator_auto",
                steps=[
                    {"title": "S0", "member_id": "codex_author", "prompt": "work", "depends_on": [], "read_only": True, "session_id": "dead-session-000"}
                ],
            )
            time.sleep(0.1)
            # Member is working (default effort='high') -> switch config to 'medium' with mode='next_round'
            sw = orch.switch_member_config("codex_author", reasoning_effort="medium", mode="next_round", reason="测试下轮生效")
            self.assertEqual(sw["applied"], "next_round")

            # Wait for completion
            deadline = time.time() + 5.0
            while time.time() < deadline:
                snap = orch.store.snapshot()
                cur_t = next(x for x in snap["tasks"] if x["id"] == t1["id"])
                if cur_t["status"] == "succeeded":
                    break
                time.sleep(0.08)

            snap = orch.store.snapshot()
            cur_t = next(x for x in snap["tasks"] if x["id"] == t1["id"])
            att = cur_t["steps"][0]["attempts"][-1]
            # Verify explicit fallback session markers
            self.assertTrue(att["is_fallback_session"])
            self.assertEqual(att["fallback_of_session_id"], "dead-session-000")
            self.assertEqual(att["session_id"], "fresh-replacement-999")
            # Verify pending_config automatically promoted at turn boundary!
            self.assertEqual(snap["members"]["codex_author"]["reasoning_effort"], "medium")
            self.assertIsNone(snap["members"]["codex_author"]["pending_config"])

            # Now create a NEWER unfinished task on codex_author (paused/needs_ruling) and verify stale-round guard!
            orch.set_pause(True)
            t2 = orch.create_task(title="更新的未完成轮次", goal="newer", dispatch_mode="single_member", target_member_id="codex_author")
            with self.assertRaises(ValueError) as ctx:
                orch.followup_task(t1["id"], "尝试绕过新轮次直接恢复旧轮次")
            self.assertIn("conflict_requires_ruling", str(ctx.exception))
        finally:
            orch.stop_scheduler()

    def test_05_crash_recovery_marks_needs_verification_and_continues_only_unfinished(self) -> None:
        # Prepare a state file simulating mid-run crash on Step 1 while Step 0 already succeeded
        mgr = TeamStateManager(state_file=self.state_path)
        now = time.time()
        mgr.state["members"]["codex_author"]["status"] = "working"
        mgr.state["tasks"].append({
            "id": "TASK-CRASH01",
            "title": "崩溃恢复核对测试",
            "goal": "test crash recovery",
            "dispatch_mode": "coordinator_auto",
            "auto_template": "custom",
            "require_manual_ruling": False,
            "max_auto_repairs": 2,
            "status": "running",
            "current_step_index": 1,
            "blocking_reason": None,
            "steps": [
                {
                    "step_index": 0,
                    "title": "已完成步骤0",
                    "step_type": "execution",
                    "member_id": "codex_author",
                    "prompt": "step0",
                    "depends_on": [],
                    "review_of": None,
                    "read_only": True,
                    "write_scope": [],
                    "workspace_scope": "Codex-Antigravity-方案/qa",
                    "sandbox": "read-only",
                    "worker_session_id": "sess-0",
                    "status": "succeeded",
                    "repair_count": 0,
                    "pending_repair_brief": None,
                    "handoff_context": None,
                    "blocking_reason": None,
                    "accepted_attempt_id": "ATT-0",
                    "latest_result_text": "step0_done",
                    "attempts": [{"attempt_id": "ATT-0", "round_num": 1, "member_id": "codex_author", "status": "succeeded", "pid": 111, "result_text": "step0_done"}],
                },
                {
                    "step_index": 1,
                    "title": "中断步骤1",
                    "step_type": "execution",
                    "member_id": "codex_author",
                    "prompt": "step1",
                    "depends_on": [0],
                    "review_of": None,
                    "read_only": True,
                    "write_scope": [],
                    "workspace_scope": "Codex-Antigravity-方案/qa",
                    "sandbox": "read-only",
                    "worker_session_id": None,
                    "status": "running",
                    "repair_count": 0,
                    "pending_repair_brief": None,
                    "handoff_context": None,
                    "blocking_reason": None,
                    "accepted_attempt_id": None,
                    "latest_result_text": "",
                    "attempts": [{"attempt_id": "ATT-DEAD", "round_num": 1, "member_id": "codex_author", "status": "running", "pid": 9999999, "partial_text": "half"}],
                },
            ],
            "deliverables": [],
            "recent_events": [],
            "messages": [],
            "reviews": [],
            "rulings": [],
            "summary": "",
            "created_at": now,
            "updated_at": now,
        })
        mgr.save()

        executed_prompts = []

        def resume_runner(prompt, session_id, **kwargs):
            executed_prompts.append(prompt)
            return CodexExecutionResult(
                session_id=session_id or "sess-1-recovered",
                response_text="step1_recovered_ok",
                partial_text="step1_recovered_ok",
                exit_code=0,
                turn_completed=True,
                requested_config=kwargs["requested_config"],
                accepted_config=kwargs["accepted_config"],
            )

        # Boot orchestrator simulating service restart -> MUST mark needs_verification and NOT auto-run!
        orch = TeamOrchestrator(
            state_file=self.state_path,
            adapter=CodexAdapter(runner_override=resume_runner),
            auto_start_scheduler=True,
        )
        try:
            time.sleep(0.25)
            snap = orch.store.snapshot()
            t = snap["tasks"][0]
            self.assertEqual(t["status"], "needs_verification")
            self.assertEqual(t["steps"][1]["status"], "needs_verification")
            self.assertEqual(len(executed_prompts), 0)  # Did NOT blindly rerun!

            # Now user/coordinator explicitly verifies and clicks Continue -> ONLY Step 1 runs!
            cont = orch.continue_task("TASK-CRASH01")
            self.assertEqual(cont["resumed_steps"], [1])

            deadline = time.time() + 4.0
            while time.time() < deadline:
                snap2 = orch.store.snapshot()
                if snap2["tasks"][0]["status"] == "succeeded":
                    break
                time.sleep(0.08)

            self.assertEqual(len(executed_prompts), 1)
            self.assertIn("step1", executed_prompts[0])
            # Step 0 was NOT rerun (still has 1 attempt)
            self.assertEqual(len( orch.store.snapshot()["tasks"][0]["steps"][0]["attempts"] ), 1)
        finally:
            orch.stop_scheduler()

    def test_06_dual_client_project_switch_and_stage_archiving(self) -> None:
        seen_cwds = []

        def agy_mock(prompt, session_id, **kwargs):
            seen_cwds.append(kwargs.get("cwd"))
            return CodexExecutionResult(
                session_id=session_id or "agy-conv-123",
                response_text='{"engine": "antigravity", "ok": true}',
                partial_text='{"engine": "antigravity", "ok": true}',
                exit_code=0,
                turn_completed=True,
                requested_config=kwargs["requested_config"],
                accepted_config=kwargs["accepted_config"],
            )

        orch = TeamOrchestrator(
            state_file=self.state_path,
            agy_adapter=AgyAdapter(runner_override=agy_mock),
            auto_start_scheduler=True,
        )
        try:
            # Create and validate Antigravity Claude Opus 5.5 member
            m_opus = orch.upsert_member("agy_opus", "implementation", client="antigravity", model="claude-opus-5-5", reasoning_effort="high")
            self.assertEqual(m_opus["client"], "antigravity")

            # Switch project to an arbitrary new temporary project directory
            proj_b = os.path.join(self.tmp_dir, f"proj_b_{time.time_ns()}")
            os.makedirs(proj_b, exist_ok=True)
            sw = orch.switch_project(proj_b)
            self.assertEqual(os.path.normcase(sw["binding"]["project_path"]), os.path.normcase(proj_b))

            t = orch.create_task(title="AGY任务", goal="run on proj_b", dispatch_mode="single_member", target_member_id="agy_opus")
            deadline = time.time() + 4.0
            while time.time() < deadline:
                cur = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == t["id"])
                if cur["status"] == "succeeded":
                    break
                time.sleep(0.08)

            self.assertEqual(os.path.normcase(seen_cwds[0]), os.path.normcase(proj_b))
            # Archive task -> stage active_task_id becomes None
            arch = orch.archive_task(t["id"], True)
            self.assertIsNone(arch["active_task_id"])
        finally:
            orch.stop_scheduler()

    def test_07_ai_question_pauses_pipeline_and_resumes_same_step_on_answer(self) -> None:
        calls = []

        def qa_runner(prompt, session_id, **kwargs):
            calls.append((prompt, session_id))
            if "独立审查输出格式契约" in prompt:
                return CodexExecutionResult(
                    session_id=session_id or "rev-s1",
                    response_text='{"verdict":"ACCEPT","problem":"","evidence":"已按用户所选Short report完成","impact":"","expected_fix":"","scope_expanded":false}',
                    partial_text="",
                    exit_code=0,
                    turn_completed=True,
                    requested_config=kwargs["requested_config"],
                    accepted_config=kwargs["accepted_config"],
                )
            if len(calls) == 1:
                # First author turn: asks a question with options!
                out = (
                    "在整理投稿包前，请先确认栏目类型：\n"
                    "```team_questions\n"
                    '{"questions":[{"id":"col","question":"目标栏目","multi_select":false,"options":["(推荐) Research article","Short report"]}]}\n'
                    "```"
                )
                return CodexExecutionResult(
                    session_id="auth-s1",
                    response_text=out,
                    partial_text=out,
                    exit_code=0,
                    turn_completed=True,
                    requested_config=kwargs["requested_config"],
                    accepted_config=kwargs["accepted_config"],
                )
            # Second author turn (after user selects Short report): completes deliverable!
            self.assertEqual(session_id, "auth-s1")
            self.assertIn("Short report", prompt)
            return CodexExecutionResult(
                session_id="auth-s1",
                response_text="已按 Short report 格式生成完整投稿包。",
                partial_text="已按 Short report 格式生成完整投稿包。",
                exit_code=0,
                turn_completed=True,
                requested_config=kwargs["requested_config"],
                accepted_config=kwargs["accepted_config"],
            )

        orch = TeamOrchestrator(
            state_file=self.state_path,
            adapter=CodexAdapter(runner_override=qa_runner),
            agy_adapter=AgyAdapter(runner_override=qa_runner),
            auto_start_scheduler=True,
        )
        try:
            t = orch.create_task(title="提问选择测试", goal="制作投稿包", dispatch_mode="coordinator_auto")
            deadline = time.time() + 4.0
            cur = None
            while time.time() < deadline:
                cur = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == t["id"])
                if cur["status"] == "awaiting_user":
                    break
                time.sleep(0.08)

            self.assertIsNotNone(cur)
            self.assertEqual(cur["status"], "awaiting_user")
            # Reviewer (step 1) MUST NOT have fired prematurely!
            self.assertEqual(cur["steps"][1]["status"], "pending")
            self.assertEqual(len(calls), 1)

            # Submit user's choice ('Short report')
            orch.answer_questions(t["id"], answers=[{"id": "col", "selected": "Short report"}])

            deadline = time.time() + 5.0
            while time.time() < deadline:
                cur = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == t["id"])
                if cur["status"] == "succeeded":
                    break
                time.sleep(0.08)

            self.assertEqual(cur["status"], "succeeded", f"test_29 roundtable failed: {cur.get('blocking_reason')} / steps={[ (s['step_index'], s['status'], s.get('blocking_reason')) for s in cur['steps'] ]}")
            self.assertIn("Short report", cur["summary"])
            # 1 question turn + 1 answer turn + 1 review turn = 3 total calls
            self.assertEqual(len(calls), 3)
        finally:
            orch.stop_scheduler()

    def test_08_roundtable_and_peer_mention_consultation(self) -> None:
        consult_triggered = {"asked": False, "replied": False, "round2_saw_chat": False}

        def rt_runner(prompt, session_id, **kwargs):
            if "团队实时协助请求" in prompt:
                consult_triggered["replied"] = True
                return CodexExecutionResult(
                    session_id="peer-consult-sess",
                    response_text="经核查：模块B接口采用幂等JSON返回，无破坏性兼容风险。",
                    partial_text="",
                    exit_code=0,
                    turn_completed=True,
                    requested_config=kwargs["requested_config"],
                    accepted_config=kwargs["accepted_config"],
                )
            if "收到队友" in prompt:
                return CodexExecutionResult(
                    session_id=session_id or "author-rt-sess",
                    response_text="结合队友核查结论，我建议直接复用模块B幂等JSON接口。",
                    partial_text="",
                    exit_code=0,
                    turn_completed=True,
                    requested_config=kwargs["requested_config"],
                    accepted_config=kwargs["accepted_config"],
                )
            if "独立检查当前项目材料" in prompt and "ID: codex_author" in prompt and not consult_triggered["asked"]:
                consult_triggered["asked"] = True
                ask_txt = (
                    "我先看完了主流程，但想请审查队友帮我确认模块B接口兼容性：\n"
                    "```team_consult\n"
                    '{"to": "agy_claude_reviewer", "question": "模块B接口是否是幂等JSON返回？"}\n'
                    "```"
                )
                return CodexExecutionResult(
                    session_id="author-rt-sess",
                    response_text=ask_txt,
                    partial_text=ask_txt,
                    exit_code=0,
                    turn_completed=True,
                    requested_config=kwargs["requested_config"],
                    accepted_config=kwargs["accepted_config"],
                )
            if "团队共享群聊记录" in prompt and "请仔细阅读共享群聊中其他参会队友上一轮的全部发言" in prompt:
                if "幂等JSON接口" in prompt:
                    consult_triggered["round2_saw_chat"] = True
            return CodexExecutionResult(
                session_id=session_id or f"rt-{time.time_ns()}",
                response_text="圆桌节点发言完毕，达成一致共识。",
                partial_text="",
                exit_code=0,
                turn_completed=True,
                requested_config=kwargs["requested_config"],
                accepted_config=kwargs["accepted_config"],
            )

        orch = TeamOrchestrator(
            state_file=self.state_path,
            adapter=CodexAdapter(runner_override=rt_runner),
            agy_adapter=AgyAdapter(runner_override=rt_runner),
            auto_start_scheduler=True,
        )
        try:
            t = orch.create_task(
                title="圆桌与@协商测试",
                goal="评估模块B重构方案",
                execution_mode="roundtable",
                host_member_id="agy_gemini",
                participants=["codex_author", "agy_claude_reviewer"],
                discussion_rounds=2,
            )
            deadline = time.time() + 20.0
            cur = None
            while time.time() < deadline:
                cur = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == t["id"])
                if cur["status"] == "succeeded":
                    break
                time.sleep(0.08)

            self.assertIsNotNone(cur)
            self.assertEqual(cur["status"], "succeeded", f"test_29 roundtable failed: {cur.get('blocking_reason')} / steps={[ (s['step_index'], s['status'], s.get('blocking_reason')) for s in cur['steps'] ]}")
            self.assertTrue(consult_triggered["asked"])
            self.assertTrue(consult_triggered["replied"])
            self.assertTrue(consult_triggered["round2_saw_chat"])
            kinds = [c["kind"] for c in cur.get("chat_room", [])]
            self.assertIn("mention_ask", kinds)
            self.assertIn("mention_reply", kinds)
            self.assertIn("deliverable", kinds)
        finally:
            orch.stop_scheduler()

    def test_09_chief_dynamic_dispatch_and_custom_dag_and_interject(self) -> None:
        def multi_runner(prompt, session_id, **kwargs):
            if "主帅总管" in prompt and "chief_plan" in prompt:
                plan_out = (
                    "我已察看项目，决定抽调 codex_author 与 codex_documenter 并行排查，再由 agy_claude_reviewer 独立复核：\n"
                    "```chief_plan\n"
                    '{"rationale":"双人并行探查 + 独立复核","steps":[\n'
                    '  {"title":"后端扫描","member_id":"codex_author","step_type":"execution","depends_on":[],"review_of":null,"read_only":true,"prompt":"扫描后端"},\n'
                    '  {"title":"文档扫描","member_id":"codex_documenter","step_type":"discussion","depends_on":[],"review_of":null,"read_only":true,"prompt":"扫描文档"},\n'
                    '  {"title":"交叉审查","member_id":"agy_claude_reviewer","step_type":"review","depends_on":[0],"review_of":0,"read_only":true,"prompt":"审查后端扫描"}\n'
                    "]}\n```"
                )
                return CodexExecutionResult(
                    session_id="chief-s1",
                    response_text=plan_out,
                    partial_text=plan_out,
                    exit_code=0,
                    turn_completed=True,
                    requested_config=kwargs["requested_config"],
                    accepted_config=kwargs["accepted_config"],
                )
            if "独立审查输出格式契约" in prompt:
                return CodexExecutionResult(
                    session_id="rev-s1",
                    response_text='{"verdict":"ACCEPT","problem":"","evidence":"核查无误","impact":"","expected_fix":"","scope_expanded":false}',
                    partial_text="",
                    exit_code=0,
                    turn_completed=True,
                    requested_config=kwargs["requested_config"],
                    accepted_config=kwargs["accepted_config"],
                )
            return CodexExecutionResult(
                session_id=session_id or f"w-{time.time_ns()}",
                response_text="节点交付完成OK",
                partial_text="",
                exit_code=0,
                turn_completed=True,
                requested_config=kwargs["requested_config"],
                accepted_config=kwargs["accepted_config"],
            )

        orch = TeamOrchestrator(
            state_file=self.state_path,
            adapter=CodexAdapter(runner_override=multi_runner),
            agy_adapter=AgyAdapter(runner_override=multi_runner),
            auto_start_scheduler=True,
        )
        try:
            # 1) Chief dynamic dispatch
            t_chief = orch.create_task(
                title="主帅动态点将测试",
                goal="全栈健康体检",
                execution_mode="chief_dispatch",
                chief_member_id="agy_gemini",
            )
            deadline = time.time() + 20.0
            cur = None
            while time.time() < deadline:
                cur = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == t_chief["id"])
                if cur["status"] == "succeeded":
                    break
                time.sleep(0.08)

            self.assertEqual(cur["status"], "succeeded", f"test_29 roundtable failed: {cur.get('blocking_reason')} / steps={[ (s['step_index'], s['status'], s.get('blocking_reason')) for s in cur['steps'] ]}")
            # 1 chief_plan + 3 recruited steps + 1 chief signoff = 5 steps!
            self.assertEqual(len(cur["steps"]), 5)
            self.assertEqual(set(cur["mode_meta"]["recruited_members"]), {"codex_author", "codex_documenter", "agy_claude_reviewer"})

            # 2) Custom DAG multi-stage + live user @interjection
            t_dag = orch.create_task(
                title="搭积木与现场插话测试",
                goal="多阶段验证",
                execution_mode="custom_dag",
                stages=[
                    [
                        {"member_id": "codex_author", "step_type": "discussion", "prompt": "工位1A"},
                        {"member_id": "agy_gemini", "step_type": "discussion", "prompt": "工位1B"},
                    ],
                    [
                        {"member_id": "codex_documenter", "step_type": "synthesis", "prompt": "工位2汇总"},
                    ],
                ],
            )
            deadline = time.time() + 20.0
            cur_dag = None
            while time.time() < deadline:
                cur_dag = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == t_dag["id"])
                if cur_dag["status"] == "succeeded":
                    break
                time.sleep(0.08)
            self.assertEqual(cur_dag["status"], "succeeded")
            self.assertEqual(len(cur_dag["steps"]), 3)

            # User interjects and @wakes agy_claude_reviewer!
            orch.interject_chat(t_dag["id"], "请补充确认一下安全边界", target_member_id="agy_claude_reviewer", trigger_reply=True)
            deadline = time.time() + 5.0
            while time.time() < deadline:
                cur_dag = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == t_dag["id"])
                if cur_dag["status"] == "succeeded" and len(cur_dag["steps"]) == 4:
                    break
                time.sleep(0.08)
            self.assertEqual(len(cur_dag["steps"]), 4)
            self.assertEqual(cur_dag["steps"][3]["member_id"], "agy_claude_reviewer")
        finally:
            orch.stop_scheduler()

    def test_10_partial_rescue_clean_step_retry_and_long_table_preservation(self) -> None:
        long_table = "| 刊物 | JIF | 结论 |\n|---|---|---|\n" + "\n".join(
            f"| Journal_{i} | {7.5 - i*0.1:.1f} | 完整证据行_{i} |" for i in range(60)
        )
        seen_efforts = []
        round2_saw_tail_row = {"ok": False}

        def res_runner(prompt, session_id, **kwargs):
            seen_efforts.append((kwargs.get("reasoning_effort"), "主帅总管" in prompt or "团队实时协助请求" in prompt))
            if "作为圆桌主持人，请综合群聊中全体成员的多轮讨论与质询意见" in prompt:
                # Final synthesis simulates streaming 3000 chars then failing on network telemetry!
                streamed = "### 最终转投决议\n\n" + ("BMC Medicine 冲刺后转投 IFAR。" * 80)
                return CodexExecutionResult(
                    session_id="bad-synth-sess",
                    response_text=streamed,
                    partial_text=streamed,
                    exit_code=1,
                    turn_completed=False,
                    error="There was a network issue connecting to the server",
                    requested_config=kwargs["requested_config"],
                    accepted_config=kwargs["accepted_config"],
                )
            if "请仔细阅读共享群聊中其他参会队友上一轮的全部发言" in prompt:
                if "完整证据行_59" in prompt:
                    round2_saw_tail_row["ok"] = True
                return CodexExecutionResult(
                    session_id="r2-sess",
                    response_text="已看到完整60行选刊表，无截断。",
                    partial_text="",
                    exit_code=0,
                    turn_completed=True,
                    requested_config=kwargs["requested_config"],
                    accepted_config=kwargs["accepted_config"],
                )
            return CodexExecutionResult(
                session_id="r1-sess",
                response_text=long_table,
                partial_text=long_table,
                exit_code=0,
                turn_completed=True,
                requested_config=kwargs["requested_config"],
                accepted_config=kwargs["accepted_config"],
            )

        orch = TeamOrchestrator(
            state_file=self.state_path,
            adapter=CodexAdapter(runner_override=res_runner),
            agy_adapter=AgyAdapter(runner_override=res_runner),
            auto_start_scheduler=True,
        )
        try:
            t = orch.create_task(
                title="长表保留与防丢稿采纳测试",
                goal="测试8000字无损群聊与断连采纳",
                execution_mode="roundtable",
                host_member_id="agy_gemini",
                participants=["codex_author", "agy_claude_reviewer"],
                discussion_rounds=2,
                enable_host_opening=False,
            )
            # Fast parallel start without opening step -> 2*2 + 1 = 5 steps total!
            self.assertEqual(len(t["steps"]), 5)

            deadline = time.time() + 20.0
            cur = None
            while time.time() < deadline:
                cur = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == t["id"])
                if cur["status"] == "failed":
                    break
                time.sleep(0.08)

            self.assertEqual(cur["status"], "failed")
            self.assertTrue(round2_saw_tail_row["ok"], "Round 2 should see row 59 without 900-char truncation!")

            # Rescue already-streamed synthesis in 1 click via accept_step_partial!
            rescued = orch.accept_step_partial(t["id"], 4)
            self.assertEqual(rescued["status"], "succeeded")
            self.assertIn("BMC Medicine 冲刺后转投 IFAR", rescued["summary"])
        finally:
            orch.stop_scheduler()

    def test_11_specialist_persona_grounding_fact_check_and_revision_loop(self) -> None:
        seen_prompts: list[tuple[str, str, object]] = []

        def audit_runner(prompt, session_id, **kwargs):
            client = kwargs["requested_config"]["client"]
            seen_prompts.append((client, prompt, session_id))
            if "请严格依据独立事实核查官" in prompt:
                return CodexExecutionResult(
                    session_id="rev-sess-1",
                    response_text="修订终稿：已将样本量修正为 100，并保留证据锚点。",
                    partial_text="",
                    exit_code=0,
                    turn_completed=True,
                    requested_config=kwargs["requested_config"],
                    accepted_config=kwargs["accepted_config"],
                )
            if "执行严格的反幻觉与事实核查审计" in prompt:
                return CodexExecutionResult(
                    session_id="fc-sess-1",
                    response_text="### 核查结论\n发现 1 处数字偏差：真实样本量为 100（非 120），请修订。",
                    partial_text="",
                    exit_code=0,
                    turn_completed=True,
                    requested_config=kwargs["requested_config"],
                    accepted_config=kwargs["accepted_config"],
                )
            if "独立重新研判并回答本任务" in prompt:
                return CodexExecutionResult(
                    session_id="reans-sess-1",
                    response_text="切换专家后的全新独立结论：所有指标均核对无误。",
                    partial_text="",
                    exit_code=0,
                    turn_completed=True,
                    requested_config=kwargs["requested_config"],
                    accepted_config=kwargs["accepted_config"],
                )
            return CodexExecutionResult(
                session_id="orig-sess-1",
                response_text="初始终稿：样本量约 120，建议继续推进。",
                partial_text="",
                exit_code=0,
                turn_completed=True,
                requested_config=kwargs["requested_config"],
                accepted_config=kwargs["accepted_config"],
            )

        orch = TeamOrchestrator(
            state_file=self.state_path,
            adapter=CodexAdapter(runner_override=audit_runner),
            agy_adapter=AgyAdapter(runner_override=audit_runner),
            auto_start_scheduler=True,
        )
        try:
            # Verify catalog templates & squad presets exist
            cat = orch.get_specialist_catalog()
            self.assertGreaterEqual(len(cat["templates"]), 6)
            self.assertGreaterEqual(len(cat["squad_presets"]), 4)

            # Customize specialist persona & strict grounding
            orch.switch_member_config(
                member_id="agy_gemini",
                specialty="自定义方法学核验",
                persona_prompt="CUSTOM_PERSONA_MARKER_XYZ: 必须逐行核对样本量。",
                grounding_mode="strict",
            )
            t = orch.create_task(
                title="防幻觉与跨引擎纠错闭环测试",
                goal="输出初步分析并接受异构核查与修订",
                execution_mode="single_member",
                target_member_id="agy_gemini",
            )

            deadline = time.time() + 15.0
            cur = None
            while time.time() < deadline:
                cur = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == t["id"])
                if cur["status"] == "succeeded":
                    break
                time.sleep(0.06)

            self.assertEqual(cur["status"], "succeeded", f"test_29 roundtable failed: {cur.get('blocking_reason')} / steps={[ (s['step_index'], s['status'], s.get('blocking_reason')) for s in cur['steps'] ]}")
            self.assertIn("CUSTOM_PERSONA_MARKER_XYZ", seen_prompts[0][1])
            self.assertIn("Strict Grounding", seen_prompts[0][1])

            # 1. Dispatch cross-engine fact-check (AGY author -> should auto-pick Codex checker!)
            fc_res = orch.fact_check_task(task_id=t["id"], focus_note="核对样本量")
            self.assertTrue(fc_res["cross_engine"])
            self.assertEqual(orch.store.snapshot()["members"][fc_res["checker_member_id"]]["client"], "codex")

            deadline = time.time() + 15.0
            while time.time() < deadline:
                cur = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == t["id"])
                if cur["status"] == "succeeded" and cur.get("latest_fact_check"):
                    break
                time.sleep(0.06)

            self.assertEqual(cur["status"], "succeeded", f"test_29 roundtable failed: {cur.get('blocking_reason')} / steps={[ (s['step_index'], s['status'], s.get('blocking_reason')) for s in cur['steps'] ]}")
            # Fact-check MUST NOT overwrite the original primary deliverable summary!
            self.assertIn("初始终稿", cur["summary"])
            self.assertIn("真实样本量为 100", cur["latest_fact_check"]["content"])
            # Clean unbiased session must have been passed to fact-checker
            self.assertIsNone(seen_prompts[1][2])

            # 2. Apply audit fixes -> updates summary cleanly
            orch.revise_from_fact_check(task_id=t["id"])
            deadline = time.time() + 15.0
            while time.time() < deadline:
                cur = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == t["id"])
                if cur["status"] == "succeeded" and "修订终稿" in cur["summary"]:
                    break
                time.sleep(0.06)
            self.assertIn("样本量修正为 100", cur["summary"])

            # 3. 1-click Switch Specialist & Re-answer
            orch.reanswer_with_specialist(task_id=t["id"], new_member_id="codex_author", instruction="请独立复核")
            deadline = time.time() + 15.0
            while time.time() < deadline:
                cur = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == t["id"])
                if cur["status"] == "succeeded" and "切换专家后的全新独立结论" in cur["summary"]:
                    break
                time.sleep(0.06)
            self.assertIn("切换专家后的全新独立结论", cur["summary"])
        finally:
            orch.stop_scheduler()

    def test_12_openscience_blind_r1_dialect_structured_consult_and_roster_calibration(self) -> None:
        captured: list[dict] = []
        consult_triggered = {"done": False}

        def sci_runner(prompt, session_id, **kwargs):
            cfg = kwargs["requested_config"]
            captured.append({"client": cfg["client"], "model": cfg["model"], "prompt": prompt})
            if "团队实时协助请求" in prompt:
                return CodexExecutionResult(
                    session_id="peer-consult-sess",
                    response_text="**判定 (Verdict)**：`CONFIRMED`\n**核查证据**：`prototype/serve_sidebar.py#L389` 返回 `server_rev: 10`。",
                    partial_text="",
                    exit_code=0,
                    turn_completed=True,
                    requested_config=cfg,
                    accepted_config=kwargs["accepted_config"],
                )
            if "第 1 轮独立盲评阶段" in prompt:
                if "(ID: agy_gemini)" in prompt:
                    return CodexExecutionResult(
                        session_id="r1-gemini",
                        response_text="ROUND1_SECRET_ANCHOR_ALPHA：提出两条正交假说 H1/H2 与最小判别实验。",
                        partial_text="",
                        exit_code=0,
                        turn_completed=True,
                        requested_config=cfg,
                        accepted_config=kwargs["accepted_config"],
                    )
                if "(ID: agy_claude_reviewer)" in prompt and not consult_triggered["done"]:
                    consult_triggered["done"] = True
                    return CodexExecutionResult(
                        session_id="r1-claude",
                        response_text=(
                            "准备核验边界条件。\n"
                            "```team_consult\n"
                            '{"to":"codex_reviewer","claim_to_verify":"server_rev 等于 10","evidence_needed":"serve_sidebar.py#L389","question":"请独立核对 health 返回值"}\n'
                            "```"
                        ),
                        partial_text="",
                        exit_code=0,
                        turn_completed=True,
                        requested_config=cfg,
                        accepted_config=kwargs["accepted_config"],
                    )
                return CodexExecutionResult(
                    session_id="r1-other",
                    response_text="独立盲评完成（含实证与竞争性解释）。",
                    partial_text="",
                    exit_code=0,
                    turn_completed=True,
                    requested_config=cfg,
                    accepted_config=kwargs["accepted_config"],
                )
            if "请仔细阅读共享群聊中其他参会队友上一轮的全部发言" in prompt:
                return CodexExecutionResult(
                    session_id="r2-sess",
                    response_text="Steelman 后指出 1 项 BLOCKING 与 1 项 OBSERVATION，已完成锦标赛对比。",
                    partial_text="",
                    exit_code=0,
                    turn_completed=True,
                    requested_config=cfg,
                    accepted_config=kwargs["accepted_config"],
                )
            return CodexExecutionResult(
                session_id="synth-sess",
                response_text="### 终稿\n已输出 M-MAD 多维仲裁矩阵与证据溯源台账。",
                partial_text="",
                exit_code=0,
                turn_completed=True,
                requested_config=cfg,
                accepted_config=kwargs["accepted_config"],
            )

        orch = TeamOrchestrator(
            state_file=self.state_path,
            adapter=CodexAdapter(runner_override=sci_runner),
            agy_adapter=AgyAdapter(runner_override=sci_runner),
            auto_start_scheduler=True,
        )
        try:
            # Verify 11 OpenScience + Engineering templates & openscience_discovery preset
            cat = orch.get_specialist_catalog()
            self.assertGreaterEqual(len(cat["templates"]), 11)
            self.assertEqual(cat["squad_presets"][0]["id"], "openscience_discovery")

            # Force max_concurrency=1 so Round 1 executes strictly serially (stress-testing Blind Round 1 isolation!)
            orch.update_binding(max_concurrency=1)

            # Test roster auto-calibration against live capabilities
            opt_res = orch.optimize_roster(apply_names=True, refresh_cli=False)
            self.assertGreaterEqual(opt_res["count"], 5)

            t = orch.create_task(
                title="OpenScience 盲评与结构化协商闭环测试",
                goal="提出并检验多模型科研协作改进假说",
                execution_mode="roundtable",
                host_member_id="agy_gemini",
                participants=["agy_gemini", "agy_claude_reviewer", "codex_reviewer"],
                discussion_rounds=2,
                enable_host_opening=False,
            )

            deadline = time.time() + 20.0
            cur = None
            while time.time() < deadline:
                cur = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == t["id"])
                if cur["status"] == "succeeded":
                    break
                time.sleep(0.06)

            self.assertEqual(cur["status"], "succeeded", f"test_29 roundtable failed: {cur.get('blocking_reason')} / steps={[ (s['step_index'], s['status'], s.get('blocking_reason')) for s in cur['steps'] ]}")

            # 1. Blind Round 1 Isolation: agy_claude_reviewer & codex_reviewer MUST NOT see agy_gemini's R1 secret anchor
            #    AND codex_reviewer MUST NOT see agy_claude_reviewer's directed R1 consult text ("server_rev 等于 10")!
            r1_gemini = [c["prompt"] for c in captured if "第 1 轮独立盲评阶段" in c["prompt"] and "(ID: agy_gemini)" in c["prompt"]]
            r1_claude = [c["prompt"] for c in captured if "第 1 轮独立盲评阶段" in c["prompt"] and "(ID: agy_claude_reviewer)" in c["prompt"]]
            r1_codex = [c["prompt"] for c in captured if "第 1 轮独立盲评阶段" in c["prompt"] and "(ID: codex_reviewer)" in c["prompt"] and "团队实时协助请求" not in c["prompt"]]
            self.assertTrue(r1_gemini and r1_claude and r1_codex)
            self.assertNotIn("ROUND1_SECRET_ANCHOR_ALPHA", r1_claude[0])
            self.assertNotIn("ROUND1_SECRET_ANCHOR_ALPHA", r1_codex[0])
            self.assertNotIn("server_rev 等于 10", r1_codex[0])
            self.assertNotIn("server_rev: 10", r1_codex[0])
            self.assertNotIn("team_rooms", r1_codex[0])

            # 2. Round 2 MUST see Round 1's output + Triad role differentiation!
            r2_prompts = [c["prompt"] for c in captured if "请仔细阅读共享群聊中其他参会队友上一轮的全部发言" in c["prompt"]]
            self.assertGreaterEqual(len(r2_prompts), 3)
            self.assertTrue(any("ROUND1_SECRET_ANCHOR_ALPHA" in p for p in r2_prompts))
            self.assertTrue(any("Skeptical Challenger" in p for p in r2_prompts))

            # 3. Model-Family Prompt Dialects + OpenScience 3-Tier Epistemic Calibration + M-MAD Host Synthesis
            self.assertIn('<dialect_calibration model_family="claude">', r1_claude[0])
            self.assertIn("Gemini 旗舰广域综合与假说演化模式", r1_gemini[0])
            self.assertIn("gpt-astra.txt", r1_codex[0])
            self.assertIn("[机制假说/深度外推 Hypothesis]", r1_claude[0])
            synth_prompts = [c["prompt"] for c in captured if "多维仲裁与演化矩阵" in c["prompt"]]
            self.assertEqual(len(synth_prompts), 1)

            # 4. Structured Persona-Aware @consult Handoff!
            consult_prompts = [c["prompt"] for c in captured if "团队实时协助请求" in c["prompt"]]
            self.assertEqual(len(consult_prompts), 1)
            self.assertIn("待核验声明：server_rev 等于 10", consult_prompts[0])
            self.assertIn("需核对证据：serve_sidebar.py#L389", consult_prompts[0])
            self.assertIn("你的专业核查视角", consult_prompts[0])
            self.assertIn("Verdict", consult_prompts[0])
        finally:
            orch.stop_scheduler()

    def test_13_consult_failure_safety_worker_crash_guard_and_state_rollback(self) -> None:
        # 1. Corrupt state file must be backed up (never silently wiped without .corrupt.*.bak)
        corrupt_path = os.path.join(self.tmp_dir, "corrupt_state.json")
        for old_f in os.listdir(self.tmp_dir):
            if old_f.startswith("corrupt_state.json"):
                os.remove(os.path.join(self.tmp_dir, old_f))
        with open(corrupt_path, "w", encoding="utf-8") as cf:
            cf.write("{invalid json content")
        mgr = TeamStateManager(state_file=corrupt_path)
        baks = [f for f in os.listdir(self.tmp_dir) if f.startswith("corrupt_state.json.corrupt.") and f.endswith(".bak")]
        self.assertEqual(len(baks), 1, "Corrupt state file must be preserved as .bak!")

        # 2. Transactional mutate() rollback on disk write failure
        orig_pause = mgr.snapshot()["paused"]
        orig_write = mgr._write_atomic

        def _boom(_data):
            raise OSError("Simulated disk full")

        mgr._write_atomic = _boom
        with self.assertRaises(OSError):
            mgr.mutate(lambda st: st.__setitem__("paused", not orig_pause))
        mgr._write_atomic = orig_write
        self.assertEqual(mgr.snapshot()["paused"], orig_pause, "Failed disk write must roll back in-memory state!")

        # 3. Failed @consult must produce UNVERIFIABLE (never fake '无补充异议') + worker exception must never hang!
        consult_state = {"asked": False}

        def fail_runner(prompt, session_id, **kwargs):
            cfg = kwargs["requested_config"]
            if "SIMULATE_OS_CRASH" in prompt:
                raise OSError("Simulated WinError 206")
            if "团队实时协助请求" in prompt:
                return CodexExecutionResult(
                    session_id=None,
                    response_text="",
                    partial_text="",
                    exit_code=1,
                    turn_completed=False,
                    error="Simulated peer timeout",
                    requested_config=cfg,
                    accepted_config=kwargs["accepted_config"],
                )
            if not consult_state["asked"]:
                consult_state["asked"] = True
                return CodexExecutionResult(
                    session_id="c-fail-sess",
                    response_text='先请队友复核：\n```team_consult\n{"to":"codex_reviewer","question":"请核查样本量"}\n```',
                    partial_text="",
                    exit_code=0,
                    turn_completed=True,
                    requested_config=cfg,
                    accepted_config=kwargs["accepted_config"],
                )
            return CodexExecutionResult(
                session_id="c-fail-sess",
                response_text="收到失败提示，保留为待核实。",
                partial_text="",
                exit_code=0,
                turn_completed=True,
                requested_config=cfg,
                accepted_config=kwargs["accepted_config"],
            )

        orch = TeamOrchestrator(
            state_file=self.state_path,
            adapter=CodexAdapter(runner_override=fail_runner),
            agy_adapter=AgyAdapter(runner_override=fail_runner),
            auto_start_scheduler=True,
        )
        try:
            t1 = orch.create_task(title="协商失败防误判", goal="核验失败回退", execution_mode="single_member", target_member_id="agy_gemini")
            deadline = time.time() + 15.0
            cur1 = None
            while time.time() < deadline:
                cur1 = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == t1["id"])
                if cur1["status"] == "succeeded":
                    break
                time.sleep(0.06)
            self.assertEqual(cur1["status"], "succeeded")
            replies = [c["content"] for c in cur1["chat_room"] if c["kind"] == "mention_reply"]
            self.assertEqual(len(replies), 1)
            self.assertIn("UNVERIFIABLE", replies[0])
            self.assertNotIn("可按原计划推进", replies[0])

            # Worker crash guard: must mark failed & release member to idle (no zombie hang!)
            t2 = orch.create_task(title="线程异常防卡死", goal="SIMULATE_OS_CRASH", execution_mode="single_member", target_member_id="codex_author")
            deadline = time.time() + 15.0
            cur2 = None
            while time.time() < deadline:
                snap = orch.store.snapshot()
                cur2 = next(x for x in snap["tasks"] if x["id"] == t2["id"])
                if cur2["status"] == "failed":
                    break
                time.sleep(0.06)
            self.assertEqual(cur2["status"], "failed")
            self.assertIn("WinError 206", cur2["steps"][0]["blocking_reason"])
            self.assertEqual(orch.store.snapshot()["members"]["codex_author"]["status"], "idle")
        finally:
            orch.stop_scheduler()

    def test_14_p0_sandbox_trust_root_path_escape_and_capability_routing(self) -> None:
        from team_state_manager import SPECIALIST_TEMPLATES

        # 1. Verify latest flagship roster: Gemini 4 Carbon present, 13 templates, ZERO gpt-6.1-terra anywhere!
        self.assertGreaterEqual(len(SPECIALIST_TEMPLATES), 13)
        for tpl in SPECIALIST_TEMPLATES:
            for slug in tpl.get("recommended_models") or []:
                self.assertNotIn("terra", slug.lower(), f"Template {tpl['template_id']} must not reference terra")

        orch = TeamOrchestrator(
            state_file=self.state_path,
            adapter=CodexAdapter(runner_override=lambda *a, **k: None),
            agy_adapter=AgyAdapter(runner_override=lambda *a, **k: None),
            auto_start_scheduler=False,
        )
        try:
            snap = orch.store.snapshot()
            self.assertIn("agy_carbon_plotter", snap["members"])
            self.assertEqual(snap["members"]["agy_carbon_plotter"]["model"], "gemini-4-carbon")
            for mid, m in snap["members"].items():
                self.assertNotIn("terra", str(m.get("model", "")).lower(), f"Default member {mid} must not use terra")

            # 2. Hard sandbox trust root: reject danger-full-access across all APIs
            with self.assertRaises(ValueError):
                orch.create_task(title="非法权限", goal="test", sandbox="danger-full-access")
            with self.assertRaises(ValueError):
                orch.upsert_member("bad_m", "implementation", client="codex", model="gpt-6.2-sol", sandbox="danger-full-access")
            with self.assertRaises(ValueError):
                orch.switch_member_config("codex_author", sandbox="danger-full-access")

            # 3. Path-escape guard: reject .. and absolute workspace_scope
            with self.assertRaises(ValueError):
                orch.create_task(title="路径越界1", goal="test", workspace_scope="../outside_escape")
            with self.assertRaises(ValueError):
                orch.create_task(title="路径越界2", goal="test", workspace_scope=os.path.abspath(os.sep))
            with self.assertRaises(ValueError):
                orch.upsert_member("esc_m", "implementation", client="codex", model="gpt-6.2-sol", workspace_scope="../../etc")

            # 4. attach_plan inherits task read-only ceiling (cannot escalate to workspace-write!)
            ro_task = orch.create_task(
                title="只读上限",
                goal="test ceiling",
                dispatch_mode="coordinator_plan",
                auto_template="none",
                sandbox="read-only",
            )
            planned = orch.attach_plan(
                ro_task["id"],
                steps=[{"member_id": "codex_author", "prompt": "try write", "read_only": False, "sandbox": "workspace-write"}],
            )
            self.assertEqual(planned["steps"][0]["sandbox"], "read-only")
            self.assertTrue(planned["steps"][0]["read_only"])

            # 5. Capability routing guard: requires_shell=True blocked on Antigravity headless nodes!
            with self.assertRaises(ValueError):
                orch.create_task(
                    title="壳能力拦截",
                    goal="run python",
                    execution_mode="custom_dag",
                    steps=[{"member_id": "agy_gemini", "prompt": "run pytest", "requires_shell": True}],
                )
            with self.assertRaises(ValueError):
                orch.create_task(
                    title="搭积木壳拦截",
                    goal="run python",
                    execution_mode="custom_dag",
                    stages=[[{"member_id": "agy_carbon_plotter", "step_type": "execution", "requires_shell": True}]],
                )
            ok_shell_t = orch.create_task(
                title="Codex可执行Shell",
                goal="run python",
                execution_mode="custom_dag",
                steps=[{"member_id": "codex_author", "prompt": "run pytest", "requires_shell": True}],
            )
            self.assertTrue(ok_shell_t["steps"][0]["requires_shell"])
            with self.assertRaises(ValueError):
                orch.reassign_step(ok_shell_t["id"], 0, "agy_gemini", reason="invalid reassign")
        finally:
            orch.stop_scheduler()

    def test_15_fail_closed_review_decoupled_signoff_and_sidecar_ledger(self) -> None:
        def fc_runner(prompt, session_id, **kwargs):
            cfg = kwargs["requested_config"]
            if "独立审查输出格式契约" in prompt:
                # Malformed prose without JSON verdict — MUST fail closed to needs_ruling, NEVER ACCEPT!
                return CodexExecutionResult(
                    session_id="rev-bad-json",
                    response_text="Looks totally fine to me, great job!",
                    partial_text="",
                    exit_code=0,
                    turn_completed=True,
                    requested_config=cfg,
                    accepted_config=kwargs["accepted_config"],
                )
            return CodexExecutionResult(
                session_id="exec-ledger-1",
                response_text="代码与统计脚本已执行完成。",
                partial_text="代码与统计脚本已执行完成。",
                exit_code=0,
                turn_completed=True,
                requested_config=cfg,
                accepted_config=kwargs["accepted_config"],
                command_logs=[
                    {"command": "python run_stats.py", "exit_code": 0, "output_excerpt": "OK " + ("X" * 3000)},
                    {"command": "python make_fig1.py", "exit_code": 0, "output_excerpt": "Saved fig1.svg"},
                ],
                changed_files=[{"path": "scripts/run_stats.py", "kind": "update"}],
                edit_after_check=True,
            )

        orch = TeamOrchestrator(
            state_file=self.state_path,
            adapter=CodexAdapter(runner_override=fc_runner),
            agy_adapter=AgyAdapter(runner_override=fc_runner),
            auto_start_scheduler=True,
        )
        try:
            t = orch.create_task(
                title="FailClosed与旁路账本",
                goal="执行统计并审查",
                dispatch_mode="coordinator_auto",
                auto_template="standard_review_loop",
                research_spec={"mode": "managed_exec", "seed": 42},
            )
            deadline = time.time() + 12.0
            cur = None
            while time.time() < deadline:
                cur = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == t["id"])
                if cur["status"] == "needs_ruling":
                    break
                time.sleep(0.06)

            self.assertIsNotNone(cur)
            self.assertEqual(cur["status"], "needs_ruling", "Malformed review MUST fail-closed to needs_ruling!")
            self.assertEqual(cur["steps"][1]["status"], "needs_ruling")
            self.assertEqual(cur["steps"][0]["repair_count"], 0, "Malformed review must NOT consume repair_count")
            self.assertEqual(cur["reviews"][-1]["verdict"], "INVALID_FORMAT")

            # Edit-After-Check caused research_signoff to be failed; human ruling override must NEVER turn it into passed!
            self.assertEqual(cur["research_signoff"]["status"], "failed")
            after_rule = orch.submit_ruling(t["id"], 1, decision="reject", reason="人工强制放行")
            self.assertEqual(after_rule["research_signoff"]["status"], "failed")
            self.assertTrue(after_rule["research_signoff"].get("human_override_noted"))

            # Verify sidecar ledger saved full command_logs while team_state.json only keeps compact summary!
            att0 = cur["steps"][0]["attempts"][0]
            self.assertIn("ledger_summary", att0)
            self.assertEqual(att0["ledger_summary"]["command_count"], 2)
            self.assertTrue(att0["ledger_summary"]["edit_after_check"])
            self.assertTrue(os.path.isfile(att0["ledger_summary"]["ledger_path"]))
            with open(self.state_path, "r", encoding="utf-8") as sf:
                raw_state_txt = sf.read()
            self.assertNotIn("X" * 1000, raw_state_txt, "Full command stdout must stay in sidecar JSONL, not team_state.json")

            arts = orch.get_task_artifacts(t["id"])
            self.assertEqual(len(arts["attempts_ledger"][0]["command_logs"]), 2)
        finally:
            orch.stop_scheduler()

    def test_16_lock_free_verifier_script_tamper_readonly_inputs_and_figure_qa(self) -> None:
        import struct
        from figure_qa import inspect_tiff, sha256_file, verify_figure_manifest
        from research_verifier import run_acceptance_checks, snapshot_readonly_inputs

        def _make_minimal_tiff(path: str, width: int, height: int, dpi_x: int, dpi_y: int) -> None:
            # Pure-stdlib minimal valid little-endian TIFF with ImageWidth, ImageLength, XResolution, YResolution, ResolutionUnit=2 (inch)
            # Header (8 bytes) + IFD at offset 8: count=5 (2B) + 5*12 (60B) + next_ifd=0 (4B) = 74 bytes -> rationals at 74 & 82
            entries = [
                (256, 4, 1, width),       # ImageWidth (LONG)
                (257, 4, 1, height),      # ImageLength (LONG)
                (282, 5, 1, 74),          # XResolution (RATIONAL @ 74)
                (283, 5, 1, 82),          # YResolution (RATIONAL @ 82)
                (296, 3, 1, 2),           # ResolutionUnit = 2 (inch)
            ]
            buf = bytearray(b"II" + struct.pack("<HI", 42, 8))
            buf += struct.pack("<H", len(entries))
            for tag, typ, cnt, val in entries:
                buf += struct.pack("<HHII", tag, typ, cnt, val)
            buf += struct.pack("<I", 0)
            buf += struct.pack("<II", dpi_x, 1)
            buf += struct.pack("<II", dpi_y, 1)
            with open(path, "wb") as f:
                f.write(buf)

        def _make_minimal_png(path: str, width: int, height: int, dpi: int = 300) -> None:
            import zlib as _zlib
            def _chunk(ctype: bytes, payload: bytes) -> bytes:
                return struct.pack(">I", len(payload)) + ctype + payload + struct.pack(">I", _zlib.crc32(ctype + payload) & 0xFFFFFFFF)
            ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
            ppm = int(round(dpi / 0.0254))
            phys = struct.pack(">IIB", ppm, ppm, 1)
            row_white = b"\x00" + (b"\xff\xff\xff" * width)
            row_ink = b"\x00" + (b"\x10\x20\x30" * min(width, 8)) + (b"\xff\xff\xff" * max(0, width - 8))
            raw = row_ink + (row_white * max(0, height - 1))
            idat = _zlib.compress(raw, level=1)
            with open(path, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"pHYs", phys) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b""))

        proj = os.path.join(self.tmp_dir, f"sci_proj_{time.time_ns()}")
        os.makedirs(os.path.join(proj, "raw"), exist_ok=True)
        os.makedirs(os.path.join(proj, "scripts"), exist_ok=True)
        os.makedirs(os.path.join(proj, "figures"), exist_ok=True)

        raw_csv = os.path.join(proj, "raw", "cohort.csv")
        with open(raw_csv, "w", encoding="utf-8", newline="") as f:
            f.write("id,group,value\n1,Control,10.0\n2,Control,12.0\n3,Treated,20.0\n4,Treated,22.0\n")

        src_csv = os.path.join(proj, "figures", "fig1_source.csv")
        with open(src_csv, "w", encoding="utf-8", newline="") as f:
            f.write("group,n,mean_value,p_raw,p_adj\nControl,24,11.0,0.03,0.08\nTreated,24,21.0,0.03,0.08\n")

        svg_p = os.path.join(proj, "figures", "fig1.svg")
        with open(svg_p, "w", encoding="utf-8") as f:
            f.write('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 252 180"><path d="M0 0 L10 10"/><text font-size="8pt">Panel A</text></svg>')
        png_p = os.path.join(proj, "figures", "fig1.png")
        _make_minimal_png(png_p, 1050, 750)
        good_tif = os.path.join(proj, "figures", "fig1.tiff")
        _make_minimal_tiff(good_tif, 2102, 1500, 600, 600)  # 2102 / 600 * 25.4 = 88.98 mm!
        fake_tif = os.path.join(proj, "figures", "fig1_forged.tiff")
        _make_minimal_tiff(fake_tif, 200, 150, 600, 600)    # Forged 600 DPI tag -> only 8.47 mm wide!

        tif_meta = inspect_tiff(good_tif)
        self.assertTrue(tif_meta["ok"])
        self.assertEqual(tif_meta["dpi_x"], 600.0)
        self.assertAlmostEqual(tif_meta["physical_width_mm"], 88.98, delta=0.2)

        # 1. Forged 600 DPI TIFF + swapped group labels + raw P vs p_adj misuse -> ALL caught!
        bad_mf = os.path.join(proj, "figures", "bad_manifest.json")
        with open(bad_mf, "w", encoding="utf-8") as f:
            json.dump({
                "figure_id": "fig1_bad",
                "target_width_mm": 89.0,
                "min_dpi": 600,
                "exports": {"svg": "figures/fig1.svg", "tiff": "figures/fig1_forged.tiff"},
                "panel_sources": [{
                    "panel_id": "A",
                    "source_table": "figures/fig1_source.csv",
                    "key_col": "group",
                    "value_col": "mean_value",
                    "n_col": "n",
                    "p_col": "p_adj",
                    "require_adjusted_p": True,
                    "expected_rows": [
                        {"key": "Control", "plotted_value": 21.0, "plotted_n": 24, "plotted_p": 0.03},  # SWAPPED value + raw P!
                        {"key": "Treated", "plotted_value": 11.0, "plotted_n": 24, "plotted_p": 0.03},
                    ],
                }],
            }, f)
        bad_qa = verify_figure_manifest(bad_mf, proj)
        self.assertFalse(bad_qa["ok"])
        joined_b = "\n".join(bad_qa["blocking"])
        self.assertIn("伪造高 DPI", joined_b)
        self.assertIn("标签互换", joined_b)
        self.assertIn("原始 P 值", joined_b)

        # 2. Valid manifest + intact readonly_inputs + hash-locked script -> FULL PASS!
        good_mf = os.path.join(proj, "figures", "fig1_manifest.json")
        with open(good_mf, "w", encoding="utf-8") as f:
            json.dump({
                "figure_id": "fig1",
                "title": "Cohort Comparison",
                "target_width_mm": 89.0,
                "min_dpi": 600,
                "exports": {"svg": "figures/fig1.svg", "png": "figures/fig1.png", "tiff": "figures/fig1.tiff"},
                "panel_sources": [{
                    "panel_id": "A",
                    "source_table": "figures/fig1_source.csv",
                    "key_col": "group",
                    "value_col": "mean_value",
                    "n_col": "n",
                    "p_col": "p_adj",
                    "require_adjusted_p": True,
                    "expected_rows": [
                        {"key": "Control", "plotted_value": 11.0, "plotted_n": 24, "plotted_p": 0.08},
                        {"key": "Treated", "plotted_value": 21.0, "plotted_n": 24, "plotted_p": 0.08},
                    ],
                }],
            }, f)

        chk_script = os.path.join(proj, "scripts", "check.py")
        with open(chk_script, "w", encoding="utf-8") as f:
            f.write("import sys; print('ACCEPTANCE_OK'); sys.exit(0)\n")
        chk_sha = sha256_file(chk_script)

        before_snap = snapshot_readonly_inputs(proj, ["raw"])
        rspec = {
            "mode": "managed_exec",
            "readonly_inputs": ["raw"],
            "acceptance_checks": [{"check_id": "unit_check", "script_path": "scripts/check.py", "expected_sha256": chk_sha}],
            "figure_manifests": ["figures/fig1_manifest.json"],
        }
        rep_ok = run_acceptance_checks(proj, rspec, before_inputs_snapshot=before_snap)
        self.assertTrue(rep_ok["ok"], f"Expected full pass, got blocking: {rep_ok['blocking']}")
        self.assertEqual(rep_ok["status"], "passed")

        # 3. Tamper check script -> SHA-256 mismatch refuses execution!
        with open(chk_script, "a", encoding="utf-8") as f:
            f.write("# tampered\n")
        rep_tamp_script = run_acceptance_checks(proj, rspec, before_inputs_snapshot=before_snap)
        self.assertFalse(rep_tamp_script["ok"])
        self.assertTrue(any("篡改" in b for b in rep_tamp_script["blocking"]))

        # 4. Tamper readonly raw data -> caught by SHA-256 snapshot!
        with open(raw_csv, "a", encoding="utf-8") as f:
            f.write("5,Treated,99.0\n")
        rep_tamp_raw = run_acceptance_checks(proj, {"readonly_inputs": ["raw"]}, before_inputs_snapshot=before_snap)
        self.assertFalse(rep_tamp_raw["ok"])
        self.assertTrue(any("原始输入文件被篡改" in b for b in rep_tamp_raw["blocking"]))

    def test_17_http_csrf_raw_file_guard_csv_wide_truncation_and_strict_artifact_api(self) -> None:
        import threading
        import urllib.error
        import urllib.parse
        import urllib.request
        from http.server import ThreadingHTTPServer
        import serve_sidebar

        proj = os.path.join(self.tmp_dir, f"http_proj_{time.time_ns()}")
        os.makedirs(os.path.join(proj, "other"), exist_ok=True)
        os.makedirs(os.path.join(proj, "figures"), exist_ok=True)
        # Wide CSV: 65 columns x 105 rows (column 25 is p_adj!)
        cols = [f"c{i}" for i in range(1, 25)] + ["p_adj"] + [f"c{i}" for i in range(26, 66)]
        wide_csv = os.path.join(proj, "wide_stats.csv")
        with open(wide_csv, "w", encoding="utf-8", newline="") as f:
            f.write(",".join(cols) + "\n")
            for r in range(105):
                f.write(",".join(f"v{r}_{i}" for i in range(65)) + "\n")
        dup_csv = os.path.join(proj, "other", "dup.csv")
        with open(dup_csv, "w", encoding="utf-8") as f:
            f.write("a,b\n1,2\n")
        svg_p = os.path.join(proj, "figures", "panel_a.svg")
        with open(svg_p, "w", encoding="utf-8") as f:
            f.write('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 80"><text font-size="7pt">A</text></svg>')

        orch = TeamOrchestrator(
            state_file=self.state_path,
            adapter=CodexAdapter(runner_override=lambda *a, **k: None),
            agy_adapter=AgyAdapter(runner_override=lambda *a, **k: None),
            auto_start_scheduler=False,
        )
        orch.switch_project(proj)
        task = orch.create_task(title="HTTP测试任务", goal="inspect figures", dispatch_mode="single_member")

        orig_orch = serve_sidebar.ORCHESTRATOR
        serve_sidebar.ORCHESTRATOR = orch
        srv = ThreadingHTTPServer(("127.0.0.1", 0), serve_sidebar.SidebarHandler)
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        base = f"http://127.0.0.1:{port}"
        try:
            # 1. Health check server_rev >= 12
            with urllib.request.urlopen(f"{base}/api/health", timeout=5) as resp:
                h = json.loads(resp.read().decode("utf-8"))
                self.assertGreaterEqual(h["server_rev"], 12)

            # 2. Foreign Origin POST -> 403 CSRF blocked!
            req_csrf = urllib.request.Request(
                f"{base}/api/pause",
                data=b'{"paused":true}',
                headers={"Origin": "https://evil.attacker.example", "Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req_csrf, timeout=5)
            self.assertEqual(cm.exception.code, 403)

            # 3. Non-JSON Content-Type with Origin -> 403 CSRF blocked!
            req_ct = urllib.request.Request(
                f"{base}/api/pause",
                data=b"paused=true",
                headers={"Origin": base, "Content-Type": "text/plain"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as cm2:
                urllib.request.urlopen(req_ct, timeout=5)
            self.assertEqual(cm2.exception.code, 403)

            # 4. Valid loopback JSON POST -> 200
            req_ok = urllib.request.Request(
                f"{base}/api/pause",
                data=b'{"paused":false}',
                headers={"Origin": base, "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req_ok, timeout=5) as resp:
                self.assertEqual(resp.status, 200)

            # 5. /api/fs/raw_file outside allowed roots -> 400 blocked!
            outside_p = os.path.join(os.path.dirname(os.path.abspath(self.tmp_dir)), "outside_secret_probe.txt")
            with open(outside_p, "w", encoding="utf-8") as f:
                f.write("secret")
            try:
                q_out = urllib.parse.quote(outside_p)
                with self.assertRaises(urllib.error.HTTPError) as cm3:
                    urllib.request.urlopen(f"{base}/api/fs/raw_file?path={q_out}", timeout=5)
                self.assertEqual(cm3.exception.code, 400)
            finally:
                if os.path.exists(outside_p):
                    os.remove(outside_p)

            # 6. Wide CSV preview: 60 cols shown (includes 25th col p_adj!), accurate col_truncated & row_truncated
            with urllib.request.urlopen(f"{base}/api/fs/preview_file?target=wide_stats.csv&task_id={task['id']}&strict=1", timeout=5) as resp:
                pv = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(pv["preview_type"], "table")
                self.assertEqual(pv["total_cols"], 65)
                self.assertEqual(pv["shown_cols"], 60)
                self.assertTrue(pv["col_truncated"])
                self.assertTrue(pv["row_truncated"])
                self.assertIn("p_adj", pv["rows"][0])

            # 7. strict=1 disables silent basename fallback!
            with self.assertRaises(urllib.error.HTTPError) as cm4:
                urllib.request.urlopen(f"{base}/api/fs/preview_file?target=missing_dir/dup.csv&task_id={task['id']}&strict=1", timeout=5)
            self.assertEqual(cm4.exception.code, 400)
            with urllib.request.urlopen(f"{base}/api/fs/preview_file?target=missing_dir/dup.csv&task_id={task['id']}", timeout=5) as resp:
                pv_fb = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(pv_fb["name"], "dup.csv")

            # 8. /api/tasks/artifacts discovers figures/panel_a.svg & wide_stats.csv!
            with urllib.request.urlopen(f"{base}/api/tasks/artifacts?task_id={task['id']}", timeout=5) as resp:
                arts = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(arts["ok"])
                self.assertTrue(any(fg["stem"] == "panel_a" for fg in arts["figures"]))
                self.assertTrue(any(tb["name"] == "wide_stats.csv" for tb in arts["tables"]))
        finally:
            srv.shutdown()
            srv.server_close()
            serve_sidebar.ORCHESTRATOR = orig_orch
            orch.stop_scheduler()

    def test_18_quota_exhaustion_auto_backup_and_manual_role_preserving_backup(self) -> None:
        seen_models = []

        def quota_runner(prompt, session_id, **kwargs):
            cfg = kwargs["requested_config"]
            mdl = cfg.get("model") or ""
            seen_models.append(mdl)
            if mdl == "gemini-4-neon":
                return CodexExecutionResult(
                    session_id=None,
                    response_text="",
                    partial_text="已读取前两份表...",
                    exit_code=1,
                    turn_completed=False,
                    error="HTTP 429 RESOURCE_EXHAUSTED: model quota exceeded for gemini-4-neon",
                    stderr_lines=["Error: insufficient_quota"],
                    requested_config=cfg,
                    accepted_config=kwargs["accepted_config"],
                )
            if mdl == "gpt-6.2-sol":
                return CodexExecutionResult(
                    session_id=None,
                    response_text="",
                    partial_text="",
                    exit_code=1,
                    turn_completed=False,
                    error="rate_limit_exceeded: usage limit reached for gpt-6.2-sol",
                    requested_config=cfg,
                    accepted_config=kwargs["accepted_config"],
                )
            return CodexExecutionResult(
                session_id=f"bk-sess-{mdl}",
                response_text=f"由同角色跨模型替补 ({mdl}) 接力完成交付！",
                partial_text="",
                exit_code=0,
                turn_completed=True,
                requested_config=cfg,
                accepted_config=kwargs["accepted_config"],
            )

        orch = TeamOrchestrator(
            state_file=self.state_path,
            adapter=CodexAdapter(runner_override=quota_runner),
            agy_adapter=AgyAdapter(runner_override=quota_runner),
            auto_start_scheduler=True,
        )
        try:
            orig_pi = orch.store.snapshot()["members"]["agy_gemini"]

            # 1. Proactive manual backup (cross-engine!) inherits 100% role/specialty/persona/tags on a different model!
            man = orch.spawn_quota_backup_member("agy_gemini", prefer_cross_engine=True, reason="Gemini 4 Neon 额度仅剩 10%")
            bk_man = man["member"]
            self.assertFalse(man["reused"])
            self.assertEqual(bk_man["backup_of"], "agy_gemini")
            self.assertEqual(bk_man["client"], "codex")
            self.assertNotIn("terra", bk_man["model"].lower())
            self.assertEqual(bk_man["role"], orig_pi["role"])
            self.assertEqual(bk_man["specialty"], orig_pi["specialty"])
            self.assertEqual(bk_man["persona_prompt"], orig_pi["persona_prompt"])
            self.assertEqual(bk_man["tags"], orig_pi["tags"])
            self.assertEqual(bk_man["grounding_mode"], orig_pi["grounding_mode"])

            # Calling again for the exact same (source, client, model) reuses idle backup cleanly!
            man2 = orch.spawn_quota_backup_member("agy_gemini", new_client=bk_man["client"], new_model=bk_man["model"])
            self.assertTrue(man2["reused"])
            self.assertEqual(man2["member"]["id"], bk_man["id"])

            # Selecting the exact same (client, model) as source MUST be rejected!
            with self.assertRaises(ValueError):
                orch.spawn_quota_backup_member("agy_gemini", new_client="antigravity", new_model="gemini-4-neon")

            # 2. Automatic quota failover on Antigravity (gemini-4-neon -> different model, same role, auto completes!)
            t_agy = orch.create_task(title="自动额度替补测试", goal="科研假说整合", execution_mode="single_member", target_member_id="agy_gemini")
            deadline = time.time() + 12.0
            cur = None
            while time.time() < deadline:
                cur = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == t_agy["id"])
                if cur["status"] in ("succeeded", "failed"):
                    break
                time.sleep(0.06)
            self.assertEqual(cur["status"], "succeeded", f"Expected auto-failover to succeed, got {cur}")
            new_mid = cur["steps"][0]["member_id"]
            self.assertNotEqual(new_mid, "agy_gemini")
            new_mem = orch.store.snapshot()["members"][new_mid]
            self.assertEqual(new_mem["backup_of"], "agy_gemini")
            self.assertEqual(new_mem["specialty"], orig_pi["specialty"])
            self.assertEqual(new_mem["persona_prompt"], orig_pi["persona_prompt"])
            self.assertNotEqual(new_mem["model"], "gemini-4-neon")
            self.assertTrue(any(ev.get("type") == "quota.auto_backup" for ev in cur.get("recent_events", [])))

            # 3. Shell-requiring Codex step (gpt-6.2-sol) hitting rate limit MUST fail over to Codex (gpt-6.2-astra), NEVER Antigravity!
            t_sh = orch.create_task(
                title="Shell步骤自动额度替补",
                goal="run pytest",
                execution_mode="custom_dag",
                steps=[{"member_id": "codex_author", "prompt": "run pytest", "requires_shell": True}],
            )
            deadline = time.time() + 12.0
            cur_sh = None
            while time.time() < deadline:
                cur_sh = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == t_sh["id"])
                if cur_sh["status"] in ("succeeded", "failed"):
                    break
                time.sleep(0.06)
            self.assertEqual(cur_sh["status"], "succeeded", f"Expected Codex shell failover to succeed, got {cur_sh}")
            sh_mid = cur_sh["steps"][0]["member_id"]
            sh_mem = orch.store.snapshot()["members"][sh_mid]
            self.assertEqual(sh_mem["client"], "codex")
            self.assertNotEqual(sh_mem["model"], "gpt-6.2-sol")
            self.assertNotIn("terra", sh_mem["model"].lower())
        finally:
            orch.stop_scheduler()

    def test_19_openscience_playbooks_provenance_dag_and_experiment_tracker(self) -> None:
        from team_state_manager import SPECIALIST_TEMPLATES

        self.assertGreaterEqual(len(SPECIALIST_TEMPLATES), 15)
        captured = []

        def os_runner(prompt, session_id, **kwargs):
            captured.append(prompt)
            out_txt = (
                "基线与优化实验已完成。\n"
                "```experiment_run\n"
                '{"run_id":"exp_001","is_baseline":true,"hypothesis":"Base Cox","metric_name":"c_index","metric_value":0.742,"status":"baseline"}\n'
                "```\n"
                "```experiment_run\n"
                '{"run_id":"exp_002","hypothesis":"Add spatial interaction","metric_name":"c_index","metric_value":0.786,"status":"keep"}\n'
                "```\n"
                "```provenance_record\n"
                '{"kind":"claim","label":"Spatial term improved C-index by +0.044 (p_adj=0.008)"}\n'
                "```"
            )
            return CodexExecutionResult(
                session_id="os-prov-s1",
                response_text=out_txt,
                partial_text=out_txt,
                exit_code=0,
                turn_completed=True,
                requested_config=kwargs["requested_config"],
                accepted_config=kwargs["accepted_config"],
                command_logs=[{"command": "python run_ablation.py", "exit_code": 0, "output_excerpt": "c_index=0.786"}],
                changed_files=[{"path": "results/ablation.csv", "kind": "add"}],
            )

        orch = TeamOrchestrator(
            state_file=self.state_path,
            adapter=CodexAdapter(runner_override=os_runner),
            agy_adapter=AgyAdapter(runner_override=os_runner),
            auto_start_scheduler=True,
        )
        try:
            # Goal triggers statistical_rigor + publication_figures_schematics automatically!
            t = orch.create_task(
                title="OpenScience融合测试",
                goal="用 ggplot2 绘制队列生存曲线并报告 FDR 校正 P 值与 95% CI",
                execution_mode="single_member",
                target_member_id="codex_reviewer",
            )
            self.assertIn("statistical_rigor", t["openscience_playbooks"])
            self.assertIn("publication_figures_schematics", t["openscience_playbooks"])

            deadline = time.time() + 10.0
            cur = None
            while time.time() < deadline:
                cur = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == t["id"])
                if cur["status"] == "succeeded":
                    break
                time.sleep(0.06)
            self.assertEqual(cur["status"], "succeeded", f"test_29 roundtable failed: {cur.get('blocking_reason')} / steps={[ (s['step_index'], s['status'], s.get('blocking_reason')) for s in cur['steps'] ]}")
            # Verify OpenScience Playbooks + gpt-astra.txt dialect + science.txt honesty injected into prompt!
            self.assertTrue(captured)
            self.assertIn("statistical-conventions", captured[0])
            self.assertIn("gpt-astra.txt", captured[0])
            self.assertIn("非破坏性重跑", captured[0])

            # Verify content-addressed Provenance DAG & Experiment Runs (with delta_vs_baseline = +0.044!)
            prov = orch.get_task_provenance(t["id"])
            kinds = {n["kind"] for n in prov["nodes"]}
            self.assertIn("run", kinds)
            self.assertIn("artifact", kinds)
            self.assertIn("claim", kinds)
            self.assertGreaterEqual(prov["edge_count"], 2)
            exps = prov["experiments"]
            self.assertEqual(len(exps), 2)
            self.assertTrue(exps[0]["is_baseline"])
            self.assertAlmostEqual(exps[1]["delta_vs_baseline"], 0.044, places=4)
        finally:
            orch.stop_scheduler()

    def test_20_live_seat_hot_swap_and_bound_quota_backup_failover(self) -> None:
        import threading

        r1_started = threading.Event()
        release_r1 = threading.Event()
        seen_turns = []

        def swap_runner(prompt, session_id, **kwargs):
            cfg = kwargs["requested_config"]
            mdl = cfg.get("model") or ""
            seen_turns.append((mdl, prompt[:40]))
            if "SIMULATE_BOUND_QUOTA" in prompt and mdl == "gemini-4-neon":
                return CodexExecutionResult(
                    session_id=None,
                    response_text="",
                    partial_text="",
                    exit_code=1,
                    turn_completed=False,
                    error="HTTP 429 quota exceeded",
                    requested_config=cfg,
                    accepted_config=kwargs["accepted_config"],
                )
            if "WAIT_MID_PROCESS" in prompt and mdl == "gemini-4-neon":
                r1_started.set()
                release_r1.wait(timeout=3.0)
                return CodexExecutionResult(
                    session_id="slow-r1",
                    response_text="第1轮完成（原席位）",
                    partial_text="第1轮完成（原席位）",
                    exit_code=0,
                    turn_completed=True,
                    requested_config=cfg,
                    accepted_config=kwargs["accepted_config"],
                )
            return CodexExecutionResult(
                session_id=f"sw-{mdl}-{time.time_ns()}",
                response_text=f"由 {mdl} 完成交付",
                partial_text="",
                exit_code=0,
                turn_completed=True,
                requested_config=cfg,
                accepted_config=kwargs["accepted_config"],
            )

        orch = TeamOrchestrator(
            state_file=self.state_path,
            adapter=CodexAdapter(runner_override=swap_runner),
            agy_adapter=AgyAdapter(runner_override=swap_runner),
            auto_start_scheduler=True,
        )
        try:
            # 1. Explicit quota backup binding takes Priority #1 on 429 AND migrates future pending steps!
            b_res = orch.bind_quota_backup("agy_gemini", backup_client="codex", backup_model="gpt-6.2-astra", backup_effort="high")
            self.assertEqual(b_res["quota_backup"]["model"], "gpt-6.2-astra")

            t_qb = orch.create_task(
                title="绑定替补优先接力",
                goal="SIMULATE_BOUND_QUOTA",
                execution_mode="custom_dag",
                steps=[
                    {"title": "S0", "member_id": "agy_gemini", "prompt": "SIMULATE_BOUND_QUOTA step0", "depends_on": []},
                    {"title": "S1", "member_id": "agy_gemini", "prompt": "follow step1", "depends_on": [0]},
                ],
            )
            deadline = time.time() + 12.0
            cur_qb = None
            while time.time() < deadline:
                cur_qb = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == t_qb["id"])
                if cur_qb["status"] in ("succeeded", "failed"):
                    break
                time.sleep(0.06)
            self.assertEqual(cur_qb["status"], "succeeded")
            # Both S0 and S1 must have migrated to the bound gpt-6.2-astra backup!
            m0 = orch.store.snapshot()["members"][cur_qb["steps"][0]["member_id"]]
            m1 = orch.store.snapshot()["members"][cur_qb["steps"][1]["member_id"]]
            self.assertEqual(m0["model"], "gpt-6.2-astra")
            self.assertEqual(m1["model"], "gpt-6.2-astra")

            # 2. One-click auto-bind all roster members (zero terra!)
            ab = orch.auto_bind_all_quota_backups(prefer_cross_engine=False, overwrite=False)
            self.assertGreaterEqual(ab["count"], 4)
            for item in ab["bound"]:
                self.assertNotIn("terra", item["quota_backup"]["model"].lower())

            # 3. Mid-process hot-swap (next_round while step 0 is running!)
            t_mid = orch.create_task(
                title="进程中热换人",
                goal="WAIT_MID_PROCESS",
                execution_mode="roundtable",
                host_member_id="agy_gemini",
                participants=["agy_gemini", "codex_author"],
                discussion_rounds=2,
            )
            self.assertTrue(r1_started.wait(timeout=5.0), "Round 1 must start running")
            sw_mid = orch.swap_task_seat(
                task_id=t_mid["id"],
                old_member_id="agy_gemini",
                new_member_id="agy_carbon_plotter",
                abort_running=False,
                bind_as_quota_backup=True,
                reason="进程中提前换上 Gemini 4 Carbon",
            )
            self.assertEqual(sw_mid["applied"], "next_round")
            self.assertIn("agy_carbon_plotter", sw_mid["task"]["mode_meta"]["participants"])
            self.assertEqual(sw_mid["task"]["mode_meta"]["host_member_id"], "agy_carbon_plotter")
            release_r1.set()

            deadline = time.time() + 15.0
            cur_mid = None
            while time.time() < deadline:
                cur_mid = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == t_mid["id"])
                if cur_mid["status"] == "succeeded":
                    break
                time.sleep(0.06)
            self.assertEqual(cur_mid["status"], "succeeded")
            # Round 2 + Synthesis steps must have been executed by agy_carbon_plotter!
            self.assertEqual(cur_mid["steps"][-1]["member_id"], "agy_carbon_plotter")

            # 4. Completed-task anytime swap (with same-role cross-model backup + fresh turn) & add_task_seat!
            sw_done = orch.swap_task_seat(
                task_id=t_mid["id"],
                old_member_id="codex_author",
                spawn_backup_client="antigravity",
                spawn_backup_model="claude-opus-5-5",
                trigger_fresh_turn=True,
                bind_as_quota_backup=True,
                reason="完成后同角色换 Claude Opus 5.5 复核",
            )
            self.assertIsNotNone(sw_done["spawned_backup"])
            self.assertEqual(sw_done["spawned_backup"]["model"], "claude-opus-5-5")
            deadline = time.time() + 10.0
            while time.time() < deadline:
                cur_after = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == t_mid["id"])
                if cur_after["status"] == "succeeded":
                    break
                time.sleep(0.06)
            self.assertEqual(cur_after["status"], "succeeded")

            add_res = orch.add_task_seat(t_mid["id"], "agy_claude_reviewer", trigger_turn=False)
            self.assertIn("agy_claude_reviewer", add_res["task"]["mode_meta"]["participants"])
        finally:
            release_r1.set()
            orch.stop_scheduler()

    def test_21_custom_specialist_count_and_high_concurrency_up_to_12(self) -> None:
        orch = TeamOrchestrator(
            state_file=self.state_path,
            adapter=CodexAdapter(runner_override=lambda *a, **k: None),
            agy_adapter=AgyAdapter(runner_override=lambda *a, **k: None),
            auto_start_scheduler=False,
        )
        try:
            # 1. Parallel concurrency cap supports 1..12 cleanly
            orch.update_binding(max_concurrency=10)
            self.assertEqual(orch.store.snapshot()["max_concurrency"], 10)
            orch.update_binding(max_concurrency=99)
            self.assertEqual(orch.store.snapshot()["max_concurrency"], 12)

            # 2. Roundtable supports arbitrary custom participant count (e.g. all 6 default seats!)
            all_ids = list(orch.store.snapshot()["members"].keys())
            self.assertGreaterEqual(len(all_ids), 6)
            t_rt = orch.create_task(
                title="6人圆桌自定义人数",
                goal="全员协作评审",
                execution_mode="roundtable",
                participants=all_ids,
                host_member_id=all_ids[0],
                discussion_rounds=1,
            )
            self.assertEqual(len(t_rt["mode_meta"]["participants"]), len(all_ids))
            # 6 blind R1 discussion steps + 1 final synthesis step = 7 steps
            self.assertEqual(len(t_rt["steps"]), len(all_ids) + 1)

            # 3. Chief Dispatch respects custom chief_max_members=10 in prompt & plan expansion
            t_ch = orch.create_task(
                title="主帅自定义点将人数",
                goal="多方向联合调研",
                execution_mode="chief_dispatch",
                chief_member_id="agy_gemini",
                chief_max_members=10,
            )
            self.assertEqual(t_ch["mode_meta"]["chief_max_members"], 10)
            self.assertIn("1~10 名成员", t_ch["steps"][0]["prompt"])
        finally:
            orch.stop_scheduler()

    def test_22_v52_figure_qa_adversarial_signoff_recovery_and_advisory_auto_qa(self) -> None:
        import re
        import struct
        import zlib
        from figure_qa import inspect_png, inspect_svg, verify_figure_manifest
        import research_verifier as rv

        def _make_png(path: str, width: int, height: int, dpi: int = 300, ink_row: int | None = 0, ink_px: int = 10) -> None:
            def _chunk(ctype: bytes, payload: bytes) -> bytes:
                return struct.pack(">I", len(payload)) + ctype + payload + struct.pack(">I", zlib.crc32(ctype + payload) & 0xFFFFFFFF)
            ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
            ppm = int(round(dpi / 0.0254))
            phys = struct.pack(">IIB", ppm, ppm, 1)
            row_w = b"\x00" + (b"\xff\xff\xff" * width)
            if ink_row is None:
                raw = row_w * height
            else:
                ink_n = min(width, max(1, ink_px))
                row_i = b"\x00" + (b"\x00\x00\x00" * ink_n) + (b"\xff\xff\xff" * (width - ink_n))
                raw = (row_w * ink_row) + row_i + (row_w * (height - ink_row - 1))
            with open(path, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"pHYs", phys) + _chunk(b"IDAT", zlib.compress(raw, 1)) + _chunk(b"IEND", b""))

        def _make_tiff(path: str, width: int, height: int, dpi: int = 600) -> None:
            entries = [
                (256, 4, 1, width), (257, 4, 1, height),
                (282, 5, 1, 74), (283, 5, 1, 82), (296, 3, 1, 2),
            ]
            buf = bytearray(b"II" + struct.pack("<HI", 42, 8) + struct.pack("<H", len(entries)))
            for tag, typ, cnt, val in entries:
                buf += struct.pack("<HHII", tag, typ, cnt, val)
            buf += struct.pack("<III", 0, dpi, 1) + struct.pack("<II", dpi, 1)
            with open(path, "wb") as f:
                f.write(buf)

        proj = os.path.join(self.tmp_dir, f"v52_fig_proj_{time.time_ns()}")
        os.makedirs(os.path.join(proj, "figures", "fig1"), exist_ok=True)
        os.makedirs(os.path.join(proj, "raw"), exist_ok=True)
        raw_p = os.path.join(proj, "raw", "locked.csv")
        with open(raw_p, "w", encoding="utf-8") as f:
            f.write("x,y\n1,2\n")

        # 1. PNG adversarial: 33B truncated, 1024x1024 solid blank, 1024x1024 sparse ink at row 701
        trunc_png = os.path.join(proj, "figures", "trunc.png")
        with open(trunc_png, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">IIBBBBB", 100, 100, 8, 2, 0, 0, 0) + b"\x00\x00\x00\x00")
        self.assertFalse(inspect_png(trunc_png)["ok"])

        blank_png = os.path.join(proj, "figures", "blank.png")
        _make_png(blank_png, 1024, 1024, dpi=300, ink_row=None)
        blank_meta = inspect_png(blank_png)
        self.assertTrue(blank_meta["is_blank"])
        self.assertFalse(blank_meta["ok"])

        sparse_png = os.path.join(proj, "figures", "sparse.png")
        _make_png(sparse_png, 1024, 1024, dpi=300, ink_row=701, ink_px=200)
        sparse_meta = inspect_png(sparse_png)
        self.assertFalse(sparse_meta["is_blank"])
        self.assertTrue(sparse_meta["ok"])

        # 2. SVG 4-stage font calibration fixtures
        svg_mpl = os.path.join(proj, "figures", "mpl_pt.svg")
        with open(svg_mpl, "w", encoding="utf-8") as f:
            f.write('<svg xmlns="http://www.w3.org/2000/svg" width="252.28pt" height="113.38pt" viewBox="0 0 252.28 113.38"><text style="font: 6px sans-serif">Axis</text></svg>')
        self.assertAlmostEqual(inspect_svg(svg_mpl, target_width_mm=89.0)["effective_min_font_pt"], 6.0, delta=0.1)

        svg_px = os.path.join(proj, "figures", "root_px.svg")
        with open(svg_px, "w", encoding="utf-8") as f:
            f.write('<svg xmlns="http://www.w3.org/2000/svg" width="252.28px" height="113.38px" viewBox="0 0 252.28 113.38"><text style="font-size: 6px">Axis</text></svg>')
        self.assertAlmostEqual(inspect_svg(svg_px, target_width_mm=89.0)["effective_min_font_pt"], 4.5, delta=0.1)

        svg_now = os.path.join(proj, "figures", "no_width.svg")
        with open(svg_now, "w", encoding="utf-8") as f:
            f.write('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 252 180"><text font-size="8pt">Label</text></svg>')
        self.assertAlmostEqual(inspect_svg(svg_now, target_width_mm=89.0)["effective_min_font_pt"], 8.0, delta=0.1)

        svg_ctm = os.path.join(proj, "figures", "ctm_scale.svg")
        with open(svg_ctm, "w", encoding="utf-8") as f:
            f.write('<svg xmlns="http://www.w3.org/2000/svg" width="252.28pt" height="113.38pt" viewBox="0 0 252.28 113.38"><g transform="scale(0.1)"><text font-size="60px">Scaled</text></g></svg>')
        self.assertAlmostEqual(inspect_svg(svg_ctm, target_width_mm=89.0)["effective_min_font_pt"], 6.0, delta=0.1)

        svg_outlined = os.path.join(proj, "figures", "outlined.svg")
        with open(svg_outlined, "w", encoding="utf-8") as f:
            f.write('<svg xmlns="http://www.w3.org/2000/svg" width="252pt" height="120pt" viewBox="0 0 252 120"><defs><path id="DejaVuSans-41" d="M0 0 L5 10"/></defs><path d="M0 0 L10 10"/></svg>')
        out_meta = inspect_svg(svg_outlined, target_width_mm=89.0)
        self.assertIsNone(out_meta["min_font_pt"])
        self.assertEqual(out_meta["font_status"], "no_text_nodes")
        self.assertTrue(any("svg.fonttype" in w for w in out_meta["warnings"]))

        svg_geom = os.path.join(proj, "figures", "geom_only.svg")
        with open(svg_geom, "w", encoding="utf-8") as f:
            f.write('<svg xmlns="http://www.w3.org/2000/svg" width="252pt" height="120pt" viewBox="0 0 252 120"><path d="M0 0 L10 10"/></svg>')
        self.assertEqual(inspect_svg(svg_geom, target_width_mm=89.0)["font_status"], "no_text")

        # 3. Malformed exports never crash & fail closed
        for bad_exp in ([{}], {"svg": None}, []):
            bad_m = os.path.join(proj, "figures", "malformed_manifest.json")
            with open(bad_m, "w", encoding="utf-8") as f:
                json.dump({"figure_id": "bad", "exports": bad_exp}, f)
            rep = verify_figure_manifest(bad_m, proj)
            self.assertFalse(rep["ok"])
            self.assertEqual(rep["status"], "failed")

        # 4. CSV adversarial checks: duplicate key, N=24 vs 24.49, P crossing 0.05, unadjusted_p, composite key_cols
        dup_csv = os.path.join(proj, "figures", "dup.csv")
        with open(dup_csv, "w", encoding="utf-8") as f:
            f.write("cohort,group,n,val,unadjusted_p,p_adj\nC1,Ctrl,24,10.0,0.04999,0.05001\nC2,Ctrl,24.49,20.0,0.04999,0.05001\n")
        dup_mf = os.path.join(proj, "figures", "dup_manifest.json")
        with open(dup_mf, "w", encoding="utf-8") as f:
            json.dump({
                "figure_id": "dup_test",
                "target_width_mm": 89.0,
                "exports": {"svg": "figures/mpl_pt.svg"},
                "panel_sources": [
                    {
                        "panel_id": "A", "source_table": "figures/dup.csv",
                        "key_col": "group", "value_col": "val", "n_col": "n", "p_col": "unadjusted_p",
                        "require_adjusted_p": True,
                        "expected_rows": [{"key": "Ctrl", "plotted_value": 10.0, "plotted_n": 24, "plotted_p": 0.04999}],
                    },
                    {
                        "panel_id": "B", "source_table": "figures/dup.csv",
                        "key_cols": ["cohort", "group"], "value_col": "val", "n_col": "n", "p_col": "p_adj",
                        "require_adjusted_p": True,
                        "expected_rows": [{"key": ["C2", "Ctrl"], "plotted_value": 20.0, "plotted_n": 24, "plotted_p": 0.04999}],
                    },
                ],
            }, f)
        dup_rep = verify_figure_manifest(dup_mf, proj)
        self.assertFalse(dup_rep["ok"])
        joined_dup = "\n".join(dup_rep["blocking"])
        self.assertIn("存在重复键", joined_dup)
        self.assertIn("unadjusted_p", joined_dup)
        self.assertIn("非精确非负整数", joined_dup)
        self.assertIn("显著性阈值", joined_dup)

        # 5. Same-name SVG shadowing + stale SHA-256 + signoff recovery + decoupled PNG/TIFF DPI
        sub_svg = os.path.join(proj, "figures", "fig1", "panel.svg")
        root_svg = os.path.join(proj, "panel.svg")
        with open(sub_svg, "w", encoding="utf-8") as f:
            f.write('<svg xmlns="http://www.w3.org/2000/svg" width="252.28pt" height="113.38pt" viewBox="0 0 252.28 113.38"><text style="font: 7px sans-serif">Good</text></svg>')
        with open(root_svg, "w", encoding="utf-8") as f:
            f.write('<svg xmlns="http://www.w3.org/2000/svg" width="252.28pt" height="113.38pt" viewBox="0 0 252.28 113.38"><text style="font: 7px sans-serif">RootShadow</text></svg>')
        good_png = os.path.join(proj, "figures", "fig1", "panel.png")
        good_tif = os.path.join(proj, "figures", "fig1", "panel.tiff")
        _make_png(good_png, 1051, 700, dpi=300, ink_row=10, ink_px=30)
        _make_tiff(good_tif, 2102, 1400, dpi=600)
        good_csv = os.path.join(proj, "figures", "fig1", "panel_source.csv")
        with open(good_csv, "w", encoding="utf-8") as f:
            f.write("cohort,group,n,val,p_adj\nC1,Ctrl,24,10.0,0.02\nC1,Treat,24,18.0,0.02\n")

        mf_path = os.path.join(proj, "figures", "fig1", "figure_manifest.json")
        with open(mf_path, "w", encoding="utf-8") as f:
            json.dump({
                "figure_id": "panel",
                "target_width_mm": 89.0,
                "min_dpi": 600,
                "exports": {"svg": "panel.svg", "png": "blank.png"},  # First fail on blank png!
                "panel_sources": [{
                    "panel_id": "A", "source_table": "panel_source.csv",
                    "key_cols": ["cohort", "group"], "value_col": "val", "n_col": "n", "p_col": "p_adj",
                    "require_adjusted_p": True,
                    "expected_rows": [
                        {"key": ["C1", "Ctrl"], "plotted_value": 10.0, "plotted_n": 24, "plotted_p": 0.02},
                        {"key": ["C1", "Treat"], "plotted_value": 18.0, "plotted_n": 24, "plotted_p": 0.02},
                    ],
                }],
            }, f)
        _make_png(os.path.join(proj, "figures", "fig1", "blank.png"), 1051, 700, dpi=300, ink_row=None)

        def _v52_runner(*args, **kwargs):
            return CodexExecutionResult(
                session_id=f"s-v52-{time.time_ns()}",
                response_text="Generated figure_manifest.json successfully.",
                partial_text="",
                exit_code=0,
                turn_completed=True,
                requested_config=kwargs.get("requested_config") or {},
                accepted_config=kwargs.get("accepted_config") or {},
            )

        orch = TeamOrchestrator(
            state_file=self.state_path,
            adapter=CodexAdapter(runner_override=_v52_runner),
            agy_adapter=AgyAdapter(runner_override=_v52_runner),
            auto_start_scheduler=False,
        )
        try:
            orch.switch_project(proj)
            tk = orch.create_task(
                title="v52图件复验恢复",
                goal="出图并校验",
                execution_mode="single_member",
                target_member_id="codex_author",
                sandbox="workspace-write",
                research_spec={"figure_manifests": ["figures/fig1/figure_manifest.json"]},
            )
            # Fail first -> blank png + ambiguous relative "panel.svg" matching both figures/fig1/panel.svg and root panel.svg!
            r_fail = orch.verify_task_figure(tk["id"], "figures/fig1/figure_manifest.json")
            self.assertFalse(r_fail["ok"])
            self.assertTrue(any("同时命中两个不同文件" in b for b in r_fail["blocking"]))
            tk_now = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == tk["id"])
            self.assertEqual(tk_now["research_signoff"]["status"], "failed")

            # Fix manifest -> unambiguous explicit path + good 300 DPI PNG + 600 DPI TIFF -> signoff MUST recover to passed!
            with open(mf_path, "w", encoding="utf-8") as f:
                json.dump({
                    "figure_id": "panel",
                    "target_width_mm": 89.0,
                    "min_dpi": 600,
                    "exports": {"svg": "figures/fig1/panel.svg", "png": "figures/fig1/panel.png", "tiff": "figures/fig1/panel.tiff"},
                    "panel_sources": [{
                        "panel_id": "A", "source_table": "figures/fig1/panel_source.csv",
                        "key_cols": ["cohort", "group"], "value_col": "val", "n_col": "n", "p_col": "p_adj",
                        "require_adjusted_p": True,
                        "expected_rows": [
                            {"key": ["C1", "Ctrl"], "plotted_value": 10.0, "plotted_n": 24, "plotted_p": 0.02},
                            {"key": ["C1", "Treat"], "plotted_value": 18.0, "plotted_n": 24, "plotted_p": 0.02},
                        ],
                    }],
                }, f)
            r_pass = orch.verify_task_figure(tk["id"], "figures/fig1/figure_manifest.json")
            self.assertTrue(r_pass["ok"], f"Expected pass, got blocking={r_pass.get('blocking')}")
            self.assertEqual(r_pass["status"], "passed")
            tk_now = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == tk["id"])
            self.assertEqual(tk_now["research_signoff"]["status"], "passed")

            # Same-name shadowing check in get_task_artifacts: root panel.svg must NOT inherit figures/fig1/panel.svg QA PASS!
            art = orch.get_task_artifacts(tk["id"])
            by_rel = {f["formats"]["svg"]["rel_path"]: f for f in art["figures"] if "svg" in f.get("formats", {})}
            self.assertEqual(by_rel["figures/fig1/panel.svg"]["qa_status"], "passed")
            self.assertNotEqual(by_rel["panel.svg"]["qa_status"], "passed")

            # Disk modification after QA -> qa_status MUST flip to stale!
            with open(sub_svg, "a", encoding="utf-8") as f:
                f.write("<!-- modified -->")
            art_stale = orch.get_task_artifacts(tk["id"])
            by_rel2 = {f["formats"]["svg"]["rel_path"]: f for f in art_stale["figures"] if "svg" in f.get("formats", {})}
            self.assertEqual(by_rel2["figures/fig1/panel.svg"]["qa_status"], "stale")

            # Signoff recovery must NOT clear an unrelated readonly_inputs_integrity failure!
            def _inject_ro_fail(st):
                t = next(x for x in st["tasks"] if x["id"] == tk["id"])
                t["research_signoff"]["checks"].append({
                    "kind": "readonly_inputs_integrity", "ok": False, "status": "failed",
                    "blocking": ["只读原始数据 raw/locked.csv 被篡改"],
                })
            orch.store.mutate(_inject_ro_fail)
            orch.verify_task_figure(tk["id"], "figures/fig1/figure_manifest.json")
            tk_ro = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == tk["id"])
            self.assertEqual(tk_ro["research_signoff"]["status"], "failed")
            self.assertTrue(any("raw/locked.csv" in b for b in tk_ro["research_signoff"]["blocking"]))

            # 6. Advisory Auto-Figure-QA + run_acceptance_checks exception isolation preserves response_text!
            def _close_tk(st):
                t = next(x for x in st["tasks"] if x["id"] == tk["id"])
                t["status"] = "succeeded"
                for s in t["steps"]:
                    s["status"] = "succeeded"
            orch.store.mutate(_close_tk)
            orig_run_acc = rv.run_acceptance_checks
            try:
                orch.start_scheduler()
                tk_adv = orch.create_task(
                    title="UI无spec自动图件校验",
                    goal="自动出图",
                    execution_mode="single_member",
                    target_member_id="codex_author",
                    sandbox="workspace-write",
                )
                deadline = time.time() + 10.0
                tk_adv_done = tk_adv
                while time.time() < deadline:
                    tk_adv_done = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == tk_adv["id"])
                    if tk_adv_done["status"] in ("succeeded", "failed", "needs_ruling"):
                        break
                    time.sleep(0.05)
                self.assertEqual(tk_adv_done["status"], "succeeded")
                self.assertIsNotNone(tk_adv_done.get("research_signoff"))
                self.assertTrue(tk_adv_done["research_signoff"].get("advisory"))

                # Force exception inside run_acceptance_checks -> expert output must NOT be lost!
                def _boom(*args, **kwargs):
                    raise RuntimeError("injected_qa_crash")
                rv.run_acceptance_checks = _boom
                import team_runtime as tr_mod
                orig_tr_acc = tr_mod.run_acceptance_checks
                tr_mod.run_acceptance_checks = _boom
                try:
                    tk_iso = orch.create_task(
                        title="验收异常隔离测试",
                        goal="测试隔离",
                        execution_mode="single_member",
                        target_member_id="codex_author",
                        sandbox="workspace-write",
                        research_spec={
                            "figure_manifests": ["figures/fig1/figure_manifest.json"],
                            "block_on_failed_signoff": True,
                        },
                    )
                    deadline = time.time() + 10.0
                    tk_iso_done = tk_iso
                    while time.time() < deadline:
                        tk_iso_done = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == tk_iso["id"])
                        if tk_iso_done["status"] in ("succeeded", "failed", "needs_ruling"):
                            break
                        time.sleep(0.05)
                    self.assertIn("Generated figure_manifest.json", tk_iso_done["steps"][0]["latest_result_text"])
                    self.assertEqual(tk_iso_done["status"], "needs_ruling")
                    self.assertEqual(tk_iso_done["research_signoff"]["status"], "error")
                finally:
                    tr_mod.run_acceptance_checks = orig_tr_acc
            finally:
                rv.run_acceptance_checks = orig_run_acc
        finally:
            orch.stop_scheduler()

        # 7. Frontend contract check on sidebar_panel.html
        html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sidebar_panel.html")
        with open(html_path, "r", encoding="utf-8") as f:
            html_src = f.read()
        self.assertIn("&#39;", html_src)
        insp_keys = set(re.findall(r"\binsp\.([a-zA-Z0-9_]+)", html_src))
        valid_keys = {
            "width_px", "height_px", "dpi_x", "dpi_y", "physical_width_mm", "physical_height_mm",
            "canvas_width_mm", "canvas_height_mm", "width_mm", "height_mm",
            "viewBox", "viewbox", "effective_min_font_pt", "min_font_pt", "min_font_size_pt",
            "font_status", "is_blank",
        }
        self.assertTrue(insp_keys.issubset(valid_keys), f"Unexpected insp keys in HTML: {insp_keys - valid_keys}")

    def test_23_v521_all_six_blocking_gates(self) -> None:
        import figure_qa as fqa
        import research_verifier as rv

        proj = os.path.join(self.tmp_dir, f"v521_gates_{time.time_ns()}")
        os.makedirs(os.path.join(proj, "figures"), exist_ok=True)
        os.makedirs(os.path.join(proj, "raw"), exist_ok=True)
        os.makedirs(os.path.join(proj, "scripts"), exist_ok=True)

        # Valid 89mm SVG + source CSV + script
        svg_p = os.path.join(proj, "figures", "fig1.svg")
        with open(svg_p, "w", encoding="utf-8") as f:
            f.write(
                '<svg xmlns="http://www.w3.org/2000/svg" width="89mm" height="60mm" viewBox="0 0 252.28 170.08">'
                '<rect width="100%" height="100%" fill="#ffffff"/>'
                '<text x="10" y="20" font-size="7pt">Control Treated p_adj=0.04</text></svg>'
            )
        src_csv = os.path.join(proj, "figures", "fig1_src.csv")
        with open(src_csv, "w", encoding="utf-8", newline="") as f:
            f.write("group,mean,n,p_adj\nControl,10.0,24,0.04\nTreated,20.0,24,0.04\n")
        raw_csv = os.path.join(proj, "raw", "orig.csv")
        with open(raw_csv, "w", encoding="utf-8") as f:
            f.write("id,val\n1,10\n")
        plot_py = os.path.join(proj, "scripts", "plot.py")
        with open(plot_py, "w", encoding="utf-8") as f:
            f.write("print('plot')\n")
        plot_sha = rv.sha256_file(plot_py)

        # --- BLK-2: Quantitative figure with script_path ONLY (0 panels) -> NOT passed / NOT schematic_verified! ---
        quant_unbound_mf = os.path.join(proj, "figures", "quant_unbound_manifest.json")
        with open(quant_unbound_mf, "w", encoding="utf-8") as f:
            json.dump({
                "figure_id": "fig_quant_unbound",
                "figure_type": "quantitative",
                "target_width_mm": 89.0,
                "script_path": "scripts/plot.py",
                "script_sha256": plot_sha,
                "exports": {"svg": "figures/fig1.svg"},
            }, f)
        r_blk2 = fqa.verify_figure_manifest(quant_unbound_mf, proj)
        self.assertEqual(r_blk2["status"], "not_verifiable")
        self.assertEqual(r_blk2["verification_level"], "provenance_only")
        self.assertNotEqual(r_blk2["status"], "passed")

        # True schematic figure with script_path -> allowed to be schematic_verified / passed
        schem_mf = os.path.join(proj, "figures", "schem_manifest.json")
        with open(schem_mf, "w", encoding="utf-8") as f:
            json.dump({
                "figure_id": "fig_schem",
                "figure_type": "schematic",
                "target_width_mm": 89.0,
                "script_path": "scripts/plot.py",
                "script_sha256": plot_sha,
                "exports": {"svg": "figures/fig1.svg"},
            }, f)
        r_schem = fqa.verify_figure_manifest(schem_mf, proj)
        self.assertEqual(r_schem["status"], "passed")
        self.assertEqual(r_schem["verification_level"], "schematic_verified")

        # --- BLK-3a: Empty expected_rows: [] MUST NOT fake checked_panels=1 / full_manifest / passed! ---
        empty_rows_mf = os.path.join(proj, "figures", "empty_rows_manifest.json")
        with open(empty_rows_mf, "w", encoding="utf-8") as f:
            json.dump({
                "figure_id": "fig_empty_rows",
                "target_width_mm": 89.0,
                "exports": {"svg": "figures/fig1.svg"},
                "panel_sources": [{"panel_id": "A", "source_table": "figures/fig1_src.csv", "expected_rows": []}],
            }, f)
        r_empty = fqa.verify_figure_manifest(empty_rows_mf, proj)
        self.assertEqual(r_empty["checked_panels"], 0)
        self.assertEqual(r_empty["status"], "not_verifiable")

        # --- BLK-3b: Fractional N=24.0000000001 vs 24 -> REJECTED! Exact "24.0" -> ACCEPTED! ---
        frac_n_mf = os.path.join(proj, "figures", "frac_n_manifest.json")
        with open(frac_n_mf, "w", encoding="utf-8") as f:
            json.dump({
                "figure_id": "fig_frac_n",
                "target_width_mm": 89.0,
                "exports": {"svg": "figures/fig1.svg"},
                "panel_sources": [{
                    "panel_id": "A",
                    "source_table": "figures/fig1_src.csv",
                    "key_col": "group",
                    "value_col": "mean",
                    "n_col": "n",
                    "p_col": "p_adj",
                    "expected_rows": [{"key": "Control", "plotted_value": 10.0, "plotted_n": 24.0000000001, "plotted_p": 0.04}],
                }],
            }, f)
        r_frac = fqa.verify_figure_manifest(frac_n_mf, proj)
        self.assertFalse(r_frac["ok"])
        self.assertEqual(r_frac["status"], "failed")

        good_mf = os.path.join(proj, "figures", "good_manifest.json")
        with open(good_mf, "w", encoding="utf-8") as f:
            json.dump({
                "figure_id": "fig_good",
                "target_width_mm": 89.0,
                "exports": {"svg": "figures/fig1.svg"},
                "panel_sources": [{
                    "panel_id": "A",
                    "source_table": "figures/fig1_src.csv",
                    "key_col": "group",
                    "value_col": "mean",
                    "n_col": "n",
                    "p_col": "p_adj",
                    "expected_rows": [{"key": "Control", "plotted_value": 10.0, "plotted_n": "24.0", "plotted_p": 0.04}],
                }],
            }, f)
        r_good = fqa.verify_figure_manifest(good_mf, proj)
        self.assertTrue(r_good["ok"])
        self.assertEqual(r_good["status"], "passed")
        self.assertEqual(r_good["checked_panels"], 1)

        # --- BLK-1: Out-of-root manifest path MUST survive recompute_signoff_summary() as failed! ---
        rep_blk1 = rv.run_acceptance_checks(
            proj,
            {"figure_manifests": ["figures/good_manifest.json", "../evil_escaped_manifest.json"], "block_on_failed_signoff": True},
        )
        self.assertFalse(rep_blk1["ok"])
        self.assertEqual(rep_blk1["status"], "failed")
        self.assertTrue(any("越界" in b for b in rep_blk1["blocking"]))
        rv.recompute_signoff_summary(rep_blk1)
        self.assertFalse(rep_blk1["ok"])
        self.assertEqual(rep_blk1["status"], "failed")
        self.assertTrue(any("越界" in b for b in rep_blk1["blocking"]))

        # --- BLK-4: not_verifiable + passing readonly_inputs MUST NOT be promoted to passed! ---
        before_snap = rv.snapshot_readonly_inputs(proj, ["raw"])
        rep_blk4 = rv.run_acceptance_checks(
            proj,
            {
                "readonly_inputs": ["raw"],
                "figure_manifests": ["figures/quant_unbound_manifest.json"],
                "block_on_failed_signoff": True,
            },
            before_inputs_snapshot=before_snap,
        )
        self.assertEqual(rep_blk4["status"], "not_verifiable")
        self.assertFalse(rep_blk4["can_release"])
        self.assertFalse(rep_blk4["ok"])

        # --- BLK-5 & BLK-6: Multi-step signoff merge & followup repair of needs_ruling step (no deadlock!) ---
        step_calls = []

        def gate_runner(prompt, session_id, **kwargs):
            step_calls.append(prompt[:160])
            # On repair step, fix frac_n_manifest.json to valid integer N=24!
            if "修复N值" in prompt:
                with open(frac_n_mf, "w", encoding="utf-8") as mf_f:
                    json.dump({
                        "figure_id": "fig_frac_n",
                        "target_width_mm": 89.0,
                        "exports": {"svg": "figures/fig1.svg"},
                        "panel_sources": [{
                            "panel_id": "A",
                            "source_table": "figures/fig1_src.csv",
                            "key_col": "group",
                            "value_col": "mean",
                            "n_col": "n",
                            "p_col": "p_adj",
                            "expected_rows": [{"key": "Control", "plotted_value": 10.0, "plotted_n": 24, "plotted_p": 0.04}],
                        }],
                    }, mf_f)
            return CodexExecutionResult(
                session_id=session_id or f"s-{len(step_calls)}",
                response_text=f"Step done #{len(step_calls)}",
                partial_text="",
                exit_code=0,
                turn_completed=True,
                requested_config=kwargs["requested_config"],
                accepted_config=kwargs["accepted_config"],
            )

        orch = TeamOrchestrator(
            state_file=self.state_path,
            adapter=CodexAdapter(runner_override=gate_runner),
            agy_adapter=AgyAdapter(runner_override=gate_runner),
            auto_start_scheduler=True,
        )
        try:
            orch.switch_project(proj)
            # BLK-5: Step 0 fails figure_qa; Step 1 (synthesis) returns pending_artifacts with empty figure_reports -> Step 0 figure_reports MUST survive!
            tk5 = orch.create_task(
                title="BLK-5多步签核保留",
                goal="出图后汇总",
                dispatch_mode="coordinator_plan",
                execution_mode=None,
                sandbox="workspace-write",
                steps=[
                    {"title": "出图步骤", "step_type": "execution", "stage_index": 0, "member_id": "codex_author", "prompt": "绘图", "depends_on": [], "read_only": False, "sandbox": "workspace-write"},
                    {"title": "汇总步骤", "step_type": "synthesis", "stage_index": 1, "member_id": "agy_gemini", "prompt": "汇总", "depends_on": [0], "read_only": True, "sandbox": "read-only"},
                ],
                research_spec={"figure_manifests": ["figures/frac_n_manifest.json"], "block_on_failed_signoff": False},
            )
            deadline = time.time() + 10.0
            tk5_done = tk5
            while time.time() < deadline:
                tk5_done = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == tk5["id"])
                if tk5_done["status"] in ("succeeded", "failed", "needs_ruling"):
                    break
                time.sleep(0.05)
            self.assertEqual(tk5_done["status"], "succeeded")
            self.assertEqual(tk5_done["research_signoff"]["status"], "failed")
            self.assertGreaterEqual(len(tk5_done["research_signoff"]["figure_reports"]), 1)

            # Explicitly verify BLK-5 merge when a later step submits an empty pending_artifacts report!
            orch._finalize_attempt(
                tk5["id"],
                1,
                tk5_done["steps"][1]["attempts"][-1]["attempt_id"],
                CodexExecutionResult(
                    session_id="s-blk5-merge", response_text="汇总完成", partial_text="", exit_code=0, turn_completed=True,
                    requested_config={}, accepted_config={},
                ),
                False,
                None,
                verif_report={
                    "ok": True, "status": "pending_artifacts", "checks": [], "figure_reports": [],
                    "blocking": [], "blocking_items": [], "observations": ["pending"],
                },
            )
            tk5_after_merge = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == tk5["id"])
            self.assertEqual(tk5_after_merge["research_signoff"]["status"], "failed")
            self.assertGreaterEqual(len(tk5_after_merge["research_signoff"]["figure_reports"]), 1)

            # Also simulate Step 1 returning empty pending_artifacts directly via _finalize_attempt merge check:
            # Now test BLK-4 verify_task_figure on not_verifiable does NOT unlock needs_ruling + BLK-6 followup repair!
            tk6 = orch.create_task(
                title="BLK-6修复不死锁",
                goal="严格图件验收与修复",
                execution_mode="single_member",
                target_member_id="codex_author",
                sandbox="workspace-write",
                research_spec={"figure_manifests": ["figures/frac_n_manifest.json"], "block_on_failed_signoff": True},
            )
            deadline = time.time() + 10.0
            tk6_cur = tk6
            while time.time() < deadline:
                tk6_cur = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == tk6["id"])
                if tk6_cur["status"] in ("succeeded", "failed", "needs_ruling"):
                    break
                time.sleep(0.05)
            self.assertEqual(tk6_cur["status"], "needs_ruling")
            self.assertEqual(tk6_cur["steps"][0]["status"], "needs_ruling")

            # Verify a not_verifiable manifest via verify_task_figure -> MUST NOT unlock needs_ruling!
            orch.verify_task_figure(tk6["id"], "figures/quant_unbound_manifest.json")
            tk6_still = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == tk6["id"])
            self.assertEqual(tk6_still["status"], "needs_ruling")

            # Remove quant_unbound from signoff so only frac_n remains, then trigger followup repair (BLK-6)!
            def _keep_only_frac(st):
                t = next(x for x in st["tasks"] if x["id"] == tk6["id"])
                t["research_signoff"]["figure_reports"] = [
                    fr for fr in t["research_signoff"]["figure_reports"] if "frac_n" in str(fr.get("manifest_path", ""))
                ]
                rv.recompute_signoff_summary(t["research_signoff"])
            orch.store.mutate(_keep_only_frac)

            orch.followup_task(tk6["id"], "请修复N值并重新验收", step_index=0, target_member_id="codex_author", sandbox="workspace-write")
            deadline = time.time() + 10.0
            tk6_fixed = tk6
            while time.time() < deadline:
                tk6_fixed = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == tk6["id"])
                if tk6_fixed["status"] in ("succeeded", "failed"):
                    break
                time.sleep(0.05)
            self.assertEqual(tk6_fixed["status"], "succeeded", f"BLK-6 followup deadlocked or failed: {tk6_fixed['status']} / {tk6_fixed.get('blocking_reason')}")
            self.assertEqual(tk6_fixed["steps"][1].get("repair_of"), 0)
            self.assertEqual(tk6_fixed["steps"][0]["status"], "succeeded")
            self.assertEqual(tk6_fixed["research_signoff"]["status"], "passed")
        finally:
            orch.stop_scheduler()

        # OBS-3 check on sidebar_panel.html
        html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sidebar_panel.html")
        with open(html_path, "r", encoding="utf-8") as f:
            hsrc = f.read()
        self.assertIn("signoff.status === 'error'", hsrc)
        self.assertNotIn("onclick=\"verifyTaskManifest('${esc(task.id)}', '${esc(m.rel_path)}')\"", hsrc)
        self.assertNotIn("onclick=\"openAnswerLink('${esc(tb.rel_path)}', true)\"", hsrc)

    def test_24_v53_context_engine_kv_prefix_masking_snapshots_rollover_and_blind_r1(self) -> None:
        import context_engine as ce

        proj = os.path.join(self.tmp_dir, f"v53_ctx_{time.time_ns()}")
        os.makedirs(proj, exist_ok=True)
        with open(os.path.join(proj, "alpha.py"), "w", encoding="utf-8") as f:
            f.write("x = 1\n")

        # 1. Error classification priority: RESOURCE_EXHAUSTED + context length -> context_length_exceeded (NOT quota 429!)
        ovf_msg = "HTTP 400 RESOURCE_EXHAUSTED: maximum context length exceeded (prompt is too long)"
        self.assertEqual(ce.classify_turn_error(ovf_msg)[0], "context_length_exceeded")
        self.assertTrue(ce.is_context_overflow_error(ovf_msg))
        import team_runtime as tr_mod
        self.assertFalse(tr_mod._is_quota_or_rate_limit_error(ovf_msg))
        self.assertTrue(tr_mod._is_quota_or_rate_limit_error("HTTP 429 RESOURCE_EXHAUSTED: insufficient_quota"))

        # 2. Observation Masking + Zero-Hallucination Anchor Extraction + BlobStore under non-C: tmp!
        bstore = ce.BlobStore("TASK-UNIT24")
        self.assertFalse(os.path.abspath(bstore.task_dir).upper().startswith("C:\\"), f"BlobStore wrote to C: {bstore.task_dir}")
        big_code = "```python\n" + "\n".join(f"# line {i}: compute_metric({i})" for i in range(60)) + "\n# prototype/figure_qa.py#L42 BLOCKING: N=24\n```"
        big_tbl = "| group | mean | n | p_adj |\n|---|---|---|---|\n" + "\n".join(
            f"| G{i} | {10+i}.0 | 24 | 0.0042 |" for i in range(32)
        )
        raw_msg = f"核查见 [figure_qa.py](prototype/figure_qa.py#L42)，实测 p_adj=0.0042，发现 BLOCKING: N=24 漂移。\n\n{big_code}\n\n{big_tbl}"
        masked, refs = ce.mask_observation_blocks(raw_msg, bstore)
        self.assertGreaterEqual(len(refs), 2)
        self.assertIn("[MASKED_OBS", masked)
        self.assertLess(len(masked), len(raw_msg) * 0.55)
        self.assertTrue(os.path.isfile(refs[0]["path"]))

        snap = ce.seal_round_snapshot(
            [{"id": "MSG-1", "sender_id": "codex_author", "sender_name": "Codex", "kind": "speech", "content": raw_msg}],
            visibility_scope="stage_0_all",
            stage_index=0,
            cutoff_seq=1,
            task_meta={"rulings": [], "research_signoff": None},
            blob_store=bstore,
            round_label="第1轮",
        )
        self.assertIn("prototype/figure_qa.py#L42", snap["rendered_md"])
        self.assertIn("p_adj=0.0042", snap["rendered_md"])
        self.assertIn("BLOCKING", snap["rendered_md"])
        self.assertIn("MSG-1", snap["event_ids"])
        self.assertTrue(os.path.isfile(snap["blob_path"]))

        # 3. AgyAdapter Byte-0 KV-Cache fix: fresh prompt has static rule at Byte 0 & dir_index at tail; resumed omits dir_index!
        captured_agy = []
        agy_ad = AgyAdapter(runner_override=lambda prompt, session_id, **kw: (
            captured_agy.append((prompt, session_id)) or CodexExecutionResult(
                session_id=session_id or "agy-s1", response_text="ok", partial_text="", exit_code=0, turn_completed=True,
                requested_config=kw["requested_config"], accepted_config=kw["accepted_config"],
            )
        ))
        # Test real prompt wrapping logic directly via helper check on agy_adapter source behavior
        # (runner_override receives raw prompt before CLI wrapping; verify CLI-wrapped prompt via fake subprocess or helper!)
        agy_src_p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agy_adapter.py")
        with open(agy_src_p, "r", encoding="utf-8") as f:
            agy_code = f.read()
        self.assertIn("if session_id:", agy_code)
        self.assertIn("Zone C", agy_code)
        self.assertIn("context_overflow", agy_code)

        # 4. End-to-End Orchestrator Tests:
        #    (a) Blind Round 1 canary isolation
        #    (b) Proactive Watermark Rollover on high CLI usage + real usage persistence
        #    (c) Resumed-Session Delta Frame shrink
        #    (d) Reactive Context-Overflow Rollover (read-only succeeds on L1; infinite overflow circuit-breaks after 2 retries;
        #        workspace-write with unknown_mutation pauses cleanly in needs_verification without replay!)
        #    (e) stateless_replay (M2) + manual compact_task_context() + budget_blocked!
        calls_log = []
        mode_flag = {"scenario": "normal"}

        def ctx_runner(prompt, session_id, **kwargs):
            calls_log.append({"prompt": prompt, "session_id": session_id, "sandbox": kwargs["requested_config"].get("sandbox")})
            sc = mode_flag["scenario"]
            if sc == "blind_r1":
                if "blind_first" in prompt:
                    return CodexExecutionResult(
                        session_id="s-b1", response_text="BLIND_R1_CANARY_XYZZY_991 独立分析完毕 [alpha.py](alpha.py#L1)",
                        partial_text="", exit_code=0, turn_completed=True,
                        usage={"input_tokens": 1200, "cached_input_tokens": 400, "output_tokens": 200},
                        requested_config=kwargs["requested_config"], accepted_config=kwargs["accepted_config"],
                    )
                return CodexExecutionResult(
                    session_id=session_id or f"s-b-{len(calls_log)}", response_text="同轮或下轮发言完毕",
                    partial_text="", exit_code=0, turn_completed=True,
                    usage={"input_tokens": 1400, "cached_input_tokens": 900, "output_tokens": 220},
                    requested_config=kwargs["requested_config"], accepted_config=kwargs["accepted_config"],
                )
            if sc == "high_usage_rollover":
                if len(calls_log) == 1:
                    return CodexExecutionResult(
                        session_id="sess-aging-1", response_text="第一轮已完成核心计算 p_adj=0.0042",
                        partial_text="", exit_code=0, turn_completed=True,
                        usage={"input_tokens": 210000, "cached_input_tokens": 115000, "output_tokens": 1500},
                        requested_config=kwargs["requested_config"], accepted_config=kwargs["accepted_config"],
                    )
                return CodexExecutionResult(
                    session_id=session_id or "sess-fresh-2", response_text="换乘新会话后顺利完成续答",
                    partial_text="", exit_code=0, turn_completed=True,
                    usage={"input_tokens": 4200, "cached_input_tokens": 2800, "output_tokens": 300},
                    requested_config=kwargs["requested_config"], accepted_config=kwargs["accepted_config"],
                )
            if sc == "delta_reuse":
                return CodexExecutionResult(
                    session_id=session_id or "sess-delta-1", response_text=f"正常续答 #{len(calls_log)}",
                    partial_text="", exit_code=0, turn_completed=True,
                    usage={"input_tokens": 3000, "cached_input_tokens": 2100, "output_tokens": 250},
                    requested_config=kwargs["requested_config"], accepted_config=kwargs["accepted_config"],
                )
            if sc == "reactive_recover_l1":
                if len(calls_log) == 1:
                    return CodexExecutionResult(
                        session_id="sess-ovf-1", response_text="", partial_text="", exit_code=1, turn_completed=False,
                        context_overflow=True, error_kind="context_length_exceeded",
                        error="RESOURCE_EXHAUSTED: maximum context length exceeded",
                        side_effect_state="read_only",
                        requested_config=kwargs["requested_config"], accepted_config=kwargs["accepted_config"],
                    )
                return CodexExecutionResult(
                    session_id="sess-ovf-l1", response_text="L1 压缩换乘后恢复成功！",
                    partial_text="", exit_code=0, turn_completed=True,
                    requested_config=kwargs["requested_config"], accepted_config=kwargs["accepted_config"],
                )
            if sc == "reactive_always_overflow":
                return CodexExecutionResult(
                    session_id=None, response_text="", partial_text="", exit_code=1, turn_completed=False,
                    context_overflow=True, error_kind="context_length_exceeded",
                    error="maximum context length exceeded",
                    side_effect_state="read_only",
                    requested_config=kwargs["requested_config"], accepted_config=kwargs["accepted_config"],
                )
            if sc == "reactive_mutated_overflow":
                return CodexExecutionResult(
                    session_id=None, response_text="", partial_text="", exit_code=1, turn_completed=False,
                    context_overflow=True, error_kind="context_length_exceeded",
                    error="maximum context length exceeded after write",
                    side_effect_state="unknown_mutation",
                    changed_files=[{"path": "alpha.py", "kind": "update"}],
                    requested_config=kwargs["requested_config"], accepted_config=kwargs["accepted_config"],
                )
            return CodexExecutionResult(
                session_id=session_id or f"s-def-{len(calls_log)}", response_text="ok",
                partial_text="", exit_code=0, turn_completed=True,
                requested_config=kwargs["requested_config"], accepted_config=kwargs["accepted_config"],
            )

        orch = TeamOrchestrator(
            state_file=self.state_path,
            adapter=CodexAdapter(runner_override=ctx_runner),
            agy_adapter=AgyAdapter(runner_override=ctx_runner),
            auto_start_scheduler=True,
        )
        try:
            orch.switch_project(proj)

            def _wait_task(tid: str, terminal=("succeeded", "failed", "needs_ruling", "needs_verification"), timeout=10.0):
                dl = time.time() + timeout
                cur = None
                while time.time() < dl:
                    cur = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == tid)
                    if cur["status"] in terminal:
                        return cur
                    time.sleep(0.05)
                return cur

            # (a) Blind Round 1 canary isolation + automatic Round Snapshot sealing!
            mode_flag["scenario"] = "blind_r1"
            calls_log.clear()
            t_rt = orch.create_task(
                title="Blind R1与状态卡测试",
                goal="blind_first 验证盲审隔离与自动封存",
                execution_mode="roundtable",
                participants=["codex_author", "agy_gemini"],
                host_member_id="agy_gemini",
                discussion_rounds=2,
                enable_host_opening=False,
            )
            t_rt_done = _wait_task(t_rt["id"])
            self.assertEqual(t_rt_done["status"], "succeeded")
            # Round 1 has 2 sibling steps; second sibling MUST NOT see BLIND_R1_CANARY_XYZZY_991!
            self.assertGreaterEqual(len(calls_log), 4)
            self.assertNotIn("BLIND_R1_CANARY_XYZZY_991", calls_log[1]["prompt"])
            # Automatic Round Snapshot sealed after Stage 0 completed!
            self.assertGreaterEqual(len(t_rt_done.get("round_snapshots") or []), 1)

            # (b) Proactive Watermark Rollover on high CLI usage + real usage persistence!
            mode_flag["scenario"] = "high_usage_rollover"
            calls_log.clear()
            t_ro = orch.create_task(
                title="水位线预防性换乘测试",
                goal="执行超长统计计算",
                execution_mode="single_member",
                target_member_id="codex_author",
            )
            t_ro_1 = _wait_task(t_ro["id"])
            self.assertEqual(t_ro_1["status"], "succeeded")
            self.assertEqual(t_ro_1["context_stats"]["total_input_tokens"], 210000)
            self.assertEqual(t_ro_1["context_stats"]["usage_source"], "cli_reported")
            self.assertGreater(t_ro_1["context_stats"]["cache_hit_ratio"], 0.5)
            # Trigger followup on same step (reusing worker_session_id="sess-aging-1") -> MUST proactively roll over to session_id=None!
            orch.followup_task(t_ro["id"], "请基于刚才结论补充敏感性分析", step_index=0, target_member_id="codex_author")
            t_ro_2 = _wait_task(t_ro["id"])
            self.assertEqual(t_ro_2["status"], "succeeded")
            self.assertEqual(len(calls_log), 2)
            self.assertIsNone(calls_log[1]["session_id"], "Expected proactive rollover to start fresh session_id=None!")
            self.assertIn("无缝会话换乘检查点", calls_log[1]["prompt"])
            self.assertGreaterEqual(t_ro_2["context_stats"]["rollover_count"], 1)

            # (c) Resumed-Session Delta Frame shrinks prompt on normal low-usage followup!
            mode_flag["scenario"] = "delta_reuse"
            calls_log.clear()
            t_dl = orch.create_task(
                title="增量帧Delta测试",
                goal="首轮全量建立上下文",
                execution_mode="single_member",
                target_member_id="codex_author",
            )
            _wait_task(t_dl["id"])
            full_len = len(calls_log[0]["prompt"])
            orch.followup_task(t_dl["id"], "简短追问一个细节", step_index=0, target_member_id="codex_author")
            t_dl_2 = _wait_task(t_dl["id"])
            self.assertEqual(t_dl_2["status"], "succeeded")
            self.assertEqual(calls_log[1]["session_id"], "sess-delta-1")
            self.assertIn("Delta Frame", calls_log[1]["prompt"])
            self.assertLess(len(calls_log[1]["prompt"]), full_len)
            self.assertGreaterEqual(t_dl_2["context_stats"]["delta_turns"], 1)

            # (d1) Reactive Context-Overflow Rollover: read-only recovers automatically on L1 (NOT routed to quota backup!)
            mode_flag["scenario"] = "reactive_recover_l1"
            calls_log.clear()
            t_rec = orch.create_task(
                title="被动超限L1恢复测试",
                goal="只读超长核查",
                execution_mode="single_member",
                target_member_id="agy_gemini",
                sandbox="read-only",
            )
            t_rec_done = _wait_task(t_rec["id"])
            self.assertEqual(t_rec_done["status"], "succeeded")
            self.assertEqual(t_rec_done["steps"][0]["member_id"], "agy_gemini")  # NOT swapped to quota backup!
            self.assertEqual(len(calls_log), 2)
            self.assertIsNone(calls_log[1]["session_id"])

            # (d2) Reactive overflow circuit breaker: max 2 overflow retries (L0 -> L1 -> L2 = 3 calls), stops cleanly, member idle!
            mode_flag["scenario"] = "reactive_always_overflow"
            calls_log.clear()
            t_cb = orch.create_task(
                title="超限熔断测试",
                goal="持续超限测试",
                execution_mode="single_member",
                target_member_id="codex_author",
                sandbox="read-only",
            )
            t_cb_done = _wait_task(t_cb["id"])
            self.assertEqual(t_cb_done["status"], "needs_ruling")
            self.assertEqual(len(calls_log), 3)  # 1 initial + L1 + L2, hard circuit break!
            self.assertEqual(orch.store.snapshot()["members"]["codex_author"]["status"], "idle")

            # (d3) Write-side-effect safety gate: workspace-write + unknown_mutation -> NEVER replays! Pauses in needs_verification!
            mode_flag["scenario"] = "reactive_mutated_overflow"
            calls_log.clear()
            t_mut = orch.create_task(
                title="写入副作用安全门禁测试",
                goal="可写目录执行并中途超限",
                execution_mode="single_member",
                target_member_id="codex_author",
                sandbox="workspace-write",
            )
            t_mut_done = _wait_task(t_mut["id"])
            self.assertEqual(t_mut_done["status"], "needs_verification")
            self.assertEqual(len(calls_log), 1)  # Strictly 1 call, zero unsafe replay!

            # (e) stateless_replay (M2) + manual compact_task_context() + budget_blocked!
            orch.update_binding(context_session_mode="stateless_replay")
            mode_flag["scenario"] = "delta_reuse"
            calls_log.clear()
            t_m2 = orch.create_task(title="M2无状态账本模式测试", goal="每轮全新会话", execution_mode="single_member", target_member_id="codex_author")
            _wait_task(t_m2["id"])
            orch.followup_task(t_m2["id"], "第二轮也应全新会话", step_index=0, target_member_id="codex_author")
            _wait_task(t_m2["id"])
            self.assertIsNone(calls_log[0]["session_id"])
            self.assertIsNone(calls_log[1]["session_id"])
            orch.update_binding(context_session_mode="bounded_native")

            comp = orch.compact_task_context(t_dl["id"], reset_sessions=True)
            self.assertIn("snapshot", comp)
            self.assertGreaterEqual(comp["reset_sessions_count"], 1)

            # budget_blocked when P0 prompt itself exceeds hard budget!
            orch.update_binding(context_budget_chars=12000)
            calls_log.clear()
            t_bb = orch.create_task(
                title="硬预算阻断测试",
                goal="X" * 16000,
                execution_mode="single_member",
                target_member_id="codex_author",
            )
            t_bb_done = _wait_task(t_bb["id"])
            self.assertEqual(t_bb_done["status"], "needs_ruling")
            self.assertEqual(len(calls_log), 0, "budget_blocked MUST NOT spawn CLI!")
        finally:
            orch.stop_scheduler()

    def test_25_v54_pillar1_anchor_regex_newest_snapshots_and_peak_occupancy(self) -> None:
        import context_engine as ce

        # 1. All 6 previously-missed scientific/CJK/spaced-path probes MUST be caught 100%!
        probes = [
            "Differential expression showed p = 1.2e-15 and q = 3.4e-12.",
            "Cox model yielded HR = 0.68 (95% CI: 0.51-0.91, P = 0.008).",
            "Cohort included N = 1,240 patients (AUC = 0.914, log2FC = -2.35).",
            "共纳入样本 328 例，中位随访 42.5 个月，准确率达 94.2%。",
            "See `figures/Fig 1A/survival_curve.svg` and `scripts/01_clean data.R#L88`.",
            "ΔAIC = -14.7, R² = 0.87, F(2, 117) = 19.4.",
        ]
        for idx, p_txt in enumerate(probes):
            anc = ce.extract_deterministic_anchors(p_txt)
            self.assertTrue(
                bool(anc["numeric_claims"] or anc["file_refs"]),
                f"Anchor probe #{idx} missed: {p_txt}",
            )
        # Spaced path inside backticks preserved intact
        anc_sp = ce.extract_deterministic_anchors(probes[4])
        self.assertIn("figures/Fig 1A/survival_curve.svg", anc_sp["file_refs"])
        self.assertIn("scripts/01_clean data.R#L88", anc_sp["file_refs"])

        # 2. Newest-first snapshot retention under tight budget (30 long snapshots -> Stage 28 kept, older folded!)
        bs = ce.BlobStore("TASK-V54-SNAP")
        snaps = []
        for s_i in range(30):
            snaps.append(
                {
                    "stage_index": s_i,
                    "round_label": f"R{s_i}",
                    "visibility_scope": f"stage_{s_i}_all",
                    "event_ids": [f"E-{s_i}"],
                    "rendered_md": f"### [不可变轮次状态卡 · Stage {s_i} (R{s_i})]\n- BLOCKING: BLK-S{s_i} p_adj=0.00{s_i % 9 + 1}\n" + ("详细核查分析要点。" * 80),
                }
            )
        cfg = ce.ContextBudgetConfig(max_prompt_chars=16_000, session_mode="bounded_native")
        plan = ce.compile_turn_prompt(
            zone_a_blocks=["Zone A"],
            zone_b_static_blocks=[],
            sealed_snapshots=snaps,
            visible_msgs=[],
            is_blind_r1=False,
            current_stage_index=30,
            step_prompt_block="Final synthesis",
            handoff_block="",
            protocol_blocks=[],
            room_md_path="room.md",
            blob_store=bs,
            session_id=None,
            session_metric=None,
            model_slug="gpt-6.2-sol",
            cfg=cfg,
        )
        self.assertIn("Stage 28 (R28)", plan["prompt"], "Newest sealed stage MUST be preserved intact!")
        self.assertIn("早期轮次折叠锚点总账", plan["prompt"], "Evicted ancient stages should fold into pinned anchor ledger!")

        # 3. Dual Billing vs Peak Context Occupancy calibration (6-tool turn 210k billing != 210k peak context!)
        u_multi = ce.normalize_usage(
            {"input_tokens": 210_000, "cached_input_tokens": 150_000, "output_tokens": 900, "internal_api_calls": 6},
            prompt_chars=3000,
            response_chars=400,
        )
        self.assertEqual(u_multi["billing_input_tokens"], 210_000)
        self.assertLess(u_multi["peak_context_tokens"], 60_000)
        need_ro_multi, _ = ce.should_proactive_rollover(
            {"turns": 2, "last_input_tokens": 210_000, "last_peak_context_tokens": u_multi["peak_context_tokens"], "estimated_cumulative_tokens": u_multi["peak_context_tokens"]},
            "gpt-6.2-sol",
            est_next_tokens=2000,
            cfg=ce.ContextBudgetConfig(),
        )
        self.assertFalse(need_ro_multi, "Multi-tool 6x35k turn MUST NOT prematurely trigger 72% rollover!")

        u_single = ce.normalize_usage(
            {"input_tokens": 210_000, "cached_input_tokens": 115_000, "output_tokens": 900, "internal_api_calls": 1},
            prompt_chars=3000,
            response_chars=400,
        )
        self.assertEqual(u_single["peak_context_tokens"], 210_000)
        need_ro_single, _ = ce.should_proactive_rollover(
            {"turns": 2, "last_input_tokens": 210_000, "last_peak_context_tokens": 210_000, "estimated_cumulative_tokens": 210_000},
            "gpt-6.2-sol",
            est_next_tokens=2000,
            cfg=ce.ContextBudgetConfig(),
        )
        self.assertTrue(need_ro_single, "True single-call 210k occupancy MUST trigger 72% proactive rollover!")

    def test_26_v54_pillar2_sqlite_fts5_bilingual_memory_and_ast_repomap(self) -> None:
        import memory_index as mi

        proj = os.path.join(self.tmp_dir, f"v54_mem_{time.time_ns()}")
        os.makedirs(os.path.join(proj, "src"), exist_ok=True)
        with open(os.path.join(proj, "src", "core_math.py"), "w", encoding="utf-8") as f:
            f.write("class StatEngine:\n    def compute_fdr(self, pvals): return pvals\ndef verify_cohort(df): return True\n")
        with open(os.path.join(proj, "src", "runner.py"), "w", encoding="utf-8") as f:
            f.write("import core_math\ndef run_pipeline(): return core_math.verify_cohort(None)\n")
        with open(os.path.join(proj, "src", "plot.R"), "w", encoding="utf-8") as f:
            f.write("library(ggplot2)\ndraw_kaplan_meier <- function(df) { return(df) }\n")

        # 1. Pure-stdlib AST Structural RepoMap extracts Python/R symbols + ranks imported core_math higher!
        rmap = mi.build_structural_repomap(proj, max_chars=2000, focus_terms=["StatEngine"])
        self.assertIn("class StatEngine", rmap)
        self.assertIn("def verify_cohort", rmap)
        self.assertIn("draw_kaplan_meier()", rmap)
        self.assertLess(rmap.find("core_math.py"), rmap.find("runner.py"), "Imported central module should rank ahead of leaf!")

        # 2. Project-scoped SQLite FTS5 bilingual search (Chinese phrases + English code/metrics)
        idx = mi.ProjectMemoryIndex(proj)
        self.assertFalse(os.path.abspath(idx.db_path).upper().startswith("C:\\"))
        self.assertTrue(idx.stats()["fts5_enabled"])
        idx.upsert_doc(
            doc_id="TASK-OLD:snap:1",
            task_id="TASK-OLD",
            kind="snapshot",
            title="队列生存分析与长上下文压缩验证",
            content="发现队列样本量漂移 BLOCKING，关键指标 p_adj=0.0042，已在 core_math.py 修复。",
            blob_path="D:/gemini/tmp/fake_snap.json",
            anchors={"file_refs": ["src/core_math.py"], "numeric_claims": ["p_adj=0.0042"]},
        )
        hits_zh = idx.search("样本量漂移", limit=5)
        self.assertGreaterEqual(len(hits_zh), 1)
        self.assertEqual(hits_zh[0]["task_id"], "TASK-OLD")
        hits_en = idx.search("core_math p_adj", limit=5)
        self.assertGreaterEqual(len(hits_en), 1)

        # 3. Automatic cross-task memory recall in live Orchestrator Task 2 (never recalling self!)
        seen_prompts = []

        def mem_runner(prompt, session_id, **kwargs):
            seen_prompts.append(prompt)
            return CodexExecutionResult(
                session_id=session_id or f"ms-{len(seen_prompts)}",
                response_text="实证核查完成：队列样本量漂移已在 core_math.py 确认 p_adj=0.0042。",
                partial_text="",
                exit_code=0,
                turn_completed=True,
                requested_config=kwargs["requested_config"],
                accepted_config=kwargs["accepted_config"],
            )

        orch = TeamOrchestrator(
            state_file=self.state_path,
            adapter=CodexAdapter(runner_override=mem_runner),
            agy_adapter=AgyAdapter(runner_override=mem_runner),
            auto_start_scheduler=True,
        )
        try:
            orch.switch_project(proj)
            t2 = orch.create_task(
                title="新任务跨记忆召回",
                goal="请复核 core_math.py 中的样本量漂移问题",
                execution_mode="single_member",
                target_member_id="codex_author",
            )
            dl = time.time() + 10.0
            while time.time() < dl:
                cur = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == t2["id"])
                if cur["status"] == "succeeded":
                    break
                time.sleep(0.05)
            self.assertGreaterEqual(len(seen_prompts), 1)
            self.assertIn("跨任务项目历史记忆召回", seen_prompts[0])
            self.assertIn("TASK-OLD", seen_prompts[0])
            self.assertIn("AST RepoMap", seen_prompts[0])
            sres = orch.search_project_memory("样本量漂移")
            self.assertGreaterEqual(len(sres["hits"]), 1)
        finally:
            orch.stop_scheduler()

    def test_27_v54_pillar3_dual_gate_vlm_visual_critique_and_self_repair_loop(self) -> None:
        proj = os.path.join(self.tmp_dir, f"v54_vqa_{time.time_ns()}")
        os.makedirs(os.path.join(proj, "figures"), exist_ok=True)
        svg_p = os.path.join(proj, "figures", "fig_vqa.svg")
        with open(svg_p, "w", encoding="utf-8") as f:
            f.write(
                '<svg xmlns="http://www.w3.org/2000/svg" width="89mm" height="60mm" viewBox="0 0 252.28 170.08">'
                '<rect width="100%" height="100%" fill="#ffffff"/>'
                '<text x="10" y="20" font-size="7pt">Panel A Control Treated</text></svg>'
            )
        csv_p = os.path.join(proj, "figures", "fig_vqa.csv")
        with open(csv_p, "w", encoding="utf-8", newline="") as f:
            f.write("group,val,n,p_adj\nControl,10,24,0.02\nTreated,20,24,0.02\n")
        mf_p = os.path.join(proj, "figures", "fig_vqa_manifest.json")
        with open(mf_p, "w", encoding="utf-8") as f:
            json.dump({
                "figure_id": "fig_vqa",
                "target_width_mm": 89.0,
                "exports": {"svg": "figures/fig_vqa.svg"},
                "panel_sources": [{
                    "panel_id": "A",
                    "source_table": "figures/fig_vqa.csv",
                    "key_col": "group",
                    "value_col": "val",
                    "n_col": "n",
                    "p_col": "p_adj",
                    "expected_rows": [{"key": "Control", "plotted_value": 10.0, "plotted_n": 24, "plotted_p": 0.02}],
                }],
            }, f)

        vqa_round_seen = {"n": 0}
        prompt_seen = []

        def vqa_runner(prompt, session_id, **kwargs):
            prompt_seen.append(prompt)
            if "VLM 科研图件视觉质检输出格式契约" in prompt:
                vqa_round_seen["n"] += 1
                if vqa_round_seen["n"] == 1:
                    verdict_json = json.dumps({
                        "verdict": "REVISE",
                        "visual_score": 5.8,
                        "defects": [{"severity": "BLOCKING", "region": "Panel A legend", "issue": "图例遮挡右上角散点", "fix": "将图例移至底部居中"}],
                        "summary": "发现图例遮挡散点，需调整位置后重绘",
                    }, ensure_ascii=False)
                else:
                    verdict_json = json.dumps({
                        "verdict": "PASS",
                        "visual_score": 9.3,
                        "defects": [],
                        "summary": "重绘后版面均衡、零遮挡、字号清晰，达到顶刊出版标准",
                    }, ensure_ascii=False)
                return CodexExecutionResult(
                    session_id=session_id or f"vqa-s-{vqa_round_seen['n']}",
                    response_text=f"```json\n{verdict_json}\n```",
                    partial_text="",
                    exit_code=0,
                    turn_completed=True,
                    requested_config=kwargs["requested_config"],
                    accepted_config=kwargs["accepted_config"],
                )
            return CodexExecutionResult(
                session_id=session_id or f"plot-s-{len(prompt_seen)}",
                response_text=f"已完成绘图脚本与 manifest 导出 (turn={len(prompt_seen)})。",
                partial_text="",
                exit_code=0,
                turn_completed=True,
                requested_config=kwargs["requested_config"],
                accepted_config=kwargs["accepted_config"],
            )

        orch = TeamOrchestrator(
            state_file=self.state_path,
            adapter=CodexAdapter(runner_override=vqa_runner),
            agy_adapter=AgyAdapter(runner_override=vqa_runner),
            auto_start_scheduler=True,
        )
        try:
            orch.switch_project(proj)
            tk = orch.create_task(
                title="双门禁VLM视觉闭环测试",
                goal="绘制 Nature 单栏主图并通过机械与视觉双重质检",
                execution_mode="single_member",
                target_member_id="codex_author",
                sandbox="workspace-write",
                research_spec={
                    "figure_manifests": ["figures/fig_vqa_manifest.json"],
                    "block_on_failed_signoff": True,
                    "figure_contract": {
                        "backend": "python",
                        "target_width_mm": 89,
                        "auto_visual_qa": True,
                        "export_drawio": True,
                        "max_visual_repairs": 2,
                        "core_claim": "Treated group doubles mean response (p_adj=0.02)",
                    },
                },
            )
            dl = time.time() + 12.0
            cur = tk
            while time.time() < dl:
                cur = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == tk["id"])
                if cur["status"] in ("succeeded", "failed", "needs_ruling") and len(cur["steps"]) >= 4:
                    break
                time.sleep(0.05)
            self.assertEqual(cur["status"], "succeeded", f"Expected dual-gate loop to pass, got {cur['status']} / {cur.get('blocking_reason')}")
            self.assertEqual(len(cur["steps"]), 4)  # plot -> VQA#1 (REVISE) -> auto-repair -> VQA#2 (PASS)
            self.assertEqual(len(cur.get("visual_qa_reports") or []), 2)
            self.assertEqual(cur["visual_qa_reports"][0]["verdict"], "REVISE")
            self.assertEqual(cur["visual_qa_reports"][1]["verdict"], "PASS")
            self.assertEqual(cur["visual_qa_reports"][1]["visual_score"], 9.3)
            self.assertNotEqual(cur["steps"][1]["member_id"], "codex_author", "Visual critic MUST be distinct from plotter!")
            self.assertIn(".drawio", prompt_seen[0])
            self.assertIn("已完成绘图脚本", cur["summary"], "VLM JSON verdict must not overwrite main deliverable summary!")
        finally:
            orch.stop_scheduler()

    def test_28_v54_pillar4_git_worktree_parallel_isolation_diff_and_safe_merge(self) -> None:
        import subprocess
        import threading
        import worktree_manager as wm

        repo = os.path.join(self.tmp_dir, f"v54_git_wt_{time.time_ns()}")
        os.makedirs(repo, exist_ok=True)
        with open(os.path.join(repo, "README.md"), "w", encoding="utf-8") as f:
            f.write("# Test Repo\n")
        for args in (
            ["init"],
            ["config", "user.name", "Test"],
            ["config", "user.email", "test@local"],
            ["add", "-A"],
            ["commit", "-m", "init"],
        ):
            r = subprocess.run(["git"] + args, cwd=repo, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"git {args} failed: {r.stderr}")

        det = wm.detect_git_repo(repo)
        self.assertTrue(det["is_git"])

        active_cwds = []
        overlap_seen = {"parallel": False, "in_flight": 0}
        lock_p = threading.Lock()

        def wt_runner(prompt, session_id, **kwargs):
            cwd_w = kwargs["cwd"]
            with lock_p:
                active_cwds.append(cwd_w)
                overlap_seen["in_flight"] += 1
                if overlap_seen["in_flight"] >= 2:
                    overlap_seen["parallel"] = True
            time.sleep(0.25)
            # Write distinct files inside each isolated worktree!
            fname = "mod_a.py" if "WRITE_A" in prompt else "mod_b.py"
            with open(os.path.join(cwd_w, fname), "w", encoding="utf-8") as wf:
                wf.write(f"# written by {fname}\nVALUE = 42\n")
            with lock_p:
                overlap_seen["in_flight"] -= 1
            return CodexExecutionResult(
                session_id=session_id or f"wt-{fname}",
                response_text=f"Wrote {fname} in isolated worktree.",
                partial_text="",
                exit_code=0,
                turn_completed=True,
                requested_config=kwargs["requested_config"],
                accepted_config=kwargs["accepted_config"],
            )

        orch = TeamOrchestrator(
            state_file=self.state_path,
            adapter=CodexAdapter(runner_override=wt_runner),
            agy_adapter=AgyAdapter(runner_override=wt_runner),
            auto_start_scheduler=True,
        )
        try:
            orch.switch_project(repo)
            orch.update_binding(max_concurrency=4, enable_git_worktree_isolation=True)
            orch.upsert_member(
                member_id="codex_author_2",
                role_id="implementation",
                name="Codex Parallel Worker 2",
                client="codex",
                model="gpt-6.2-astra",
                reasoning_effort="medium",
                sandbox="workspace-write",
            )
            tk = orch.create_task(
                title="Git Worktree 并行写盘隔离测试",
                goal="两位写盘专员并行改代码并安全合并",
                dispatch_mode="coordinator_plan",
                execution_mode=None,
                sandbox="workspace-write",
                enable_worktree_isolation=True,
                steps=[
                    {"title": "写盘A", "member_id": "codex_author", "prompt": "WRITE_A", "depends_on": [], "read_only": False, "sandbox": "workspace-write"},
                    {"title": "写盘B", "member_id": "codex_author_2", "prompt": "WRITE_B", "depends_on": [], "read_only": False, "sandbox": "workspace-write"},
                ],
            )
            dl = time.time() + 12.0
            cur = tk
            while time.time() < dl:
                cur = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == tk["id"])
                if cur["status"] in ("succeeded", "failed"):
                    break
                time.sleep(0.05)
            self.assertEqual(cur["status"], "succeeded", f"test_29 roundtable failed: {cur.get('blocking_reason')} / steps={[ (s['step_index'], s['status'], s.get('blocking_reason')) for s in cur['steps'] ]}")
            self.assertTrue(overlap_seen["parallel"], "Two worktree-isolated writable steps MUST execute in parallel!")
            self.assertEqual(len(set(active_cwds)), 2)
            # Main repo MUST remain untouched before merge!
            self.assertFalse(os.path.exists(os.path.join(repo, "mod_a.py")))
            self.assertFalse(os.path.exists(os.path.join(repo, "mod_b.py")))

            wts = orch.get_task_worktrees(tk["id"])
            self.assertEqual(len(wts["worktrees"]), 2)
            self.assertTrue(wts["worktrees"][0]["live_diff"]["has_changes"])

            # Dirty main repo guard: refuses to clobber unsaved edits!
            dirty_p = os.path.join(repo, "README.md")
            with open(dirty_p, "a", encoding="utf-8") as df:
                df.write("unsaved local edit\n")
            with self.assertRaises(ValueError):
                orch.merge_task_worktree(tk["id"], 0)
            subprocess.run(["git", "checkout", "--", "README.md"], cwd=repo, capture_output=True)

            # Safe merge BOTH branches into main & auto-cleanup!
            m0 = orch.merge_task_worktree(tk["id"], 0, cleanup_after=True)
            m1 = orch.merge_task_worktree(tk["id"], 1, cleanup_after=True)
            self.assertTrue(m0["merge"]["merged"])
            self.assertTrue(m1["merge"]["merged"])
            self.assertTrue(os.path.isfile(os.path.join(repo, "mod_a.py")))
            self.assertTrue(os.path.isfile(os.path.join(repo, "mod_b.py")))
            self.assertFalse(os.path.isdir(wts["worktrees"][0]["worktree_path"]))
            self.assertFalse(os.path.isdir(wts["worktrees"][1]["worktree_path"]))
        finally:
            orch.stop_scheduler()

    def test_29_v54_pillar5_anonymous_peer_review_borda_voting_and_token_budget_cap(self) -> None:
        import re
        r2_prompts = []
        synth_prompts = []

        def anon_runner(prompt, session_id, **kwargs):
            if "匿名互评与 Borda 共识计票协议" in prompt:
                r2_prompts.append(prompt)
                # Extract peer labels dynamically from prompt line: 其余待评席位为：席位 X, 席位 Y
                m = re.search(r"其余待评席位为：([^\n]+)", prompt)
                peers = [x.strip() for x in (m.group(1).split(",") if m else []) if x.strip()]
                # Always rank alphabetically first peer #1 so deterministic winner emerges
                peers_sorted = sorted(peers)
                ballot = json.dumps({"ranking": peers_sorted, "confidence": 0.9, "rationale": "实证锚点最扎实"}, ensure_ascii=False)
                return CodexExecutionResult(
                    session_id=session_id or f"a2-{len(r2_prompts)}",
                    response_text=f"互评完毕。\n```peer_ranking\n{ballot}\n```",
                    partial_text="",
                    exit_code=0,
                    turn_completed=True,
                    usage={"input_tokens": 5000, "cached_input_tokens": 2000, "output_tokens": 200},
                    requested_config=kwargs["requested_config"],
                    accepted_config=kwargs["accepted_config"],
                )
            if "作为圆桌主持人" in prompt:
                synth_prompts.append(prompt)
            return CodexExecutionResult(
                session_id=session_id or f"a1-{time.time_ns()}",
                response_text="独立首轮分析完成。",
                partial_text="",
                exit_code=0,
                turn_completed=True,
                usage={"input_tokens": 5000, "cached_input_tokens": 1000, "output_tokens": 200},
                requested_config=kwargs["requested_config"],
                accepted_config=kwargs["accepted_config"],
            )

        orch = TeamOrchestrator(
            state_file=self.state_path,
            adapter=CodexAdapter(runner_override=anon_runner),
            agy_adapter=AgyAdapter(runner_override=anon_runner),
            auto_start_scheduler=True,
        )
        try:
            # 1. Anonymous Peer Review + Deterministic Borda Consensus Tally!
            tk = orch.create_task(
                title="匿名互评与Borda计票测试",
                goal="评估多组学归因模型路线",
                execution_mode="roundtable",
                participants=["codex_author", "agy_gemini", "agy_claude_reviewer"],
                host_member_id="agy_gemini",
                discussion_rounds=2,
                enable_host_opening=False,
                anonymous_peer_review=True,
            )
            dl = time.time() + 12.0
            cur = tk
            while time.time() < dl:
                cur = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == tk["id"])
                if cur["status"] in ("succeeded", "failed"):
                    break
                time.sleep(0.05)
            self.assertEqual(cur["status"], "succeeded", f"test_29 roundtable failed: {cur.get('blocking_reason')} / steps={[ (s['step_index'], s['status'], s.get('blocking_reason')) for s in cur['steps'] ]}")
            self.assertEqual(len(r2_prompts), 3)
            # Round 2 shared chat headers MUST use anonymized 席位 A/B/C instead of real names!
            self.assertTrue(any("席位 A" in p and "席位 B" in p for p in r2_prompts))
            self.assertNotIn("[第 1 轮 · 盲评立论] OpenScience 科研首席", r2_prompts[0])
            bs = cur.get("borda_scoreboard") or {}
            self.assertEqual(bs.get("ballots_count"), 3)
            self.assertIsNotNone(bs.get("winner_member_id"))
            self.assertTrue(any("匿名互评 Borda 共识计票结果" in p for p in synth_prompts))

            # 2. Per-Task Token Budget Hard Ceiling -> pauses in needs_ruling & resumes cleanly on ruling accept!
            def heavy_runner(prompt, session_id, **kwargs):
                return CodexExecutionResult(
                    session_id=session_id or "hb-1",
                    response_text="Step complete",
                    partial_text="",
                    exit_code=0,
                    turn_completed=True,
                    usage={"input_tokens": 120_000, "cached_input_tokens": 10_000, "output_tokens": 500},
                    requested_config=kwargs["requested_config"],
                    accepted_config=kwargs["accepted_config"],
                )
            orch.adapter = CodexAdapter(runner_override=heavy_runner)
            orch.agy_adapter = AgyAdapter(runner_override=heavy_runner)

            t_cap = orch.create_task(
                title="Token预算硬熔断测试",
                goal="测试单任务预算上限拦截与裁决放行",
                dispatch_mode="coordinator_plan",
                execution_mode=None,
                task_token_budget=100_000,
                steps=[
                    {"title": "重计算步0", "member_id": "codex_author", "prompt": "S0", "depends_on": [], "read_only": True, "sandbox": "read-only"},
                    {"title": "下游步1", "member_id": "agy_gemini", "prompt": "S1", "depends_on": [0], "read_only": True, "sandbox": "read-only"},
                ],
            )
            dl = time.time() + 10.0
            cur_c = t_cap
            while time.time() < dl:
                cur_c = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == t_cap["id"])
                if cur_c["status"] in ("needs_ruling", "succeeded", "failed"):
                    break
                time.sleep(0.05)
            self.assertEqual(cur_c["status"], "needs_ruling")
            self.assertIn("预算熔断", cur_c.get("blocking_reason") or "")
            self.assertEqual(cur_c["steps"][1]["status"], "pending", "Step 1 MUST NOT launch past budget cap!")

            # Ruling accept overrides token cap and finishes Step 1 cleanly!
            orch.submit_ruling(t_cap["id"], 0, decision="accept", reason="批准追加预算完成终审")
            dl = time.time() + 10.0
            cur_c2 = cur_c
            while time.time() < dl:
                cur_c2 = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == t_cap["id"])
                if cur_c2["status"] == "succeeded":
                    break
                time.sleep(0.05)
            self.assertEqual(cur_c2["status"], "succeeded")
        finally:
            orch.stop_scheduler()

    def test_30_v55_perf_zero_idle_writes_slim_304_redos_and_resilient_failover(self) -> None:
        """v5.5 Fluency & Calling Resilience:
        1. Zero idle disk writes / zero idle rev churn on _schedule_tick()
        2. Debounced telemetry mutate() skips synchronous fsync until flush_if_dirty()
        3. slim_snapshot() dramatically shrinks payload & strips non-focused history while preserving focused chat_room
        4. Effort-aware resolve_idle_timeout() protects high/xhigh reasoning from false 240s kills
        5. Codex read-only shell policy_blocked & watchdog_idle_timeout auto-failover cleanly
        6. sidebar_panel.html Markdown link parser is strictly O(N) zero-ReDoS even on 500-char unclosed links!
        """
        import subprocess
        from codex_adapter import resolve_idle_timeout

        # (4) Effort-aware idle watchdog timeouts
        self.assertEqual(resolve_idle_timeout("high"), 480.0)
        self.assertEqual(resolve_idle_timeout("xhigh"), 600.0)
        self.assertEqual(resolve_idle_timeout("max"), 600.0)
        self.assertEqual(resolve_idle_timeout("medium"), 300.0)
        self.assertEqual(resolve_idle_timeout("low"), 240.0)
        self.assertEqual(resolve_idle_timeout("high", 123.0), 123.0)

        calls: List[Tuple[str, str]] = []

        def fake_codex(prompt, session_id=None, model=None, reasoning_effort=None, **kw):
            calls.append(("codex", str(model)))
            # Simulate Windows Codex read-only PowerShell policy block on first attempt
            return CodexExecutionResult(
                session_id="sess-codex-blocked",
                response_text="",
                partial_text="",
                exit_code=1,
                error="命令被策略拒绝: powershell.exe -Command '$env:PYTHONIOENCODING=...'",
                blocking_reason="命令被策略拒绝: powershell.exe -Command '$env:PYTHONIOENCODING=...'",
                policy_blocked=True,
                turn_completed=False,
                requested_config=kw.get("requested_config") or {},
                accepted_config=kw.get("accepted_config") or {},
            )

        def fake_agy(prompt, session_id=None, model=None, reasoning_effort=None, **kw):
            calls.append(("antigravity", str(model)))
            return CodexExecutionResult(
                session_id="sess-agy-ok",
                response_text="Antigravity 原生 read_file 接力核查完成：关键结论已核验。" + (" 数据行验证" * 20),
                partial_text="",
                exit_code=0,
                turn_completed=True,
                requested_config=kw.get("requested_config") or {},
                accepted_config=kw.get("accepted_config") or {},
            )

        orch = TeamOrchestrator(
            state_file=self.state_path,
            adapter=CodexAdapter(runner_override=fake_codex),
            agy_adapter=AgyAdapter(runner_override=fake_agy),
            auto_start_scheduler=True,
        )
        try:
            # (1) Zero idle disk writes & rev churn when idle
            rev0 = orch.store.get_rev()
            mtime0 = os.path.getmtime(orch.store.state_file)
            for _ in range(5):
                orch._schedule_tick()
            self.assertEqual(orch.store.get_rev(), rev0, "Idle _schedule_tick MUST NOT mutate state or bump rev!")
            self.assertEqual(os.path.getmtime(orch.store.state_file), mtime0, "Idle _schedule_tick MUST NOT write disk!")

            # (2) Debounced mutate bumps rev immediately without writing disk until flush_if_dirty()
            rev1 = orch.store.get_rev()
            orch.store.mutate(lambda st: st["binding"].__setitem__("_perf_probe", 1), persist="debounced")
            self.assertEqual(orch.store.get_rev(), rev1 + 1)
            self.assertTrue(orch.store._dirty)
            self.assertTrue(orch.store.flush_if_dirty())
            self.assertFalse(orch.store._dirty)

            # (5) Codex read-only shell policy_blocked auto-fails over cross-engine to Antigravity!
            t_ro = orch.create_task(
                title="只读沙箱策略受阻自动跨引擎切替补测试",
                goal="测试 Codex 只读策略拦截后自动切 Antigravity 原生读取替补",
                dispatch_mode="coordinator_plan",
                execution_mode=None,
                sandbox="read-only",
                steps=[
                    {"title": "只读数据核验", "member_id": "codex_author", "prompt": "核查数据", "depends_on": [], "read_only": True, "sandbox": "read-only"},
                ],
            )
            dl = time.time() + 10.0
            cur = t_ro
            while time.time() < dl:
                cur = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == t_ro["id"])
                if cur["status"] in ("succeeded", "failed", "blocked"):
                    break
                time.sleep(0.05)
            self.assertEqual(cur["status"], "succeeded", f"Expected cross-engine auto-failover success, got {cur.get('status')}: {cur.get('blocking_reason')}")
            self.assertTrue(any(c[0] == "codex" for c in calls) and any(c[0] == "antigravity" for c in calls), f"Expected codex -> antigravity failover, calls={calls}")
            self.assertTrue(any(ev.get("type") == "failover.codex_ro_policy" for ev in cur.get("recent_events", [])))

            # (3) slim_snapshot strips non-focused history bloat & duplicate attempt blobs
            big_hist = "X" * 30000
            orch.store.mutate(
                lambda st: st["tasks"].append(
                    {
                        "id": "TASK-big-hist",
                        "title": "历史巨型任务",
                        "goal": "G" * 1000,
                        "project_path": st["binding"]["project_path"],
                        "normalized_project_path": st["binding"]["normalized_project_path"],
                        "project_name": st["binding"]["project_name"],
                        "archived": True,
                        "dispatch_mode": "roundtable",
                        "execution_mode": "roundtable",
                        "status": "succeeded",
                        "summary": big_hist,
                        "chat_room": [{"id": "C1", "content": big_hist}, {"id": "C2", "content": big_hist}],
                        "steps": [{"step_index": 0, "member_id": "agy_gemini", "status": "succeeded", "title": "S0", "latest_result_text": big_hist, "attempts": [{"result_text": big_hist}]}],
                        "created_at": time.time() - 100,
                        "updated_at": time.time() - 50,
                    }
                )
            )
            full_bytes = len(json.dumps(orch.store.snapshot(), ensure_ascii=False).encode("utf-8"))
            slim_rev, slim_obj = orch.store.slim_snapshot(focus_task_id=t_ro["id"])
            slim_bytes = len(json.dumps(slim_obj, ensure_ascii=False).encode("utf-8"))
            self.assertEqual(slim_rev, orch.store.get_rev())
            self.assertLess(slim_bytes, full_bytes * 0.35, f"slim_snapshot ({slim_bytes}B) should be << full ({full_bytes}B)")
            hist_item = next(x for x in slim_obj["tasks"] if x["id"] == "TASK-big-hist")
            self.assertEqual(hist_item["chat_count"], 2)
            self.assertEqual(hist_item["chat_room"], [])
            foc_item = next(x for x in slim_obj["tasks"] if x["id"] == t_ro["id"])
            self.assertFalse(foc_item.get("_slim"))
            self.assertGreaterEqual(len(foc_item.get("chat_room") or []), 1)

            # (6) Zero-ReDoS linear Markdown parser test in Node.js (if node available)
            with open(os.path.join(PROTO_DIR, "sidebar_panel.html"), encoding="utf-8") as _hf:
                html_src = _hf.read()
            m_start = html_src.index("function _fastStrHash")
            m_end = html_src.index("// ==================== NAVIGATION")
            md_snippet = html_src[m_start:m_end]
            js_test = (
                "const FOCUSED_TASK_ID='T1'; let FIG_PROOF_FILTER='normal'; const MD_RENDER_CACHE=new Map();\n"
                "const esc=s=>String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\"/g,'&quot;').replace(/'/g,'&#39;');\n"
                "const svgIcon=()=>'<svg></svg>'; const proofFilterClass=()=>'';\n"
                + md_snippet +
                "\nconst evil='[label](' + 'a_b_c_d_1234567890_'.repeat(30);\n"
                "const t0=performance.now(); const r1=formatInlineMd(esc(evil),'T1'); const dt=performance.now()-t0;\n"
                "const r2=formatInlineMd(esc('[图1](dir/fig(1).png) and ![alt](dir/plot(2).svg)'),'T1');\n"
                "console.log(JSON.stringify({dt_ms: dt, hasLink: r2.includes('data-ans-link=\"dir/fig(1).png\"'), hasImg: r2.includes('dir%2Fplot(2).svg')}));\n"
            )
            proc = subprocess.run(["node", "-e", js_test], capture_output=True, text=True, timeout=5)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            res_js = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertLess(res_js["dt_ms"], 50.0, f"ReDoS regression! took {res_js['dt_ms']}ms")
            self.assertTrue(res_js["hasLink"], f"Nested-paren link failed: {res_js}")
            self.assertTrue(res_js["hasImg"], f"Nested-paren image failed: {res_js}")
        finally:
            orch.stop_scheduler()

    def test_31_v55_codex_configurable_write_permissions_and_hidden_subprocess(self) -> None:
        """Verify configurable Codex file-edit permissions (per-member, batch, global ceiling, roundtable) and Win32 zero-popup subprocess flags."""
        from codex_adapter import CREATE_NO_WINDOW, hidden_subprocess_kwargs

        if os.name == "nt":
            hk = hidden_subprocess_kwargs(new_process_group=True)
            self.assertTrue(hk["creationflags"] & CREATE_NO_WINDOW)
            self.assertEqual(hk["creationflags"] & 0x00000008, 0, "MUST NOT mix DETACHED_PROCESS with CREATE_NO_WINDOW!")
            self.assertEqual(hk["startupinfo"].wShowWindow, 0)

        def ok_runner(prompt, session_id=None, **kw):
            return CodexExecutionResult(
                session_id=session_id or "s-perm",
                response_text="ok",
                partial_text="",
                exit_code=0,
                turn_completed=True,
                requested_config=kw.get("requested_config") or {},
                accepted_config=kw.get("accepted_config") or {},
            )

        orch = TeamOrchestrator(
            state_file=self.state_path,
            adapter=CodexAdapter(runner_override=ok_runner),
            agy_adapter=AgyAdapter(runner_override=ok_runner),
            auto_start_scheduler=False,
        )
        try:
            # 1. Batch grant workspace-write to all Codex members
            res_w = orch.set_members_sandbox(sandbox="workspace-write", client="codex")
            self.assertEqual(res_w["sandbox"], "workspace-write")
            self.assertIn("codex_author", res_w["updated"])
            snap = orch.store.snapshot()
            for mid, m in snap["members"].items():
                if m.get("client") == "codex":
                    self.assertEqual(m["sandbox"], "workspace-write")
            # Keep agy_gemini read-only to test mixed roundtable write permissions
            orch.set_members_sandbox(sandbox="read-only", member_id="agy_gemini")

            # 2. Global binding settings: default_task_sandbox + roundtable_allow_member_writes
            orch.update_binding(max_sandbox="workspace-write", default_task_sandbox="workspace-write", roundtable_allow_member_writes=True)
            t_rw = orch.create_task(
                title="可写圆桌测试",
                goal="测试 Codex 成员在可写圆桌下获取写权限而只读成员仍受只读约束",
                execution_mode="roundtable",
                participants=["codex_author", "agy_gemini"],
                host_member_id="agy_gemini",
                discussion_rounds=1,
                sandbox="workspace-write",
                allow_member_writes=True,
            )
            step_codex = next(s for s in t_rw["steps"] if s["step_type"] == "discussion" and s["member_id"] == "codex_author")
            step_agy = next(s for s in t_rw["steps"] if s["step_type"] == "discussion" and s["member_id"] == "agy_gemini")
            self.assertFalse(step_codex["read_only"])
            self.assertEqual(step_codex["sandbox"], "workspace-write")
            self.assertTrue(step_agy["read_only"])
            self.assertEqual(step_agy["sandbox"], "read-only")

            # 3. Hard ceiling lock: max_sandbox='read-only' blocks workspace-write grant and rejects danger-full-access
            orch.update_binding(max_sandbox="read-only")
            with self.assertRaises(ValueError):
                orch.set_members_sandbox(sandbox="workspace-write", member_id="codex_author")
            with self.assertRaises(ValueError):
                orch.set_members_sandbox(sandbox="danger-full-access", member_id="codex_author")
        finally:
            orch.stop_scheduler()

    def test_32_v55_version_update_checker_and_old_version_notification(self) -> None:
        """Verify SemVer comparator, remote GitHub VERSION.json & Releases API fallback, and offline safety for old-version update popups."""
        import version_checker as vc

        # 1. SemVer comparison accuracy
        self.assertEqual(vc.compare_versions("5.4.0", "5.5.0"), -1)
        self.assertEqual(vc.compare_versions("v5.5.0", "5.5.0"), 0)
        self.assertEqual(vc.compare_versions("5.10.0", "5.5.0"), 1)
        self.assertEqual(vc.compare_versions("5.5.0", "5.5.1"), -1)

        # 2. Local VERSION.json loads cleanly
        loc = vc.get_local_version_info()
        self.assertGreaterEqual(vc.compare_versions(loc["version"], "5.5.0"), 0)
        self.assertGreaterEqual(loc["server_rev"], 19)

        # 3. Simulated older installation (v5.4.0) detects newer GitHub VERSION.json (v5.5.0)
        old_ver_file = os.path.join(self.tmp_dir, "OLD_VERSION.json")
        with open(old_ver_file, "w", encoding="utf-8") as f:
            json.dump({"version": "5.4.0", "server_rev": 16, "repo": "QUDAD-ENT/agy-roundtable"}, f)

        def fake_raw_fetch(url: str, timeout: float) -> Dict[str, Any]:
            if "VERSION.json" in url:
                return {
                    "version": "5.5.0",
                    "server_rev": 19,
                    "release_date": "2026-11-20",
                    "release_url": "https://github.com/QUDAD-ENT/agy-roundtable/releases/tag/v5.5.0",
                    "title_zh": "新版本 v5.5.0",
                    "highlights_zh": ["零 ReDoS", "自动更新弹窗"],
                }
            raise RuntimeError("unexpected")

        res_upd = vc.check_for_updates(force=True, version_file=old_ver_file, fetcher=fake_raw_fetch)
        self.assertTrue(res_upd["ok"])
        self.assertTrue(res_upd["has_update"])
        self.assertEqual(res_upd["current_version"], "5.4.0")
        self.assertEqual(res_upd["latest_version"], "5.5.0")
        self.assertEqual(res_upd["source"], "github_raw_version_json")
        self.assertIn("零 ReDoS", res_upd["highlights_zh"])

        # 4. Fallback to GitHub Releases API when raw VERSION.json fails
        def fake_releases_only(url: str, timeout: float) -> Dict[str, Any]:
            if "VERSION.json" in url:
                raise RuntimeError("404 raw")
            return {
                "tag_name": "v5.6.0",
                "name": "AGY Roundtable v5.6.0",
                "body": "- Feature A\n- Feature B",
                "html_url": "https://github.com/QUDAD-ENT/agy-roundtable/releases/tag/v5.6.0",
                "published_at": "2026-11-25T10:00:00Z",
            }

        res_rel = vc.check_for_updates(force=True, version_file=old_ver_file, fetcher=fake_releases_only)
        self.assertTrue(res_rel["has_update"])
        self.assertEqual(res_rel["latest_version"], "5.6.0")
        self.assertEqual(res_rel["source"], "github_releases_api")
        self.assertEqual(res_rel["highlights_zh"], ["Feature A", "Feature B"])

        # 5. Offline degradation never throws and reports has_update=False safely
        def fake_offline(url: str, timeout: float) -> Dict[str, Any]:
            raise OSError("network unreachable")

        res_off = vc.check_for_updates(force=True, version_file=old_ver_file, fetcher=fake_offline)
        self.assertTrue(res_off["ok"])
        self.assertFalse(res_off["has_update"])
        self.assertEqual(res_off["source"], "local_only")
        self.assertTrue(bool(res_off["offline_or_error"]))

    def test_33_v551_win32_crash_dialog_suppression_and_ro_rscript_guard(self) -> None:
        """Verify Win32 SetErrorMode crash-dialog suppression and Windows read-only Rscript crash guardrail."""
        from codex_adapter import suppress_windows_crash_dialogs

        if os.name == "nt":
            import ctypes
            suppress_windows_crash_dialogs()
            cur_mode = int(ctypes.windll.kernel32.GetErrorMode())
            self.assertTrue(cur_mode & 0x0001, f"SEM_FAILCRITICALERRORS not set: {cur_mode:#x}")
            self.assertTrue(cur_mode & 0x0002, f"SEM_NOGPFAULTERRORBOX not set: {cur_mode:#x}")

        with open(os.path.join(PROTO_DIR, "codex_adapter.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("严禁在 read-only 只读沙箱内调用 Rscript", src)
        self.assertIn("0xc0000005", src)

    def test_34_v56_three_tier_sandbox_opt_in_add_dirs_per_seat_and_mcp_default(self) -> None:
        """Verify v5.6.0 Scheme A: fail-closed danger-full-access opt-in, instant relock clamping, --add-dir propagation, per-seat roundtable overrides, and default sandbox inheritance."""
        proj_dir = os.path.join(self.tmp_dir, f"v56_proj_{time.time_ns()}")
        extra_dir = os.path.join(self.tmp_dir, f"v56_extra_data_{time.time_ns()}")
        os.makedirs(proj_dir, exist_ok=True)
        os.makedirs(extra_dir, exist_ok=True)

        seen_calls: List[Dict[str, Any]] = []

        def fake_runner(prompt, session_id, **kwargs):
            req_cfg = dict(kwargs.get("requested_config") or {})
            seen_calls.append({
                "sandbox": kwargs.get("sandbox"),
                "add_dirs": list(req_cfg.get("add_dirs") or []),
                "cwd": kwargs.get("cwd"),
            })
            return CodexExecutionResult(
                session_id=session_id or f"v56-{len(seen_calls)}",
                response_text="ok",
                partial_text="",
                exit_code=0,
                turn_completed=True,
                requested_config=req_cfg,
                accepted_config=dict(kwargs.get("accepted_config") or {}),
            )

        orch = TeamOrchestrator(
            state_file=self.state_path,
            adapter=CodexAdapter(runner_override=fake_runner),
            agy_adapter=AgyAdapter(runner_override=fake_runner),
            auto_start_scheduler=True,
        )
        try:
            orch.switch_project(proj_dir)

            # 1. Fail-closed by default: danger-full-access is rejected everywhere before opt-in unlock
            self.assertFalse(bool(orch.store.snapshot()["binding"].get("allow_danger_full_access", False)))
            with self.assertRaises(ValueError):
                orch.update_binding(max_sandbox="danger-full-access")
            with self.assertRaises(ValueError):
                orch.set_members_sandbox("danger-full-access", member_id="codex_author")
            with self.assertRaises(ValueError):
                orch.create_task(title="blocked", goal="blocked", execution_mode="single_member", sandbox="danger-full-access")

            # Non-existent --add-dir is rejected cleanly
            with self.assertRaises(ValueError):
                orch.update_binding(extra_add_dirs=[os.path.join(self.tmp_dir, "does_not_exist_999")])

            # 2. Explicit 2-step opt-in unlock + extra_add_dirs + default_task_sandbox inheritance
            orch.update_binding(
                allow_danger_full_access=True,
                max_sandbox="danger-full-access",
                default_task_sandbox="workspace-write",
                extra_add_dirs=[extra_dir],
            )
            st_u = orch.store.snapshot()
            self.assertTrue(st_u["binding"]["allow_danger_full_access"])
            self.assertEqual(st_u["binding"]["max_sandbox"], "danger-full-access")
            self.assertEqual(st_u["binding"]["extra_add_dirs"], [os.path.normpath(os.path.abspath(extra_dir))])

            # sandbox=None (as MCP team_dispatch_task now sends) inherits settings['default_task_sandbox'] = workspace-write!
            t_inh = orch.create_task(
                title="MCP默认继承测试",
                goal="测试默认继承 workspace-write",
                execution_mode="single_member",
                target_member_id="codex_author",
                sandbox=None,
            )
            self.assertEqual(t_inh["sandbox"], "workspace-write")
            self.assertEqual(t_inh["steps"][0]["sandbox"], "workspace-write")
            self.assertIn(os.path.normpath(os.path.abspath(extra_dir)), t_inh["add_dirs"])

            dl = time.time() + 10.0
            while time.time() < dl:
                cur = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == t_inh["id"])
                if cur["status"] == "succeeded":
                    break
                time.sleep(0.05)
            self.assertEqual(cur["status"], "succeeded")
            self.assertEqual(seen_calls[-1]["sandbox"], "workspace-write")
            self.assertIn(os.path.normpath(os.path.abspath(extra_dir)), seen_calls[-1]["add_dirs"])

            # 3. Tier 3 unlocked: member + Roundtable per-seat overrides (codex_author=danger-full-access, agy_gemini=read-only)
            orch.set_members_sandbox("danger-full-access", member_id="codex_author")
            self.assertEqual(orch.store.snapshot()["members"]["codex_author"]["sandbox"], "danger-full-access")
            orch.update_binding(default_task_sandbox="danger-full-access")

            seen_calls.clear()
            t_rt = orch.create_task(
                title="圆桌单席三档权限验证",
                goal="测试单席混合权限与 --add-dir",
                execution_mode="roundtable",
                participants=["codex_author", "agy_gemini"],
                host_member_id="codex_author",
                discussion_rounds=1,
                enable_host_opening=False,
                sandbox="danger-full-access",
                seat_sandboxes={"codex_author": "danger-full-access", "agy_gemini": "read-only"},
            )
            step_codex = next(s for s in t_rt["steps"] if s["step_type"] == "discussion" and s["member_id"] == "codex_author")
            step_agy = next(s for s in t_rt["steps"] if s["step_type"] == "discussion" and s["member_id"] == "agy_gemini")
            self.assertEqual(step_codex["sandbox"], "danger-full-access")
            self.assertFalse(step_codex["read_only"])
            self.assertEqual(step_agy["sandbox"], "read-only")
            self.assertTrue(step_agy["read_only"])

            dl = time.time() + 12.0
            while time.time() < dl:
                cur_rt = next(x for x in orch.store.snapshot()["tasks"] if x["id"] == t_rt["id"])
                if cur_rt["status"] == "succeeded":
                    break
                time.sleep(0.05)
            self.assertEqual(cur_rt["status"], "succeeded")
            sbs_seen = {c["sandbox"] for c in seen_calls}
            self.assertIn("danger-full-access", sbs_seen)
            self.assertIn("read-only", sbs_seen)
            self.assertTrue(all(os.path.normpath(os.path.abspath(extra_dir)) in c["add_dirs"] for c in seen_calls))

            # 4. Instant Relock clamps binding.max_sandbox, settings.default_task_sandbox, and all members[*].sandbox back to workspace-write!
            orch.update_binding(allow_danger_full_access=False)
            st_lk = orch.store.snapshot()
            self.assertFalse(st_lk["binding"]["allow_danger_full_access"])
            self.assertEqual(st_lk["binding"]["max_sandbox"], "workspace-write")
            self.assertEqual(st_lk["settings"]["default_task_sandbox"], "workspace-write")
            self.assertEqual(st_lk["members"]["codex_author"]["sandbox"], "workspace-write")
            with self.assertRaises(ValueError):
                orch.create_task(title="relocked", goal="relocked", execution_mode="single_member", sandbox="danger-full-access")
        finally:
            orch.stop_scheduler()


if __name__ == "__main__":
    unittest.main(verbosity=2)

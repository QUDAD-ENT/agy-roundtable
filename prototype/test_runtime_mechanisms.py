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

            self.assertEqual(cur["status"], "succeeded")
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
            self.assertEqual(cur["status"], "succeeded")
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

            self.assertEqual(cur["status"], "succeeded")
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

            self.assertEqual(cur["status"], "succeeded")
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

            self.assertEqual(cur["status"], "succeeded")
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

            self.assertEqual(cur["status"], "succeeded")

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

        def _make_minimal_png(path: str, width: int, height: int) -> None:
            ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
            with open(path, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr + b"\x00\x00\x00\x00")

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
            # 1. Health check server_rev == 11
            with urllib.request.urlopen(f"{base}/api/health", timeout=5) as resp:
                h = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(h["server_rev"], 11)

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
            self.assertEqual(cur["status"], "succeeded")
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


if __name__ == "__main__":
    unittest.main(verbosity=2)

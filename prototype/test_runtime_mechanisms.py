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
            # Member is working -> switch config with mode='next_round'
            sw = orch.switch_member_config("codex_author", reasoning_effort="high", mode="next_round", reason="测试下轮生效")
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
            self.assertEqual(snap["members"]["codex_author"]["reasoning_effort"], "high")
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


if __name__ == "__main__":
    unittest.main(verbosity=2)

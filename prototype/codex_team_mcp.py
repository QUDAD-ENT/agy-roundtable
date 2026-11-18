"""
Official MCP Server ('codex-team') for Antigravity Coordinator.
Bridges Antigravity MCP tool calls directly to the shared TeamOrchestrator runtime
so both Antigravity Coordinator and the Sidebar Web Panel operate on a single scheduler.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, Optional

PROTO_DIR = os.path.dirname(os.path.abspath(__file__))
if PROTO_DIR not in sys.path:
    sys.path.insert(0, PROTO_DIR)

from mcp.server import FastMCP
from team_cli import TeamRuntimeClient

mcp = FastMCP("codex-team")
_client = TeamRuntimeClient(auto_start=True)

try:
    _client.ensure_server_running()
except Exception:
    pass


def _fmt(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
def team_status() -> str:
    """获取当前绑定的项目路径、总管会话、全体角色与成员状态、排队及运行中任务列表。"""
    return _fmt(_client.get_state())


@mcp.tool()
def team_list_models(refresh_cli: bool = False) -> str:
    """查询本机官方 Codex CLI 登录状态及实际报告的模型与思考强度可用组合。"""
    return _fmt(_client.get_models(refresh=refresh_cli))


@mcp.tool()
def team_switch_project(project_path: str) -> str:
    """自由切换当前活动项目根目录（校验目录存在并同步切换工作区）。"""
    return _fmt(_client._request("POST", "/api/projects/switch", {"project_path": project_path}))


@mcp.tool()
def team_list_projects() -> str:
    """列出本机自动发现的全部课题/软件项目及最近使用项目。"""
    return _fmt(_client._request("GET", "/api/projects"))


@mcp.tool()
def team_dispatch_task(
    title: str,
    goal: str,
    dispatch_mode: str = "single_member",
    execution_mode: Optional[str] = None,
    target_member_id: str = "codex_author",
    reviewer_member_id: str = "agy_claude_reviewer",
    chief_member_id: Optional[str] = None,
    host_member_id: Optional[str] = None,
    participants_json: str = "[]",
    discussion_rounds: int = 2,
    stages_json: str = "[]",
    project_path: Optional[str] = None,
    sandbox: str = "read-only",
    wait: bool = True,
) -> str:
    """发起协作任务，支持 4 种自由组织模式：roundtable(圆桌自主协商)、chief_dispatch(总管自动点将)、custom_dag(自定义多阶段串并行搭积木)、single_member(单人专办) 及 coordinator_auto。"""
    participants = json.loads(participants_json) if participants_json.strip() else None
    stages = json.loads(stages_json) if stages_json.strip() else None
    res = _client.create_task(
        title=title,
        goal=goal,
        dispatch_mode=dispatch_mode,
        execution_mode=execution_mode or dispatch_mode,
        target_member_id=target_member_id,
        reviewer_member_id=reviewer_member_id,
        chief_member_id=chief_member_id,
        host_member_id=host_member_id,
        participants=participants or None,
        discussion_rounds=discussion_rounds,
        stages=stages or None,
        project_path=project_path,
        sandbox=sandbox,
    )["task"]
    if wait:
        res = _client.wait_for_task(res["id"])
    return _fmt(res)


@mcp.tool()
def team_interject_chat(
    task_id: str,
    content: str,
    target_member_id: Optional[str] = None,
    trigger_reply: bool = True,
    wait: bool = False,
) -> str:
    """向任务的共享实时群聊 (chat_room) 发送插话或 @指定成员 立即唤醒答复。"""
    res = _client._request(
        "POST",
        "/api/tasks/interject",
        {
            "task_id": task_id,
            "content": content,
            "target_member_id": target_member_id,
            "trigger_reply": trigger_reply,
        },
    )["task"]
    if wait:
        res = _client.wait_for_task(res["id"])
    return _fmt(res)


@mcp.tool()
def team_followup(task_id: str, prompt: str, wait: bool = True) -> str:
    """沿用原工作者官方 Session ID 追加续聊追问（自动拦截跨轮次冲突）。"""
    res = _client._request("POST", "/api/tasks/followup", {"task_id": task_id, "prompt": prompt})["task"]
    if wait:
        res = _client.wait_for_task(res["id"])
    return _fmt(res)


@mcp.tool()
def team_answer_questions(
    task_id: str,
    answers_json: str = "[]",
    custom_note: str = "",
    use_recommended: bool = False,
    wait: bool = True,
) -> str:
    """当任务处于 awaiting_user 状态（AI 提出选项问题）时，提交用户选择并恢复原步骤继续执行。"""
    parsed_ans = json.loads(answers_json) if answers_json.strip() else []
    res = _client._request(
        "POST",
        "/api/tasks/answer",
        {
            "task_id": task_id,
            "answers": parsed_ans,
            "custom_note": custom_note,
            "use_recommended": use_recommended,
        },
    )["task"]
    if wait:
        res = _client.wait_for_task(res["id"])
    return _fmt(res)


@mcp.tool()
def team_submit_ruling(
    task_id: str,
    step_index: int,
    decision: str,
    reason: str,
    repair_instructions: Optional[str] = None,
    wait: bool = True,
) -> str:
    """提交总管裁决：accept(采纳审查退回原作者修复) / reject(驳回审查放行) / defer(暂缓)。"""
    res = _client._request(
        "POST",
        "/api/tasks/ruling",
        {
            "task_id": task_id,
            "step_index": step_index,
            "decision": decision,
            "reason": reason,
            "repair_instructions": repair_instructions,
        },
    )["task"]
    if wait:
        res = _client.wait_for_task(res["id"])
    return _fmt(res)


@mcp.tool()
def team_reassign_step(task_id: str, step_index: int, new_member_id: str, reason: str, wait: bool = True) -> str:
    """将步骤改派给另一名成员，继承任务ID与上下文交接包并创建新尝试。"""
    res = _client._request(
        "POST",
        "/api/tasks/reassign",
        {
            "task_id": task_id,
            "step_index": step_index,
            "new_member_id": new_member_id,
            "reason": reason,
        },
    )["task"]
    if wait:
        res = _client.wait_for_task(res["id"])
    return _fmt(res)


@mcp.tool()
def team_switch_member_config(
    member_id: str,
    name: Optional[str] = None,
    client: Optional[str] = None,
    role: Optional[str] = None,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    mode: str = "next_round",
    reason: str = "总管调整配置",
) -> str:
    """修改成员显示名称、切换客户端(codex/antigravity)、角色、模型与思考强度；闲置立即生效，运行中支持 next_round 或 abort_now。"""
    return _fmt(
        _client._request(
            "POST",
            "/api/members/switch",
            {
                "id": member_id,
                "name": name,
                "client": client,
                "role": role,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "mode": mode,
                "reason": reason,
            },
        )
    )


@mcp.tool()
def team_control(action: str, task_id: Optional[str] = None, step_index: Optional[int] = None) -> str:
    """生命周期控制：pause / resume_dispatch / abort / continue。"""
    if action == "pause":
        return _fmt(_client._request("POST", "/api/pause", {"paused": True}))
    if action == "resume_dispatch":
        return _fmt(_client._request("POST", "/api/pause", {"paused": False}))
    if action == "abort":
        return _fmt(_client._request("POST", "/api/tasks/abort", {"task_id": task_id, "step_index": step_index}))
    if action == "continue":
        if not task_id:
            raise ValueError("continue 需要提供 task_id")
        return _fmt(_client._request("POST", "/api/tasks/continue", {"task_id": task_id}))
    raise ValueError(f"不支持的动作: {action}")


if __name__ == "__main__":
    mcp.run()

"""LangGraph 房間狀態定義與 SQLite Checkpointer 管理。

所有欄位均為純 JSON 可序列化型別（dict / list / str / int / bool），
確保 SqliteSaver 跨重啟斷點續傳穩定無誤。
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any, Dict, List, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver


class RoomGraphState(TypedDict, total=False):
    room_id: str
    goal: str
    task_mode: str  # "standard" | "triage"
    d1_fast_review: bool
    autonomy: str  # "careful" | "balanced" | "bold"
    global_test_cmd: str
    max_unit_attempts: int

    # 結構化單元清單（UnitSpec.model_dump() 列表）
    units: List[Dict[str, Any]]
    current_idx: int
    current_unit_id: str
    plan_ready: bool
    plan_refinements: int
    replan_reason: str
    replan_count: int
    max_replans: int
    completed_ledger: List[str]

    # 當前單元執行狀態
    unit_attempts: int
    head_before_impl: str
    last_error_tail: str
    prev_error_tail: str
    change_strategy_hint: bool
    last_verify: Dict[str, Any]
    last_review: Dict[str, Any]
    last_diagnosis: Dict[str, Any]
    waive_current: bool
    waive_reason: str

    # 已完成單元 Artifact（unit_id -> UnitResult.model_dump()）
    artifacts: Dict[str, Dict[str, Any]]

    # 外迴圈全盤覆檢輪次
    audit_round: int
    max_audit_rounds: int

    # 流程控制與人工介入
    next_action: str
    human_reason: str
    human_detail: str
    human_kind: str
    completed: bool
    paused: bool
    error: str


def open_sqlite_checkpointer(db_path: str) -> tuple[SqliteSaver, sqlite3.Connection]:
    """開啟並初始化 LangGraph SqliteSaver（單房獨立 graph.sqlite）。"""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    return saver, conn

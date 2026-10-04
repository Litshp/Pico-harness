from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.web import ApprovalBroker


def build_agent(tmp_path, approval_callback=None):
    (tmp_path / "README.md").write_text("before\n", encoding="utf-8")
    return Pico(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        approval_policy="ask",
        approval_callback=approval_callback,
    )


def test_approval_callback_receives_bound_snapshot_and_patch_preview(tmp_path):
    requests = []

    def approve(request):
        requests.append(request)
        return True

    agent = build_agent(tmp_path, approve)
    result = agent.run_tool(
        "patch_file_v2",
        {
            "patch": "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-before\n+after\n",
            "expected_hashes": [],
            "verify": False,
        },
    )

    assert requests and requests[0]["tool"] == "patch_file_v2"
    assert requests[0]["id"]
    assert requests[0]["args_hash"]
    assert requests[0]["workspace_fingerprint"]
    assert requests[0]["affected_paths"] == ["README.md"]
    assert "-before" in requests[0]["diff_preview"]
    assert "+after" in requests[0]["diff_preview"]
    assert "after" in result


def test_approval_rejects_arguments_changed_after_user_decision(tmp_path):
    args = {"path": "note.txt", "content": "approved\n"}

    def approve(request):
        args["content"] = "tampered\n"
        return True

    agent = build_agent(tmp_path, approve)
    result = agent.run_tool("write_file", args)

    assert result == "error: approval is stale for write_file; review the updated request"
    assert not (tmp_path / "note.txt").exists()
    assert agent._last_tool_result_metadata["tool_error_code"] == "approval_stale"


def test_approval_rejects_workspace_changed_after_user_decision(tmp_path):
    def approve(request):
        (tmp_path / "outside-change.txt").write_text("changed\n", encoding="utf-8")
        return True

    agent = build_agent(tmp_path, approve)
    result = agent.run_tool("write_file", {"path": "note.txt", "content": "approved\n"})

    assert result == "error: approval is stale for write_file; review the updated request"
    assert not (tmp_path / "note.txt").exists()
    assert agent._last_tool_result_metadata["security_event_type"] == "approval_stale"


def test_denied_patch_exposes_preview_without_modifying_workspace(tmp_path):
    agent = build_agent(tmp_path, lambda request: False)
    result = agent.run_tool(
        "patch_file_v2",
        {
            "patch": "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-before\n+after\n",
            "expected_hashes": [],
            "verify": False,
        },
    )

    assert result == "error: approval denied for patch_file_v2"
    assert agent._last_tool_result_metadata["diff_preview"]
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "before\n"


def test_legacy_two_argument_approval_callback_remains_supported(tmp_path):
    calls = []
    agent = build_agent(tmp_path, lambda name, args: calls.append((name, args)) or True)

    agent.run_tool("write_file", {"path": "note.txt", "content": "ok\n"})

    assert calls == [("write_file", {"path": "note.txt", "content": "ok\n"})]


def test_web_approval_broker_keeps_structured_snapshot_fields():
    broker = ApprovalBroker(timeout=1)
    request = {
        "id": "approval-1",
        "tool": "write_file",
        "args": {"path": "note.txt", "content": "ok"},
        "args_hash": "abc",
        "workspace_fingerprint": "def",
        "affected_paths": ["note.txt"],
        "diff_preview": "+ok",
        "risk_level": "high",
    }

    import threading
    import time

    result = []
    thread = threading.Thread(target=lambda: result.append(broker.request(request)))
    thread.start()
    for _ in range(100):
        if broker.snapshot() is not None:
            break
        time.sleep(0.001)
    assert broker.snapshot()["args_hash"] == "abc"
    assert broker.snapshot()["diff_preview"] == "+ok"
    assert broker.resolve("approval-1", True, args_hash="abc", workspace_fingerprint="def")
    thread.join(timeout=1)
    assert result == [True]

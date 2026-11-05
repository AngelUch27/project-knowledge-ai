from types import SimpleNamespace

from google.genai.types import Content, Part

from backend import agent as ag


# --- a scripted fake Gemini ---------------------------------------------------

def model_turn(*calls, text=""):
    """A fake GenerateContentResponse: function calls (name, args) and/or text."""
    parts = [Part.from_function_call(name=n, args=a) for n, a in calls] or [Part(text=text)]
    return SimpleNamespace(
        function_calls=[SimpleNamespace(name=n, args=a) for n, a in calls],
        text=text,
        candidates=[SimpleNamespace(content=Content(role="model", parts=parts))],
    )


class ScriptedClient:
    def __init__(self, turns):
        self._turns = list(turns)
        self.calls = []  # (contents, had_tools)
        self.models = self

    def generate_content(self, model, contents, config):
        self.calls.append((list(contents), bool(config.tools)))
        if not self._turns:
            raise AssertionError("fake model asked for more turns than scripted")
        return self._turns.pop(0)


def toolbox(log):
    def search(args):
        log.append(("search", args))
        return {"results": [{"title": "README", "uri": "gs://b/README.md"}], "_sources": [{"filename": "README", "location": "gs://b/README.md", "url": "/f"}]}

    def read(args):
        log.append(("read", args))
        return {"passages": ["Supported formats are PDF and MD."], "_sources": [{"filename": "README", "location": "gs://b/README.md", "url": "/f"}]}

    def boom(args):
        raise RuntimeError("bucket unavailable")

    def import_repo(args):
        log.append(("import", args))
        return {"staged_documents": 3, "summary": "imported 3"}

    return ag.Toolbox([
        ag.ToolSpec("search_documents", "d", {"type": "object", "properties": {}}, search),
        ag.ToolSpec("read_document", "d", {"type": "object", "properties": {}}, read),
        ag.ToolSpec("list_documents", "d", {"type": "object", "properties": {}}, boom),
        ag.ToolSpec("import_github_repository", "d", {"type": "object", "properties": {}}, import_repo, action=True,
                    confirm_text=lambda a: f"Import {a['repository_url']}"),
    ])


# --- behaviour ----------------------------------------------------------------

def test_agent_searches_then_reads_then_answers_and_dedupes_sources():
    log = []
    client = ScriptedClient([
        model_turn(("search_documents", {"query": "formats"})),
        model_turn(("read_document", {"uri": "gs://b/README.md", "focus": "formats"})),
        model_turn(text="PDF and MD (Source: README)."),
    ])
    result = ag.run_agent(client, "gemini", "sys", [], "which formats?", toolbox(log))

    assert result.answer == "PDF and MD (Source: README)."
    assert [s.tool for s in result.steps] == ["search_documents", "read_document"]
    assert [(k, a.get("query") or a.get("uri")) for k, a in log] == [("search", "formats"), ("read", "gs://b/README.md")]
    assert result.sources == [{"filename": "README", "location": "gs://b/README.md", "url": "/f"}]  # once, not twice
    assert result.pending_action is None
    # the model turn with the call and a user turn with the function response were appended in order
    second_call_contents = client.calls[1][0]
    assert [c.role for c in second_call_contents[-3:]] == ["user", "model", "user"]
    assert second_call_contents[-1].parts[0].function_response.name == "search_documents"


def test_tool_errors_are_returned_to_the_model_not_raised():
    log = []
    client = ScriptedClient([
        model_turn(("list_documents", {})),
        model_turn(text="I could not list the library right now."),
    ])
    result = ag.run_agent(client, "gemini", "sys", [], "how many docs?", toolbox(log))
    assert result.answer.startswith("I could not")
    assert result.steps[0].summary == "list_documents failed: RuntimeError: bucket unavailable"
    response = client.calls[1][0][-1].parts[0].function_response.response
    assert "RuntimeError" in response["error"]


def test_action_tools_stop_for_confirmation_until_approved():
    log = []
    client = ScriptedClient([model_turn(("import_github_repository", {"repository_url": "https://github.com/o/r"}), text="Sure, importing it.")])
    result = ag.run_agent(client, "gemini", "sys", [], "import https://github.com/o/r", toolbox(log))

    assert result.pending_action == ag.ToolCall("import_github_repository", {"repository_url": "https://github.com/o/r"})
    assert result.pending_description == "Import https://github.com/o/r"
    assert result.answer == "Sure, importing it."
    assert log == [] and result.steps == []  # nothing ran

    # the user confirmed: same question, approval attached → the tool runs and the model reports
    client = ScriptedClient([
        model_turn(("import_github_repository", {"repository_url": "https://github.com/o/r/"})),  # trailing slash differs
        model_turn(text="Imported 3 documents."),
    ])
    result = ag.run_agent(client, "gemini", "sys", [], "import https://github.com/o/r", toolbox(log),
                          approved_actions=[ag.ToolCall("import_github_repository", {"repository_url": "https://github.com/o/r"})])
    assert log == [("import", {"repository_url": "https://github.com/o/r/"})]
    assert result.answer == "Imported 3 documents." and result.pending_action is None
    assert result.steps[0].summary == "imported 3"


def test_approval_for_one_tool_does_not_unlock_another():
    assert ag._is_approved(ag.ToolCall("sync_github_repositories", {}), [ag.ToolCall("import_github_repository", {"repository_url": "x"})]) is False
    assert ag._is_approved(ag.ToolCall("import_github_repository", {"repository_url": "X/"}), [ag.ToolCall("import_github_repository", {"repository_url": "x"})]) is True


def test_unknown_tool_is_reported_to_the_model():
    client = ScriptedClient([model_turn(("delete_everything", {})), model_turn(text="That tool does not exist.")])
    result = ag.run_agent(client, "gemini", "sys", [], "hi there", toolbox([]))
    assert result.steps[0].summary == "delete_everything failed: Unknown tool: delete_everything"


def test_step_budget_forces_a_final_answer_without_tools():
    log = []
    turns = [model_turn(("search_documents", {"query": f"q{i}"})) for i in range(3)] + [model_turn(text="Best effort answer.")]
    client = ScriptedClient(turns)
    result = ag.run_agent(client, "gemini", "sys", [], "loop forever", toolbox(log), max_steps=3)
    assert result.answer == "Best effort answer."
    assert len(result.steps) == 3
    assert client.calls[-1][1] is False  # the final call offered no tools
    assert "Do not call any more tools" in client.calls[-1][0][-1].parts[0].text


def test_history_is_sent_before_the_new_question():
    client = ScriptedClient([model_turn(text="ok")])
    history = [Content(role="user", parts=[Part(text="earlier")]), Content(role="model", parts=[Part(text="reply")])]
    ag.run_agent(client, "gemini", "sys", history, "now", toolbox([]))
    sent = client.calls[0][0]
    assert [c.parts[0].text for c in sent] == ["earlier", "reply", "now"]

from app.services.minimax_client import _strip_thinking


def test_strip_thinking_blocks():
    text = "<think>private scratchpad</think>\nFinal answer"
    assert _strip_thinking(text).strip() == "Final answer"

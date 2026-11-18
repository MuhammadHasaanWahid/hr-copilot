"""Headless UI test of the Streamlit app (streamlit.testing): sign in, run a
scenario, see the trace, approve as an admin - the full user journey."""
from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parents[1] / "frontend" / "streamlit_app.py")
S4 = ("I recently changed departments, my reporting manager has changed, and I think my leave approval is still going "
      "to my previous manager. Can you check what might be wrong and help me resolve it?")


def _sign_in(at: AppTest, employee_id: str) -> None:
    at.sidebar.radio[0].set_value(employee_id).run()
    at.sidebar.button[0].click().run()


def test_full_journey_in_streamlit():
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    assert "HR Copilot" in at.title[0].value                 # landing page before sign-in
    _sign_in(at, "E1010")
    assert not at.exception
    assert any("Bilal Ahmed" in s.value for s in at.sidebar.success)
    at.chat_input[0].set_value(S4).run()
    assert not at.exception
    replies = [m for m in at.chat_message if m.name == "assistant"]
    assert replies and "Hina Shah" in replies[-1].markdown[0].value
    assert any("Actions (" not in t.label for t in at.tabs)    # Bilal can't approve his own fix
    # Admin approves the high-risk fix in the same sandbox
    _sign_in(at, "E0502")
    assert any(t.label.startswith("✅ Actions (1)") for t in at.tabs)
    approve = next(b for b in at.button if b.label == "Approve")
    approve.click().run()
    assert not at.exception
    assert any("executed" in m.value for m in at.markdown)

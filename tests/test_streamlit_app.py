"""Headless UI tests of the Streamlit portal (streamlit.testing): sign-in, role-based
pages, the assistant, request submission and approvals - the full user journey."""
from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parents[1] / "frontend" / "streamlit_app.py")
S4 = ("I recently changed departments, my reporting manager has changed, and I think my leave approval is still going "
      "to my previous manager. Can you check what might be wrong and help me resolve it?")


def _new() -> AppTest:
    at = AppTest.from_file(APP, default_timeout=90).run()
    assert not at.exception
    return at


def _sign_in(at: AppTest, employee_id: str) -> None:
    at.selectbox[0].set_value(employee_id).run()
    next(b for b in at.button if b.label == "Sign in").click().run()
    assert not at.exception, at.exception


def _goto(at: AppTest, page: str) -> None:
    at.session_state["goto"] = page
    at.run()


def _sign_out(at: AppTest) -> None:
    next(b for b in at.button if b.label == "Sign out").click().run()


def _no_stray_output(at: AppTest) -> None:
    assert not at.get("help"), "internal object rendered in the UI (Streamlit magic)"


def _text(at: AppTest) -> str:
    return " ".join(m.value for m in at.markdown) + " " + " ".join(c.value for c in at.caption)


def test_login_and_role_based_home():
    at = _new()
    assert "HR Copilot" in _text(at) and not at.metric           # login screen, nothing private shown
    _sign_in(at, "E1001")
    assert "Ayesha" in _text(at)
    labels = {m.label for m in at.metric}
    assert {"Annual leave available", "My open requests"} <= labels
    assert not any(b.label == "Open approvals" for b in at.button)  # employees have no approvals page


def test_assistant_request_and_admin_approval_journey():
    at = _new()
    _sign_in(at, "E1010")
    _goto(at, "assistant")
    at.chat_input[0].set_value(S4).run()
    assert not at.exception, at.exception
    replies = [m for m in at.chat_message if m.name == "assistant"]
    assert replies and "Hina Shah" in replies[-1].markdown[0].value
    _no_stray_output(at)
    # the high-risk fix is waiting for an HR administrator, not for Bilal
    _sign_out(at)
    _sign_in(at, "E0502")
    _goto(at, "approvals")
    assert not at.exception, at.exception
    assert "Approval routing fix" in _text(at)
    next(b for b in at.button if b.key and b.key.startswith("case") and b.key.endswith("True")).click().run()
    assert not at.exception, at.exception
    assert any("completed" in s.value.lower() for s in at.success)


def test_manager_sees_policy_checks_and_injection_warning():
    at = _new()
    _sign_in(at, "E0102")
    _goto(at, "approvals")
    assert not at.exception, at.exception
    text = _text(at)
    assert "Kenji Watanabe" in text and "Lucas Meyer" in text
    assert any("instruction-like" in w.value for w in at.warning)
    assert "Busiest upcoming week" in text


def test_hr_insights_and_every_page_renders():
    at = _new()
    _sign_in(at, "E0502")
    for page in ("home", "assistant", "approvals", "insights", "audit", "usage", "requests", "leave"):
        _goto(at, page)
        assert not at.exception, (page, at.exception)
        _no_stray_output(at)
    _goto(at, "insights")
    assert "Engineering / Platform" in _text(at)

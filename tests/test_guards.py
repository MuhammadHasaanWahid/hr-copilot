from app.agent import guards


class KB:
    @staticmethod
    def validate_citations(text):
        return [], []


def claims(text, executed=0):
    return guards.check(text, [text], [], executed, KB()).false_action_claims


def test_action_claims_are_caught():
    assert claims("Your leave request has been submitted.")
    assert claims("I've approved the request for you.")
    assert claims("The routing is now fixed.")


def test_factual_descriptions_and_pending_states_are_not_claims():
    assert not claims("His HRIS reporting line has been updated, but the approval route still points to Hina.")
    assert not claims("It is not submitted yet - confirm it on the My requests page.")
    assert not claims("Once it has been approved by Omar, the balance updates.")
    assert not claims("Your request has been submitted.", executed=1)


def test_unsupported_numbers_and_dates():
    rep = guards.check("You have 13 days left from 2026-12-01.", [{"available": 13}], [], 0, KB())
    assert rep.unsupported == ["2026-12-01"]

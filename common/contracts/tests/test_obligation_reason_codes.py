from truealpha_contracts.obligation_reason_codes import (
    ObligationDisposition,
    ObligationReasonCode,
    disposition_for,
)


def test_every_code_maps_to_exactly_one_disposition() -> None:
    for code in ObligationReasonCode:
        assert disposition_for(code) in ObligationDisposition


def test_dispositions_are_as_specified() -> None:
    assert disposition_for(ObligationReasonCode.CHECKSUM_MISMATCH) is ObligationDisposition.STOP
    assert disposition_for(ObligationReasonCode.LOOK_AHEAD_VIOLATION) is ObligationDisposition.STOP
    assert disposition_for(ObligationReasonCode.RATE_LIMITED) is ObligationDisposition.RETRY
    assert disposition_for(ObligationReasonCode.TIMEOUT) is ObligationDisposition.RETRY
    assert disposition_for(ObligationReasonCode.FIELD_UNAVAILABLE) is ObligationDisposition.TRACE_ONLY
    assert disposition_for(ObligationReasonCode.NOT_YET_KNOWABLE) is ObligationDisposition.TRACE_ONLY
    # #729: a call the source gateway refused because the day's budget is spent waits for
    # the next UTC-day reset — recorded and continued, never retried inside the run (nor
    # after a rate window), never "field absent".
    assert disposition_for(ObligationReasonCode.DEFERRED_CAPACITY) is ObligationDisposition.TRACE_ONLY
    assert ObligationReasonCode.DEFERRED_CAPACITY.value == "deferred_capacity"

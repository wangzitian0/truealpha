from data_engine.capture.topt import TOPT_INSTRUMENTS, build_topt_scope
from truealpha_contracts import CaptureRequirementLevel, CaptureSubjectKind, DataDomain


def test_topt_scope_freezes_twenty_issuers_and_twenty_one_instruments():
    scope = build_topt_scope()
    issuers = [subject for subject in scope.subjects if subject.kind is CaptureSubjectKind.ISSUER]
    instruments = [subject for subject in scope.subjects if subject.kind is CaptureSubjectKind.INSTRUMENT]
    assert len(issuers) == 20
    assert len(instruments) == 21
    assert len(TOPT_INSTRUMENTS) == 21
    assert len({instrument.parent_subject_id for instrument in instruments}) == 20


def test_alphabet_share_classes_remain_distinct_under_one_issuer():
    scope = build_topt_scope()
    alphabet = [
        subject
        for subject in scope.subjects
        if subject.kind is CaptureSubjectKind.INSTRUMENT and subject.parent_subject_id == "company:cik:1652044"
    ]
    assert {subject.identifiers["ticker"] for subject in alphabet} == {"GOOG", "GOOGL"}
    assert len({subject.subject_id for subject in alphabet}) == 2


def test_exxon_is_a_first_class_resolved_scope_member():
    scope = build_topt_scope()
    xom = next(subject for subject in scope.subjects if subject.subject_id == "instrument:isin:US30231G1022")
    assert xom.parent_subject_id == "company:cik:34088"
    assert xom.identifiers["ticker"] == "XOM"
    assert xom.identifiers["moomoo"] == "US.XOM"


def test_scope_expands_every_required_domain_cell():
    scope = build_topt_scope()
    assert len(scope.subjects) == 42
    assert len(scope.requirements) == 245
    assert all(requirement.level is CaptureRequirementLevel.REQUIRED for requirement in scope.requirements)
    assert {requirement.domain for requirement in scope.requirements} >= {
        DataDomain.FINANCIAL_FACTS,
        DataDomain.FORECASTS,
        DataDomain.COMPANY_GUIDANCE,
        DataDomain.FUND_HOLDINGS,
        DataDomain.INSTRUMENTS,
        DataDomain.MARKET_PRICES,
        DataDomain.CORPORATE_ACTIONS,
        DataDomain.FILING_EXTRACTIONS,
    }


def test_scope_id_is_stable_for_the_same_approved_baseline():
    assert build_topt_scope().capture_scope_id == build_topt_scope().capture_scope_id

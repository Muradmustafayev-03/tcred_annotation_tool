from __future__ import annotations

from copy import deepcopy

from .presentation import displayed_evidence, require_visible_citations
from .protocol import CATEGORICAL_FIELDS

TUTORIAL_VERSION = "5.2"


def _labels(applicable: list[str]) -> dict[str, str]:
    return {field: "" if field in applicable else "not_applicable" for field in CATEGORICAL_FIELDS}


def _evidence(
    evidence_id: str,
    text: str,
    *,
    start: str | None,
    end: str | None,
) -> dict[str, object]:
    valid_time: dict[str, object]
    if start is None:
        valid_time = {"type": "unknown", "granularity": "unknown"}
    else:
        valid_time = {
            "type": "interval",
            "start": start,
            "end": end or start,
            "granularity": "day",
        }
    return {
        "evidence_id": evidence_id,
        "text": text,
        "publication_time": "2024-01-10",
        "valid_time": valid_time,
    }


def _path(
    path_id: str,
    *,
    person: str,
    place: str,
    evidence_id: str,
    evidence_text: str,
    start: str | None,
    end: str | None,
    reversed_direction: bool = False,
) -> dict[str, object]:
    person_node = {"id": "N1", "label": person, "type": "person"}
    place_node = {"id": "N2", "label": place, "type": "organization"}
    source = place_node if reversed_direction else person_node
    target = person_node if reversed_direction else place_node
    valid_time: dict[str, object]
    if start is None:
        valid_time = {"type": "unknown", "granularity": "unknown"}
    else:
        valid_time = {
            "type": "interval",
            "start": start,
            "end": end or start,
            "granularity": "day",
        }
    return {
        "path_id": path_id,
        "path_source": "candidate_path",
        "nodes": [person_node, place_node],
        "edges": [
            {
                "fact_id": evidence_id,
                "relation": "directed",
                "relation_label": "directed",
                "source": source,
                "target": target,
                "valid_time": valid_time,
                "evidence_text": evidence_text,
                "traversal_direction": "reverse" if reversed_direction else "forward",
                "directional": True,
                "symmetric": False,
            }
        ],
    }


_ALL_STANDARD_FIELDS = [
    "answer_correct",
    "temporal_correct",
    "evidence_supports_answer",
    "citation_temporally_valid",
    "graph_evidence_sufficient",
]

_MAYA_TEXT = (
    "Maya Patel directed the Harbor Arts Centre from 1 January 2020 through 31 December 2022."
)
_MAYA_EVIDENCE = _evidence(
    "E1",
    _MAYA_TEXT,
    start="2020-01-01",
    end="2022-12-31",
)
_MAYA_PATH = _path(
    "practice_path_valid",
    person="Maya Patel",
    place="Harbor Arts Centre",
    evidence_id="E1",
    evidence_text=_MAYA_TEXT,
    start="2020-01-01",
    end="2022-12-31",
)

_TUTORIAL_ITEMS: tuple[dict[str, object], ...] = (
    {
        "tutorial_id": "tutorial_guided_valid",
        "mode": "guided",
        "title": "Guided example: supported answer",
        "question": "Who directed the Harbor Arts Centre on 15 June 2021?",
        "reference_answer": "Maya Patel",
        "answer_text": "Maya Patel",
        "cited_evidence_ids": ["E1"],
        "cited_evidence": [_MAYA_EVIDENCE],
        "retrieved_evidence": [_MAYA_EVIDENCE],
        "graph_paths": [
            _MAYA_PATH,
            _path(
                "practice_path_irrelevant",
                person="Jon Bell",
                place="Riverside Theatre",
                evidence_id="E4",
                evidence_text=(
                    "Jon Bell directed the Riverside Theatre from 1 January 2020 through "
                    "31 December 2022."
                ),
                start="2020-01-01",
                end="2022-12-31",
            ),
        ],
        "applicable_fields": _ALL_STANDARD_FIELDS,
        "annotation": {
            "labels": _labels(_ALL_STANDARD_FIELDS),
            "reasons": {},
            "issue_code": "",
            "issue_detail": "",
            "comment": "",
            "workflow_stage": "evidence_review",
        },
        "expected_labels": {
            "answer_correct": "yes",
            "temporal_correct": "yes",
            "evidence_supports_answer": "yes",
            "citation_temporally_valid": "yes",
            "graph_evidence_sufficient": "yes",
        },
        "expected_reasons": {},
        "guidance": {
            "temporal_correct": {
                "instruction": "Check the candidate against the requested date.",
                "reason": "15 June 2021 is inside the 2020 to 2022 valid-time interval.",
            },
            "evidence_supports_answer": {
                "instruction": (
                    "For this content-only field, pretend that every date and interval is hidden."
                ),
                "reason": (
                    "The words in E1 directly support the claim that Maya Patel directed the "
                    "centre."
                ),
            },
            "citation_temporally_valid": {
                "instruction": "Inspect only the evidence marked as cited.",
                "reason": "The cited E1 interval includes the requested date.",
            },
            "graph_evidence_sufficient": {
                "instruction": (
                    "Judge the two displayed paths together. Find whether the set contains every "
                    "connection needed for the candidate."
                ),
                "reason": (
                    "The Maya Patel path completely supports the candidate. The separate Jon Bell "
                    "path concerns another venue and does not cancel the sufficient path."
                ),
            },
            "answer_correct": {
                "instruction": "Now reveal and compare with the reference answer.",
                "reason": (
                    "The candidate exactly matches the reference and fully answers the question."
                ),
            },
        },
    },
    {
        "tutorial_id": "tutorial_guided_stale_content",
        "mode": "guided",
        "title": "Guided example: stale evidence still supports content",
        "question": "Who directed the Harbor Arts Centre on 15 June 2023?",
        "reference_answer": "Jon Bell",
        "answer_text": "Maya Patel",
        "cited_evidence_ids": ["E1"],
        "cited_evidence": [_MAYA_EVIDENCE],
        "retrieved_evidence": [
            _MAYA_EVIDENCE,
            _evidence(
                "E2",
                (
                    "Jon Bell directed the Harbor Arts Centre from 1 January 2023 through "
                    "31 December 2025."
                ),
                start="2023-01-01",
                end="2025-12-31",
            ),
        ],
        "graph_paths": [_MAYA_PATH],
        "applicable_fields": _ALL_STANDARD_FIELDS,
        "annotation": {
            "labels": _labels(_ALL_STANDARD_FIELDS),
            "reasons": {},
            "issue_code": "",
            "issue_detail": "",
            "comment": "",
            "workflow_stage": "evidence_review",
        },
        "expected_labels": {
            "answer_correct": "no",
            "temporal_correct": "no",
            "evidence_supports_answer": "yes",
            "citation_temporally_valid": "no",
            "graph_evidence_sufficient": "no",
        },
        "expected_reasons": {},
        "guidance": {
            "temporal_correct": {
                "instruction": "Compare the candidate's interval with the requested date.",
                "reason": "Maya Patel's interval ended before 15 June 2023, so select No.",
            },
            "evidence_supports_answer": {
                "instruction": (
                    "Now hide every date mentally and judge only whether E1 supports the words "
                    "\"Maya Patel directed the centre.\""
                ),
                "reason": (
                    "E1 directly supports that factual content. Its being stale is handled by "
                    "other fields, so select Yes here."
                ),
            },
            "citation_temporally_valid": {
                "instruction": "Inspect the interval of cited evidence E1.",
                "reason": "E1 does not cover 15 June 2023, so select No.",
            },
            "graph_evidence_sufficient": {
                "instruction": "Check the path set's relations, directions, and edge intervals.",
                "reason": (
                    "The direction is readable, but the edge interval is stale, so select No."
                ),
            },
            "answer_correct": {
                "instruction": "Compare the candidate with the newly revealed reference.",
                "reason": "The reference gives Jon Bell for the requested date, so select No.",
            },
        },
    },
    {
        "tutorial_id": "tutorial_guided_partial_answer",
        "mode": "guided",
        "title": "Guided example: Partial means decidably incomplete",
        "question": "Which two people co-directed the Harbor Arts Centre on 15 June 2021?",
        "reference_answer": "Maya Patel and Jon Bell",
        "answer_text": "Maya Patel",
        "cited_evidence_ids": ["E3"],
        "cited_evidence": [
            _evidence(
                "E3",
                (
                    "Maya Patel and Jon Bell co-directed the Harbor Arts Centre from "
                    "1 January 2020 through 31 December 2022."
                ),
                start="2020-01-01",
                end="2022-12-31",
            )
        ],
        "retrieved_evidence": [
            _evidence(
                "E3",
                (
                    "Maya Patel and Jon Bell co-directed the Harbor Arts Centre from "
                    "1 January 2020 through 31 December 2022."
                ),
                start="2020-01-01",
                end="2022-12-31",
            ),
        ],
        "graph_paths": [],
        "applicable_fields": [
            "answer_correct",
            "temporal_correct",
            "evidence_supports_answer",
            "citation_temporally_valid",
        ],
        "annotation": {
            "labels": _labels(
                [
                    "answer_correct",
                    "temporal_correct",
                    "evidence_supports_answer",
                    "citation_temporally_valid",
                ]
            ),
            "reasons": {},
            "issue_code": "",
            "issue_detail": "",
            "comment": "",
            "workflow_stage": "evidence_review",
        },
        "expected_labels": {
            "answer_correct": "partial",
            "temporal_correct": "yes",
            "evidence_supports_answer": "yes",
            "citation_temporally_valid": "yes",
        },
        "expected_reasons": {
            "answer_correct": "incomplete_answer",
        },
        "guidance": {
            "temporal_correct": {
                "instruction": "Check the timing of the claim that Maya Patel co-directed.",
                "reason": "The requested date falls inside the displayed interval.",
            },
            "evidence_supports_answer": {
                "instruction": (
                    "Judge only whether the candidate's stated content appears in the evidence."
                ),
                "reason": "E3 directly supports the stated Maya Patel claim.",
            },
            "citation_temporally_valid": {
                "instruction": "Check whether cited E3 covers 15 June 2021.",
                "reason": "The cited interval covers the requested date.",
            },
            "answer_correct": {
                "instruction": (
                    "The reference and evidence let you decide exactly what is missing. Select "
                    "Partial, not Cannot judge."
                ),
                "reason": (
                    "Maya Patel is one correct co-director, but Jon Bell is a required missing "
                    "answer part. Then select the reason \"A required answer part is missing.\""
                ),
            },
        },
    },
    {
        "tutorial_id": "tutorial_guided_missing_evidence",
        "mode": "guided",
        "title": "Guided example: a justified refusal is still not an answer",
        "question": "Who directed the Lakeside Gallery on 15 June 2024?",
        "reference_answer": "Maya Patel",
        "answer_text": "There is not enough information to answer.",
        "cited_evidence_ids": [],
        "cited_evidence": [],
        "retrieved_evidence": [],
        "graph_paths": [],
        "applicable_fields": ["answer_correct", "response_decision_appropriate"],
        "annotation": {
            "labels": _labels(["answer_correct", "response_decision_appropriate"]),
            "reasons": {},
            "issue_code": "",
            "issue_detail": "",
            "comment": "",
            "workflow_stage": "evidence_review",
        },
        "expected_labels": {
            "answer_correct": "no",
            "response_decision_appropriate": "yes",
        },
        "expected_reasons": {},
        "guidance": {
            "answer_correct": {
                "instruction": (
                    "Compare what the candidate actually supplies with the revealed reference. "
                    "Judge the requested answer, not whether refusing was sensible."
                ),
                "reason": (
                    "The reference gives Maya Patel, but the candidate does not supply the "
                    "requested name. Select No for Answer correct."
                ),
            },
            "response_decision_appropriate": {
                "instruction": (
                    "Decide whether refusing rather than inventing a name was the right response "
                    "decision."
                ),
                "reason": (
                    "The shown context contains no supporting material that identifies a "
                    "director, so refusing was the responsible decision. Select Yes even though "
                    "Answer correct is No."
                ),
            },
        },
    },
    {
        "tutorial_id": "tutorial_independent_wrong_direction",
        "mode": "independent",
        "title": "Independent practice: irrelevant evidence and wrong graph direction",
        "question": "Did Maya Patel direct the Harbor Arts Centre on 15 June 2021?",
        "reference_answer": "Yes",
        "answer_text": "Yes. Maya Patel directed the Harbor Arts Centre.",
        "cited_evidence_ids": ["E1"],
        "cited_evidence": [
            _evidence(
                "E1",
                (
                    "Jon Bell directed the Harbor Arts Centre from 1 January 2020 through "
                    "31 December 2022."
                ),
                start="2020-01-01",
                end="2022-12-31",
            )
        ],
        "retrieved_evidence": [
            _evidence(
                "E1",
                (
                    "Jon Bell directed the Harbor Arts Centre from 1 January 2020 through "
                    "31 December 2022."
                ),
                start="2020-01-01",
                end="2022-12-31",
            )
        ],
        "graph_paths": [
            _path(
                "practice_path_reversed",
                person="Jon Bell",
                place="Harbor Arts Centre",
                evidence_id="E1",
                evidence_text=(
                    "Jon Bell directed the Harbor Arts Centre from 1 January 2020 through "
                    "31 December 2022."
                ),
                start="2020-01-01",
                end="2022-12-31",
                reversed_direction=True,
            )
        ],
        "applicable_fields": _ALL_STANDARD_FIELDS,
        "annotation": {
            "labels": _labels(_ALL_STANDARD_FIELDS),
            "reasons": {},
            "issue_code": "",
            "issue_detail": "",
            "comment": "",
            "workflow_stage": "evidence_review",
        },
        "expected_labels": {
            "answer_correct": "yes",
            "temporal_correct": "unjudgeable",
            "evidence_supports_answer": "no",
            "citation_temporally_valid": "yes",
            "graph_evidence_sufficient": "no",
        },
        "expected_reasons": {
            "temporal_correct": "candidate_time_not_established",
        },
        "feedback": {
            "answer_correct": "The candidate fully matches the revealed reference.",
            "temporal_correct": (
                "The only displayed dates concern a different person, Jon Bell, so Maya Patel's "
                "timing cannot be established from the shown material."
            ),
            "evidence_supports_answer": (
                "E1 is about Jon Bell, not Maya Patel, so it does not support the candidate's "
                "factual content."
            ),
            "citation_temporally_valid": (
                "The cited E1 interval covers the requested date. This timing-only judgment can "
                "be Yes even though E1's content does not support the candidate."
            ),
            "graph_evidence_sufficient": (
                "The only displayed path concerns Jon Bell and reverses who directed what, so "
                "the path set is not sufficient for the Maya Patel answer."
            ),
        },
    },
    {
        "tutorial_id": "tutorial_independent_unknown_time",
        "mode": "independent",
        "title": "Independent practice: insufficient time information",
        "question": "Who directed the Riverfront Museum on 15 June 2024?",
        "reference_answer": "The displayed information does not establish an answer.",
        "answer_text": "Alex Morgan directed the Riverfront Museum.",
        "cited_evidence_ids": ["E1"],
        "cited_evidence": [
            _evidence(
                "E1",
                "Alex Morgan directed the Riverfront Museum, but the record gives no dates.",
                start=None,
                end=None,
            )
        ],
        "retrieved_evidence": [
            _evidence(
                "E1",
                "Alex Morgan directed the Riverfront Museum, but the record gives no dates.",
                start=None,
                end=None,
            )
        ],
        "graph_paths": [
            _path(
                "practice_path_unknown_time",
                person="Alex Morgan",
                place="Riverfront Museum",
                evidence_id="E1",
                evidence_text=(
                    "Alex Morgan directed the Riverfront Museum, but the record gives no dates."
                ),
                start=None,
                end=None,
            )
        ],
        "applicable_fields": [*CATEGORICAL_FIELDS],
        "annotation": {
            "labels": _labels([*CATEGORICAL_FIELDS]),
            "reasons": {},
            "issue_code": "",
            "issue_detail": "",
            "comment": "",
            "workflow_stage": "evidence_review",
        },
        "expected_labels": {
            "answer_correct": "no",
            "temporal_correct": "unjudgeable",
            "evidence_supports_answer": "yes",
            "citation_temporally_valid": "unjudgeable",
            "graph_evidence_sufficient": "unjudgeable",
            "response_decision_appropriate": "no",
        },
        "expected_reasons": {
            "temporal_correct": "missing_valid_time",
            "citation_temporally_valid": "citation_valid_time_missing",
            "graph_evidence_sufficient": "edge_time_missing",
        },
        "feedback": {
            "answer_correct": (
                "The displayed material cannot establish the requested date-specific answer."
            ),
            "temporal_correct": "The relationship has no recorded valid time.",
            "evidence_supports_answer": (
                "E1 supports the undated relationship claim when time is ignored."
            ),
            "citation_temporally_valid": "The citation exists, but its valid time is missing.",
            "graph_evidence_sufficient": (
                "The graph relation is readable, but the required edge time is unknown."
            ),
            "response_decision_appropriate": (
                "The candidate answered even though the displayed material requires refusal."
            ),
        },
    },
)


def tutorial_payload() -> dict[str, object]:
    """Return isolated practice data that is never part of a research assignment."""
    items = deepcopy(_TUTORIAL_ITEMS)
    for item in items:
        item["displayed_evidence"] = displayed_evidence(item)
        require_visible_citations(item)
    return {
        "version": TUTORIAL_VERSION,
        "items": items,
    }


def independent_tutorial_ids() -> tuple[str, ...]:
    """Return the tutorial items whose checked attempts are meaningful QC signals."""
    return tuple(
        str(item["tutorial_id"]) for item in _TUTORIAL_ITEMS if item.get("mode") == "independent"
    )

"""Golden case dataset for evaluation.

Expanded from the original 30-case retrieval set to include:
  - Multi-entity questions
  - Multi-hop questions
  - Self-reported vs independent source cases
  - Follow-up questions
  - Abstention cases (SHOULD_ABSTAIN)
  - Partial answer cases (SHOULD_PARTIAL)
  - Comparison questions
  - Trend/temporal questions
"""
from eval_golden import GOLDEN_SET as _ORIGINAL_GOLDEN


# Extended golden cases with richer annotations
# Each case has:
#   q: question text
#   correct: list of correct record indices (for retrieval eval)
#   type: question type
#   expected_status: SHOULD_ANSWER | SHOULD_PARTIAL | SHOULD_ABSTAIN
#   tags: additional tags for filtering

EXTENDED_GOLDEN = [
    # ── Original 30 cases (re-exported with richer annotations) ──
    # (inherited from eval_golden.GOLDEN_SET)

    # ── Multi-entity comparison ──
    {
        "q": "宁德时代和比亚迪的固态电池技术路线对比",
        "correct": [],
        "type": "comparison",
        "expected_status": "SHOULD_ANSWER",
        "tags": ["multi_entity", "comparison"],
    },
    {
        "q": "钙钛矿太阳能电池和传统硅电池的效率比较",
        "correct": [],
        "type": "comparison",
        "expected_status": "SHOULD_ANSWER",
        "tags": ["multi_entity", "comparison"],
    },

    # ── Multi-hop ──
    {
        "q": "使用钙钛矿材料的太阳能电池有哪些研究进展？",
        "correct": [],
        "type": "multi_hop",
        "expected_status": "SHOULD_ANSWER",
        "tags": ["multi_hop", "material"],
    },

    # ── Self-reported vs independent ──
    {
        "q": "某公司宣称其固态电池能量密度达到500Wh/kg，这个数据有没有第三方验证？",
        "correct": [],
        "type": "verification",
        "expected_status": "SHOULD_PARTIAL",
        "tags": ["self_reported", "independent_validation"],
    },

    # ── Follow-up ──
    {
        "q": "刚才提到的固态电池能量密度，具体是多少？",
        "correct": [],
        "type": "followup",
        "expected_status": "SHOULD_ANSWER",
        "tags": ["followup"],
    },

    # ── Should abstain — no data in KB ──
    {
        "q": "2026年诺贝尔物理学奖获得者是谁？",
        "correct": [],
        "type": "abstention",
        "expected_status": "SHOULD_ABSTAIN",
        "tags": ["abstention", "out_of_scope"],
    },
    {
        "q": "火星探测器最新发现什么了？",
        "correct": [],
        "type": "abstention",
        "expected_status": "SHOULD_ABSTAIN",
        "tags": ["abstention", "out_of_scope"],
    },

    # ── Partial answer ──
    {
        "q": "全球固态电池产业化进展如何，各主要厂商的量产时间表是什么？",
        "correct": [],
        "type": "broad_survey",
        "expected_status": "SHOULD_PARTIAL",
        "tags": ["partial", "broad"],
    },

    # ── Trend/temporal ──
    {
        "q": "固态电池技术从2023年到2026年的发展历程",
        "correct": [],
        "type": "trend",
        "expected_status": "SHOULD_ANSWER",
        "tags": ["temporal", "trend"],
    },

    # ── Numeric-specific ──
    {
        "q": "NVIDIA Blackwell B200的NVLink带宽是多少？",
        "correct": [],
        "type": "numeric",
        "expected_status": "SHOULD_ANSWER",
        "tags": ["numeric", "spec_lookup"],
    },

    # ── Novelty (follow-up seeking new info) ──
    {
        "q": "还有别的固态电池公司吗？",
        "correct": [],
        "type": "novelty",
        "expected_status": "SHOULD_ANSWER",
        "tags": ["novelty", "followup"],
    },
]


def get_all_golden() -> list:
    """Get all golden cases (original + extended)."""
    # Original cases with type annotation
    result = []
    for case in _ORIGINAL_GOLDEN:
        result.append({
            "q": case["q"],
            "correct": case["correct"],
            "type": case.get("type", "direct"),
            "expected_status": "SHOULD_ANSWER",
            "tags": [case.get("type", "direct")],
        })
    result.extend(EXTENDED_GOLDEN)
    return result


def get_retrieval_cases() -> list:
    """Get only cases with known correct records (for retrieval eval)."""
    return [c for c in get_all_golden() if c["correct"]]


def get_abstention_cases() -> list:
    """Get cases where the system SHOULD_ABSTAIN."""
    return [c for c in get_all_golden() if c["expected_status"] == "SHOULD_ABSTAIN"]


def get_partial_cases() -> list:
    """Get cases where the system SHOULD_PARTIAL."""
    return [c for c in get_all_golden() if c["expected_status"] == "SHOULD_PARTIAL"]

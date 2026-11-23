"""Data crossing the workflow's boundaries: ticket in, LLM outputs, result out.

Everything is Pydantic so it is validated at the edge (LLM output is never trusted unchecked).
The LLM output models (`Classification`, `DraftOut`) are sent to the API as JSON schemas
(structured outputs), so keep them flat and free of numeric constraints; clamping is done in code.
"""

from typing import Literal

from pydantic import BaseModel, Field

Category = Literal[
    "deposit",
    "withdrawal",
    "account_opening",
    "verification",
    "complaint",
    "trading_advice_request",
    "aml_compliance",
    "other",
]
Decision = Literal["none", "complaint", "kyc", "aml"]
Urgency = Literal["low", "medium", "high", "critical"]
Sentiment = Literal["positive", "neutral", "negative", "angry"]
Destination = Literal["support_agent", "senior_support", "compliance"]

# PRD FR-5.1. Closed list: a new trigger needs a code here, a rule in routing.py and a test.
ReasonCode = Literal[
    "hard_rule_topic",
    "not_in_kb",
    "weak_retrieval",
    "low_confidence",
    "post_check_failed",
    "kb_conflict",
    "angry_urgent",
    "technical_failure",
    "low_resource_language",
    "account_action_required",
    "vulnerable_customer",
    "security_sensitive",
    "multi_intent_blocked",
]


class Ticket(BaseModel):
    id: str
    subject: str = ""
    body: str
    channel: str | None = None
    customer_id: str | None = None
    created_at: str | None = None


# ---- LLM call 1: classification -------------------------------------------------------------


class Intent(BaseModel):
    topic: Category
    decision_requested: Decision = Field(
        description="Set when the customer asks US to decide an outcome (approve verification, "
        "refund, unfreeze, accept a document). A process question is 'none'."
    )
    text: str = Field(description="The part of the ticket this intent refers to, quoted briefly.")


class Classification(BaseModel):
    category: Category = Field(description="Topic of the ticket; the most restrictive intent.")
    intents: list[Intent]
    urgency: Urgency
    language: str = Field(description="ISO 639-1 code of the ticket (ckb, zh-TW allowed).")
    reply_language: str = Field(
        description="Language the reply must be written in. An explicit request wins; "
        "otherwise the ticket language. Arabizi → 'ar'."
    )
    sentiment: Sentiment
    needs_account_action: bool = Field(
        description="Resolving it needs this customer's account checked or changed "
        "(e.g. approved withdrawal not received, failed deposit, stuck verification)."
    )
    vulnerable_customer: bool = Field(
        description="Distress, hopelessness, self-harm or severe financial-hardship language."
    )
    security_sensitive: bool = Field(
        description="Another person's account/data, account data disclosure, change of email/"
        "phone, 2FA or login-security change."
    )
    search_query: str = Field(
        description="The customer's answerable question(s) restated in plain English for a "
        "knowledge-base search. Never include personal data."
    )
    confidence: float = Field(description="0.0–1.0, your confidence in this classification.")


# ---- LLM call 2: grounded draft -------------------------------------------------------------


class DraftOut(BaseModel):
    abstain: bool = Field(description="True if the sources do not answer the question at all.")
    partial: bool = Field(description="True if only part of the question is answered.")
    reply: str = Field(
        description="Reply to the customer in the reply language. No citations "
        "inline; facts only from the sources."
    )
    cited_chunk_ids: list[str] = Field(description="IDs of every source used in the reply.")
    uncovered: list[str] = Field(description="Parts of the question the sources do not cover.")
    note_for_agent: str = Field(description="Short internal note: conflicts, gaps, caveats.")


# ---- Workflow output ------------------------------------------------------------------------


class Citation(BaseModel):
    chunk_id: str
    page_title: str
    page_url: str
    section: str


class Usage(BaseModel):
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, other: "Usage") -> "Usage":
        return Usage(
            calls=self.calls + other.calls,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cost_usd=round(self.cost_usd + other.cost_usd, 6),
        )


class Routing(BaseModel):
    decision: Literal["auto_draft_ok", "human_required"]
    destination: Destination | None
    reasons: list[ReasonCode]


class Result(BaseModel):
    """One record per ticket (PRD FR-6.1)."""

    ticket_id: str
    classification: Classification | None
    rule_signals: list[str]
    draft: str
    citations: list[Citation]
    abstained: bool
    partial: bool
    uncovered: list[str]
    note_for_agent: str
    kb_conflicts: list[str]
    post_check_failures: list[str]
    routing: Routing
    retrieved: list[tuple[str, float]]
    pii_types: list[str]
    models: dict[str, str]
    prompt_version: str
    kb_hash: str | None
    usage: Usage
    latency_ms: float

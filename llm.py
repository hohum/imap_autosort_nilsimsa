from __future__ import annotations
from typing import List, Optional, Tuple
import fnmatch
import re

try:
    from openai import OpenAI  # type: ignore
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore

class LLMClassifier:
    """Email intent classification via OpenAI. Optional and tolerant."""
    # Centralized system prompt (explicit format instructions for stable output)
    SYSTEM_PROMPT = (
        "You are an email intent detector for an IMAP autosorter.\n"
        "Input: a small email header block (From, Subject). "
        "Output: ONLY a compact JSON array string on one line in this exact shape: "
        '[{\"cta\":\"<short action>\"},{\"label\":[\"<Label>:<0.00-1.00>\",\"<Label>:<0.00-1.00>...\"]}]. '
        "Rules: keep under 160 chars, no newlines, no extra commentary. "
        "If unsure, use Unclassified:1.00. If suspicious, include Spam or Phishing with a reasonable probability "
        "and a short CTA like Review or Report."
    )
    # Detailed user prompt (restored)
    USER_PROMPT_TEMPLATE = r'''
Return exactly one JSON array with two objects:
[{"cta":"..."},{"label":[["X",0.00],["Y",0.00],["Z",0.00],["A",0.00],["B",0.00]]}]

Rules:
- JSON output returned
  - must be valid
  - Keys and all string values MUST use double quotes.
  - Output the JSON document directly — no quotes, no code fences, no extra text.
- Provide ≥5 labels; probabilities have two decimals and sum to 1.00.
- CTA: 3–10 words, imperative, generic, dictionary words only (avoid “now”, “immediately”, etc.); include a generic but relevant domain noun if obvious (e.g., “Review military aircraft discussion thread”).
- Use From/Subject + domain for inference; prefer abstract action (don’t parrot topic words/brands unless essential for safety/finance).
- Labels: noun phrases, sorted desc; include "Spam" and/or "Phishing Suspected" only if very confident.

Guidance:
- Detect distinctive signals — including subtle role phrases — and generalize into brand-agnostic concepts; capture oddities that differentiate the message; avoid proper nouns/department names and fixed keyword lists; do not over-prioritize any single field (e.g., “photo desk” ⇒ “photo”).
- Some emails are internal notifications from my own systems (e.g., Macrodroid, fail2ban).
'''

    def __init__(self, api_key: Optional[str], sender_skip_globs: List[str], logger=None) -> None:
        self.api_key = (api_key or "").strip()
        self.sender_skip_globs = [g.lower() for g in (sender_skip_globs or [])]
        self.logger = logger

    def classify_email(self, header_block: str) -> Tuple[str, bool]:
        """Public safe wrapper so callers don't rely on a private method."""
        try:
            return self._classify_email(header_block)
        except Exception as e:
            if self.logger:
                self.logger.error("LLM classify_email exception: %s", e)
            return '[{"cta":"Notice LLM internal error"},{"label":["Unclassified:1.00"]}]', False

    # Back-compat alias for older callers
    def classify_mail(self, header_block: str) -> Tuple[str, bool]:
        return self.classify_email(header_block)

    def _classify_email(self, header_block: str) -> Tuple[str, bool]:
        # Sender skip guard
        from_addr = ""
        m = re.search(r"^From:\s*(.*)$", header_block, re.I | re.M)
        if m:
            raw_from = (m.group(1) or "").strip()
            addr_match = re.search(r"<([^>]+)>", raw_from)
            from_addr = (addr_match.group(1) if addr_match else raw_from).strip().lower()
        if any(fnmatch.fnmatch(from_addr, pat) for pat in self.sender_skip_globs):
            if self.logger:
                self.logger.info("LLM skipped for sender %s (sender_skip_llm matched)", from_addr)
            return '[{"cta":"Sender skipped"},{"label":["SenderSkipped:1.00"]}]', False

        # Fallback if API unavailable
        if not self.api_key or not OpenAI:
            return '[{"cta":"Notice LLM not configured"},{"label":["Unclassified:1.00"]}]', False

        # Call OpenAI
        try:
            client = OpenAI(api_key=self.api_key)  # type: ignore
            prompt = f"{self.USER_PROMPT_TEMPLATE}\n\n{header_block}"
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                top_p=1,
                presence_penalty=0,
                frequency_penalty=0,
                timeout=60,
            )
            result = (resp.choices[0].message.content or "").strip()
            if self.logger:
                self.logger.info("LLM API response: %s", result)
            is_suss = bool(re.search(r'\b(Spam|Phishing)\b', result, re.I))
            return result, is_suss
        except Exception as e:
            if self.logger:
                self.logger.error("GPT classification error: %s", e)
            return '[{"cta":"Notice LLM classification error"},{"label":["Unclassified:1.00"]}]', False


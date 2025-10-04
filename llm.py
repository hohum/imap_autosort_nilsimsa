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
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are an email intent detector."},
                    {"role": "user", "content": header_block},
                ],
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


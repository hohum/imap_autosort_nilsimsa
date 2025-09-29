from __future__ import annotations
from typing import List, Optional
import json

try:
    from openai import OpenAI  # type: ignore
except Exception:
    OpenAI = None  # type: ignore

class LLMClassifier:
    """Thin wrapper for email intent classification via OpenAI.
    Handles sender_skip_llm globs and missing API key gracefully.
    """
    def __init__(self, api_key: Optional[str], sender_skip_globs: List[str], logger=None) -> None:
        self.logger = logger
        self.sender_skip_globs = [g.lower() for g in (sender_skip_globs or [])]
        self.client = None
        api_key = (api_key or '').strip()
        if api_key and OpenAI is not None:
            try:
                self.client = OpenAI(api_key=api_key)
            except Exception as e:
                if self.logger:
                    self.logger.warning("LLM client init failed: %s", e)
                self.client = None

    def _classify_email(self, msg_header: str) -> tuple[str, bool]:
        if not self.client:
            return '[{"cta":"Notice LLM not configured"},{"label":[["Unclassified:1.00"]]}]', False

        prompt = (
            msg_header + "\n\n" + r'''
Return exactly one JSON array with two objects:
[{"cta":"..."},{"label":[["X",0.00],["Y",0.00],["Z",0.00],["A",0.00],["B",0.00]]}]

Rules:
- Keys and all string values MUST use double quotes.
- Provide ≥5 labels as pairs [<string>, <number>]; probabilities have two decimals and sum to 1.00.
- CTA: 3–10 words, imperative, generic, dictionary words only (avoid “now”, “immediately”, etc.); include a generic but relevant domain noun if obvious (e.g., “Review military aircraft discussion thread”).
- Use From/Subject + domain for inference; prefer abstract action (don’t parrot topic words/brands unless essential for safety/finance).
- Labels: noun phrases, sorted desc; include "Spam" and/or "Phishing Suspected" only if very confident.
- Output the JSON document directly — no quotes, no code fences, no extra text.

Guidance:
- Detect distinctive signals — including subtle role phrases — and generalize into brand-agnostic concepts; capture oddities that differentiate the message; avoid proper nouns/department names and fixed keyword lists; do not over-prioritize any single field (e.g., “photo desk” ⇒ “photo”).
- Some emails are internal notifications from my own systems (e.g., Macrodroid, fail2ban).
'''
        )
        try:
            response = self.client.chat.completions.create(
                model="gpt-5-mini",
                messages=[
                    {"role": "system", "content": "You are an email intent detector."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,      # most deterministic
                top_p=1,            # no nucleus sampling
                presence_penalty=0,
                frequency_penalty=0,
                timeout=60,
            )
            result = (response.choices[0].message.content or "").strip()
            if self.logger:
                self.logger.info("ChatGPT API response: %s", result)
            # get and return flag is_suss
            is_suss = False
            try:
                data = json.loads(result)
                if isinstance(data, list):
                    label_obj = next((o for o in data if isinstance(o, dict) and "label" in o), None)
                    if label_obj:
                        for item in label_obj["label"]:
                            # New schema: ["Label", 0.00]
                            if isinstance(item, list) and len(item) == 2:
                                name, prob = item[0], item[1]
                                try:
                                    p = float(prob)
                                except Exception:
                                    continue
                                if name in ("Spam", "Phishing Suspected") and p >= 0.50:
                                    is_suss = True
                                    break
            except json.JSONDecodeError:
                # leave is_suss = False on malformed output
                pass
                        
            return result, is_suss
        except Exception as e:
            if self.logger:
                self.logger.error("GPT classification error: %s", e)
            return '[{"cta":"Notice LLM not configured"},{"label":[["Unclassified",1.00]]}]', False


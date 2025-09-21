from __future__ import annotations
from dataclasses import dataclass
from typing import Dict
from ..parsing.message_parser import ParsedMessage
from ..scoring.base import ScoreResult, ScoreStrategy

@dataclass
class LLMScorer(ScoreStrategy):
	model_name: str

	@property
	def name(self) -> str:
		return f"llm:{self.model_name}"

	def score(self, message: ParsedMessage) -> ScoreResult:
		# ... integrate an LLM here (prompt, call, parse) ...
		# Return a ScoreResult with labels and confidences.
		return ScoreResult(strategy=self.name, labels=[], scores={}, metadata={"note": "not implemented"})

def classify_email(message, openai_api_key=None, *args, **kwargs):
	# ...existing code...
	if not openai_api_key:
		# LLM not configured, skip
		return None
	try:
		import openai
		openai.api_key = openai_api_key
		# ...LLM logic...
	except Exception:
		# On error, skip LLM
		return None
	# ...existing code...

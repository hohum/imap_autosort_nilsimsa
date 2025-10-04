f"""
Refactor rules
   - Strictly keep functionality identical,
   - reduce duplication/lines.
   - maintrin code clarify modifying/adding/deleting comments to aid in maintainability.
   - remove overkill error handling.
"""

from __future__ import annotations
import re
import email
from typing import List

def _unfold(header_block: str) -> List[str]:
	# RFC 5322 unfold: continuation lines (starting with WSP) join previous line with a space
	lines = header_block.replace("\r\n", "\n").replace("\r", "\n").split("\n")
	out: List[str] = []
	for line in lines:
		if not line:
			continue
		if line[:1] in (" ", "\t") and out:
			out[-1] = f"{out[-1]} {line.strip()}"
		else:
			out.append(line.rstrip())
	return out

def normalize_header(
	mail_txt: str,
	*,
	exclude_headers: re.Pattern,
	headers_skip_re: re.Pattern,
	chomp_header: re.Pattern,
	headerIsX: re.Pattern,
	xinclude: List[str],
	dkim_just_d: re.Pattern,
	exclude_received_from_localhost: re.Pattern,
	weight_headers_re: re.Pattern,
	weight_headers_by: int,
) -> str:
	# Precompute x-include set (case-insensitive)
	xinclude_lc = {h.lower() for h in (xinclude or [])}

	result_lines: List[str] = []

	for raw in _unfold(mail_txt):
		if ":" not in raw:
			continue
		name, value = raw.split(":", 1)
		hname = name.strip()
		hval = value.strip()

		# Skip excluded headers (e.g., Date, Message-ID, noisy X-headers, ARC-*)
		if exclude_headers.search(hname):
			continue
		# Skip headers by explicit skip regex
		if headers_skip_re.search(hname):
			continue
		# Skip local Received lines
		if hname.lower() == "received" and exclude_received_from_localhost.search(hval):
			continue
		# Only include allowed X- headers if xinclude list is provided
		if headerIsX.search(hname) and xinclude_lc and hname.lower() not in xinclude_lc:
			continue
		# DKIM: reduce to d=... token if present
		if hname.lower() == "dkim-signature":
			m = dkim_just_d.match(hval)
			if m:
				hval = m.group(1)

		# Collapse internal whitespace/newlines per provided regex
		if chomp_header:
			hval = chomp_header.sub(" ", hval).strip()

		# Emit, applying weighting if configured
		repeats = max(1, int(weight_headers_by)) if weight_headers_re.search(hname) else 1
		line = f"{hname}: {hval}"
		for _ in range(repeats):
			result_lines.append(line)

	# Join with newline to produce a stable, hashable block
	return "\n".join(result_lines)

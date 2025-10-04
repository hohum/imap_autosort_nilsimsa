f"""
Refactor rules
   - Strictly keep functionality identical,
   - reduce duplication/lines.
   - maintrin code clarify modifying/adding/deleting comments to aid in maintainability.
   - remove overkill error handling.
"""

import re
import email
from typing import List

def normalize_header(
    mail_txt: str,
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
    msg = email.message_from_string(mail_txt)
    out: List[str] = []
    xincl = {h.lower() for h in (xinclude or [])}

    for header in sorted(set(msg.keys() or [])):
        if exclude_headers.search(header):
            continue
        if headers_skip_re.search(header) and (header.lower() not in xincl):
            continue

        vals = msg.get_all(header, []) or []

        # DKIM: keep only the d= token from each DKIM-Signature value
        if header == 'DKIM-Signature':
            toks: List[str] = []
            for v in vals:
                s = str(v)  # do not unfold DKIM; extract only token
                toks.append(dkim_just_d.sub(lambda m: m.group(1), s))
            if toks:
                out.append(f"{header}: " + ", ".join(toks))
            continue

        for v in vals:
            s = chomp_header.sub(' ', str(v))  # unfold folded headers
            if header in {'Received', 'X-Received'}:
                if 'port 10024' in s:
                    continue
                if header == 'Received' and exclude_received_from_localhost.search(s):
                    continue
                s = re.sub(r' id \S+', '', s)
                s = re.sub(r' (Sun|Mon|Tue|Wed|Thu|Fri|Sat),', '', s)
                s = re.sub(r' (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)', '', s)
                s = re.sub(r' \d{4}-\d{2}-\d{2}', '', s)
                s = re.sub(r' \d{2}:\d{2}:\d{2}(\.\d+)*', '', s)
                s = re.sub(r' ( [A-Z]{3,4} )*m=\+\d+\.\d+', '', s)
                s = re.sub(r' \+\d{4}( (\([A-Z]{3,4}\)))*', '', s)
                s = re.sub(r' \(.*?\) by ', ' by ', s)
            out.append(f"{header}: {s}")
            if weight_headers_re.search(header):
                for _ in range(max(0, weight_headers_by - 1)):
                    out.append(f"{header}: {s}")

    return "\n".join(out) + "\n"

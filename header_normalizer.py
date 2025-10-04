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
    # Strip weekday banners early (cheap pre-pass)
    mail_txt = re.sub(r'(?:Sun|Mon|Tue|Wed|Thu|Fri|Sat).*?([;\n])', r'\1', mail_txt)

    result: List[str] = []
    msg = email.message_from_string(mail_txt)

    for header in sorted(set(msg.keys())):
        if exclude_headers.search(header) or headers_skip_re.search(header):
            continue
        # Drop most X- headers unless explicitly kept
        if headerIsX.search(header) and header not in xinclude:
            continue

        for value in msg.get_all(header, []):
            # Unfold header lines and preserve bytes via backslash escapes
            value = chomp_header.sub(' ', value.encode('ascii', 'backslashreplace').decode()) + "\n"

            if header in {'Received', 'X-Received'}:
                # Remove amavis noise and local Received lines
                if re.search(r'port 10024', value):
                    continue
                if header == 'Received' and exclude_received_from_localhost.search(value):
                    continue
                # Trim typical volatile bits
                value = re.sub(r' id \S+', '', value)
                value = re.sub(r' (Sun|Mon|Tue|Wed|Thu|Fri|Sat),', '', value)
                value = re.sub(r' (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)', '', value)
                value = re.sub(r' \d{4}-\d{2}-\d{2}', '', value)
                value = re.sub(r' \d{2}:\d{2}:\d{2}(\.\d+)*', '', value)
                value = re.sub(r' ( [A-Z]{3,4} )*m=\+\d+\.\d+', '', value)
                value = re.sub(r' \+\d{4}( (\([A-Z]{3,4}\)))*', '', value)
                value = re.sub(r' \(.*?\) by ', ' by ', value)
                add = f"{header}: {value}"
            elif header == 'DKIM-Signature':
                add = f"{header}: {dkim_just_d.sub(lambda m: m.group(1), value)}\n"
            else:
                add = f"{header}: {value}"

            # Header weighting: exact same effect as original (string repetition)
            if weight_headers_re.search(header):
                add += add * weight_headers_by
            result.append(add)

    return ''.join(result)

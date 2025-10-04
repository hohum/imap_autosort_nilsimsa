import re
from typing import List, Tuple, Optional, Any

def parse_uid_set(s: str) -> List[int]:
    out: List[int] = []
    s = (s or '').strip()
    if not s:
        return out
    for part in s.replace(',', ' ').split():
        if ':' in part:
            a, b = map(int, part.split(':', 1))
            lo, hi = (a, b) if a <= b else (b, a)
            out.extend(range(lo, hi + 1))
        else:
            out.append(int(part))
    return out

def extract_copyuid(result: Any) -> Optional[Tuple[int, List[int], List[int]]]:
    typ, data = result or (None, None)
    pieces: list[str] = []
    for d in (data or []):
        if isinstance(d, (bytes, bytearray)):
            pieces.append(d.decode('utf-8', 'ignore'))
        elif isinstance(d, tuple) and len(d) > 1 and isinstance(d[1], (bytes, bytearray)):
            pieces.append(d[1].decode('utf-8', 'ignore'))
        elif isinstance(d, str):
            pieces.append(d)
    joined = ' '.join(pieces)
    m = re.search(r'\[(COPYUID|APPENDUID)\s+(\d+)\s+([^\s]+)\s+([^\]]+)\]', joined)
    if not m:
        return None
    uidvalidity = int(m.group(2))
    src = parse_uid_set(m.group(3))
    dst = parse_uid_set(m.group(4))
    return uidvalidity, src, dst
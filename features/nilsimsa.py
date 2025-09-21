from typing import Optional, Union

def _ensure_str(value: Union[str, bytes]) -> str:
	if isinstance(value, bytes):
		return value.decode("utf-8", errors="ignore")
	return value

def digest(text: Union[str, bytes]) -> str:
	"""
	Return hex digest for the given text using an installed nilsimsa library.
	pip install pynilsimsa  (or)  pip install nilsimsa
	Accepts str or bytes.
	"""
	text_str = _ensure_str(text)
	try:
		# pynilsimsa
		from pynilsimsa import Nilsimsa  # type: ignore
		return Nilsimsa(text_str).hexdigest()
	except Exception:
		pass
	try:
		# nilsimsa
		from nilsimsa import Nilsimsa  # type: ignore
		return Nilsimsa(text_str).hexdigest()
	except Exception as exc:
		raise RuntimeError("Install 'pynilsimsa' or 'nilsimsa' to compute NilSimsa digests.") from exc

def similarity_hex(h1: str, h2: str) -> int:
	"""
	Return similarity score between two NilSimsa hex digests.
	Range depends on backend; typically -128..128 (higher is more similar).
	"""
	# Prefer backend-provided comparison if available.
	try:
		from pynilsimsa import compare_digests  # type: ignore
		return int(compare_digests(h1, h2))
	except Exception:
		pass
	try:
		from nilsimsa import compare_digests  # type: ignore
		return int(compare_digests(h1, h2))
	except Exception as exc:
		raise RuntimeError("Install a nilsimsa library that supports compare_digests(h1, h2).") from exc

def hexdigest(text: Union[str, bytes]) -> str:
	"Alias for digest(...) for drop-in compatibility."
	return digest(text)

def compare_hex(h1: str, h2: str) -> int:
	"Alias for similarity_hex(...) for drop-in compatibility."
	return similarity_hex(h1, h2)

def compare_texts(t1: Union[str, bytes], t2: Union[str, bytes]) -> int:
	"Compute digests of two texts (or files read as text) and return similarity."
	return similarity_hex(digest(t1), digest(t2))

__all__ = [
	"digest",
	"similarity_hex",
	"hexdigest",
	"compare_hex",
	"compare_texts",
]

def _read_arg_as_text(arg: str) -> str:
	from pathlib import Path
	p = Path(arg)
	if p.exists():
		return p.read_text(encoding="utf-8", errors="ignore")
	return arg

def _main() -> None:
	import argparse
	parser = argparse.ArgumentParser(prog="nilsimsa", description="NilSimsa digest and similarity tools")
	sub = parser.add_subparsers(dest="cmd", required=True)

	p_digest = sub.add_parser("digest", help="Print NilSimsa hexdigest of a file or text")
	p_digest.add_argument("input", help="Path to a file or a raw text")

	p_cmp_hex = sub.add_parser("compare", help="Compare two NilSimsa hex digests")
	p_cmp_hex.add_argument("hex1"); p_cmp_hex.add_argument("hex2")

	p_cmp_txt = sub.add_parser("compare-text", help="Compare two texts (or files) by NilSimsa similarity")
	p_cmp_txt.add_argument("a"); p_cmp_txt.add_argument("b")

	args = parser.parse_args()
	if args.cmd == "digest":
		print(digest(_read_arg_as_text(args.input)))
	elif args.cmd == "compare":
		print(similarity_hex(args.hex1, args.hex2))
	elif args.cmd == "compare-text":
		a = _read_arg_as_text(args.a)
		b = _read_arg_as_text(args.b)
		print(compare_texts(a, b))

if __name__ == "__main__":
	_main()

# vim: set ts=4 sts=4 sw=4 noet:

from __future__ import annotations

import hashlib
from typing import Dict

# ---------------------------------------------------------------------------
# Token substitution
# ---------------------------------------------------------------------------
class TokenSubstitution:
    THRESHOLD = 500

    def __init__(self):
        self._vault:   Dict[str, str] = {}
        self._reverse: Dict[str, str] = {}
        self._n = 0

    def _mk(self) -> str:
        self._n += 1
        return f"__BLOB_{self._n:04d}__"

    @staticmethod
    def _is_b64(s: str) -> bool:
        if len(s) < 100:
            return False
        sample = s[:200].strip()
        return (
            sum(1 for c in sample if c.isalnum() or c in "+/=") / len(sample)
        ) > 0.9 and "\n" not in sample[:100]

    def compress_tree(self, tree: Dict[str, str]) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for path, content in tree.items():
            if content and len(content) > self.THRESHOLD:
                if (
                    path.endswith(".b64")
                    or self._is_b64(content)
                    or (path.endswith(".json") and len(content) > 5000)
                    or (path.endswith(".svg")  and len(content) > 3000)
                ):
                    h = hashlib.md5(content[:200].encode()).hexdigest()
                    if h in self._reverse:
                        out[path] = self._reverse[h]
                    else:
                        pid = self._mk()
                        self._vault[pid]   = content
                        self._reverse[h]   = pid
                        out[path]          = pid
                    continue
            out[path] = content
        return out

    def expand(self, text: str) -> str:
        for ph, original in self._vault.items():
            if ph in text:
                text = text.replace(ph, original)
        return text

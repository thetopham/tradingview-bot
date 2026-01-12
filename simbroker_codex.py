# simbroker_codex.py
"""
Codex-generated SimBroker should live here.

Your api.py router can select between:
- simbroker_assistant.SimBroker  (reference implementation)
- simbroker_codex.SimBroker      (codex implementation)

Set:
  SIMBROKER_IMPL=assistant|codex
to switch.

This stub exists so imports don't crash before Codex writes the real file.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, List


class SimBroker:  # pragma: no cover
    def __init__(self, *args, **kwargs):
        raise RuntimeError(
            "simbroker_codex.SimBroker is a stub. Generate it with Codex prompts, "
            "or set SIMBROKER_IMPL=assistant."
        )

    def handle(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def sim_update(self, account_id: int, contract_id: Optional[str] = None, now_ts_iso: Optional[str] = None) -> List[Dict[str, Any]]:
        raise NotImplementedError

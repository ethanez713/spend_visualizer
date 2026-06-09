"""The LLM stage's crash guard: swallow runtime errors, but NEVER a Ctrl+C.

Swallowing KeyboardInterrupt would let an aborted audit "complete" with no LLM
review while still stamping rows as audited — silently skipping them forever.
"""
import pytest

from src.llm import CategoryLLM


def _llm_raising(exc: BaseException) -> CategoryLLM:
    llm = CategoryLLM()
    llm._categorize_inner = lambda items: (_ for _ in ()).throw(exc)
    return llm


def given_runtime_error_when_categorize_then_skips_and_returns_empty(capsys):
    llm = _llm_raising(ValueError("boom"))
    assert llm.categorize([{"row_index": 0}]) == {}
    assert "skipping LLM stage" in capsys.readouterr().out


def given_keyboard_interrupt_when_categorize_then_propagates():
    llm = _llm_raising(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        llm.categorize([{"row_index": 0}])

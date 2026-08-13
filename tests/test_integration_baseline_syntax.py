"""Regression guards for syntax-sensitive integration seams.

These modules have previously been damaged by overlapping cherry-picks that
spliced adjacent constructor changes together. Compile the real source before
any import-heavy suite can hide the root failure behind collection errors.
"""

from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("relative_path", ["cli.py", "tools/delegate_tool.py"])
def test_integration_baseline_module_compiles(relative_path):
    source_path = REPOSITORY_ROOT / relative_path
    source = source_path.read_text(encoding="utf-8")

    compile(source, str(source_path), "exec")


def test_cli_streaming_override_and_interim_setting_are_both_closed():
    source = (REPOSITORY_ROOT / "cli.py").read_text(encoding="utf-8")

    expected = '''        self.streaming_enabled = (
            False if no_streaming
            else CLI_CONFIG["display"].get("streaming", False)
        )
        self.interim_assistant_messages = CLI_CONFIG["display"].get(
            "interim_assistant_messages", False
        )'''
    assert expected in source


def test_delegate_child_build_combines_schema_context_and_memory_mode():
    source = (REPOSITORY_ROOT / "tools/delegate_tool.py").read_text(encoding="utf-8")

    expected = '''        child = _build_child_preserving_parent_tools(
            task_index=i,
            goal=t["goal"],
            context=_child_context,'''
    assert expected in source
    assert "            memory_mode=effective_memory_mode,\n        )" in source
    assert source.count("        children.append((i, t, child))") == 1

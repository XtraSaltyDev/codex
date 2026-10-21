"""Adapt the upstream file-editing instruction to Spark's function-only tools."""

import json
import re


PATCH_EXAMPLE = {'cmd': "apply_patch <<'PATCH'\n*** Begin Patch\n*** Update File: path/to/file.py\n@@\n-old\n+new\n*** End Patch\nPATCH"}
PATCH_GUIDANCE = (
    '- Edit files with the `apply_patch` shell helper through `exec_command`. '
    'In this Spark profile, `apply_patch` is not a callable function tool; do not call it directly. '
    'Call `exec_command` with a `cmd` string containing a quoted patch heredoc. '
    'Codex intercepts the command and applies its normal patch validation, sandbox, and approval handling. '
    'For example: ' + json.dumps(PATCH_EXAMPLE)
)


def spark_instructions(instructions):
    if PATCH_GUIDANCE in instructions:
        return instructions
    pattern = r'^- Use the `apply_patch` tool to edit files[^\n]*$'
    matches = list(re.finditer(pattern, instructions, re.MULTILINE))
    if len(matches) != 1:
        raise ValueError('Expected one upstream apply_patch instruction; review the Spark prompt adaptation before updating the profile.')
    match = matches[0]
    return instructions[:match.start()] + PATCH_GUIDANCE + instructions[match.end():]

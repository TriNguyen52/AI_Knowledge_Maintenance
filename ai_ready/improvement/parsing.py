"""JSON extraction and repair utilities for LLM responses.

LLMs frequently wrap JSON in markdown fences, add explanatory text
before/after the payload, or produce syntactically invalid JSON
(missing commas, trailing commas, truncated output). These utilities
handle those cases so action code can focus on domain logic.
"""

from __future__ import annotations

import json
import re
import logging

logger = logging.getLogger(__name__)


def extract_json(content: str) -> str:
    """Extract the first complete JSON object from an LLM response.

    LLMs often wrap JSON in markdown fences or add explanatory text
    before/after the JSON. This function finds the first '{' and its
    matching '}' using brace counting, handling strings and escapes.
    If the JSON is truncated (missing closing braces), they are appended.

    Args:
        content: Raw LLM response text.

    Returns:
        The extracted JSON string (may be invalid if the LLM produced
        garbage; caller should handle parse errors).
    """
    content = content.strip()

    # Strip markdown code fences
    if content.startswith("```"):
        lines = content.split("\n")
        # Remove first line (```json or ```) and last line (```)
        if len(lines) > 2 and lines[-1].strip() == "```":
            content = "\n".join(lines[1:-1])
        elif len(lines) > 1:
            content = "\n".join(lines[1:])

    # Find the first complete JSON object using brace/bracket counting
    start = content.find("{")
    if start == -1:
        start = content.find("[")
        if start == -1:
            return content  # No JSON found, return as-is

    brace_depth = 0
    bracket_depth = 0
    in_string = False
    escape = False
    for i in range(start, len(content)):
        ch = content[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            brace_depth += 1
        elif ch == "}":
            brace_depth -= 1
        elif ch == "[":
            bracket_depth += 1
        elif ch == "]":
            bracket_depth -= 1

        # Return when everything is balanced
        if brace_depth == 0 and bracket_depth == 0 and i > start:
            return content[start:i + 1]

    # JSON is truncated — append missing closing braces/brackets
    suffix = "]" * max(0, bracket_depth) + "}" * max(0, brace_depth)
    return content[start:] + suffix


def repair_json(json_str: str) -> str:
    """Attempt to fix common JSON syntax errors produced by LLMs.

    Uses a line-based approach to add missing commas between lines
    that need them, avoiding false matches inside string values.
    Also removes trailing commas before closing brackets.
    """
    lines = json_str.split("\n")
    fixed_lines = []
    for i, line in enumerate(lines):
        stripped = line.rstrip()
        if i + 1 < len(lines):
            next_stripped = lines[i + 1].lstrip()
            # Add comma if this line ends with a value/bracket (not comma/colon)
            # and the next line starts with a key/bracket
            if (stripped.endswith('"') or stripped.endswith(']') or stripped.endswith('}')) and \
               not stripped.endswith(',') and not stripped.endswith(':') and \
               not stripped.endswith('[') and not stripped.endswith('{') and \
               (next_stripped.startswith('"') or next_stripped.startswith('[')
                or next_stripped.startswith('{')):
                fixed_lines.append(stripped + ",")
            else:
                fixed_lines.append(line)
        else:
            fixed_lines.append(line)

    result = "\n".join(fixed_lines)

    # Remove trailing commas before } or ]
    result = re.sub(r',\s*([}\]])', r'\1', result)

    # Fix within-line missing commas between structural elements.
    # Only match } or ] (not ") followed by " or { or [ to avoid
    # false positives inside string values.
    # Pattern: }  "  → }, "  (missing comma after object, next is key)
    # Pattern: ]  "  → ], "  (missing comma after array, next is key)
    # Pattern: }  {  → }, {  (missing comma between objects in array)
    # Pattern: ]  [  → ], [  (missing comma between arrays in array)
    # Pattern: }  [  → }, [  (missing comma between object and array)
    # Pattern: ]  {  → ], {  (missing comma between array and object)
    result = re.sub(r'([}\]])\s+(["\[\{])', r'\1, \2', result)

    return result


def parse_llm_json(content: str) -> dict | list:
    """Parse an LLM response as JSON, with extraction and repair fallbacks.

    Tries in order:
    1. Direct json.loads on the extracted JSON
    2. json.loads on the line-based + structural repaired JSON
    3. json.loads on aggressively repaired JSON (adds commas between any
       closing/opening delimiter, including " to ", as a last resort)

    Args:
        content: Raw LLM response text.

    Returns:
        Parsed JSON as a dict or list.

    Raises:
        json.JSONDecodeError: If both direct and repaired parsing fail.
    """
    json_str = extract_json(content)
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        pass
    # Second attempt: line-based + structural repair
    try:
        repaired = repair_json(json_str)
        return json.loads(repaired)
    except json.JSONDecodeError:
        pass
    # Third attempt: aggressive repair — add commas between any closing
    # and opening delimiter (including " to ") as a last resort.
    try:
        aggressive = re.sub(r'(["\]\}])\s+(["\[\{])', r'\1, \2', json_str)
        aggressive = re.sub(r',\s*([}\]])', r'\1', aggressive)
        return json.loads(aggressive)
    except json.JSONDecodeError as e:
        raise e

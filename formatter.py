"""Markdown to Telegram MarkdownV2 formatter.

Copied and adapted from hermes-agent/gateway/platforms/telegram.py.
Handles message formatting, table conversion, and chunking for Telegram.
"""

import re
from typing import Optional

MAX_MESSAGE_LENGTH = 4096
SPLIT_THRESHOLD = 4000


# Matches every character that MarkdownV2 requires to be backslash-escaped
# when it appears outside a code span or fenced code block.
_MDV2_ESCAPE_RE = re.compile(r"([_*\[\]()~`>#\+\-=|{}.!\\])")


def escape_mdv2(text: str) -> str:
    """Escape Telegram MarkdownV2 special characters with a preceding backslash."""
    return _MDV2_ESCAPE_RE.sub(r"\\\1", text)


def strip_mdv2(text: str) -> str:
    """Strip MarkdownV2 escape backslashes and formatting markers to produce clean plain text."""
    # Remove escape backslashes before special characters
    cleaned = re.sub(r"\\([_*\[\]()~`>#\+\-=|{}.!\\])", r"\1", text)
    # Remove MarkdownV2 bold markers
    cleaned = re.sub(r"\*([^*]+)\*", r"\1", cleaned)
    # Remove italic markers (word boundary to avoid breaking snake_case)
    cleaned = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"\1", cleaned)
    # Remove strikethrough
    cleaned = re.sub(r"~([^~]+)~", r"\1", cleaned)
    # Remove spoiler markers
    cleaned = re.sub(r"\|\|([^|]+)\|\|", r"\1", cleaned)
    return cleaned


# Matches a GFM table delimiter row
_TABLE_SEPARATOR_RE = re.compile(
    r"^\s*\|?\s*:?-+:?\s*(?:\|s*:?-+:?\s*){1,}\|?\s*$"
)


def _is_table_row(line: str) -> bool:
    """Return True if line could plausibly be a table data row."""
    return bool(line.strip()) and "|" in line.strip()


def _split_markdown_table_row(line: str) -> list[str]:
    """Split a GFM table row into stripped cell values."""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _render_table_block(text: str) -> str:
    """Render a detected GFM table as Telegram-friendly bullet groups.

    Telegram's MarkdownV2 has no table syntax -- pipes are just escaped literals.
    Reformating each row into a bold heading plus bullet list keeps content
    readable on mobile while preserving the data.
    """
    if "|" not in text or "-" not in text:
        return text

    lines = text.split("\n")
    out: list[str] = []
    in_fence = False
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()

        if stripped.startswith("```"):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        if in_fence:
            out.append(line)
            i += 1
            continue

        if (
            "|" in line
            and i + 1 < len(lines)
            and _TABLE_SEPARATOR_RE.match(lines[i + 1])
        ):
            table_block = [line, lines[i + 1]]
            j = i + 2
            while j < len(lines) and _is_table_row(lines[j]):
                table_block.append(lines[j])
                j += 1

            if len(table_block) >= 3:
                headers = _split_markdown_table_row(table_block[0])
                if len(headers) >= 2:
                    rendered_rows: list[str] = []
                    for index, row in enumerate(table_block[2:], start=1):
                        cells = _split_markdown_table_row(row)
                        if len(cells) < len(headers):
                            cells.extend([""] * (len(headers) - len(cells)))
                        elif len(cells) > len(headers):
                            cells = cells[: len(headers)]
                        heading = next(
                            (cell for cell in cells if cell),
                            f"Row {index}",
                        )
                        rendered_rows.append(f"*{heading}*")
                        rendered_rows.extend(
                            f"• {header}: {value}"
                            for header, value in zip(headers, cells)
                        )
                    out.append("\n\n".join(rendered_rows))
                    i = j
                    continue

        out.append(line)
        i += 1

    return "\n".join(out)


def _utf16_len(s: str) -> int:
    """Count UTF-16 code units in s.

    Telegram's message-length limit (4096) is measured in UTF-16 code units,
    not Unicode code-points. Characters outside the Basic Multilingual Plane
    (emoji, CJK Extension B, musical symbols) consume two UTF-16 code units.
    """
    return len(s.encode("utf-16-le")) // 2


def _prefix_within_utf16_limit(s: str, limit: int) -> str:
    """Return the longest prefix of s whose UTF-16 length <= limit.

    Respects surrogate-pair boundaries so multi-code-unit characters are
    never sliced in half.
    """
    if _utf16_len(s) <= limit:
        return s
    lo, hi = 0, len(s)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _utf16_len(s[:mid]) <= limit:
            lo = mid
        else:
            hi = mid - 1
    return s[:lo]


def format_message(text: str) -> str:
    """Format a message for Telegram MarkdownV2.

    1. Convert GFM tables to bullet groups
    2. Escape MarkdownV2 special characters
    3. Wrap result in code fences if it contains unescaped newlines
       (Telegram requires code blocks for multi-line content in MDV2)
    """
    # Convert tables first
    text = _render_table_block(text)

    # Escape special characters
    text = escape_mdv2(text)

    # Wrap in code block if it has newlines (MDV2 requires it for multi-line)
    if "\n" in text:
        # Use a neutral language hint, triple backticks
        # Note: backticks are already escaped above, so we add them raw
        # Actually, in MDV2 inside code blocks we DON'T escape backticks
        # Let's use a different approach: escape_mdv2 is called first, then
        # we wrap. But wrapping requires unescaping backticks inside the code block.
        # Simpler approach: use HTML mode or just send as plain text.
        # For now: if there are newlines, wrap in triple backticks with no lang.
        # But backticks were escaped, so we need to unescape them.
        text = text.replace("\\`", "`").replace("\\~", "~")
        text = "```\n" + text + "\n```"

    return text


def chunk_message(text: str, max_len: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Split a message into chunks that fit Telegram's 4096 limit.

    Prefers splitting on double newlines (paragraphs), then single newlines,
    then arbitrary boundaries. Always respects UTF-16 code units.
    """
    if _utf16_len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if _utf16_len(remaining) <= max_len:
            chunks.append(remaining)
            break

        # Try to find a paragraph break first
        chunk = _prefix_within_utf16_limit(remaining, max_len)

        # Look for paragraph break (double newline) before the limit
        paragraph_break = chunk.rfind("\n\n")
        single_break = chunk.rfind("\n")

        if paragraph_break > max_len * 0.5:
            # Paragraph break is in the second half -- split there
            split_at = paragraph_break
        elif single_break > max_len * 0.7:
            # Single newline is late -- split there
            split_at = single_break
        else:
            # No good break point -- split at word boundary
            split_at = chunk.rfind(" ")
            if split_at < max_len * 0.3:
                # No word boundary, just hard split
                split_at = _prefix_within_utf16_limit(
                    remaining, max_len - 10
                ).rfind(" ")
                if split_at < 1:
                    split_at = max_len - 10

        if split_at < 1:
            split_at = max_len - 10

        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()

    return [c for c in chunks if c]


def format_and_chunk(text: str) -> list[str]:
    """Split a message into sendable chunks (plain text, no formatting)."""
    return chunk_message(text)
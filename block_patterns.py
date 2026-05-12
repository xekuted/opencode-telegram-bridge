"""Dangerous command detection patterns.

Copied from hermes-agent/tools/approval.py (HARDLINE_PATTERNS + DANGEROUS_PATTERNS).
These patterns are checked BEFORE sending a message to OpenCode.

Two tiers:
- HARDLINE: always blocked, even with any override
- DANGEROUS: requires user confirmation before proceeding
"""

import re
import unicodedata
from typing import Optional

# Regex fragment matching command start positions.
# Matches: start of string, after command separators, after subshell openers,
# optionally consuming leading wrapper commands (sudo, env, exec, nohup, setsid).
_CMDPOS = (
    r"(?:^|[;&|\n`]|\$\()"       # start position
    r"\s*"                        # optional whitespace
    r"(?:sudo\s+(?:-[^\s]+\s+)*)?"  # optional sudo with flags
    r"(?:env\s+(?:\w+=\S*\s+)*)?"   # optional env with VAR=VAL pairs
    r"(?:(?:exec|nohup|setsid|time)\s+)*"  # optional wrapper commands
    r"\s*"
)

# Sensitive path patterns
_SSH_SENSITIVE_PATH = r"(?:~|\$home|\$\{home\})/\.ssh(?:/|$)"
_HERMES_ENV_PATH = (
    r"(?:~\/\.hermes/|"
    r"(?:\$home|\$\{home\})/\.hermes/|"
    r"(?:\$hermes_home|\$\{hermes_home\})/)"
    r"\.env\b"
)
_PROJECT_ENV_PATH = r"(?:(?:/|\.{1,2}/)?(?:[^\s/\"'`]+/)*\.env(?:\.[^/\s\"'`]+)*)"
_PROJECT_CONFIG_PATH = r"(?:(?:/|\.{1,2}/)?(?:[^\s/\"'`]+/)*config\.yaml)"
_SENSITIVE_WRITE_TARGET = (
    r"(?:/etc/|/dev/sd|"
    rf"{_SSH_SENSITIVE_PATH}|"
    rf"{_HERMES_ENV_PATH})"
)
_PROJECT_SENSITIVE_WRITE_TARGET = rf"(?:{_PROJECT_ENV_PATH}|{_PROJECT_CONFIG_PATH})"
_COMMAND_TAIL = r"(?:\s*(?:&&|\|\||;).*)?$"

_RE_FLAGS = re.IGNORECASE | re.DOTALL

# =========================================================================
# Hardline patterns -- unconditional blocks
# =========================================================================
# These commands are ALWAYS blocked. Even if the user sends "/yolo",
# these cannot be executed. Only things with no recovery path.
HARDLINE_PATTERNS = [
    # Recursive delete targeting root filesystem
    (r"\brm\s+(-[^\s]*\s+)*(/|/\*|/ \*)(\s|$)", "recursive delete of root filesystem"),
    # Recursive delete of system directories
    (r"\brm\s+(-[^\s]*\s+)*(/home|/home/\*|/root|/root/\*|/etc|/etc/\*|/usr|/usr/\*|/var|/var/\*|/bin|/bin/\*|/sbin|/sbin/\*|/boot|/boot/\*|/lib|/lib/\*)(\s|$)", "recursive delete of system directory"),
    # Recursive delete of home directory
    (r"\brm\s+(-[^\s]*\s+)*(~|\$HOME)(/?|/\*)?(\s|$)", "recursive delete of home directory"),
    # Filesystem format
    (r"\bmkfs(\.[a-z0-9]+)?\b", "format filesystem (mkfs)"),
    # Raw block device overwrites (dd + redirection)
    (r"\bdd\b[^\n]*\bof=/dev/(sd|nvme|hd|mmcblk|vd|xvd)[a-z0-9]*", "dd to raw block device"),
    (r">\s*/dev/(sd|nvme|hd|mmcblk|vd|xvd)[a-z0-9]*\b", "redirect to raw block device"),
    # Fork bomb
    (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "fork bomb"),
    # Kill every process on the system
    (r"\bkill\s+(-[^\s]+\s+)*-1\b", "kill all processes"),
    # System shutdown / reboot (anchored to command position)
    (_CMDPOS + r"(shutdown|reboot|halt|poweroff)\b", "system shutdown/reboot"),
    (_CMDPOS + r"init\s+[06]\b", "init 0/6 (shutdown/reboot)"),
    (_CMDPOS + r"systemctl\s+(poweroff|reboot|halt|kexec)\b", "systemctl poweroff/reboot"),
    (_CMDPOS + r"telinit\s+[06]\b", "telinit 0/6 (shutdown/reboot)"),
]

HARDLINE_COMPILED = [
    (re.compile(p, _RE_FLAGS), desc) for p, desc in HARDLINE_PATTERNS
]

# =========================================================================
# Dangerous patterns -- require user confirmation
# =========================================================================
DANGEROUS_PATTERNS = [
    (r"\brm\s+(-[^\s]*\s+)*/", "delete in root path"),
    (r"\brm\s+-[^\s]*r", "recursive delete"),
    (r"\brm\s+--recursive\b", "recursive delete (long flag)"),
    (r"\bchmod\s+(-[^\s]*\s+)*(777|666|o\+[rwx]*w|a\+[rwx]*w)\b", "world/other-writable permissions"),
    (r"\bchmod\s+--recursive\b.*(777|666|o\+[rwx]*w|a\+[rwx]*w)", "recursive world/other-writable (long flag)"),
    (r"\bchown\s+(-[^\s]*)?R\s+root", "recursive chown to root"),
    (r"\bchown\s+--recursive\b.*root", "recursive chown to root (long flag)"),
    (r"\bmkfs\b", "format filesystem"),
    (r"\bdd\s+.*if=", "disk copy"),
    (r">\s*/dev/sd", "write to block device"),
    (r"\bDROP\s+(TABLE|DATABASE)\b", "SQL DROP"),
    (r"\bDELETE\s+FROM\b(?!.*\bWHERE\b)", "SQL DELETE without WHERE"),
    (r"\bTRUNCATE\s+(TABLE)?\s*\w", "SQL TRUNCATE"),
    (r">\s*/etc/", "overwrite system config"),
    (r"\bsystemctl\s+(-[^\s]+\s+)*(stop|restart|disable|mask)\b", "stop/restart system service"),
    (r"\bkill\s+-9\s+-1\b", "kill all processes"),
    (r"\bpkill\s+-9\b", "force kill processes"),
    (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "fork bomb"),
    (r"\b(bash|sh|zsh|ksh)\s+-[^\s]*c(\s+|$)", "shell command via -c/-lc flag"),
    (r"\b(python[23]?|perl|ruby|node)\s+-[ec]\s+", "script execution via -e/-c flag"),
    (r"\b(curl|wget)\b.*\|\s*(ba)?sh\b", "pipe remote content to shell"),
    (r"\b(bash|sh|zsh|ksh)\s+<\s*<?\s*\(\s*(curl|wget)\b", "execute remote script via process substitution"),
    (rf"\btee\b.*[\"']?{_SENSITIVE_WRITE_TARGET}", "overwrite system file via tee"),
    (rf">>?\s*[\"']?{_SENSITIVE_WRITE_TARGET}", "overwrite system file via redirection"),
    (rf"\btee\b.*[\"']?{_PROJECT_SENSITIVE_WRITE_TARGET}[\"']?{_COMMAND_TAIL}", "overwrite project env/config via tee"),
    (rf">>?\s*[\"']?{_PROJECT_SENSITIVE_WRITE_TARGET}[\"']?{_COMMAND_TAIL}", "overwrite project env/config via redirection"),
    (r"\bxargs\s+.*\brm\b", "xargs with rm"),
    (r"\bfind\b.*-exec\s+(/\S*/)?rm\b", "find -exec rm"),
    (r"\bfind\b.*-delete\b", "find -delete"),
    # Gateway protection
    (r"\bhermes\s+gateway\s+(stop|restart)\b", "stop/restart hermes gateway"),
    (r"\bhermes\s+update\b", "hermes update (restarts gateway)"),
    (r"gateway\s+run\b.*(&\s*$|&\s*;|\bdisown\b|\bsetsid\b)", "start gateway outside systemd"),
    (r"\bnohup\b.*gateway\s+run\b", "start gateway outside systemd"),
    (r"\b(pkill|killall)\b.*\b(hermes|gateway|cli\.py)\b", "kill hermes/gateway process"),
    (r"\bkill\b.*\$\(\s*pgrep\b", "kill process via pgrep expansion"),
    (r"\bkill\b.*`\s*pgrep\b", "kill process via backtick pgrep expansion"),
    (r"\b(cp|mv|install)\b.*\s/etc/", "copy/move file into /etc/"),
    (rf"\b(cp|mv|install)\b.*\s[\"']?{_PROJECT_SENSITIVE_WRITE_TARGET}[\"']?{_COMMAND_TAIL}", "overwrite project env/config file"),
    (r"\bsed\s+-[^\s]*i.*\s/etc/", "in-place edit of system config"),
    (r"\bsed\s+--in-place\b.*\s/etc/", "in-place edit of system config (long flag)"),
    (r"\b(python[23]?|perl|ruby|node)\s+<<", "script execution via heredoc"),
    (r"\bgit\s+reset\s+--hard\b", "git reset --hard (destroys uncommitted changes)"),
    (r"\bgit\s+push\b.*--force\b", "git force push (rewrites remote history)"),
    (r"\bgit\s+push\b.*-f\b", "git force push short flag (rewrites remote history)"),
    (r"\bgit\s+clean\s+-[^\s]*f", "git clean with force (deletes untracked files)"),
    (r"\bgit\s+branch\s+-D\b", "git branch force delete"),
    (r"\bchmod\s+\+x\b.*[;&|]+\s*\./", "chmod +x followed by immediate execution"),
]

DANGEROUS_COMPILED = [
    (re.compile(p, _RE_FLAGS), desc) for p, desc in DANGEROUS_PATTERNS
]


def _normalize(command: str) -> str:
    """Normalize command string before pattern matching."""
    # Strip ANSI escape sequences
    command = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", command)
    command = command.replace("\x00", "")
    command = unicodedata.normalize("NFKC", command)
    return command


def check_hardline(command: str) -> Optional[str]:
    """Check if a command matches the hardline blocklist.

    Returns a description string if blocked, None if safe.
    """
    normalized = _normalize(command).lower()
    for pattern_re, description in HARDLINE_COMPILED:
        if pattern_re.search(normalized):
            return description
    return None


def check_dangerous(command: str) -> Optional[str]:
    """Check if a command requires user confirmation.

    Returns a description string if dangerous, None if safe.
    Note: does NOT check hardline patterns -- caller should check those first.
    """
    normalized = _normalize(command).lower()
    for pattern_re, description in DANGEROUS_COMPILED:
        if pattern_re.search(normalized):
            return description
    return None


def check_command(command: str) -> tuple[str, bool, bool]:
    """Check a command against both hardline and dangerous patterns.

    Returns (description, is_hardline, is_dangerous).
    If is_hardline is True, the command is unconditionally blocked.
    If is_dangerous is True (and is_hardline is False), the command
    requires user confirmation before execution.
    """
    hardline_desc = check_hardline(command)
    if hardline_desc:
        return (hardline_desc, True, False)
    dangerous_desc = check_dangerous(command)
    return (dangerous_desc, False, bool(dangerous_desc))
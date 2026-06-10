"""Non-interactive command preprocessing for shell tool execution.

Agent-Liveness RFC, Layer 1 (A1) — "bounded tool execution".

A shell tool that shells out to an interactive CLI (``vercel``, ``npm``,
``gh``, ``git``) can block FOREVER on a TTY prompt:

  * ``npx vercel`` → "Set up and deploy ...? [Y/n]" (waits on stdin)
  * ``gh auth login`` → interactive auth wizard
  * ``npm init`` → package-name prompt
  * ``git`` operations that pop a credential / passphrase prompt

When such a command runs inside the agent's subprocess tool, there is no
human at the keyboard, so the prompt never gets an answer and the whole
agent turn wedges until the (now Layer-1) hard timeout fires. The timeout
is the backstop; this module is the cheap first line of defence: make the
command non-interactive up front so it FAILS FAST (or proceeds with a
default) instead of hanging.

Two mechanisms, in increasing order of intrusiveness:

1. **stdin redirection** (always, for every command) — the caller wires
   the child's stdin to ``/dev/null`` so any CLI that reads from a TTY
   sees EOF immediately rather than blocking. See ``stdin_should_be_closed``.

2. **flag injection** (conservative, allowlisted CLIs only) — for a small
   set of well-known interactive CLIs we additionally inject the documented
   "assume yes / non-interactive" flag so the command can make forward
   progress non-interactively instead of just erroring on EOF. We only
   touch a command when we are confident about its shape; anything we do
   not positively recognise is returned UNCHANGED.

Design constraints (deliberately conservative):

* We never rewrite a command we don't recognise. Unknown binaries, shell
  pipelines, redirections, command substitution, etc. pass through
  untouched — stdin redirection alone protects them.
* Idempotent: if the non-interactive flag is already present we don't add
  a second copy.
* Token-level: we operate on a shlex token list, never on raw substrings,
  so we can't corrupt quoting.
"""

from __future__ import annotations

import shlex

# CLIs that prompt on a TTY and for which we know a safe, documented
# "non-interactive / assume-yes" flag. Maps the program name (argv[0]
# basename) to the flag we inject when it is absent.
#
# Conservative on purpose:
#   * vercel  --yes   : skip all "Set up and deploy?" confirmations.
#   * npm     --yes   : answer "yes" to npm init / npx install prompts.
#   * git: NO flag is injected. There is no single global non-interactive
#     git flag, and injecting subcommand-specific ones is risky. git is
#     made non-interactive purely via stdin=/dev/null + the env hardening
#     (GIT_TERMINAL_PROMPT=0) applied by the caller. It still appears in
#     INTERACTIVE_CLIS so the caller knows to apply that env hardening.
#   * gh: NO flag injected for the same reason (subcommand-specific); the
#     gh prompts are suppressed by GH_PROMPT_DISABLED + stdin=/dev/null.
_FLAG_INJECTION = {
    "vercel": "--yes",
    "npm": "--yes",
}

# Full set of CLIs we consider interactive. The caller uses this to decide
# whether to apply the extra non-interactive ENV hardening
# (GIT_TERMINAL_PROMPT=0, GH_PROMPT_DISABLED=1, etc.). A command is
# "interactive" if argv[0]'s basename is in here, OR it is an ``npx``
# invocation of one of these (e.g. ``npx vercel``).
INTERACTIVE_CLIS = frozenset({"vercel", "npm", "gh", "git"})

# Non-interactive environment hardening applied (by the caller) whenever an
# interactive CLI is detected. These make the CLIs fail-fast on EOF rather
# than block, even where we don't inject a flag.
NONINTERACTIVE_ENV = {
    # git: never prompt for credentials / passphrases on a terminal.
    "GIT_TERMINAL_PROMPT": "0",
    # gh: disable interactive prompts.
    "GH_PROMPT_DISABLED": "1",
    # npm: never run in interactive mode.
    "npm_config_yes": "true",
    # generic CI hint many tools honour to suppress prompts/spinners.
    "CI": "1",
    # Vercel: explicit non-interactive hint (belt-and-suspenders with --yes).
    "VERCEL_CLI_FORCE_NON_INTERACTIVE": "1",
}


def _basename(tok: str) -> str:
    """Last path component of a token, e.g. /usr/local/bin/vercel -> vercel."""
    return tok.rsplit("/", 1)[-1]


def _resolve_cli_name(tokens: list[str]) -> tuple[str | None, int]:
    """Identify the effective CLI being invoked and the index of its argv0.

    Handles the common ``npx <cli> ...`` wrapper: ``npx vercel deploy``
    is, for our purposes, a ``vercel`` invocation. We look past a leading
    ``npx`` (and its ``-y``/``--yes``/``--no-install`` style flags) to the
    first non-flag token.

    Returns ``(cli_name, argv_index)`` where ``argv_index`` is the position
    of the token whose flags we should augment, or ``(None, -1)`` if we
    don't recognise an interactive CLI.
    """
    if not tokens:
        return None, -1

    first = _basename(tokens[0])

    if first == "npx":
        # Skip npx and its own option flags to find the wrapped program.
        i = 1
        while i < len(tokens) and tokens[i].startswith("-"):
            i += 1
        if i < len(tokens):
            wrapped = _basename(tokens[i])
            if wrapped in INTERACTIVE_CLIS:
                return wrapped, i
        return None, -1

    if first in INTERACTIVE_CLIS:
        return first, 0

    return None, -1


def _is_simple_command(command: str) -> bool:
    """True if ``command`` is a single simple command (no shell operators).

    We refuse to flag-inject into anything containing shell control
    operators (pipes, ``&&``, ``;``, redirection, command substitution,
    subshells), because token-splitting such a string and re-joining it
    would change its meaning. Those commands still get stdin redirection
    and env hardening from the caller — we just don't rewrite their args.
    """
    # Cheap substring scan for shell metacharacters. Conservative: a false
    # positive just means "don't inject a flag", which is always safe.
    for meta in ("|", "&", ";", ">", "<", "$(", "`", "\n", "(", ")"):
        if meta in command:
            return False
    return True


def make_noninteractive(command: str) -> tuple[str, bool]:
    """Return a non-interactive form of a shell ``command`` string.

    Best-effort, conservative flag injection for known interactive CLIs.

    Returns ``(possibly_rewritten_command, is_interactive_cli)``:

      * ``possibly_rewritten_command`` — the command with the documented
        non-interactive flag injected when (a) it's a simple command and
        (b) we recognise the CLI and (c) the flag isn't already present.
        Otherwise the original string is returned unchanged.
      * ``is_interactive_cli`` — True when the command invokes one of the
        known interactive CLIs (vercel/npm/gh/git, incl. ``npx <cli>``).
        The caller uses this to decide whether to apply the extra
        NONINTERACTIVE_ENV hardening. Note this is computed even when we
        DON'T rewrite the command (e.g. a piped ``git`` invocation).
    """
    # Detect interactivity even for non-simple commands so the caller can
    # still apply env hardening to a piped/compound interactive CLI.
    try:
        probe_tokens = shlex.split(command)
    except ValueError:
        # Unbalanced quotes etc. — leave entirely untouched, not interactive
        # enough to risk a rewrite.
        return command, False

    cli_name, _ = _resolve_cli_name(probe_tokens)
    is_interactive = cli_name is not None

    if not is_interactive:
        return command, False

    # Only rewrite simple commands — compound/piped ones keep their exact
    # text (env hardening + stdin=/dev/null still protect them).
    if not _is_simple_command(command):
        return command, True

    flag = _FLAG_INJECTION.get(cli_name)
    if flag is None:
        # Interactive CLI we harden via env/stdin but don't flag-inject
        # (git, gh). Return unchanged.
        return command, True

    tokens = probe_tokens
    _, argv_index = _resolve_cli_name(tokens)

    # Idempotent: don't add a duplicate flag (accept either ``--yes`` or
    # its short ``-y`` alias as "already non-interactive").
    short_alias = "-y"
    if flag in tokens or short_alias in tokens:
        return command, True

    # Insert the flag immediately after the CLI's argv0 token so it binds
    # to the CLI itself, e.g. ``vercel --yes deploy``.
    tokens.insert(argv_index + 1, flag)
    return shlex.join(tokens), True


def stdin_should_be_closed() -> bool:
    """Whether the shell tool should wire child stdin to /dev/null.

    Always True under A1 — there is never a human at the keyboard for an
    agent's subprocess tool, so closing stdin is uniformly correct and is
    the cheapest universal anti-hang guard. Factored as a function so the
    policy has a single named home and can be made conditional later
    without touching call sites.
    """
    return True

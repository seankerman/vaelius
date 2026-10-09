"""Recognize standalone task transport, never keyword matches in work output."""
from pathlib import Path
import shlex


def is_task_control(command):
    if isinstance(command, str):
        # Preserve ambiguous shell programs, including substitutions and pipelines.
        if any(x in command for x in ('\n', '`', '$(', '<(', '>(')):
            return False
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            lexer.commenters = ''
            argv = list(lexer)
        except ValueError:
            return False
        if any(t and set(t) <= set(';&|<>()') for t in argv):
            return False
    elif isinstance(command, list) and all(isinstance(v, str) for v in command):
        argv = command
    else:
        return False
    if (len(argv) == 3 and Path(argv[0]).name in ('sh', 'bash', 'zsh')
            and argv[1] in ('-c', '-lc')):
        return is_task_control(argv[2])
    if len(argv) < 2 or Path(argv[0]).name != 'codex' or argv[1] != 'queue':
        return False
    # Only the verified queue form. Unknown options/commands remain ordinary work.
    options = argv[2:]
    if len(options) != 4:
        return False
    return ({options[0], options[2]} == {'--thread', '--message'}
            and bool(options[1]) and bool(options[3]))

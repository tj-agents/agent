"""Read native JSONL transcripts shared by every host (Python 3.9+)."""

import json


def records(path):
    with path.open(encoding='utf-8') as stream:
        for number, line in enumerate(stream, 1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue  # A running session can have an incomplete trailing record.
            if isinstance(value, dict):
                yield number, value


def text_content(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ' '.join(v.get('text', '') for v in value if isinstance(v, dict)
                        and v.get('type') in ('text', 'input_text', 'output_text'))
    return ''


def iter_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_strings(item)

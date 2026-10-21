"""Translate ACP image blocks without dropping attachments from saved submissions."""

import base64
import binascii

MAX_IMAGE_BYTES = 10 * 1024 * 1024
IMAGE_TYPES = ('image/png', 'image/jpeg', 'image/webp', 'image/gif')


def prompt_input(prompt, modalities):
    if not isinstance(prompt, list) or not all(isinstance(c, dict) for c in prompt):
        raise ValueError('Invalid message content.')
    text = []
    native = []
    image_bytes = 0
    for content in prompt:
        kind = content.get('type')
        if kind == 'text' and isinstance(content.get('text'), str):
            text.append(content['text'])
            native.append({'type': 'text', 'text': content['text']})
        elif kind == 'image':
            if 'image' not in modalities:
                raise ValueError('Image input is not enabled for this chat’s model.')
            data, mime = content.get('data'), content.get('mimeType')
            if not isinstance(data, str) or mime not in IMAGE_TYPES:
                raise ValueError('Attach a PNG, JPEG, WebP, or GIF image with base64 data.')
            if len(data) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
                raise ValueError('Image attachments must total at most 10 MiB.')
            try:
                decoded = base64.b64decode(data, validate=True)
            except (ValueError, binascii.Error) as error:
                raise ValueError('Invalid base64 image data.') from error
            image_bytes += len(decoded)
            if not decoded or image_bytes > MAX_IMAGE_BYTES:
                raise ValueError('Image attachments must be nonempty and total at most 10 MiB.')
            native.append({'type': 'image', 'url': 'data:' + mime + ';base64,' + base64.b64encode(decoded).decode(), 'detail': 'auto'})
        else:
            raise ValueError('This adapter accepts text and supported image attachments.')
    summary = '\n'.join(text)
    if not summary.strip() and not image_bytes:
        raise ValueError('Enter a message or attach an image.')
    # Text-only retries keep their original digest and dispatch format.
    return summary if summary.strip() else '[Image]', native if image_bytes else None


def user_content_blocks(content):
    """Convert native history back to the ACP blocks accepted on submission."""
    for item in content:
        if item.get('type') == 'text':
            yield {'type': 'text', 'text': item['text']}
        elif item.get('type') == 'image':
            url = item.get('url', '')
            header, comma, data = url.partition(',')
            if comma and header.startswith('data:image/') and header.endswith(';base64'):
                yield {'type': 'image', 'mimeType': header[5:-7], 'data': data}
            else:
                yield {'type': 'text', 'text': '[Image]'}
        else:
            yield {'type': 'text', 'text': '[Attachment]'}

import os
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('DATABASE_URL', 'sqlite://')
from app.services import telegram_webhook as webhook

@pytest.mark.parametrize('chat_type', ['group', 'supergroup', 'channel', None])
@pytest.mark.parametrize('album', [False, True])
def test_group_photos_are_silent(monkeypatch, chat_type, album):
    def unexpected(*args, **kwargs):
        pytest.fail('Group photo must not send messages or access the database')
    monkeypatch.setattr(webhook, 'send_message', unexpected)
    monkeypatch.setattr(webhook, 'session_scope', unexpected)
    msg={'chat': {'id': -100123, 'type': chat_type}, 'from': {'id': 42},
         'photo': [{'file_id': 'photo'}]}
    if album: msg['media_group_id']='album'
    webhook.handle_update({'message': msg})

def test_private_photo_retains_guidance(monkeypatch):
    sent=[]
    monkeypatch.setattr(webhook, 'send_message', lambda *args: sent.append(args))
    webhook.handle_update({'message': {'chat': {'id': 42, 'type': 'private'},
                                      'photo': [{'file_id': 'photo'}]}})
    assert len(sent)==1
    assert sent[0][0]==42
    assert 'Mini App' in sent[0][1]

def test_channel_photo_post_is_silent(monkeypatch):
    monkeypatch.setattr(webhook, 'send_message', lambda *args: pytest.fail('Unexpected reply'))
    webhook.handle_update({'channel_post': {'chat': {'id': -100123, 'type': 'channel'},
                                          'photo': [{'file_id': 'photo'}]}})

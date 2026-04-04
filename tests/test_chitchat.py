import pytest
from xibi.routing.chitchat import is_chitchat

def test_simple_thanks_is_chitchat():
    assert is_chitchat("thanks") is True

def test_ok_is_chitchat():
    assert is_chitchat("ok") is True

def test_sounds_good_is_chitchat():
    assert is_chitchat("sounds good") is True

def test_email_request_not_chitchat():
    assert is_chitchat("send an email to Jake") is False

def test_question_not_chitchat():
    assert is_chitchat("what time is my meeting?") is False

def test_long_message_not_chitchat():
    # 12-word sentence with no tool keywords, has chitchat token
    # "I just wanted to say thank you for the wonderful dinner tonight" -> 12 words
    assert is_chitchat("I just wanted to say thank you for the wonderful dinner tonight") is False

def test_tool_keyword_not_chitchat():
    assert is_chitchat("ok great can you remind me tomorrow") is False

def test_chitchat_with_punctuation():
    assert is_chitchat("thanks!") is True

def test_empty_string_not_chitchat():
    assert is_chitchat("") is False

def test_chitchat_case_insensitive():
    assert is_chitchat("OK") is True
    assert is_chitchat("THANKS") is True

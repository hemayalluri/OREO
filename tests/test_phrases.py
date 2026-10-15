"""Wake ("Hey Oreo"), goodbye and noise phrases, as Whisper tends to write them."""

from __future__ import annotations

import pytest

from backend.phrases import classify


@pytest.mark.parametrize("text", ["Hey Oreo.", "Hey, Oreo!", "hey oreo", "Hi Oreo", "Hey Orio.", "Okay Oreo"])
def test_wake_phrase_wakes_oreo(text):
    heard = classify(text, "wake")
    assert heard.intent == "wake" and heard.question == ""


def test_wake_phrase_with_a_question_answers_it_straight_away():
    heard = classify("Hey Oreo, what were our top 3 products last quarter?", "wake")
    assert heard.intent == "wake" and heard.question == "what were our top 3 products last quarter?"


@pytest.mark.parametrize("text", ["", "Thank you.", "I love Oreos with milk", "What's the weather?", "Hey there"])
def test_other_speech_does_not_wake_oreo(text):
    assert classify(text, "wake").intent == "ignore"


@pytest.mark.parametrize("text", ["Okay, bye.", "OK bye", "Thank you, bye!", "Thanks, bye.", "Bye Oreo.",
                                  "Goodbye.", "Bye-bye.", "Thank you so much, bye!", "Go to sleep.",
                                  "Stop listening.", "That's all.", "Okay, good bye Oreo.", "Nothing, bye!",
                                  "Alright, thank you so much. See you, bye!", "Cool, thanks. Have a nice day, bye!"])
def test_goodbyes_put_oreo_to_sleep(text):
    assert classify(text).intent == "sleep"


@pytest.mark.parametrize("text", ["Bye.", "Say bye to the sales team", "Which product should we say goodbye to?"])
def test_bye_inside_a_question_or_alone_does_not(text):
    assert classify(text).intent != "sleep"


@pytest.mark.parametrize("text", ["", "Thank you.", "you", "Um.", "Okay."])
def test_noise_is_ignored(text):
    assert classify(text).intent == "noise"


def test_questions_go_to_the_brain():
    heard = classify("What were our sales in Europe?")
    assert heard.intent == "question" and heard.question == "What were our sales in Europe?"

import json
import traceback

import httpx2
import pytest

from shafa.bot.telegram import Telegram, TelegramError, Upload

TOKEN = "123456:ТЕСТ-секрет-токен-AAAbbbCCC"


def telegram(handler):
    return Telegram(TOKEN, httpx2.Client(transport=httpx2.MockTransport(handler)))


def answer(result=None, status=200, **body):
    def handler(request):
        if body or status != 200:
            return httpx2.Response(status, json=body or {"ok": False})
        return httpx2.Response(200, json={"ok": True, "result": result})

    return handler


def raising(error_cls):
    def handler(request):
        raise error_cls("помилка з адресою " + str(request.url), request=request)

    return handler


def no_token(error: BaseException) -> None:
    text = "".join(traceback.format_exception(error)) + repr(error) + str(error)
    assert TOKEN not in text
    assert "api.telegram.org" not in text


def test_call_returns_result_and_sends_json():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx2.Response(200, json={"ok": True, "result": {"id": 7}})

    assert telegram(handler).get_me() == {"id": 7}
    assert seen[0].url.path == f"/bot{TOKEN}/getMe"


def test_upload_is_multipart_with_json_markup():
    seen = []

    def handler(request):
        seen.append(request.read())
        return httpx2.Response(200, json={"ok": True, "result": {"message_id": 5}})

    markup = {"inline_keyboard": [[{"text": "Так", "callback_data": "pub:1"}]]}
    result = telegram(handler).send_photo(-100, Upload("1.jpg", b"\xff\xd8jpeg"), "Текст", markup)

    assert result == {"message_id": 5}
    body = seen[0].decode("utf-8", errors="replace")
    assert 'name="photo"; filename="1.jpg"' in body
    assert json.dumps(markup, ensure_ascii=False) in body
    assert "Текст" in body


@pytest.mark.parametrize(
    ("handler", "uncertain"),
    [
        (raising(httpx2.ConnectError), False),
        (raising(httpx2.ConnectTimeout), False),
        (raising(httpx2.ReadTimeout), True),
        (raising(httpx2.ReadError), True),
        (raising(httpx2.RemoteProtocolError), True),
        (raising(httpx2.WriteTimeout), True),
        (answer(status=500), True),
        (answer(status=502, ok=False, error_code=502, description="Bad Gateway"), True),
        (answer(status=403, ok=False, error_code=403, description="bot is not a member"), False),
        (lambda r: httpx2.Response(200, text="<html>"), False),
        (lambda r: httpx2.Response(504, text="<html>"), True),
    ],
)
def test_every_error_is_ours_without_token(handler, uncertain):
    with pytest.raises(TelegramError) as caught:
        telegram(handler).send_message(1, "x")
    assert caught.value.uncertain is uncertain
    no_token(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_description_echoing_token_is_masked():
    handler = answer(status=400, ok=False, error_code=400, description=f"bad token {TOKEN}")
    with pytest.raises(TelegramError) as caught:
        telegram(handler).get_me()
    no_token(caught.value)
    assert "***" in str(caught.value)


def test_flood_control_gives_retry_after():
    handler = answer(
        status=429, ok=False, error_code=429, description="Too Many Requests",
        parameters={"retry_after": 17},
    )  # fmt: skip
    with pytest.raises(TelegramError) as caught:
        telegram(handler).send_message(1, "x")
    assert (caught.value.code, caught.value.retry_after, caught.value.uncertain) == (429, 17, False)


def test_download_and_its_errors():
    def handler(request):
        assert request.url.path == f"/file/bot{TOKEN}/photos/1.jpg"
        return httpx2.Response(200, content=b"\xff\xd8abc")

    assert telegram(handler).download("photos/1.jpg", 100) == b"\xff\xd8abc"
    for bad, text in (
        (lambda r: httpx2.Response(404), "Файл не завантажився (404)"),
        (lambda r: httpx2.Response(200, content=b"x" * 101), "Файл завеликий"),
        (raising(httpx2.ReadTimeout), "Telegram не відповів вчасно (file)"),
    ):
        with pytest.raises(TelegramError) as caught:
            telegram(bad).download("photos/1.jpg", 100)
        assert str(caught.value) == text
        no_token(caught.value)


def test_get_updates_waits_longer_than_poll():
    seen = []

    def handler(request):
        seen.append((json.loads(request.read()), request.extensions["timeout"]["read"]))
        return httpx2.Response(200, json={"ok": True, "result": []})

    assert telegram(handler).get_updates(41, 25) == []
    params, read_timeout = seen[0]
    assert params == {"timeout": 25, "allowed_updates": ["message", "callback_query", "edited_channel_post"],
                      "offset": 41}  # fmt: skip
    assert read_timeout > 25


def test_media_group_is_album_reply_with_attachments():
    seen = []

    def handler(request):
        seen.append((request.url.path, request.read()))
        return httpx2.Response(200, json={"ok": True, "result": [{"message_id": 9}]})

    photos = [Upload("1.jpg", b"\xff\xd8one"), Upload("2.jpg", b"\xff\xd8two")]
    assert telegram(handler).send_media_group(-100777, photos, reply_to=55) == [{"message_id": 9}]

    path, body = seen[0]
    assert path.endswith("/sendMediaGroup")
    text = body.decode("utf-8", errors="replace")
    media = [{"type": "photo", "media": "attach://p0"}, {"type": "photo", "media": "attach://p1"}]
    assert json.dumps(media) in text
    assert json.dumps({"message_id": 55}) in text
    assert 'name="p0"; filename="1.jpg"' in text and 'name="p1"; filename="2.jpg"' in text

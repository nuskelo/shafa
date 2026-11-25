"""KLB-015 (Б): вкладки Клубку показують запасне фото з Експорту, коли фото Картки на диску немає.

Еталон Шафи `borh-<N>`: рядки `photos` у базі є, файли в `<дані>/borh/<N>` стерто (PHOTO-001), а
`<дані>/klubok-old/<N>/1.jpg` лишився — його бере робот (`fields.ad`). Шафа ходить без запасного.
"""

import pytest

from shafa import folders
from shafa.db import Status
from shafa.klubok import queue
from test_klb002_queue import ad_row, add
from test_klb008 import photo
from test_klb009 import app, klubok_on, lock_file, spawner  # noqa: F401
from test_web import get, html


def etalon(conn, root, post_id=7301, *, spare=True):
    """Опублікований `borh-<N>` без файлів фото; запасне фото — за потреби."""
    item_id = add(
        conn, root, post_id, folder=f"borh-{post_id}", status=Status.PUBLISHED, photos=False
    )
    ad_row(conn, item_id, queue.PUBLISHED, url="https://klubok.com/item-x-5", klubok_id=5,
           published_at="2026-11-09T10:00:00+00:00")  # fmt: skip
    if spare:
        photo(folders.klubok_spare(post_id) / folders.SPARE_PHOTO)
    return item_id


def test_spare_thumbnail_on_klubok_but_not_on_shafa(conn, root, app):  # noqa: F811
    item_id = etalon(conn, root)
    on_klubok = get(app, f"/klubok/thumb/{item_id}/1")
    assert on_klubok.status == 200 and on_klubok.content_type == "image/jpeg"
    assert get(app, f"/klubok/photo/{item_id}/1").status == 200
    assert get(app, f"/thumb/{item_id}/1").status == 404  # Шафа — без змін
    assert get(app, f"/photo/{item_id}/1").status == 404


def test_without_spare_it_is_a_stub(conn, root, app):  # noqa: F811
    item_id = etalon(conn, root, spare=False)
    assert get(app, f"/klubok/thumb/{item_id}/1").status == 404
    page = html(get(app, "/klubok/published"))
    assert "немає фото" in page and f"/klubok/thumb/{item_id}/" not in page
    card = html(get(app, f"/klubok/item/{item_id}"))
    assert "фото з Експорту" not in card


def test_lists_and_card_use_spare(conn, root, app):  # noqa: F811
    item_id = etalon(conn, root)
    assert f'src="/klubok/thumb/{item_id}/1"' in html(get(app, "/klubok/published"))
    card = html(get(app, f"/klubok/item/{item_id}"))
    assert "фото з Експорту" in card and f'src="/klubok/photo/{item_id}/1"' in card
    assert "Фото з Експорту" in card  # alt
    shafa = html(get(app, f"/item/{item_id}"))
    assert "фото з Експорту" not in shafa and "/klubok/photo/" not in shafa


def test_problems_tab_shows_spare(conn, root, app):  # noqa: F811
    item_id = add(conn, root, 7302, folder="borh-7302", status=Status.PUBLISHED, photos=False)
    ad_row(conn, item_id, queue.PROBLEM, problem_code="klubok_field_missing", problem_text="x")
    photo(folders.klubok_spare(7302) / folders.SPARE_PHOTO)
    assert f'src="/klubok/thumb/{item_id}/1"' in html(get(app, "/klubok/decide"))


def test_disk_photo_wins_over_spare(conn, root, app):  # noqa: F811
    item_id = add(conn, root, 7303, status=Status.APPROVED)  # фото в Головній
    photo(folders.klubok_spare(7303) / folders.SPARE_PHOTO)
    wanted = get(app, f"/thumb/{item_id}/1").body
    assert get(app, f"/klubok/thumb/{item_id}/1").body == wanted


@pytest.mark.parametrize("path", ["/klubok/thumb/999/1", "/klubok/photo/999/1"])
def test_unknown_item_is_404(app, path):  # noqa: F811
    assert get(app, path).status == 404

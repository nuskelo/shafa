from shafa.export import Post, plain_text, read_export


def test_plain_text_string():
    assert plain_text("Сукня") == "Сукня"


def test_plain_text_list_joined_without_formatting():
    text = ["Светр ", {"type": "bold", "text": "S"}, " ", {"type": "link", "text": "https://x"}]
    assert plain_text(text) == "Светр S https://x"


def test_read_export(export_path):
    posts = read_export(export_path)
    assert sorted(posts) == [10, 11, 12, 13, 20]  # службові 1 і 14 пропущено
    assert posts[10] == Post(10, "2025-01-11T10:00:00", "Сукня літня, розмір M. Ціна 500 грн")
    # Хештеги й посилання лишаються як текст — їх чистить CORE-002.
    assert posts[11].text == "Светр вовняний S\n#светр https://example.invalid/svetr"
    assert posts[13].text == "#куртка Куртка демісезонна"
    assert posts[12].text == ""

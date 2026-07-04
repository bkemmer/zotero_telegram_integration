import bot

PAPER = {
    "itemType": "journalArticle",
    "title": "Attention Is All You Need",
    "date": "2017-06-12",
    "creators": [{"lastName": "Vaswani", "firstName": "Ashish"}],
    "DOI": "10.5555/3295222.3295349",
    "attachments": [{"mimeType": "application/pdf", "url": "http://x/p.pdf"}],
}
NEWS = {"itemType": "webpage", "title": "Some News", "date": "2024"}


def test_is_paper():
    assert bot.is_paper([PAPER]) is True
    assert bot.is_paper([NEWS]) is False
    assert bot.is_paper([]) is False


def test_slugify():
    assert bot.slugify(PAPER) == "Vaswani_2017_Attention_Is_All_You_Need"
    assert bot.slugify(NEWS) == "Unknown_2024_Some_News"


def test_first_url():
    assert (
        bot.first_url("see https://arxiv.org/abs/1706.03762).")
        == "https://arxiv.org/abs/1706.03762"
    )
    assert bot.first_url("no link here") is None


def test_pdf_url_from_item():
    assert bot.pdf_url_from_item(PAPER) == "http://x/p.pdf"
    assert bot.pdf_url_from_item(NEWS) is None


def test_doi_of():
    assert bot.doi_of(PAPER) == "10.5555/3295222.3295349"


def test_arxiv_pdf():
    assert (
        bot.arxiv_pdf("https://arxiv.org/abs/2605.30621")
        == "https://arxiv.org/pdf/2605.30621"
    )
    assert bot.arxiv_pdf("https://example.com/x") is None


def test_build_payload():
    p = bot.build_payload(PAPER)
    assert {"tag": "paperbot"} in p["tags"]
    assert "attachments" not in p and "notes" not in p
    assert "collections" not in p
    assert bot.build_payload(PAPER, "ABCD")["collections"] == ["ABCD"]
    tagged = dict(PAPER, tags=[{"tag": "nlp"}, {"tag": "paperbot"}])
    assert bot.build_payload(tagged)["tags"] == [{"tag": "nlp"}, {"tag": "paperbot"}]


def test_pick_collection():
    cols = [{"key": "K1", "name": "PAPERBOT", "parent": None}]
    assert bot.pick_collection(PAPER, cols, None) == "PAPERBOT"
    assert bot.pick_collection(PAPER, cols, "ML Papers") == "ML Papers"


def test_ensure_collection_existing_no_network():
    cols = [{"key": "K1", "name": "PAPERBOT", "parent": None}]
    assert bot.zotero_ensure_collection("PAPERBOT", cols, "k", "u") == "K1"


def test_doi_matches():
    assert bot._doi_matches({"DOI": "10.5555/X"}, "10.5555/x")  # case-insensitive
    assert bot._doi_matches({"extra": "DOI: 10.1/abc"}, "10.1/abc")  # stashed in extra
    assert not bot._doi_matches({"DOI": "10.9/z"}, "10.1/x")
    assert not bot._doi_matches({}, None)


def test_item_matches():
    assert bot._item_matches(
        {"title": "Attention Is All You Need"}, None, "attention is all you need"
    )
    assert bot._item_matches({"DOI": "10.5555/X"}, "10.5555/x", "different title")
    assert not bot._item_matches({"title": "Some Paper"}, None, "Other Paper")
    assert not bot._item_matches({}, None, "")


def test_find_existing_no_id_no_request():
    assert bot.zotero_find_existing({}, "COLL", "k", "u") is None


def test_ensure_collection_creates_via_api(mocker):
    """pytest-mock: creation path POSTs and returns the new key."""
    post = mocker.patch("bot.requests.post")
    post.return_value.json.return_value = {"successful": {"0": {"key": "NEW"}}}
    key = bot.zotero_ensure_collection("Fresh", [], "k", "u")
    assert key == "NEW"
    post.assert_called_once()

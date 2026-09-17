import json

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


def test_normalize_url():
    assert (
        bot.normalize_url("https://arxiv.org/pdf/2607.19297")
        == "https://arxiv.org/abs/2607.19297"
    )
    assert (
        bot.normalize_url("https://arxiv.org/pdf/2607.19297.pdf")
        == "https://arxiv.org/abs/2607.19297"
    )
    # non-arxiv urls pass through unchanged
    assert (
        bot.normalize_url("https://arxiv.org/abs/1706.03762")
        == "https://arxiv.org/abs/1706.03762"
    )
    assert (
        bot.normalize_url("https://doi.org/10.1234/abc")
        == "https://doi.org/10.1234/abc"
    )


def test_resolve_url():
    # bare arXiv id (with and without version)
    assert bot.resolve_url("2608.11888v1") == "https://arxiv.org/abs/2608.11888v1"
    assert (
        bot.resolve_url("see arXiv:2608.11888 please")
        == "https://arxiv.org/abs/2608.11888"
    )
    # full url wins over a bare id, /pdf/ normalized to /abs/
    assert (
        bot.resolve_url("https://arxiv.org/pdf/2607.19297 2608.11888")
        == "https://arxiv.org/abs/2607.19297"
    )
    # non-arxiv urls pass through
    assert (
        bot.resolve_url("https://doi.org/10.1234/abc") == "https://doi.org/10.1234/abc"
    )
    assert bot.resolve_url("no link here") is None


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


def test_check_public_url_blocks_ssrf():
    import pytest

    bot._check_public_url("https://arxiv.org/pdf/1234")  # public host: ok
    for bad in [
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://localhost:1969/",  # loopback
        "http://127.0.0.1/",
        "http://192.168.1.1/",  # private LAN
        "file:///etc/passwd",  # non-http scheme
        "ftp://example.com/x",
    ]:
        with pytest.raises(ValueError):
            bot._check_public_url(bad)


def test_ensure_collection_creates_via_api(mocker):
    """pytest-mock: creation path POSTs and returns the new key."""
    post = mocker.patch("bot.requests.post")
    post.return_value.json.return_value = {"successful": {"0": {"key": "NEW"}}}
    key = bot.zotero_ensure_collection("Fresh", [], "k", "u")
    assert key == "NEW"
    post.assert_called_once()


def test_ensure_collection_with_parent(mocker):
    cols = [
        {"key": "P1", "name": "PAPERBOT", "parent": None},
        {"key": "C1", "name": "Sub", "parent": "P1"},
        {"key": "C2", "name": "Other", "parent": "X9"},
    ]
    post = mocker.patch("bot.requests.post")
    assert bot.zotero_ensure_collection("Sub", cols, "k", "u", parent="P1") == "C1"
    post.assert_not_called()
    # same name under a different parent is a different collection: create it
    post.return_value.json.return_value = {"successful": {"0": {"key": "NEW"}}}
    assert bot.zotero_ensure_collection("Other", cols, "k", "u", parent="P1") == "NEW"
    body = json.loads(post.call_args.kwargs["data"])
    assert body == [{"name": "Other", "parentCollection": "P1"}]


def test_new_subcollection(mocker):
    env = {"ZOTERO_API_KEY": "k", "ZOTERO_USER_ID": "u"}
    mocker.patch(
        "bot.zotero_collections",
        return_value=[
            {"key": "P1", "name": "PAPERBOT", "parent": None},
            {"key": "C1", "name": "Sub", "parent": "P1"},
        ],
    )
    post = mocker.patch("bot.requests.post")
    post.return_value.json.return_value = {"successful": {"0": {"key": "NEW"}}}
    assert bot.new_subcollection("   ", env) == "Usage: /subcollection <name>"
    assert bot.new_subcollection(" Sub ", env).startswith("↺ PAPERBOT › Sub already")
    post.assert_not_called()
    assert bot.new_subcollection("Fresh", env) == "✓ Created PAPERBOT › Fresh (NEW)"
    post.assert_called_once()


def test_subcollection_name():
    asked = {"text": bot.SUBCOLLECTION_PROMPT}
    assert bot.subcollection_name({"text": "/subcollection Deep RL"}) == "Deep RL"
    assert bot.subcollection_name({"text": "/subcollection@paper_bot NLP"}) == "NLP"
    assert bot.subcollection_name({"text": "/subcollection"}) == ""  # ask for it
    assert bot.subcollection_name({"text": " Vision ", "reply_to_message": asked}) == (
        "Vision"
    )
    # not a request: a normal link, or a reply to some other bot message
    assert bot.subcollection_name({"text": "https://arxiv.org/abs/1"}) is None
    other = {"text": "Where should x.pdf go?"}
    assert bot.subcollection_name({"text": "NLP", "reply_to_message": other}) is None


def test_command():
    assert bot.command("/listSubcollections") == ("/listsubcollections", "")
    assert bot.command("/Subcollection@paper_bot  Deep RL ") == (
        "/subcollection",
        "Deep RL",
    )


def test_subcollection_name_command_in_reply():
    # a command typed into the name prompt is a command, not a collection name
    asked = {"text": bot.SUBCOLLECTION_PROMPT}
    msg = {"text": "/listSubcollections", "reply_to_message": asked}
    assert bot.subcollection_name(msg) is None


def test_list_subcollections(mocker):
    env = {"ZOTERO_API_KEY": "k", "ZOTERO_USER_ID": "u"}
    cols = mocker.patch("bot.zotero_collections")
    cols.return_value = [
        {"key": "P1", "name": "PAPERBOT", "parent": None},
        {"key": "C2", "name": "vision", "parent": "P1"},
        {"key": "C1", "name": "NLP", "parent": "P1"},
        {"key": "C3", "name": "Elsewhere", "parent": "X9"},
    ]
    assert (
        bot.list_subcollections(env) == "PAPERBOT subcollections (2):\n• NLP\n• vision"
    )
    cols.return_value = cols.return_value[:1]
    assert bot.list_subcollections(env).startswith("No PAPERBOT subcollections yet")


def test_file_keyboard():
    cols = [
        {"key": "P1", "name": "PAPERBOT", "parent": None},
        {"key": "C2", "name": "vision", "parent": "P1"},
        {"key": "C1", "name": "NLP", "parent": "P1"},
        {"key": "C3", "name": "Elsewhere", "parent": "X9"},
    ]
    kb = bot.file_keyboard("ITEMKEY1", cols)
    buttons = [r[0] for r in kb["inline_keyboard"]]
    assert [b["text"] for b in buttons] == ["NLP", "vision"]  # only PAPERBOT's, sorted
    assert buttons[0]["callback_data"] == "z:ITEMKEY1:C1"
    assert all(len(b["callback_data"].encode()) <= 64 for b in buttons)
    assert bot.file_keyboard(None, cols) is None  # zotero add failed: nothing to file
    assert bot.file_keyboard("ITEMKEY1", cols[:1]) is None  # no subcollections yet


def test_zotero_file_item(mocker):
    get = mocker.patch("bot.requests.get")
    get.return_value.json.return_value = {"data": {"version": 7, "collections": ["A"]}}
    patch = mocker.patch("bot.requests.patch")
    assert bot.zotero_file_item("I1", "B", "k", "u") is True
    kw = patch.call_args.kwargs
    assert json.loads(kw["data"]) == {"collections": ["A", "B"]}  # keeps existing
    assert kw["headers"]["If-Unmodified-Since-Version"] == "7"
    assert bot.zotero_file_item("I1", "A", "k", "u") is False  # already there
    patch.assert_called_once()


def test_safe_name():
    assert bot.safe_name("Attention Is All You Need.pdf") == "Attention Is All You Need"
    assert bot.safe_name("../../etc/passwd.pdf") == "passwd"  # no traversal
    assert bot.safe_name("a/b/c.PDF") == "c"
    assert "/" not in bot.safe_name("x/../y.pdf")
    assert bot.safe_name("") == "document"
    assert bot.safe_name("...") == "document"
    assert bot.safe_name("x" * 300) == "x" * 120


def test_folder_keyboard():
    kb = bot.folder_keyboard("deadbeef", ["ToRead", "Books"])
    rows = kb["inline_keyboard"]
    assert [r[0]["text"] for r in rows[:2]] == ["ToRead", "Books"]
    assert rows[0][0]["callback_data"] == "deadbeef:0"
    assert rows[-2][0]["callback_data"] == "deadbeef:r"  # root
    assert rows[-1][0]["callback_data"] == "deadbeef:x"  # cancel
    # telegram rejects callback_data over 64 bytes
    assert all(len(b["callback_data"].encode()) <= 64 for r in rows for b in r)


def test_translate_non_item_responses(mocker):
    """The two ways translation-server answers without an item list."""
    import pytest

    post = mocker.patch("bot.requests.post")
    # 300 = listing/search page: a dict of candidates, and 3xx slips past
    # raise_for_status(), so it must raise rather than look like "not a paper"
    post.return_value.status_code = 300
    with pytest.raises(RuntimeError):
        bot.translate("https://arxiv.org/list/cs.AI/2603")
    # 200 with a non-list body is still nothing to index
    post.return_value.status_code = 200
    post.return_value.json.return_value = {"unexpected": "shape"}
    assert bot.translate("https://example.com/x") == []


def test_to_drive_subdir(mocker):
    run = mocker.patch("bot.subprocess.run")
    assert (
        bot.to_drive("/tmp/x.pdf", "Paper", "ToRead")
        == "gdrive:Papers/ToRead/Paper.pdf"
    )
    assert bot.to_drive("/tmp/x.pdf", "Paper") == "gdrive:Papers/Paper.pdf"
    assert run.call_count == 2

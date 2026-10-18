from whats_cooking.refs import canonical_url, refs_from_text, url_ref


def test_canonical_url_strips_tracking_and_www():
    assert canonical_url("http://www.Example.com/a/b/?utm_source=x&id=3#frag") == "https://example.com/a/b?id=3"
    assert canonical_url("https://old.reddit.com/r/x/") == "https://reddit.com/r/x"
    assert canonical_url("not a url") is None


def test_arxiv_refs_from_any_mirror():
    assert url_ref("https://arxiv.org/abs/2610.01234v2") == "arxiv:2610.01234"
    assert url_ref("https://arxiv.org/pdf/2610.01234") == "arxiv:2610.01234"
    assert url_ref("https://huggingface.co/papers/2610.01234") == "arxiv:2610.01234"
    assert refs_from_text("see arXiv:2610.01234 for details") == {"arxiv:2610.01234"}


def test_github_repo_vs_issue_vs_release():
    assert url_ref("https://github.com/Owner/Repo") == "github:owner/repo"
    assert url_ref("https://github.com/owner/repo/blob/main/README.md") == "github:owner/repo"
    assert url_ref("https://github.com/owner/repo/issues/42") == "github:owner/repo#42"
    assert url_ref("https://github.com/owner/repo/pull/7") == "github:owner/repo#7"
    assert url_ref("https://github.com/owner/repo/releases/tag/v1.2") == "github:owner/repo@v1.2"
    assert url_ref("https://github.com/features/copilot") != "github:features/copilot"


def test_huggingface_and_youtube_refs():
    assert url_ref("https://huggingface.co/google/embeddinggemma-2") == "hf:google/embeddinggemma-2"
    assert url_ref("https://huggingface.co/spaces/a/b") == "hf:spaces/a/b"
    assert url_ref("https://huggingface.co/docs/transformers") != "hf:docs/transformers"
    assert url_ref("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10") == "youtube:dQw4w9WgXcQ"
    assert url_ref("https://youtu.be/dQw4w9WgXcQ") == "youtube:dQw4w9WgXcQ"


def test_homepages_are_too_generic():
    assert url_ref("https://example.com/") is None
    assert url_ref("https://example.com/post/1") == "url:example.com/post/1"

from app.services.base_agent import _has_unmatched_reads


def test_doc_analysis_read_does_not_require_write():
    assert not _has_unmatched_reads(
        ["read_feishu_doc", "web_search"],
        "你看一下这个文档，帮我分析一下我该怎么办。",
    )


def test_doc_update_read_requires_write():
    assert _has_unmatched_reads(
        ["read_feishu_doc"],
        "帮我把这个飞书文档改一下，补充成正式版本。",
    )


def test_bitable_analysis_read_does_not_require_write():
    assert not _has_unmatched_reads(
        ["list_bitable_records"],
        "把这张表看看，然后总结一下问题出在哪。",
    )


def test_bitable_update_read_requires_write():
    assert _has_unmatched_reads(
        ["search_bitable_records"],
        "找到这条记录后直接帮我更新到多维表格里。",
    )
